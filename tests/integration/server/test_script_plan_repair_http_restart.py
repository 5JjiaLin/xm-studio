"""Authenticated HTTP recovery through the actual application lifespan and worker."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lib.db.models.api_call import ApiCall
from lib.db.models.task import Task
from lib.infra.content_digest import prefixed_canonical_json_digest
from lib.project.project_manager import ProjectManager
from tests.factories import register_project_sources
from tests.integration.server.script_plan_repair_process import CRASH_EXIT_CODE


@asynccontextmanager
async def application_process(control: Path, action: str, phase: str, env: dict[str, str]):
    port_file = control / "port.json"
    port_file.unlink(missing_ok=True)
    with (control / f"{action}.log").open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.integration.server.script_plan_repair_http_process",
            str(control), action, phase, env=env, stdout=output, stderr=output,
        )
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                async with asyncio.timeout(30):
                    while True:
                        assert process.returncode is None, (control / f"{action}.log").read_text()
                        if port_file.exists():
                            client.base_url = f"http://127.0.0.1:{json.loads(port_file.read_text())}"
                            try:
                                ready = await client.get("/health")
                                if ready.status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                        await asyncio.sleep(0.02)
                login = await client.post("/api/v1/auth/token", data={"username": "fixture-admin", "password": "fixture-password"})
                assert login.status_code == 200, login.text
                client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
                yield process, client
        finally:
            if process.returncode is None:
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=3) as stop_client:
                        response = await stop_client.post(str(client.base_url) + "/__fixture__/shutdown")
                        response.raise_for_status()
                    await asyncio.wait_for(process.wait(), 15)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()


async def terminal_task(client: httpx.AsyncClient, task_id: str) -> dict:
    async with asyncio.timeout(30):
        while True:
            response = await client.get(f"/api/v1/tasks/{task_id}")
            assert response.status_code == 200, response.text
            task = response.json()["task"]
            if task["status"] in {"succeeded", "failed", "cancelled"}:
                return task
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("phase", ["revalidating", "committing", "completed", "acceptance_unknown"])
async def test_http_restart_recovery_never_replays_a_model_or_advances_downstream(tmp_path: Path, phase: str):
    root = tmp_path / "中文部署 数据根"
    projects = ProjectManager(root)
    project = "http-restart"
    path = projects.create_project(project, content_mode="narration")
    projects.create_project_metadata(project, "HTTP恢复", "", "narration")
    projects.update_project(project, lambda value: value.update(
        generation_mode="reference_video", characters={"张三": {"description": "主角"}},
    ))
    dialogue = "这段台词保持原词" * 6
    source = f"张三抬头说：{dialogue}。"
    register_project_sources(projects, project, own_episodes=(source, "第二集不变。"))
    control = tmp_path / "control"
    control.mkdir()
    unit = {"duration_seconds": 4, "source_text": source, "text": f"@[张三]抬头。\n@[张三]{{{dialogue}}}"}
    reply = {"status": "repaired", "reason_code": None, "replacements": [{
        "item_index": 0, "content_digest": prefixed_canonical_json_digest(unit),
        "units": [{**unit, "duration_seconds": 12}],
    }]}
    (control / "responses.json").write_text(json.dumps([
        json.dumps({"units": [unit], "new_assets": []}, ensure_ascii=False),
        json.dumps(reply, ensure_ascii=False),
    ], ensure_ascii=False))
    source_files = {file: file.read_bytes() for file in (path / "source").rglob("*") if file.is_file()}
    assets = projects.load_project(project)["characters"]
    database_url = f"sqlite+aiosqlite:///{root / 'http-test.db'}"
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    env.update(
        PYTHONUTF8="1", USERPROFILE=str(root / "fixture-profile"), HOME=str(root / "fixture-profile"),
        ARCREEL_PROFILE_DIR=str(root / "fixture-profile" / "agent"),
        PYTHON_DOTENV_DISABLED="1", ARCREEL_DATA_DIR=str(root), DATABASE_URL=database_url,
        CLAUDE_CONFIG_DIR=str(root / "fixture-sdk"), AUTH_ENABLED="true",
        AUTH_USERNAME="fixture-admin", AUTH_PASSWORD="fixture-password",
        AUTH_TOKEN_SECRET="fixture-only-http-test-signing-secret",
        XM_SCRIPT_PLAN_REPAIR_ENABLED="1",
    )
    route = f"/api/v1/projects/{project}/episodes/1/script-plan"
    review_route = f"/api/v1/projects/{project}/episodes/1/script-review"
    formal = path / "drafts/episode_1/script_plan_reference_units.json"
    pids = []
    async with application_process(control, "start", phase, env) as (process, client):
        pids.append(process.pid)
        unauthorized = await client.post(route, json={"repair_once": True}, headers={"Authorization": "Bearer invalid"})
        assert unauthorized.status_code == 401
        response = await client.post(route, json={"repair_once": True})
        assert response.status_code == 200, response.text
        task_id = response.json()["batch"]["members"][0]["task_id"]
        await client.post("/__fixture__/release")
        assert await asyncio.wait_for(process.wait(), 20) == CRASH_EXIT_CODE
    original_bytes = formal.read_bytes() if formal.exists() else None
    count = 1 if phase == "acceptance_unknown" else 2
    assert json.loads((control / "calls.json").read_text()) == count

    env["XM_SCRIPT_PLAN_REPAIR_ENABLED"] = "0"
    async with application_process(control, "recover", phase, env) as (process, client):
        pids.append(process.pid)
        failed = await terminal_task(client, task_id)
        assert failed["status"] == "failed", failed
        assert "restart_lost_text" in failed.get("error_detail", failed["error_message"])
        for _ in range(2):
            review = await client.get(review_route)
            assert review.status_code == 200, review.text
        assert json.loads((control / "calls.json").read_text()) == count
        wrong = await client.post(route.replace("episodes/1", "episodes/2"), json={"repair_recheck_task_id": task_id})
        assert wrong.status_code == 422, wrong.text
        recovered = await client.post(route, json={"repair_recheck_task_id": task_id})
        if phase == "acceptance_unknown":
            assert recovered.status_code == 422, recovered.text
            assert not formal.exists()
        else:
            assert recovered.status_code == 200, recovered.text
            recovered_id = recovered.json()["batch"]["members"][0]["task_id"]
            done = await terminal_task(client, recovered_id)
            assert done["status"] == "succeeded", done
            candidate = json.loads(formal.read_text())
            assert candidate["units"][0]["text"] == unit["text"]
            assert candidate["units"][0]["duration_seconds"] == 12
            if original_bytes is not None:
                assert formal.read_bytes() == original_bytes

    async with application_process(control, "observe", phase, env) as (process, client):
        pids.append(process.pid)
        assert (await client.get(review_route)).status_code == 200
        assert (await terminal_task(client, task_id))["status"] == "failed"
        if phase != "acceptance_unknown":
            assert (await terminal_task(client, recovered_id))["status"] == "succeeded"
    assert len(set(pids)) == 3
    assert json.loads((control / "calls.json").read_text()) == count
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            calls = (await session.scalars(select(ApiCall))).all()
            tasks = (await session.scalars(select(Task))).all()
            assert len(calls) == count
            assert {call.task_id for call in calls} == {task_id}
            assert len(tasks) == (1 if phase == "acceptance_unknown" else 2)
    finally:
        await engine.dispose()
    assert not (path / "scripts/episode_1.json").exists()
    assert projects.load_project(project)["characters"] == assets
    assert all(file.read_bytes() == data for file, data in source_files.items())
