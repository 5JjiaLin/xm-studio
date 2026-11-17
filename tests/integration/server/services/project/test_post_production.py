"""One confirmed merge: no audio provider, real MP4 and editable local draft."""

import asyncio
import json
from dataclasses import replace
from itertools import pairwise
from unittest.mock import AsyncMock

import pytest

from lib.config.resolver import ConfigResolver
from lib.db.base import DEFAULT_USER_ID
from lib.edit_timeline import EditTimelineService, RevisionAuthor
from lib.edit_timeline.operations import InsertBgm, SetVolume
from lib.edit_timeline.store import EditTimelineStore
from lib.generation.generation_queue import GenerationQueue
from lib.infra.json_io import atomic_write_json
from lib.infra.media_probe import probe_media
from lib.jianying_draft import local_delivery
from server.services.presentation.jianying_delivery import delivery_store
from server.services.project.post_production import ProductionError, score_segments
from server.services.project.production import ProductionService
from server.services.tasks.render_tasks import execute_render_task
from server.tool_runtime import Services
from tests.factories import install_uploaded_video
from tests.fakes import use_jianying_compatibility_ready, use_jianying_process_state


def test_score_short_and_overlapping_regions_do_not_overlap_or_raise_speech_gain():
    clips = [
        {"unit_id": "silent", "start_us": 0, "end_us": 2_200_000},
        {"unit_id": "speech", "start_us": 1_800_000, "end_us": 4_000_000},
    ]
    segments = score_segments(clips, 1_300_000, silent_units={"silent"})
    assert segments[0]["start_us"] == 0
    for first, second in pairwise(segments):
        assert first["start_us"] + first["out_us"] - first["in_us"] == second["start_us"]
    assert segments[-1]["start_us"] + segments[-1]["out_us"] - segments[-1]["in_us"] == 4_000_000
    assert all(1_000_000 <= s["out_us"] - s["in_us"] <= 1_300_000 for s in segments)
    assert all(s["volume"] <= 0.12 for s in segments if s["start_us"] >= 1_800_000)


def test_score_rejects_unrepresentable_short_loop():
    with pytest.raises(ProductionError, match="finish_music_too_short"):
        score_segments([{"unit_id": "u", "start_us": 0, "end_us": 1_800_000}], 1_300_000, silent_units=set())


@pytest.fixture
async def finishing(timeline_project, file_db_factory, monkeypatch):
    pm = timeline_project
    for unit in ("E1U1", "E1U2"):
        await asyncio.to_thread(
            install_uploaded_video, pm.get_project_path("demo"), "reference_videos", unit, seconds=1
        )
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=pm)
    assert await queue.acquire_or_renew_worker_lease(name="default", owner_id="test", ttl_seconds=300)
    production = ProductionService(pm, replace(Services.defaults(pm), queue=queue))
    monkeypatch.setattr(
        ConfigResolver, "provider_config", AsyncMock(side_effect=AssertionError("No audio provider lookup"))
    )
    monkeypatch.setattr("server.services.tasks.music_tasks.get_project_manager", lambda: pm)
    monkeypatch.setattr("server.services.tasks.music_tasks.async_session_factory", file_db_factory)
    # Use an isolated real draft index; only OS process discovery is replaced.
    root = pm.data_root / "drafts"
    root.mkdir()
    atomic_write_json(root / "root_meta_info.json", {"all_draft_store": []})
    use_jianying_compatibility_ready(monkeypatch)
    monkeypatch.setattr("server.services.project.post_production.compatibility", lambda _: {"ready": True})
    use_jianying_process_state(monkeypatch, "stopped")
    delivery_store(pm).save_settings(
        local_delivery.LocalSettings(enabled=True, app_path=str(root), draft_root=str(root))
    )
    return production, queue, root


async def tick(production):
    await production.advance("demo", 1)
    return await production.status("demo", 1)


async def run_task(queue, pm, lane):
    task = await queue.claim_next_task(lane)
    assert task is not None
    assert lane == "render"
    result = await execute_render_task(task, projects=pm)
    assert await queue.mark_task_succeeded(task["task_id"], result) == 1
    return task


@pytest.mark.parametrize("delivery", ["post_production", "use_tts"])
async def test_one_merge_without_audio_config_preserves_source_and_registers_draft(finishing, monkeypatch, delivery):
    import subprocess

    from lib.infra.ffmpeg import ffmpeg_executable

    production, queue, root = finishing
    pm = production.projects
    await asyncio.to_thread(pm.update_project, "demo", lambda p: p.update({"narration_delivery": delivery}))
    first = await production.post_production.start("demo", 1, DEFAULT_USER_ID, local=True)
    assert (await production.post_production.start("demo", 1, DEFAULT_USER_ID, local=True))["candidate_id"] == first[
        "candidate_id"
    ]
    state = await tick(production)
    assert state["stage"] == "render"
    assert state["narration"] == "without_narration"
    original = EditTimelineStore(pm, "demo").find(state["timeline_id"]).latest
    assert not original.content.bgm
    assert (await tick(production))["batch_id"]
    # Failed local rendering is recoverable; no music or TTS is submitted.
    failed = await queue.claim_next_task("render")
    assert failed is not None
    assert await queue.mark_task_failed(failed["task_id"], "local render interrupted") == 1
    assert (await tick(production))["error"] == "finish_render_failed"
    await production.post_production.resume("demo", 1, local=True)
    await tick(production)
    await run_task(queue, pm, "render")
    assert (await tick(production))["stage"] == "draft"
    await tick(production)
    use_jianying_process_state(monkeypatch, "running")
    await run_task(queue, pm, "render")
    state = await tick(production)
    assert state["error"] == "finish_close_jianying"
    assert state["output"]["status"] == "current"
    store = delivery_store(pm)
    record = store.read(state["delivery_id"])
    assert store.package_path(record["delivery_id"]).stat().st_size > 0
    assert not (root / record["folder"]).exists()
    use_jianying_process_state(monkeypatch, "stopped")
    await production.post_production.resume("demo", 1, local=True)
    await tick(production)
    state = await tick(production)
    assert state["status"] == "completed"
    assert (await production.post_production.start("demo", 1, DEFAULT_USER_ID, local=True))["candidate_id"] == first[
        "candidate_id"
    ]
    assert EditTimelineStore(pm, "demo").find(state["timeline_id"]).latest == original
    assert (await queue.list_tasks(project_name="demo", task_type="music"))["total"] == 0
    assert (await queue.list_tasks(project_name="demo", task_type="tts"))["total"] == 0
    draft = json.loads((root / record["folder"] / "draft_info.json").read_text())
    assert draft["duration"] == 2_000_000
    video_tracks = [t for t in draft["tracks"] if t["type"] == "video"]
    assert len(video_tracks) == 1
    assert all(s["volume"] > 0 for s in video_tracks[0]["segments"])
    assert not any(t["type"] == "audio" for t in draft["tracks"])
    output = pm.get_project_path("demo") / state["output"]["artifact_path"]
    probe = await probe_media(output)
    assert probe.first_stream("video").duration_seconds == pytest.approx(2, abs=0.1)
    assert probe.first_stream("audio") is not None
    # Probe metadata alone cannot establish that every frame/packet decodes.
    await asyncio.to_thread(
        subprocess.run,
        [ffmpeg_executable(), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
        capture_output=True, check=True,
    )
    # An audio stream alone could still be silent. Check audible source audio in BOTH clips.
    for start in ("0.2", "1.2"):
        signal = await asyncio.to_thread(
            subprocess.run,
            [
                ffmpeg_executable(),
                "-v",
                "error",
                "-ss",
                start,
                "-i",
                str(output),
                "-t",
                "0.2",
                "-map",
                "0:a:0",
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-",
            ],
            capture_output=True,
            check=True,
        )
        from array import array

        samples = array("h", signal.stdout)
        assert max(abs(v) for v in samples) > 100


@pytest.mark.parametrize("change", ["media", "edit"])
async def test_inputs_changed_before_render_stop_submission(finishing, change):
    production, queue, _ = finishing
    await production.post_production.start("demo", 1, DEFAULT_USER_ID, local=True)
    state = await tick(production)
    if change == "media":
        await asyncio.to_thread(
            install_uploaded_video,
            production.projects.get_project_path("demo"),
            "reference_videos",
            "E1U1",
            seconds=1.5,
        )
    else:
        await EditTimelineService(production.projects).edit(
            "demo",
            state["timeline_id"],
            base_revision=1,
            summary="人工静音",
            author=RevisionAuthor(kind="creator"),
            operations=[SetVolume(op="set_volume", clip="c1", volume=0)],
        )
    state = await tick(production)
    assert state["status"] == "failed"
    assert state["error"] == ("finish_inputs_changed" if change == "media" else "finish_timeline_changed")
    assert (await queue.list_tasks(project_name="demo", task_type="music"))["total"] == 0
    # Explicit reconfirmation after the change can create a fresh preparation.
    assert (await production.post_production.start("demo", 1, DEFAULT_USER_ID, local=True))["stage"] == "prepare"


async def test_finishing_preserves_manual_edit_and_existing_bgm_without_audio_provider(finishing):
    from lib.bgm.service import BgmLibraryService
    from tests.factories import wav_bytes

    production, queue, _ = finishing
    timelines = EditTimelineService(production.projects)
    readout = await timelines.create_from_script(
        "demo", episode=1, name="人工初剪", author=RevisionAuthor(kind="creator")
    )
    track = await BgmLibraryService(production.projects).upload(
        "demo", filename="配乐.wav", content=wav_bytes(2, tone_hz=330)
    )
    await timelines.edit(
        "demo",
        readout.timeline.id,
        base_revision=1,
        summary="人工静音和配乐",
        author=RevisionAuthor(kind="creator"),
        operations=[
            SetVolume(op="set_volume", clip="c1", volume=0),
            InsertBgm(op="insert_bgm", bgm_id=track.id, start=0),
        ],
    )
    original = EditTimelineStore(production.projects, "demo").find(readout.timeline.id).latest
    await asyncio.to_thread((production.projects.get_project_path("demo") / "production").mkdir, exist_ok=True)
    await production._write("demo", 1, {"episode": 1, "status": "completed", "timeline_id": readout.timeline.id})
    await production.post_production.start("demo", 1, DEFAULT_USER_ID, local=True)
    state = await tick(production)
    assert state["stage"] == "render"
    assert state["revision"] == 2
    await tick(production)
    await run_task(queue, production.projects, "render")
    assert (await tick(production))["output"]["status"] == "current"
    assert EditTimelineStore(production.projects, "demo").find(readout.timeline.id).latest == original
    assert (await queue.list_tasks(project_name="demo", task_type="music"))["total"] == 0


@pytest.mark.parametrize("stage", ["music", "apply_music"])
@pytest.mark.parametrize("status", ["failed", "unknown", "active"])
async def test_legacy_music_stage_continues_locally_without_provider_or_new_music(finishing, stage, status):
    production, queue, _ = finishing
    await production.post_production.start("demo", 1, DEFAULT_USER_ID, local=True)
    state = await tick(production)
    state.update(stage=stage, status=status, error="finish_configure_minimax", narration="with_narration")
    # A prior paid attempt remains auditable; no retry and no new generation.
    state["music_task_id"] = "previous-attempt"
    state["bgm_id"] = "not-downloaded"
    state.pop("media_basis", None)
    await production._write("demo", 1, state)
    if status != "active":
        await production.post_production.resume("demo", 1, local=True)
    recovered = await tick(production)
    assert recovered["stage"] == "render"
    assert recovered["narration"] == "without_narration"
    assert recovered["music_task_id"] == "previous-attempt"
    assert recovered["candidate_id"] == state["candidate_id"]
    await tick(production)
    task = await run_task(queue, production.projects, "render")
    assert task["payload"]["narration"] == "without_narration"
    assert (await tick(production))["output"]["status"] == "current"
    assert (await queue.list_tasks(project_name="demo", task_type="music"))["total"] == 0
