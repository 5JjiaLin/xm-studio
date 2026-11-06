"""Explicit production handoff through the real render queue and local package writer."""

import asyncio
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from lib.generation.generation_queue import GenerationQueue
from lib.infra.json_io import atomic_write_json
from lib.jianying_draft import local_delivery as local
from server.dependencies import require_project_migration_ok
from server.error_handlers import register_error_handlers
from server.routers import production
from server.services.presentation.jianying_delivery import delivery_store, execute_delivery
from server.services.project.production import ProductionService
from tests.auth_deps import override_auth
from tests.integration.server.services.presentation.timeline_render_support import edited_timeline, setup_project


@pytest.fixture
async def delivery_client(tmp_path, monkeypatch, file_db_factory):
    pm, _ = setup_project(tmp_path)
    timeline_id = await edited_timeline(pm)
    svc = ProductionService(pm)
    (pm.get_project_path("demo") / "production").mkdir()
    await svc._write("demo", 1, {"episode": 1, "status": "completed", "timeline_id": timeline_id})
    root = tmp_path / "剪映 草稿"
    root.mkdir()
    atomic_write_json(root / "root_meta_info.json", {"all_draft_store": [], "future": "keep"})
    monkeypatch.setattr(local, "compatibility", lambda _: {"ready": True})
    monkeypatch.setattr(local, "process_state", lambda: "running")
    store = delivery_store(pm)
    store.save_settings(local.LocalSettings(enabled=True, app_path=str(tmp_path), draft_root=str(root)))
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=pm)
    monkeypatch.setattr(production, "get_generation_queue", lambda: queue)
    app = FastAPI()
    app.state.production_service = svc
    override_auth(app)
    register_error_handlers(app)
    app.include_router(production.router, prefix="/api/v1", dependencies=[Depends(require_project_migration_ok)])
    app.include_router(production.source_router, prefix="/api/v1")
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://127.0.0.1"
    ) as client:
        yield client, pm, store, queue


URL = "/api/v1/projects/demo/episodes/1/production/jianying"


async def test_explicit_queue_wait_resume_idempotency_and_download(delivery_client, monkeypatch):
    client, pm, store, queue = delivery_client
    for _ in range(2):
        assert (await client.get(URL)).json() == {"delivery": None}
    assert store.latest("demo", 1) is None
    first = await client.post(URL, json={})
    assert first.status_code == 200, first.text
    record = first.json()["delivery"]
    task = await queue.get_task(record["task_id"])
    assert task["task_type"] == "render_jianying_draft"
    again = await client.post(URL, json={})
    assert again.json()["delivery"]["delivery_id"] == record["delivery_id"]
    await execute_delivery(pm, record["delivery_id"])
    assert (await client.get(URL)).json()["delivery"]["state"] == "waiting_for_exit"
    monkeypatch.setattr(local, "process_state", lambda: "stopped")
    resumed = await client.post(URL, json={})
    assert resumed.json()["delivery"]["state"] == "delivered"
    target = Path(record["draft_root"]) / record["folder"] / "draft_info.json"
    await asyncio.to_thread(target.write_bytes, b"edited in Jianying")
    assert (await client.post(URL, json={})).json()["delivery"]["draft_id"] == record["draft_id"]
    assert target.read_bytes() == b"edited in Jianying"
    package = await client.get(URL + "/package")
    assert package.status_code == 200
    assert package.content[:2] == b"PK"


async def test_forwarded_client_cannot_write_local_settings_or_register(delivery_client):
    client, _, store, _ = delivery_client
    headers = {"x-forwarded-for": "192.0.2.1"}
    settings = await client.get("/api/v1/production/jianying/settings", headers=headers)
    assert settings.json()["local"] is False
    assert (await client.post(URL, json={}, headers=headers)).status_code == 409
    assert (
        await client.put("/api/v1/production/jianying/settings", json={"enabled": False}, headers=headers)
    ).status_code == 409
    assert store.latest("demo", 1) is None
    assert (await client.post(URL, json={"local": False}, headers=headers)).status_code == 200
    record = store.latest("demo", 1)
    assert record["local"] is False


async def test_local_settings_use_non_project_router(delivery_client):
    client, _, store, _ = delivery_client
    response = await client.put("/api/v1/production/jianying/settings", json=store.settings().model_dump())
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["enabled"] is True


async def test_interruption_before_task_link_can_be_retried(delivery_client):
    client, _, store, _ = delivery_client
    submitted = (await client.post(URL, json={})).json()["delivery"]
    # Simulate interruption after enqueue but before the task id was persisted.
    await asyncio.to_thread(store.patch, submitted["delivery_id"], task_id=None)
    status = (await client.get(URL)).json()["delivery"]
    assert status["state"] == "failed"
    assert store.read(submitted["delivery_id"])["state"] == "preparing"
    resumed = (await client.post(URL, json={})).json()["delivery"]
    assert resumed["delivery_id"] == submitted["delivery_id"]
    assert resumed["task_id"] == submitted["task_id"]
