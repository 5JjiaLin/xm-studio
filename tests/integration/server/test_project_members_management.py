"""Exercise project membership management through authenticated HTTP routes."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import lib.db
from lib.db.models import AuditEvent, ProjectMembership
from lib.identity.accounts import create_employee
from lib.project.project_manager import ProjectManager
from server.auth import create_employee_token
from server.routers import project_members
from tests.auth_deps import AUTH_DEPENDENCIES


@pytest.fixture
async def project_members_api(tmp_path, db_factory, monkeypatch):
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(lib.db, "async_session_factory", db_factory)
    manager = ProjectManager(tmp_path / "data")
    manager.create_project("alpha")
    project_id = manager.load_project("alpha")["project_id"]
    monkeypatch.setattr(project_members, "get_project_manager", lambda: manager)

    async with db_factory() as session, session.begin():
        admin = await create_employee(session, username="members-admin", password="long-test-password", role="admin")
        editor = await create_employee(session, username="members-editor", password="long-test-password", role="editor")
        admin_token = create_employee_token(admin.id, admin.username, admin.session_version)
        editor_token = create_employee_token(editor.id, editor.username, editor.session_version)

    app = FastAPI()
    app.include_router(project_members.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, db_factory, admin, editor, project_id, admin_token, editor_token


async def test_admin_can_manage_project_members_and_non_admin_cannot(project_members_api):
    client, factory, _admin, editor, project_id, admin_token, editor_token = project_members_api
    path = "/api/v1/projects/alpha/members"
    client.headers["Authorization"] = f"Bearer {admin_token}"

    added = await client.put(f"{path}/{editor.id}", json={"permission": "viewer"})
    assert added.status_code == 200
    assert added.json() == [
        {
            "id": editor.id,
            "username": "members-editor",
            "role": "editor",
            "is_active": True,
            "permission": "viewer",
        }
    ]
    assert (await client.get(path)).status_code == 200

    client.headers["Authorization"] = f"Bearer {editor_token}"
    assert (await client.get(path)).status_code == 403
    assert (await client.put(f"{path}/{editor.id}", json={"permission": "editor"})).status_code == 403

    client.headers["Authorization"] = f"Bearer {admin_token}"
    removed = await client.delete(f"{path}/{editor.id}")
    assert removed.status_code == 204
    async with factory() as session:
        assert await session.get(ProjectMembership, (project_id, editor.id)) is None
        actions = list(
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.project_id == project_id).order_by(AuditEvent.id)
            )
        )
    assert actions == ["project.member.set", "project.member.remove"]


async def test_member_api_rejects_ambiguous_project_identity(project_members_api):
    client, _factory, _admin, _editor, _project_id, admin_token, _editor_token = project_members_api
    manager = project_members.get_project_manager()
    original = manager.get_project_path("alpha")
    duplicate = manager.get_project_path("alpha").with_name("copied")
    import shutil

    shutil.copytree(original, duplicate)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    response = await client.get("/api/v1/projects/alpha/members")
    assert response.status_code == 409


async def test_member_api_requires_employee_authentication(project_members_api):
    client, _factory, _admin, _editor, _project_id, _admin_token, _editor_token = project_members_api
    response = await client.get("/api/v1/projects/alpha/members")
    assert response.status_code == 401


async def test_member_api_rejects_shared_mode_even_when_auth_is_disabled(project_members_api, monkeypatch):
    client, _factory, _admin, _editor, _project_id, _admin_token, _editor_token = project_members_api
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    response = await client.get("/api/v1/projects/alpha/members")
    assert response.status_code == 403
