import asyncio

import httpx
import pytest
from fastapi import FastAPI

from lib.infra.server_runtime import MaintenanceError, ServerRuntime, runtime
from server.auth import EMBEDDED_AGENT_SUB, create_token
from server.company_runtime import MaintenanceMiddleware, local_readiness
from server.error_handlers import register_error_handlers


@pytest.fixture(autouse=True)
def clean_runtime(monkeypatch):
    monkeypatch.delenv("XMSTUDIO_INSPECTION", raising=False)
    runtime.reset()
    yield
    runtime.reset()


def make_app():
    app = FastAPI()
    app.add_middleware(MaintenanceMiddleware)

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PATCH", "PUT"])
    async def endpoint(path: str):
        return {"ok": True}

    return app


async def test_critical_disk_rejects_new_work_without_blocking_cleanup_or_result_saves():
    runtime.storage_critical = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(make_app()), base_url="http://test") as client:
        assert (await client.post("/api/v1/projects/import")).status_code == 507
        assert (await client.post("/api/v1/tasks")).json()["code"] == "storage_critical"
        assert (await client.get("/api/v1/projects/demo")).status_code == 200
        assert (await client.delete("/api/v1/projects/demo")).status_code == 200
        assert (await client.post("/api/v1/auth/token")).status_code == 200
        assert (await client.patch("/api/v1/providers/demo/config")).status_code == 200
        assert (await client.post("/api/v1/projects/demo/export/token")).status_code == 200
        assert (await client.post("/api/v1/projects/demo/assistant/sessions/s/interrupt")).status_code == 200
        headers = {"Authorization": "Bearer " + create_token(EMBEDDED_AGENT_SUB)}
        assert (await client.post("/api/v1/projects/demo/files/save", headers=headers)).status_code == 200
    with pytest.raises(MaintenanceError, match="磁盘空间"):
        runtime.require_admission()


async def test_maintenance_blocks_new_work_and_allows_reads():
    runtime.maintenance = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(make_app()), base_url="http://test") as client:
        denied = await client.post("/api/v1/tasks")
        assert denied.status_code == 503
        assert denied.headers["Retry-After"] == "60"
        assert (await client.get("/api/v1/tasks")).status_code == 200
        assert (await client.post("/api/v1/auth/token")).status_code == 200
        assert (await client.post("/mcp")).status_code == 503
    assert not runtime.activities


@pytest.mark.parametrize("mode", ["maintenance", "storage_critical", "inspection"])
async def test_session_transport_stays_available_except_during_isolated_inspection(mode):
    setattr(runtime, mode, True)
    expected = 503 if mode == "inspection" else 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(make_app()), base_url="http://test") as client:
        for endpoint in ("token", "media-session", "logout"):
            assert (await client.post(f"/api/v1/auth/{endpoint}")).status_code == expected


@pytest.mark.parametrize(
    ("host", "subject", "headers", "status"),
    [
        ("127.0.0.1", EMBEDDED_AGENT_SUB, {}, 200),
        ("::1", EMBEDDED_AGENT_SUB, {}, 200),
        ("172.16.20.25", EMBEDDED_AGENT_SUB, {}, 503),
        ("127.0.0.1", "admin", {}, 503),
        ("127.0.0.1", EMBEDDED_AGENT_SUB, {"X-Forwarded-For": "172.16.20.25"}, 503),
        ("127.0.0.1", EMBEDDED_AGENT_SUB, {"Forwarded": "for=172.16.20.25"}, 503),
    ],
)
async def test_only_direct_signed_embedded_agent_can_save_during_drain(host, subject, headers, status):
    runtime.maintenance = True
    transport = httpx.ASGITransport(make_app(), client=(host, 23456))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/projects/demo/files/save", headers={"Authorization": "Bearer " + create_token(subject), **headers}
        )
        assert response.status_code == status


async def test_inspection_cannot_run_agents_even_with_internal_token():
    runtime.inspection = True
    transport = httpx.ASGITransport(make_app(), client=("127.0.0.1", 23456))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/projects/demo/assistant/sessions/x/events")).status_code == 503
        assert (
            await client.post("/api/v1/tasks", headers={"Authorization": "Bearer " + create_token(EMBEDDED_AGENT_SUB)})
        ).status_code == 503


async def test_drain_allows_answers_and_interrupts_but_not_more_messages():
    runtime.maintenance = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(make_app()), base_url="http://test") as client:
        prefix = "/api/v1/projects/demo/assistant/sessions/s1"
        for path in (prefix + "/interrupt", prefix + "/questions/q1/answer"):
            assert (await client.post(path)).status_code == 200
        assert (await client.post(prefix + "/queued-messages/m1/send-now")).status_code == 503
        assert (await client.post("/api/v1/projects/demo/assistant/sessions/send")).status_code == 503


async def test_background_task_is_counted_before_it_gets_scheduled_and_after_failure():
    state = ServerRuntime()

    async def fail():
        raise ValueError("simulated endpoint failure")

    task = state.track_task("trial", fail())
    assert state.activities == {"trial": 1}
    with pytest.raises(RuntimeError, match="active"):
        state.reset()
    with pytest.raises(ValueError, match="simulated endpoint failure"):
        await task
    await asyncio.sleep(0)
    assert not state.activities


async def test_maintenance_error_from_get_side_effect_is_503():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/events")
    async def events():
        raise MaintenanceError("maintenance")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/events")).status_code == 503


@pytest.mark.parametrize("headers", [{"X-Forwarded-For": "127.0.0.1"}, {"Forwarded": "for=127.0.0.1"}])
async def test_readiness_rejects_proxy_even_on_loopback(headers):
    app = FastAPI()
    app.add_api_route("/ready", local_readiness)
    transport = httpx.ASGITransport(app, client=("127.0.0.1", 23456))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/ready", headers=headers)).status_code == 404
