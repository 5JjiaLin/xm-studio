"""Episode-cut orchestration with a fake text decision, optionally real native media."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.db.base import DEFAULT_USER_ID
from lib.generation.generation_queue import GenerationQueue
from lib.infra.json_io import atomic_write_json, load_json
from lib.openfilm import runtime
from server.services.presentation.jianying_delivery import delivery_store, execute_delivery
from server.services.project.episode_edit import directory
from server.services.project.production import ProductionService
from server.services.tasks.render_tasks import execute_render_task
from server.tool_runtime import Services
from tests.factories import install_uploaded_video


async def setup(projects, file_db_factory):
    root = projects.get_project_path("demo")
    for unit in ("E1U1", "E1U2"):
        install_uploaded_video(root, "reference_videos", unit, seconds=1)
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=projects)
    for _ in range(2):
        assert await queue.acquire_or_renew_worker_lease(name="default", owner_id="test", ttl_seconds=60)
    return ProductionService(projects, replace(Services.defaults(projects), queue=queue)), queue, root


async def test_cut_duplicate_click_and_confirmation_basis(timeline_project, file_db_factory, monkeypatch):
    production, queue, root = await setup(timeline_project, file_db_factory)
    monkeypatch.setattr(runtime, "engine_path", lambda: root)

    async def fake_execute(projects, project, task, work):
        await asyncio.to_thread((work / "final.mp4").write_bytes, b"candidate")
        return {
            "sha256": await asyncio.to_thread(runtime.sha256_file, work / "final.mp4"),
            "review": [],
            "draft": "unused",
        }

    monkeypatch.setattr(runtime, "execute", fake_execute)
    edit = production.episode_edit
    await edit.start("demo", 1, DEFAULT_USER_ID, False)
    first = await production.status("demo", 1)
    assert first["status"] == "active"
    await edit.start("demo", 1, DEFAULT_USER_ID, False)
    assert (await production.status("demo", 1))["batch_id"] == first["batch_id"]

    task = await queue.claim_next_task("render")
    outcome = await execute_render_task(task, projects=timeline_project)
    assert await queue.mark_task_succeeded(task["task_id"], outcome) == 1
    assert await queue.claim_next_task("render") is None
    await production.advance("demo", 1)
    candidate = (await production.status("demo", 1))["edit_output"]
    assert candidate["current"]
    assert not candidate["adopted"]

    with pytest.raises(runtime.EditError, match="刷新"):
        await edit.confirm("demo", 1, candidate["id"], "wrong")
    await edit.confirm("demo", 1, candidate["id"], candidate["sha256"])
    assert (await production.status("demo", 1))["edit_output"]["adopted"]

    install_uploaded_video(root, "reference_videos", "E1U2", seconds=0.7)
    assert not (await production.status("demo", 1))["edit_output"]["current"]
    await edit.start("demo", 1, DEFAULT_USER_ID, False)
    task = await queue.claim_next_task("render")
    outcome = await execute_render_task(task, projects=timeline_project)
    assert await queue.mark_task_succeeded(task["task_id"], outcome) == 1
    await production.advance("demo", 1)
    replacement = (await production.status("demo", 1))["edit_output"]
    assert replacement["id"] != candidate["id"]
    install_uploaded_video(root, "reference_videos", "E1U2", seconds=0.8)
    with pytest.raises(runtime.EditError, match="素材已更新"):
        await edit.confirm("demo", 1, replacement["id"], replacement["sha256"])
    assert edit.adopted("demo", 1)["id"] == candidate["id"]


async def test_cut_unknown_reply_requires_explicit_retry(timeline_project, file_db_factory, monkeypatch):
    production, queue, root = await setup(timeline_project, file_db_factory)
    monkeypatch.setattr(runtime, "engine_path", lambda: root)
    edit = production.episode_edit
    await edit.start("demo", 1, DEFAULT_USER_ID, True)
    task = await queue.claim_next_task("render")
    old_job = task["payload"]["job"]
    work = directory(root, 1, old_job)
    atomic_write_json(work / "model-submitted-0.json", {"model": "test"})
    await queue.mark_task_failed(task["task_id"], "missing reply")
    await production.advance("demo", 1)
    assert (await production.status("demo", 1))["edit_recovery"] == "unconfirmed"
    with pytest.raises(runtime.EditError, match="不能直接继续"):
        await edit.start("demo", 1, DEFAULT_USER_ID, False, resume=True)
    await edit.start("demo", 1, DEFAULT_USER_ID, True)
    assert await queue.claim_next_task("render") is None
    await edit.start("demo", 1, DEFAULT_USER_ID, False, retry=True)
    task = await queue.claim_next_task("render")
    new_job = task["payload"]["job"]
    assert new_job != old_job
    assert load_json(directory(root, 1, new_job) / "input.json")["music"] is True


@pytest.mark.skipif(not os.getenv("XM_TEST_NATIVE_EDIT"), reason="needs installed native editor and Chromium")
async def test_synthetic_native_edit_and_draft(timeline_project, file_db_factory, monkeypatch, tmp_path):
    import shutil
    import subprocess
    import zipfile

    from lib.jianying_draft import local_delivery

    production, queue, root = await setup(timeline_project, file_db_factory)
    import wave

    import numpy as np

    def write_score(work):
        # A synthetic tone exercises the music path without invoking any generator.
        with wave.open(str(work / "assets/score.wav"), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            tone = np.sin(np.arange(64000) * 2 * np.pi * 220 / 16000) * 5000
            wav.writeframes(tone.astype("<i2").tobytes())

    async def fake_score(work, _decisions, _observations):
        await asyncio.to_thread(write_score, work)
        return True

    class FakeGenerator:
        provider_id = "fake"
        model = "synthetic"

        async def generate(self, request, *_args, **_kwargs):
            assert len(request.images) == 2
            assert await asyncio.to_thread(lambda: all(item.path.is_file() for item in request.images))
            assert "# 漫剧初剪" in request.system_prompt
            assert "# Making a film" in request.system_prompt
            decision = runtime.EditDecision(
                shots=[
                    runtime.ShotDecision(key="shot-001", start=0, end=1, reason="保留", review="复看"),
                    runtime.ShotDecision(key="shot-002", start=0.2, end=0.8, reason="剪掉停顿", review=""),
                ],
                music_prompt="Soft instrumental",
            )
            return SimpleNamespace(text=decision.model_dump_json())

    async def fake_create(*_args, **_kwargs):
        return FakeGenerator()

    # Add synthetic speech only to the first shot; leave the second free to trim.
    script_path = root / "reference_videos/E1U1.mp4"
    backup = tmp_path / "fixture.mp4"
    shutil.copyfile(script_path, backup)
    speaking = tmp_path / "speaking.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-i",
            str(backup),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            str(speaking),
        ],
        check=True,
    )
    from lib.artifacts.version_manager import VersionManager

    VersionManager(root).add_version("reference_videos", "E1U1", "synthetic audio", speaking)
    monkeypatch.setattr(runtime.TextGenerator, "create", fake_create)
    monkeypatch.setattr(runtime, "score", fake_score)
    edit = production.episode_edit
    await edit.start("demo", 1, DEFAULT_USER_ID, True)
    task = await queue.claim_next_task("render")
    work = directory(root, 1, task["payload"]["job"])
    try:
        outcome = await execute_render_task(task, projects=timeline_project)
    except Exception:
        if (work / "engine.log").is_file():
            print((work / "engine.log").read_text(errors="replace"))
        raise
    assert await queue.mark_task_succeeded(task["task_id"], outcome) == 1
    await production.advance("demo", 1)
    output = (await production.status("demo", 1))["edit_output"]
    assert output["current"]
    assert not output["adopted"]
    assert not output["music_failed"]
    await edit.confirm("demo", 1, output["id"], output["sha256"])

    store = delivery_store(timeline_project)
    draft_root = tmp_path / "fake-jianying-root"
    draft_root.mkdir()
    atomic_write_json(draft_root / "root_meta_info.json", {"all_draft_store": []})
    monkeypatch.setattr(local_delivery, "compatibility", lambda _: {"ready": True})
    monkeypatch.setattr(local_delivery, "process_state", lambda: "stopped")
    monkeypatch.setattr(
        local_delivery.LocalDeliveryStore,
        "settings",
        lambda _: SimpleNamespace(enabled=True, draft_root=str(draft_root)),
    )
    for local in (False, True):
        previous = store.latest("demo", 1)
        record = store.reserve(
            project="demo",
            episode=1,
            timeline_id=f"edit-{output['id']}",
            revision=1,
            narration="without_narration",
            basis=output["sha256"],
            name="Demo",
            new_version=local,
            previous_id=previous["delivery_id"] if previous else None,
            local=local,
            edit_id=output["id"],
        )
        result = await execute_delivery(timeline_project, record["delivery_id"])
        assert result["state"] == ("delivered" if local else "package_ready")
        with zipfile.ZipFile(store.package_path(record["delivery_id"])) as archive:
            assert archive.testzip() is None
            assert any(name.endswith("/media/score-ducked.wav") for name in archive.namelist())
        if local:
            index = load_json(draft_root / "root_meta_info.json")["all_draft_store"]
            assert index[0]["draft_id"] == record["draft_id"]
            assert await asyncio.to_thread(Path(index[0]["draft_json_file"]).is_file)
