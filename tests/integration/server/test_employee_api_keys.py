"""Employee API-key identity and project limits across HTTP, MCP, and downloads."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image
from sqlalchemy import select

import lib.db
from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.db.models import ApiKey, AuditEvent
from lib.db.repositories.api_key_repository import ApiKeyRepository
from lib.identity.accounts import (
    change_employee_password,
    change_employee_role,
    create_employee,
    set_employee_active,
)
from lib.identity.projects import remove_project_member, set_project_member
from lib.project.project_manager import ProjectManager
from server import auth, project_access
from server.i18n import get_translator
from server.project_access import authorize_project_download, require_project_request_access, visible_project_names
from server.remote_mcp import ArcApiKeyVerifier, build_remote_mcp_server, mount_remote_mcp
from server.routers import api_keys, edit_timelines, files, projects
from server.routers import auth as auth_router
from server.tool_runtime import Services
from tests.factories import make_translator


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def employee_keys(tmp_path, db_factory, monkeypatch):
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(lib.db, "async_session_factory", db_factory)
    monkeypatch.setattr(api_keys, "async_session_factory", db_factory)
    auth._api_key_cache.clear()
    manager = ProjectManager(tmp_path / "data")
    manager.create_project("alpha")
    manager.create_project("beta")
    for name in ("alpha", "beta"):
        manager.create_project_metadata(name, name.title(), "", "drama")
        activate_artifact_target_state(manager.get_project_path(name), bump_schema=False)
    monkeypatch.setattr(project_access, "get_project_manager", lambda: manager)
    monkeypatch.setattr(projects, "get_project_manager", lambda: manager)
    monkeypatch.setattr(files, "get_project_manager", lambda: manager)
    alpha_id = manager.load_project("alpha")["project_id"]
    async with db_factory() as session, session.begin():
        admin = await create_employee(session, username="key-admin", password="long-test-password", role="admin")
        editor = await create_employee(session, username="key-editor", password="long-test-password", role="editor")
        viewer = await create_employee(session, username="key-viewer", password="long-test-password", role="viewer")
        for user, permission in ((editor, "editor"), (viewer, "viewer")):
            await set_project_member(
                session, actor_id=admin.id, project_id=alpha_id, user_id=user.id, permission=permission
            )
    app = FastAPI()
    app.dependency_overrides[get_translator] = lambda: make_translator()
    for router in (api_keys.router, auth_router.router, projects.router):
        app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_project_request_access)])
    app.include_router(files.public_router, prefix="/api/v1")
    app.include_router(edit_timelines.self_auth_router, prefix="/api/v1")
    probes = APIRouter()

    @probes.get("/projects/{project_name}/key-probe")
    @probes.post("/projects/{project_name}/key-probe")
    async def probe(project_name: str, user: auth.CurrentUser):
        return {"project": project_name, "user_id": user.id, "api_key_id": user.api_key_id}

    @probes.post("/projects/{project_name}/assistant/probe")
    async def assistant_probe(project_name: str):
        return {"project": project_name}

    app.include_router(probes, prefix="/api/v1", dependencies=[Depends(require_project_request_access)])
    admin_token = auth.create_employee_token(admin.id, admin.username, admin.session_version)

    async def issue(*, owner=editor, permission="read", name="employee-key", project="alpha"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post(
                "/api/v1/api-keys", headers=bearer(admin_token),
                json={"name": name, "project_name": project, "user_id": owner.id, "project_permission": permission},
            )
        assert response.status_code == 201, response.text
        return response.json()

    yield SimpleNamespace(
        app=app, manager=manager, admin=admin, editor=editor, viewer=viewer, factory=db_factory,
        admin_token=admin_token, alpha_id=alpha_id, issue=issue,
    )
    auth._api_key_cache.clear()


async def test_http_keys_bind_owner_and_project_and_enforce_permission(employee_keys):
    ctx = employee_keys
    key = await ctx.issue()
    assert key["user_id"] == ctx.editor.id
    assert key["project_id"] == ctx.alpha_id
    assert key["project_permission"] == "read"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="https://test") as client:
        headers = bearer(key["key"])
        response = await client.get("/api/v1/projects/alpha/key-probe", headers=headers)
        assert response.status_code == 200
        assert response.json()["user_id"] == ctx.editor.id
        assert response.json()["api_key_id"] == key["id"]
        assert (await client.post("/api/v1/projects/alpha/key-probe", headers=headers)).status_code == 403
        assert (await client.get("/api/v1/projects/beta/key-probe", headers=headers)).status_code == 403
        assert (await client.post("/api/v1/projects/alpha/export/token", headers=headers)).status_code == 403
        listed = await client.get("/api/v1/projects", headers=headers)
        assert listed.status_code == 200, listed.text
        assert {row["name"] for row in listed.json()["projects"]} == {"alpha"}
        for path in ("/api-keys", "/users", "/auth/media-session", "/projects/alpha/assistant/probe"):
            method = client.post if path.endswith(("media-session", "probe")) else client.get
            assert (await method(f"/api/v1{path}", headers=headers)).status_code == 403


async def test_write_key_can_edit_but_admin_key_cannot_escape_scope_or_manage_members(employee_keys):
    ctx = employee_keys
    key = await ctx.issue(permission="write")
    admin_key = await ctx.issue(owner=ctx.admin, permission="write", name="admin-project-key")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="https://test") as client:
        assert (await client.post("/api/v1/projects/alpha/key-probe", headers=bearer(key["key"]))).status_code == 200
        headers = bearer(admin_key["key"])
        assert (await client.get("/api/v1/projects/beta/key-probe", headers=headers)).status_code == 403
        assert (await client.get("/api/v1/users", headers=headers)).status_code == 403
        assert (await client.post("/api/v1/projects", headers=headers, json={})).status_code == 403
        assert (await client.delete("/api/v1/projects/alpha", headers=headers)).status_code == 403
    user = await auth.get_current_user(key["key"])
    assert user.role == "editor"
    assert await visible_project_names(await auth.get_current_user(admin_key["key"]), ctx.manager) == {"alpha"}


@pytest.mark.parametrize("revocation", ["disable", "password", "role", "delete", "expiry", "membership", "permission"])
async def test_employee_keys_recheck_current_state_without_cache(employee_keys, revocation):
    ctx = employee_keys
    key = await ctx.issue(permission="write")
    assert await auth.verify_api_key(key["key"]) is not None
    # A legacy positive cache entry must not override the employee DB checks.
    auth._set_api_key_cache(auth.hash_api_key(key["key"]), {"sub": "apikey:stale", "via": "apikey"})
    async with ctx.factory() as session, session.begin():
        if revocation == "disable":
            await set_employee_active(session, user_id=ctx.editor.id, is_active=False)
        elif revocation == "password":
            await change_employee_password(session, user_id=ctx.editor.id, password="new-long-test-password")
        elif revocation == "role":
            await change_employee_role(session, user_id=ctx.editor.id, role="viewer")
        elif revocation == "delete":
            await ApiKeyRepository(session).delete(key["id"])
        elif revocation == "expiry":
            (await session.get(ApiKey, key["id"])).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif revocation == "permission":
            await set_project_member(
                session, actor_id=ctx.admin.id, project_id=ctx.alpha_id, user_id=ctx.editor.id, permission="viewer"
            )
        else:
            await remove_project_member(session, actor_id=ctx.admin.id, project_id=ctx.alpha_id, user_id=ctx.editor.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="https://test") as client:
        response = await client.post("/api/v1/projects/alpha/key-probe", headers=bearer(key["key"]))
    assert response.status_code == (403 if revocation in {"membership", "permission"} else 401)


async def test_legacy_and_employee_keys_are_not_promoted_on_mode_switch(employee_keys, monkeypatch):
    ctx = employee_keys
    async with ctx.factory() as session, session.begin():
        await ApiKeyRepository(session).create(
            name="legacy", key_hash=auth.hash_api_key("arc-legacy"), key_prefix="arc-lega"
        )
    key = await ctx.issue()
    verifier = ArcApiKeyVerifier()
    assert await verifier.verify_token("arc-legacy") is None
    assert await verifier.verify_token(key["key"]) is not None
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
    assert await auth.verify_api_key(key["key"]) is None
    assert await verifier.verify_token(key["key"]) is None
    assert await verifier.verify_token("arc-legacy") is not None
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    assert await auth.verify_api_key("arc-legacy") is None


async def test_creation_refuses_missing_project_or_excess_owner_permissions_and_audits_revocation(employee_keys):
    ctx = employee_keys
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="https://test") as client:
        headers = bearer(ctx.admin_token)
        assert (await client.post("/api/v1/api-keys", headers=headers, json={"name": "unbound"})).status_code == 422
        for owner, project in ((ctx.viewer, "alpha"), (ctx.editor, "beta")):
            response = await client.post("/api/v1/api-keys", headers=headers, json={
                "name": "excess", "user_id": owner.id, "project_name": project, "project_permission": "write",
            })
            assert response.status_code == 403
    key = await ctx.issue()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="https://test") as client:
        listed = await client.get("/api/v1/api-keys", headers=bearer(ctx.admin_token))
        assert listed.status_code == 200
        assert listed.json()[0]["project_id"] == ctx.alpha_id
        assert "key_hash" not in listed.text
        assert key["key"] not in listed.text
        assert (await client.delete(f"/api/v1/api-keys/{key['id']}", headers=bearer(ctx.admin_token))).status_code == 204
    async with ctx.factory() as session:
        events = list(await session.scalars(select(AuditEvent).where(AuditEvent.object_type == "api_key")))
    assert {event.action for event in events} == {"api_key.create", "api_key.revoke"}
    assert all(event.actor_user_id == ctx.admin.id and event.project_id == ctx.alpha_id for event in events)
    assert all(key["key"] not in str(event.attributes) and key["key"] not in str(event.detail) for event in events)


async def test_download_link_does_not_survive_key_revocation(employee_keys):
    ctx = employee_keys
    key = await ctx.issue(permission="write")
    user = await auth.get_current_user(key["key"])
    token = auth.create_download_token(user.sub, "alpha", employee=user, project_id=ctx.alpha_id)
    payload = auth.verify_download_token(token, "alpha")
    assert payload["api_key_id"] == key["id"]
    await authorize_project_download(payload, "alpha")
    async with ctx.factory() as session, session.begin():
        await ApiKeyRepository(session).delete(key["id"])
    with pytest.raises(HTTPException) as exc:
        await authorize_project_download(payload, "alpha")
    assert exc.value.status_code == 401


async def test_project_key_media_cannot_read_another_project_or_global_library(employee_keys):
    ctx = employee_keys
    key = await ctx.issue()
    for name in ("alpha", "beta"):
        Image.new("RGB", (8, 8), "blue").save(ctx.manager.get_project_path(name) / "storyboards" / "probe.png")
    Image.new("RGB", (8, 8), "blue").save(ctx.manager.get_global_assets_root() / "character" / "probe.png")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="https://test") as client:
        headers = bearer(key["key"])
        allowed = await client.get("/api/v1/files/alpha/storyboards/probe.png", headers=headers)
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "private, no-store"
        for path in ("/files/beta/storyboards/probe.png", "/global-assets/character/probe.png"):
            assert (await client.get(f"/api/v1{path}", headers=headers)).status_code == 403
        async with ctx.factory() as session, session.begin():
            await ApiKeyRepository(session).delete(key["id"])
        assert (await client.get("/api/v1/files/alpha/storyboards/probe.png", headers=headers)).status_code == 401


@pytest.mark.parametrize("credential", ["legacy", "key", "member"])
async def test_jianying_download_rechecks_employee_and_key_before_packaging(employee_keys, tmp_path, credential):
    from unittest.mock import AsyncMock

    ctx = employee_keys
    package = tmp_path / "package" / "draft.zip"
    package.parent.mkdir()
    package.write_bytes(b"PK-draft-fixture")
    service = SimpleNamespace(package_download=AsyncMock(return_value=(package, "draft")))
    ctx.app.dependency_overrides[edit_timelines.get_jianying_draft_service] = lambda: service
    if credential == "legacy":
        token = auth.create_download_token("shared", "alpha")
    else:
        key = await ctx.issue(permission="write")
        user = await auth.get_current_user(key["key"])
        token = auth.create_download_token(user.sub, "alpha", employee=user, project_id=ctx.alpha_id)
    params = {"download_token": token, "draft_path": "C:/Users/Employee/Drafts"}
    url = "/api/v1/projects/alpha/edit-timelines/t1/jianying-draft/download"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="https://test") as client:
        if credential != "legacy":
            response = await client.get(url, params=params)
            assert response.status_code == 200
            assert response.content == b"PK-draft-fixture"
            service.package_download.reset_mock()
            async with ctx.factory() as session, session.begin():
                if credential == "key":
                    await ApiKeyRepository(session).delete(key["id"])
                else:
                    await remove_project_member(
                        session, actor_id=ctx.admin.id, project_id=ctx.alpha_id, user_id=ctx.editor.id
                    )
        denied = await client.get(url, params=params)
    assert denied.status_code == (403 if credential == "member" else 401)
    service.package_download.assert_not_awaited()


@asynccontextmanager
async def mcp_session(ctx, key):
    services = Services(projects=ctx.manager, workflow_planner=SimpleNamespace(), capabilities=SimpleNamespace())
    server = build_remote_mcp_server(services=services)
    app = FastAPI()
    mount_remote_mcp(app, server.streamable_http_app())
    async with (
        server.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers=bearer(key["key"])
        ) as client,
        streamable_http_client("http://localhost/mcp", http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


@pytest.mark.parametrize("permission", ["read", "write"])
async def test_remote_mcp_enforces_scope_and_read_only_before_writes(employee_keys, permission):
    ctx = employee_keys
    key = await ctx.issue(permission=permission)
    async with mcp_session(ctx, key) as session:
        content = await session.call_tool("get_project_content", {"project": "alpha"})
        assert not content.isError
        listed = await session.call_tool("list_projects", {})
        assert {row["name"] for row in listed.structuredContent["projects"]} == {"alpha"}
        for name, args in (
            ("get_project_content", {"project": "beta"}),
            ("get_project_content", {"project": "missing"}),
            ("create_project", {"name": "new-project", "title": "Escape"}),
        ):
            refused = await session.call_tool(name, args)
            assert refused.isError
            assert refused.structuredContent["problem"]["code"] == "permission_denied"
        written = await session.call_tool("upload_source", {
            "project": "alpha", "filename": "novel.txt", "content": "A scoped source file.",
        })
        assert written.isError == (permission == "read")
    assert (ctx.manager.get_project_path("alpha") / "source" / "novel.txt").exists() == (permission == "write")
    assert not (ctx.manager.projects_dir / "new-project").exists()


async def test_remote_mcp_rechecks_membership_within_connected_session(employee_keys):
    ctx = employee_keys
    key = await ctx.issue()
    async with mcp_session(ctx, key) as session:
        assert not (await session.call_tool("get_project_content", {"project": "alpha"})).isError
        async with ctx.factory() as db, db.begin():
            await remove_project_member(db, actor_id=ctx.admin.id, project_id=ctx.alpha_id, user_id=ctx.editor.id)
        denied = await session.call_tool("get_project_content", {"project": "alpha"})
        assert denied.isError
        assert denied.structuredContent["problem"]["code"] == "permission_denied"
        listed = await session.call_tool("list_projects", {})
        assert listed.structuredContent["projects"] == []
