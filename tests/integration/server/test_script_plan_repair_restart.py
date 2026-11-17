"""Separate worker processes share a real SQLite queue, project files and usage ledger."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from lib.db.base import DEFAULT_USER_ID
from lib.db.models.api_call import ApiCall
from lib.db.models.task import Task
from lib.db.models.user import User
from lib.generation.generation_queue import GenerationQueue
from lib.infra.content_digest import prefixed_canonical_json_digest
from lib.project.project_manager import ProjectManager
from tests.factories import register_project_sources
from tests.integration.server.script_plan_repair_process import CRASH_EXIT_CODE


async def run_process(control: Path, action: str, phase: str, env: dict[str, str]) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.server.script_plan_repair_process",
        str(control), action, phase,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=25)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    expected = CRASH_EXIT_CODE if action == "start" else 0
    assert process.returncode == expected, (stdout + stderr).decode("utf-8", errors="replace")
    return process.pid


@pytest.mark.parametrize("phase", ["revalidating", "committing", "completed", "acceptance_unknown"])
async def test_restart_does_not_replay_and_explicit_recovery_reuses_persisted_response(
    tmp_path: Path, file_db_factory, phase: str,
):
    projects = ProjectManager(tmp_path / "中文数据根")
    project_name = "restart-demo"
    project_path = projects.create_project(project_name, content_mode="narration")
    projects.create_project_metadata(project_name, "重启恢复", "", "narration")
    async with file_db_factory() as session:
        session.add(User(id=DEFAULT_USER_ID, username="restart-fixture", role="admin"))
        await session.commit()
    dialogue = "这段台词保持原词" * 6
    source = f"张三抬头说：{dialogue}。"
    projects.update_project(project_name, lambda project: project.update(
        generation_mode="reference_video", characters={"张三": {"description": "主角"}},
    ))
    register_project_sources(projects, project_name, own_episodes=(source, "第二集的原文不能改变。"))
    control = tmp_path / "control"
    control.mkdir()
    original = {"duration_seconds": 4, "source_text": source, "text": f"@[张三]抬头。\n@[张三]{{{dialogue}}}"}
    repair = {
        "status": "repaired", "reason_code": None,
        "replacements": [{
            "item_index": 0, "content_digest": prefixed_canonical_json_digest(original),
            "units": [{**original, "duration_seconds": 12}],
        }],
    }
    (control / "responses.json").write_text(json.dumps([
        json.dumps({"units": [original], "new_assets": []}, ensure_ascii=False),
        json.dumps(repair, ensure_ascii=False),
    ], ensure_ascii=False), encoding="utf-8")
    source_files = {path: path.read_bytes() for path in (project_path / "source").rglob("*") if path.is_file()}
    before_assets = projects.load_project(project_name)["characters"]
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "SYSTEMROOT") if key in os.environ}
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        ARCREEL_DATA_DIR=str(projects.data_root),
        DATABASE_URL=file_db_factory.kw["bind"].url.render_as_string(hide_password=False),
        XM_SCRIPT_PLAN_REPAIR_ENABLED="1",
    )
    pids = [await run_process(control, "start", phase, env)]
    task_id = json.loads((control / "task.json").read_text())
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=projects)
    task = await queue.get_task(task_id)
    assert task["status"] == "running"
    calls = 1 if phase == "acceptance_unknown" else 2
    assert json.loads((control / "calls.json").read_text()) == calls
    formal_path = project_path / "drafts/episode_1/script_plan_reference_units.json"
    committed = formal_path.read_bytes() if formal_path.exists() else None
    assert (committed is not None) is (phase == "completed")

    # The fresh worker must first classify the old text request without replaying it.
    env["XM_SCRIPT_PLAN_REPAIR_ENABLED"] = "0"
    pids.append(await run_process(control, "observe", phase, env))
    assert json.loads((control / "calls.json").read_text()) == calls
    async with file_db_factory() as session:
        tasks = (await session.scalars(select(Task))).all()
        assert len(tasks) == 1
        assert tasks[0].status == "failed"
    if committed is not None:
        assert formal_path.read_bytes() == committed

    # User-authorized local revalidation is a new task, with no new provider calls.
    pids.append(await run_process(control, "recover", phase, env))
    assert len(set(pids)) == 3
    assert json.loads((control / "calls.json").read_text()) == calls
    async with file_db_factory() as session:
        tasks = (await session.scalars(select(Task))).all()
        rows = (await session.scalars(select(ApiCall))).all()
        assert len(rows) == calls
        assert {row.task_id for row in rows} == {task_id}
        assert all(row.project_name == project_name and row.episode_id == 1 for row in rows)
        assert len(tasks) == (1 if phase == "acceptance_unknown" else 2)
        if phase != "acceptance_unknown":
            assert sorted(task.status for task in tasks) == ["failed", "succeeded"]
    if phase == "acceptance_unknown":
        assert json.loads((control / "recovery.json").read_text()) == {"refused": True}
        assert not formal_path.exists()
    else:
        formal = json.loads(formal_path.read_text())
        assert formal["units"][0]["text"] == original["text"]
        assert formal["units"][0]["duration_seconds"] == 12
        if committed is not None:
            assert formal_path.read_bytes() == committed
    assert not (project_path / "scripts/episode_1.json").exists()
    assert projects.load_project(project_name)["characters"] == before_assets
    assert all(path.read_bytes() == data for path, data in source_files.items())
