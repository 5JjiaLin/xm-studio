"""Employee project authorization at the core HTTP boundary."""

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.sse import ServerSentEvent
from httpx import ASGITransport, AsyncClient

import lib.db
from lib.identity.accounts import create_employee, set_employee_active
from lib.identity.projects import remove_project_member, set_project_member
from lib.project.project_manager import ProjectManager
from server import project_access
from server.auth import (
    CurrentUserInfo,
    create_download_token,
    create_employee_token,
    get_current_user,
    verify_download_token,
)
from server.error_handlers import register_error_handlers
from server.i18n import get_translator
from server.project_access import authorize_project_download, require_project_request_access
from server.routers import assistant, projects
from tests.auth_deps import AUTH_DEPENDENCIES
from tests.factories import make_session_meta, make_translator


async def test_core_project_routes_enforce_membership_and_creation_owner(tmp_path, db_factory, monkeypatch):
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(lib.db, "async_session_factory", db_factory)
    manager = ProjectManager(tmp_path / "data")
    manager.create_project("alpha")
    manager.create_project("beta")
    monkeypatch.setattr(project_access, "get_project_manager", lambda: manager)
    monkeypatch.setattr(projects, "get_project_manager", lambda: manager)

    async def seed():
        async with db_factory() as session, session.begin():
            admin = await create_employee(session, username="scope-admin", password="long-test-password", role="admin")
            editor = await create_employee(session, username="scope-editor", password="long-test-password", role="editor")
            viewer = await create_employee(session, username="scope-viewer", password="long-test-password", role="viewer")
            alpha_id = manager.load_project("alpha")["project_id"]
            await set_project_member(session, actor_id=admin.id, project_id=alpha_id, user_id=editor.id, permission="editor")
            await set_project_member(session, actor_id=admin.id, project_id=alpha_id, user_id=viewer.id, permission="viewer")
            return admin, editor, viewer, alpha_id

    admin, editor, viewer, alpha_id = await seed()
    active = {"user": editor}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(
        id=active["user"].id, sub=active["user"].username, role=active["user"].role
    )
    app.include_router(projects.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)

    probe_router = APIRouter()

    @probe_router.get("/projects/{project_name}/scope-probe")
    async def read_project_probe(project_name: str):
        return {"project": project_name}

    @probe_router.post("/projects/{project_name}/scope-probe")
    async def write_project_probe(project_name: str):
        return {"project": project_name}

    @probe_router.get("/api-keys")
    async def list_api_keys():
        return []

    @probe_router.get("/providers")
    async def list_providers():
        return []

    @probe_router.get("/providers/{provider_id}/credentials")
    async def list_provider_credentials(provider_id: str):
        return {"provider": provider_id}

    @probe_router.post("/providers/gemini-vertex/credentials/upload")
    async def upload_provider_credentials():
        return {"uploaded": True}

    @probe_router.patch("/system/config")
    async def patch_system_config():
        return {"updated": True}

    @probe_router.post("/assets")
    async def create_global_asset():
        return {"created": True}

    @probe_router.get("/tasks/{task_id}")
    async def get_global_task(task_id: str):
        return {"id": task_id}

    @probe_router.get("/usage/records")
    async def list_usage_records():
        return []

    app.include_router(probe_router, prefix="/api/v1", dependencies=[Depends(require_project_request_access)])
    register_error_handlers(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/projects/alpha/agent-profile")).status_code == 200
        assert (await client.get("/api/v1/projects/beta/agent-profile")).status_code == 403
        assert (await client.get("/api/v1/projects/alpha/scope-probe")).status_code == 200
        assert (await client.get("/api/v1/projects/beta/scope-probe")).status_code == 403

        active["user"] = viewer
        assert (await client.get("/api/v1/projects/alpha/agent-profile")).status_code == 200
        assert (await client.post("/api/v1/projects/alpha/agent-profile/reset")).status_code == 403
        assert (await client.delete("/api/v1/projects/alpha")).status_code == 403
        assert (await client.get("/api/v1/projects/alpha/scope-probe")).status_code == 200
        assert (await client.post("/api/v1/projects/alpha/scope-probe")).status_code == 403
        assert (await client.get("/api/v1/api-keys")).status_code == 403
        assert (await client.get("/api/v1/providers/vision/credentials")).status_code == 403
        assert (await client.post("/api/v1/providers/gemini-vertex/credentials/upload")).status_code == 403
        assert (await client.patch("/api/v1/system/config")).status_code == 403
        assert (await client.get("/api/v1/providers")).status_code == 200
        assert (await client.post("/api/v1/assets")).status_code == 403
        assert (await client.get("/api/v1/deleted-projects")).status_code == 403
        assert (await client.get("/api/v1/tasks/task-1")).status_code == 403
        assert (await client.get("/api/v1/usage/records?project_name=alpha")).status_code == 403

        active["user"] = editor
        assert (await client.post("/api/v1/projects/alpha/scope-probe")).status_code == 200
        assert (await client.delete("/api/v1/projects/alpha")).status_code == 403
        assert (await client.get("/api/v1/api-keys")).status_code == 403
        assert (await client.get("/api/v1/providers/vision/credentials")).status_code == 403
        created = await client.post(
            "/api/v1/projects",
            json={"generation_mode": "storyboard", "title": "Owned project", "style": "", "content_mode": "narration"},
        )
        assert created.status_code == 200
        new_id = created.json()["project"]["project_id"]
        assert (await client.get(f"/api/v1/projects/{created.json()['name']}/agent-profile")).status_code == 200

        listed = await client.get("/api/v1/projects")
        assert listed.status_code == 200
        names = {entry["name"] for entry in listed.json()["projects"]}
        assert names == {"alpha", created.json()["name"]}

        active["user"] = admin
        assert (await client.get("/api/v1/api-keys")).status_code == 200
        assert (await client.get("/api/v1/providers/vision/credentials")).status_code == 200
        assert (await client.post("/api/v1/providers/gemini-vertex/credentials/upload")).status_code == 200
        assert (await client.patch("/api/v1/system/config")).status_code == 200
        assert (await client.post("/api/v1/assets")).status_code == 200
        assert (await client.get("/api/v1/deleted-projects")).status_code == 200
        assert (await client.get("/api/v1/tasks/task-1")).status_code == 200
        assert (await client.get("/api/v1/usage/records?project_name=alpha")).status_code == 200

    async def check_owner_membership():
        from sqlalchemy import select

        from lib.db.models import ProjectMembership

        async with db_factory() as session:
            membership = await session.scalar(
                select(ProjectMembership).where(
                    ProjectMembership.project_id == new_id,
                    ProjectMembership.user_id == editor.id,
                )
            )
            assert membership is not None
            assert membership.permission == "editor"

    await check_owner_membership()

    download_user = CurrentUserInfo(
        id=editor.id, sub=editor.username, role=editor.role, session_version=editor.session_version
    )
    token = create_download_token(
        editor.username, "alpha", employee=download_user, project_id=alpha_id
    )
    payload = verify_download_token(token, "alpha")
    await authorize_project_download(payload, "alpha")

    async with db_factory() as session, session.begin():
        await remove_project_member(session, actor_id=admin.id, project_id=alpha_id, user_id=editor.id)
    with pytest.raises(HTTPException) as denied_download:
        await authorize_project_download(payload, "alpha")
    assert denied_download.value.status_code == 403


@pytest.mark.parametrize("revocation", ["membership", "account"])
async def test_assistant_entry_stream_stops_after_employee_revocation(tmp_path, db_factory, monkeypatch, revocation):
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(lib.db, "async_session_factory", db_factory)
    manager = ProjectManager(tmp_path / "data")
    manager.create_project("alpha")
    monkeypatch.setattr(project_access, "get_project_manager", lambda: manager)
    project_id = manager.load_project("alpha")["project_id"]

    async with db_factory() as session, session.begin():
        admin = await create_employee(
            session, username=f"stream-admin-{revocation}", password="long-test-password", role="admin"
        )
        editor = await create_employee(
            session, username=f"stream-editor-{revocation}", password="long-test-password", role="editor"
        )
        await set_project_member(
            session, actor_id=admin.id, project_id=project_id, user_id=editor.id, permission="editor"
        )

    class _RevocationStreamService:
        async def get_session(self, session_id):
            return make_session_meta(id=session_id, user_id=editor.id, status="running", project_name="alpha")

        async def stream_entry_events(
            self, session_id, *, authorization_check=None, **_kwargs
        ):
            if authorization_check is None or not await authorization_check():
                return
            yield ServerSentEvent(event="status", data={"status": "running"})
            if revocation == "membership":
                async with db_factory() as session, session.begin():
                    await remove_project_member(
                        session, actor_id=admin.id, project_id=project_id, user_id=editor.id
                    )
            else:
                async with db_factory() as session, session.begin():
                    await set_employee_active(session, user_id=editor.id, is_active=False)
            if await authorization_check():
                yield ServerSentEvent(event="entry", data={"content": "must not be sent"})

        async def stream_startup_failure_events(self, session_id, failure):
            if False:
                yield ServerSentEvent(event="entry", data={})

    service = _RevocationStreamService()
    monkeypatch.setattr(assistant, "get_assistant_service", lambda: service)
    app = FastAPI()
    app.dependency_overrides[get_translator] = lambda: make_translator()
    app.include_router(
        assistant.router,
        prefix="/api/v1/projects/{project_name}/assistant",
        dependencies=[Depends(require_project_request_access)],
    )
    register_error_handlers(app)
    token = create_employee_token(editor.id, editor.username, editor.session_version)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/projects/alpha/assistant/sessions/stream-session/entries/stream",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert "event: status" in response.text
    assert "must not be sent" not in response.text
