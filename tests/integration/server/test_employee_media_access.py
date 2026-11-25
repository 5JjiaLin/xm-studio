"""Employee media login, project boundaries, revocation and native file semantics."""

import asyncio
import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

import lib.db
from lib.identity.accounts import change_employee_password, change_employee_role, create_employee, set_employee_active
from lib.identity.projects import remove_project_member, set_project_member
from lib.project.project_manager import ProjectManager
from server import project_access
from server.auth import create_employee_token, create_token, get_current_user, get_token_secret, verify_token
from server.media_auth import MEDIA_COOKIE_NAME, MEDIA_COOKIE_PATHS
from server.routers import auth, files


@pytest.fixture
async def media_api(tmp_path, db_factory, monkeypatch):
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "false")  # employee mode must still force authentication
    monkeypatch.setattr(lib.db, "async_session_factory", db_factory)
    manager = ProjectManager(tmp_path / "data")

    def prepare_files():
        for name in ("alpha", "beta"):
            manager.create_project(name)
            directory = manager.get_project_path(name)
            (directory / "storyboards").mkdir(exist_ok=True)
            (directory / "videos").mkdir(exist_ok=True)
            Image.new("RGB", (640, 480), "red").save(directory / "storyboards" / "scene.png")
            (directory / "videos" / "clip.mp4").write_bytes(b"0123456789")
        root = manager.get_global_assets_root() / "character"
        root.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), "blue").save(root / "shared.png")
        return manager.load_project("alpha")["project_id"]

    alpha_id = await asyncio.to_thread(prepare_files)
    monkeypatch.setattr(project_access, "get_project_manager", lambda: manager)
    monkeypatch.setattr(files, "get_project_manager", lambda: manager)
    async with db_factory() as session, session.begin():
        admin = await create_employee(session, username="media-admin", password="long-test-password", role="admin")
        viewer = await create_employee(session, username="media-viewer", password="long-test-password", role="viewer")
        editor = await create_employee(session, username="media-editor", password="long-test-password", role="editor")
        for member in (viewer, editor):
            await set_project_member(
                session, actor_id=admin.id, project_id=alpha_id, user_id=member.id, permission=member.role
            )
    app = FastAPI()
    app.include_router(auth.public_router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1", dependencies=[Depends(get_current_user)])
    app.include_router(files.public_router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://studio.test") as client:
        yield client, db_factory, admin, viewer, editor, alpha_id


async def login(client, user):
    response = await client.post(
        "/api/v1/auth/token", data={"username": user.username, "password": "long-test-password"}
    )
    assert response.status_code == 200
    return response


def assert_private(response):
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie, Authorization"


async def test_native_login_media_range_thumbnail_and_api_separation(media_api):
    client, _, _, viewer, _, _ = media_api
    response = await login(client, viewer)
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for cookie, path in zip(cookies, MEDIA_COOKIE_PATHS, strict=True):
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=strict" in cookie
        assert f"Path={path}" in cookie
        assert "Domain=" not in cookie
    cookie = next(cookie for cookie in client.cookies.jar if cookie.path == MEDIA_COOKIE_PATHS[0])
    assert verify_token(cookie.value) is None  # purpose-bound media credential cannot authenticate APIs
    assert cookie.value != response.json()["access_token"]

    image = await client.get("/api/v1/files/alpha/storyboards/scene.png?v=1")
    assert image.status_code == 200
    assert_private(image)
    thumb = await client.get("/api/v1/files/alpha/storyboards/scene.png?v=1&w=320")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert_private(thumb)
    ranged = await client.get("/api/v1/files/alpha/videos/clip.mp4?v=1", headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206
    assert ranged.content == b"2345"
    assert ranged.headers["content-range"] == "bytes 2-5/10"
    assert_private(ranged)
    not_modified = await client.get(
        "/api/v1/files/alpha/storyboards/scene.png?v=1", headers={"If-None-Match": image.headers["etag"]}
    )
    assert not_modified.status_code == 304
    assert_private(not_modified)
    assert (await client.get("/api/v1/files/alpha/project.json")).status_code == 404
    assert (await client.get("/api/v1/auth/verify")).status_code == 401
    assert (
        await client.get("/api/v1/auth/verify", headers={"Cookie": f"{MEDIA_COOKIE_NAME}={cookie.value}"})
    ).status_code == 401
    assert (
        await client.get("/api/v1/auth/verify", headers={"Authorization": f"Bearer {cookie.value}"})
    ).status_code == 401
    assert (
        await client.get("/api/v1/auth/verify", headers={"Authorization": f"Bearer {response.json()['access_token']}"})
    ).status_code == 200


async def test_anonymous_query_and_wrong_purpose_cannot_authenticate(media_api):
    client, _, _, viewer, _, _ = media_api
    token = create_employee_token(viewer.id, viewer.username, viewer.session_version)
    for path in (
        "/api/v1/files/alpha/storyboards/scene.png",
        "/api/v1/files/unknown/storyboards/scene.png",
        "/api/v1/global-assets/character/shared.png",
        f"/api/v1/files/alpha/storyboards/scene.png?token={token}",
    ):
        denied = await client.get(path)
        assert denied.status_code == 401
        assert_private(denied)
    for wrong_cookie in (token, create_token("legacy"), "broken"):
        denied = await client.get(
            "/api/v1/files/alpha/storyboards/scene.png", headers={"Cookie": f"{MEDIA_COOKIE_NAME}={wrong_cookie}"}
        )
        assert denied.status_code == 401


async def test_project_boundaries_member_removal_and_global_library(media_api):
    client, factory, admin, viewer, _, alpha_id = media_api
    await login(client, viewer)
    assert (await client.get("/api/v1/files/beta/storyboards/scene.png")).status_code == 403
    shared = await client.get("/api/v1/global-assets/character/shared.png?v=1")
    assert shared.status_code == 200
    assert_private(shared)
    original = await client.get("/api/v1/files/alpha/storyboards/scene.png?v=1")
    async with factory() as session, session.begin():
        await remove_project_member(session, actor_id=admin.id, project_id=alpha_id, user_id=viewer.id)
    for headers in ({}, {"If-None-Match": original.headers["etag"]}, {"Range": "bytes=0-3"}):
        revoked = await client.get("/api/v1/files/alpha/storyboards/scene.png?v=1", headers=headers)
        assert revoked.status_code == 403
        assert_private(revoked)
    await login(client, admin)
    assert (await client.get("/api/v1/files/beta/storyboards/scene.png")).status_code == 200


@pytest.mark.parametrize("change", ["password", "disabled", "role"])
async def test_employee_changes_revoke_media_and_bootstrap(media_api, change):
    client, factory, _, _, editor, _ = media_api
    response = await login(client, editor)
    token = response.json()["access_token"]
    async with factory() as session, session.begin():
        if change == "password":
            await change_employee_password(session, user_id=editor.id, password="another-long-password")
        elif change == "disabled":
            await set_employee_active(session, user_id=editor.id, is_active=False)
        else:
            await change_employee_role(session, user_id=editor.id, role="viewer")
    for path in ("/api/v1/files/alpha/videos/clip.mp4", "/api/v1/global-assets/character/shared.png"):
        revoked = await client.get(path)
        assert revoked.status_code == 401
        assert_private(revoked)
    assert (
        await client.post("/api/v1/auth/media-session", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 401


async def test_bearer_bootstrap_logout_expiry_and_cookie_transport(media_api):
    client, _, _, viewer, _, _ = media_api
    token = create_employee_token(viewer.id, viewer.username, viewer.session_version)
    headers = {"Authorization": f"Bearer {token}"}
    # API clients can read media with an explicit employee Bearer.
    assert (await client.get("/api/v1/files/alpha/videos/clip.mp4", headers=headers)).status_code == 200
    bootstrap = await client.post("/api/v1/auth/media-session", headers=headers)
    assert bootstrap.status_code == 204
    cookie = next(cookie for cookie in client.cookies.jar if cookie.path == MEDIA_COOKIE_PATHS[0])
    media_payload = jwt.decode(cookie.value, get_token_secret(), algorithms=["HS256"])
    assert media_payload["exp"] == verify_token(token)["exp"]
    assert (await client.get("http://studio.test/api/v1/files/alpha/videos/clip.mp4")).status_code == 401
    # An invalid header cannot silently fall back to an otherwise-valid cookie.
    assert (
        await client.get("/api/v1/files/alpha/videos/clip.mp4", headers={"Authorization": "Bearer broken"})
    ).status_code == 401
    expired = jwt.encode({**media_payload, "exp": time.time() - 1}, get_token_secret(), algorithm="HS256")
    assert (
        await client.get("/api/v1/files/alpha/videos/clip.mp4", headers={"Cookie": f"{MEDIA_COOKIE_NAME}={expired}"})
    ).status_code == 401
    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert len(logout.headers.get_list("set-cookie")) == 2
    assert not list(client.cookies.jar)
    assert (await client.get("/api/v1/files/alpha/videos/clip.mp4")).status_code == 401


async def test_cross_site_login_and_logout_rejected(media_api):
    client, _, _, viewer, _, _ = media_api
    denied_login = await client.post(
        "/api/v1/auth/token",
        headers={"Origin": "https://other.test"},
        data={"username": viewer.username, "password": "long-test-password"},
    )
    assert denied_login.status_code == 403
    assert not denied_login.headers.get_list("set-cookie")
    await login(client, viewer)
    assert (await client.post("/api/v1/auth/logout", headers={"Origin": "https://other.test"})).status_code == 403
    token = create_employee_token(viewer.id, viewer.username, viewer.session_version)
    assert (
        await client.post(
            "/api/v1/auth/media-session",
            headers={"Authorization": f"Bearer {token}", "Origin": "https://other.test"},
        )
    ).status_code == 403
    assert (await client.get("/api/v1/files/alpha/videos/clip.mp4")).status_code == 200


async def test_shared_mode_keeps_existing_anonymous_media(media_api, monkeypatch):
    client, _, _, _, _, _ = media_api
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    original = await client.get("/api/v1/files/alpha/videos/clip.mp4?v=1")
    assert original.status_code == 200
    assert original.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert (await client.get("/api/v1/global-assets/character/shared.png?v=1")).status_code == 200
