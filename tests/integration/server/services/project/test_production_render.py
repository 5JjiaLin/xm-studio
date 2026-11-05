"""Production rebuild uses durable render batches and real bundled ffmpeg only."""

import asyncio
from dataclasses import replace

import pytest

from lib.db.base import DEFAULT_USER_ID
from lib.edit_timeline import EditTimelineService
from lib.generation.generation_queue import GenerationQueue
from lib.infra.media_probe import probe_media
from server.services.project.production import ProductionService
from server.services.tasks.render_tasks import execute_render_task
from server.tool_runtime import Services
from tests.factories import install_uploaded_video


@pytest.mark.asyncio
async def test_rebuild_renders_full_clips_and_refreshes_after_replacement(
    timeline_project,
    file_db_factory,
):
    def install(unit_id, seconds):
        install_uploaded_video(timeline_project.get_project_path("demo"), "reference_videos", unit_id, seconds=seconds)

    await asyncio.to_thread(install, "E1U1", 0.5)
    await asyncio.to_thread(install, "E1U2", 0.75)
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=timeline_project)
    assert await queue.acquire_or_renew_worker_lease(name="default", owner_id="test", ttl_seconds=60)
    assert await queue.acquire_or_renew_worker_lease(name="default", owner_id="test", ttl_seconds=60)
    services = replace(Services.defaults(timeline_project), queue=queue)
    production = ProductionService(timeline_project, services)

    first = await production.start_render("demo", 1, DEFAULT_USER_ID)
    assert first["status"] == "active"
    timeline_id = first["timeline_id"]
    readout = await EditTimelineService(timeline_project).read("demo", timeline_id)
    assert [clip.unit_id for clip in readout.clips] == ["E1U1", "E1U2"]
    assert all(
        clip.source_volume == 1 and clip.trim is None and clip.transition_to_next is None for clip in readout.clips
    )

    # Run the accepted local task as the render worker would; provider generation
    # is never involved in this rebuild path.
    async def complete(state):
        batch = await queue.get_generation_batch(
            project_name="demo", batch_id=state["batch_id"], user_id=DEFAULT_USER_ID
        )
        task = await queue.claim_next_task("render")
        assert task is not None
        assert task["task_id"] == batch.members[0].task_id
        result = await execute_render_task(task, projects=timeline_project)
        assert await queue.mark_task_succeeded(task["task_id"], result) == 1
        await production.advance("demo", 1)
        return await production.status("demo", 1)

    current = await complete(first)
    assert current["status"] == "completed"
    probe = await probe_media(timeline_project.get_project_path("demo") / current["output"]["artifact_path"])
    assert probe.first_stream("video") is not None
    assert probe.first_stream("audio") is not None
    assert probe.first_stream("video").duration_seconds == pytest.approx(1.25, abs=0.1)
    await asyncio.to_thread(install, "E1U2", 1.0)
    stale = await production.status("demo", 1)
    assert stale["output"]["status"] == "stale"
    second = await production.start_render("demo", 1, DEFAULT_USER_ID)
    assert second["timeline_id"] == timeline_id
    updated = await complete(second)
    assert updated["status"] == "completed"
    assert updated["output"]["version"] == current["output"]["version"] + 1

    def edit_prompt():
        project = timeline_project.load_project("demo")
        script_file = project["episodes"][0]["script_file"]
        script = timeline_project.load_script("demo", script_file)
        script["video_units"][0]["text"] = "新的画面提示词"
        timeline_project.save_script("demo", script, script_file.removeprefix("scripts/"))

    await asyncio.to_thread(edit_prompt)
    assert (await production.status("demo", 1))["output"]["status"] == "stale"


async def test_manual_edits_survive_rebuild_and_media_changes_require_repair(timeline_project, file_db_factory):
    from lib.edit_timeline.model import RevisionAuthor
    from lib.edit_timeline.operations import TimelineOperationAdapter
    from lib.edit_timeline.store import EditTimelineStore
    from lib.final_cut.errors import FinalCutError
    from lib.final_cut.service import FinalCutService
    from server.services.presentation.timeline_units import TimelineUnitMaterials

    for unit_id in ("E1U1", "E1U2"):
        await asyncio.to_thread(
            install_uploaded_video, timeline_project.get_project_path("demo"), "reference_videos", unit_id, seconds=1.0
        )
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=timeline_project)
    assert await queue.acquire_or_renew_worker_lease(name="default", owner_id="test", ttl_seconds=60)
    services = replace(Services.defaults(timeline_project), queue=queue)
    production = ProductionService(timeline_project, services)
    timelines = EditTimelineService(timeline_project)
    readout = await timelines.create_from_script(
        "demo", episode=1, name="人工成片", author=RevisionAuthor(kind="creator")
    )
    timeline_id = readout.timeline.id
    cue = {
        "id": "subtitle-1",
        "text": "人工字幕",
        "start_us": 200000,
        "end_us": 700000,
        "basis_version": 1,
        "font_size": 64,
        "color": "#FFFF00",
        "position": "top",
    }
    await timelines.edit(
        "demo",
        timeline_id,
        base_revision=1,
        summary="编辑字幕和片段",
        author=RevisionAuthor(kind="creator"),
        operations=[
            TimelineOperationAdapter.validate_python(op)
            for op in [
                {"op": "move", "clip": "c2", "after": None},
                {"op": "set_trim", "clip": "c1", "trim": {"source_in": 0.1, "source_out": 0.8}},
                {"op": "set_volume", "clip": "c1", "volume": 0},
                {"op": "set_subtitles", "clip": "c1", "subtitles": [cue]},
                {"op": "set_included", "clip": "c2", "included": False},
            ]
        ],
    )

    def install_journal():
        from lib.infra.json_io import atomic_write_json

        journal = timeline_project.get_project_path("demo") / "production" / "episode_1.json"
        journal.parent.mkdir(exist_ok=True)
        atomic_write_json(journal, {"episode": 1, "status": "completed", "timeline_id": timeline_id})

    await asyncio.to_thread(install_journal)
    state = await production.start_render("demo", 1, DEFAULT_USER_ID)
    assert state["timeline_id"] == timeline_id
    stored = await asyncio.to_thread(lambda: EditTimelineStore(timeline_project, "demo").find(timeline_id))
    assert stored.latest.number == 2
    assert stored.latest.content.clips[1].source_volume == 0
    assert stored.latest.content.clips[1].subtitles[0].text == "人工字幕"
    assert not stored.latest.content.clips[0].included
    final = FinalCutService(timeline_project, unit_materials=TimelineUnitMaterials(timeline_project))
    rendered = await final.render("demo", timeline_id, narration="without_narration", subtitles="burned_subtitles")
    assert rendered.acceptance.expected_duration == pytest.approx(0.7)
    await asyncio.to_thread(
        install_uploaded_video, timeline_project.get_project_path("demo"), "reference_videos", "E1U1", seconds=0.6
    )
    with pytest.raises(FinalCutError, match="人工剪辑需要调整"):
        await final.check("demo", timeline_id, narration="without_narration", subtitles="burned_subtitles")
    # The original edit remains recoverable, rather than being silently clamped or deleted.
    stored = await asyncio.to_thread(lambda: EditTimelineStore(timeline_project, "demo").find(timeline_id))
    assert stored.latest.content.clips[1].trim.out_us == 800000
    assert stored.latest.content.clips[1].subtitles[0].end_us == 700000


async def test_manual_cut_pixels_subtitle_timing_and_audio(timeline_project):
    """Real MP4 acceptance: reordered colors, source-relative captions and muted audio."""
    import os
    import shutil
    import subprocess
    from array import array
    from pathlib import Path

    from lib.artifacts.version_manager import MANUAL_UPLOAD_VERSION_SOURCE, VersionManager
    from lib.edit_timeline.model import RevisionAuthor
    from lib.edit_timeline.operations import TimelineOperationAdapter
    from lib.final_cut.service import FinalCutService
    from lib.infra.ffmpeg import ffmpeg_executable
    from lib.project.resource_paths import resource_relative_path
    from server.services.presentation.timeline_units import TimelineUnitMaterials
    from tests.factories import run_bundled_ffmpeg

    path = timeline_project.get_project_path("demo")

    def install():
        for uid, color in [("E1U1", "red"), ("E1U2", "blue")]:
            staged = path / f".{uid}.upload.mp4"
            run_bundled_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                f"color={color}:size=180x320:duration=2:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(staged),
            )
            VersionManager(path).commit_staged_version(
                "reference_videos",
                uid,
                "",
                staged_file=staged,
                current_file=path / resource_relative_path("reference_videos", uid),
                source=MANUAL_UPLOAD_VERSION_SOURCE,
            )

    await asyncio.to_thread(install)
    service = EditTimelineService(timeline_project)
    author = RevisionAuthor(kind="creator")
    initial = await service.create_from_script("demo", episode=1, name="验收样片", author=author)
    timeline_id = initial.timeline.id
    await service.edit(
        "demo",
        timeline_id,
        base_revision=1,
        summary="人工剪辑",
        author=author,
        operations=[
            TimelineOperationAdapter.validate_python(op)
            for op in [
                {"op": "move", "clip": "c2", "after": None},
                {"op": "set_trim", "clip": "c2", "trim": {"source_in": 0, "source_out": 1}},
                {"op": "set_volume", "clip": "c2", "volume": 0},
                {"op": "set_trim", "clip": "c1", "trim": {"source_in": 0.5, "source_out": 1.5}},
                {"op": "set_volume", "clip": "c1", "volume": 1},
                {
                    "op": "set_subtitles",
                    "clip": "c1",
                    "subtitles": [
                        {
                            "id": "manual",
                            "text": "人工字幕验收",
                            "start_us": 600000,
                            "end_us": 1100000,
                            "basis_version": 1,
                            "font_size": 96,
                            "color": "#FFFF00",
                            "position": "top",
                        }
                    ],
                },
            ]
        ],
    )
    final = await FinalCutService(timeline_project, unit_materials=TimelineUnitMaterials(timeline_project)).render(
        "demo", timeline_id, narration="without_narration", subtitles="burned_subtitles"
    )
    assert final.acceptance.expected_duration == pytest.approx(2)
    output = path / final.artifact_path

    def inspect():
        def decode(*args):
            return subprocess.run(
                [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-i", str(output), *args, "-"],
                check=True,
                capture_output=True,
            ).stdout

        def frame(at):
            raw = decode(
                "-ss", str(at), "-frames:v", "1", "-vf", "scale=320:180", "-f", "rawvideo", "-pix_fmt", "rgb24"
            )
            return list(zip(raw[::3], raw[1::3], raw[2::3], strict=True))

        blue, red, caption, after = (frame(at) for at in (0.5, 1.05, 1.3, 1.8))
        assert sum(b > 180 and r < 50 for r, g, b in blue) > 50000
        assert sum(r > 180 and b < 50 for r, g, b in red) > 50000

        def yellow(pixels):
            return sum(r > 180 and g > 180 and b < 80 for r, g, b in pixels)

        assert yellow(caption[: 320 * 90]) > 50
        assert yellow(caption[320 * 90 :]) == 0
        assert yellow(red) == yellow(after) == 0
        samples = array("h", decode("-vn", "-ac", "1", "-ar", "8000", "-f", "s16le"))
        assert max(map(abs, samples[800:6400])) < 100
        assert max(map(abs, samples[9600:14400])) > 1000
        evidence = os.environ.get("XM_E5_EVIDENCE_DIR")
        if evidence:
            dest = Path(evidence)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, dest / "manual-edit-acceptance.mp4")
            run_bundled_ffmpeg("-ss", "1.3", "-i", str(output), "-frames:v", "1", str(dest / "manual-caption.png"))
            (dest / "checks.txt").write_text(
                "PASS: 2s total; blue then red; blue muted; red audible; yellow top caption at 1.1-1.6s only.\n",
                encoding="utf-8",
            )

    await asyncio.to_thread(inspect)
