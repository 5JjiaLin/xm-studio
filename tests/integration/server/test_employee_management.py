"""Exercise employee management through HTTP with real database sessions."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import lib.db
from lib.db.models import AuditEvent, User
from lib.identity.accounts import create_employee
from server.auth import create_employee_token
from server.routers import auth
from tests.auth_deps import AUTH_DEPENDENCIES


@pytest.fixture
async def employee_api(db_factory, monkeypatch):
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(lib.db, "async_session_factory", db_factory)
    async with db_factory() as session, session.begin():
        admin = await create_employee(session, username="team-admin", password="long-test-password", role="admin")
        token = create_employee_token(admin.id, admin.username, admin.session_version)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    app.include_router(auth.public_router, prefix="/api/v1")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client, db_factory, admin


async def test_create_list_duplicate_and_audit(employee_api):
    client, factory, admin = employee_api
    body = {"username": "Alice.Example", "password": "long-test-password", "role": "editor"}
    created = await client.post("/api/v1/users", json=body)
    assert created.status_code == 201
    employee = created.json()
    assert employee["username"] == "alice.example"
    assert "password_hash" not in employee
    assert "password" not in employee
    assert (await client.post("/api/v1/users", json=body)).status_code == 409
    listed = await client.get("/api/v1/users")
    assert listed.status_code == 200
    assert {row["username"] for row in listed.json()} == {"team-admin", "alice.example"}
    async with factory() as session:
        events = list(await session.scalars(select(AuditEvent)))
    assert len(events) == 1
    assert (events[0].action, events[0].actor_user_id, events[0].object_id) == (
        "employee.create",
        admin.id,
        employee["id"],
    )
    assert "long-test-password" not in str(events[0].attributes)


@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_non_admin_cannot_manage_employees(employee_api, role):
    client, factory, _ = employee_api
    async with factory() as session, session.begin():
        user = await create_employee(session, username="regular-user", password="long-test-password", role=role)
        token = create_employee_token(user.id, user.username, user.session_version)
    client.headers["Authorization"] = f"Bearer {token}"
    for method, path, body in [
        ("GET", "/api/v1/users", None),
        ("POST", "/api/v1/users", {"username": "another-user", "password": "long-test-password"}),
        ("PATCH", f"/api/v1/users/{user.id}", {"role": "admin"}),
    ]:
        response = await client.request(method, path, json=body)
        assert response.status_code == 403


@pytest.mark.parametrize("changes", [{"role": "viewer"}, {"is_active": False}, {"password": "another-long-password"}])
async def test_changes_revoke_employee_session_and_audit(employee_api, changes):
    client, factory, admin = employee_api
    async with factory() as session, session.begin():
        employee = await create_employee(session, username="regular-user", password="long-test-password")
        token = create_employee_token(employee.id, employee.username, employee.session_version)
    updated = await client.patch(f"/api/v1/users/{employee.id}", json=changes)
    assert updated.status_code == 200
    revoked = await client.get("/api/v1/auth/verify", headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401
    async with factory() as session:
        row = await session.get(User, employee.id)
        event = await session.scalar(select(AuditEvent).where(AuditEvent.action == "employee.update"))
        assert row.session_version == 1
        assert event.actor_user_id == admin.id
        assert event.attributes == {key: "[changed]" if key == "password" else value for key, value in changes.items()}


@pytest.mark.parametrize("changes", [{"role": "editor"}, {"is_active": False}])
async def test_last_admin_cannot_disable_or_demote_self(employee_api, changes):
    client, factory, admin = employee_api
    response = await client.patch(f"/api/v1/users/{admin.id}", json=changes)
    assert response.status_code == 409
    async with factory() as session:
        row = await session.get(User, admin.id)
        assert row.is_active
        assert row.role == "admin"
        assert await session.scalar(select(AuditEvent)) is None


async def test_failed_http_login_commits_failure_counter(employee_api):
    client, factory, admin = employee_api
    response = await client.post(
        "/api/v1/auth/token", data={"username": admin.username, "password": "incorrect-password"}
    )
    assert response.status_code == 401
    async with factory() as session:
        row = await session.get(User, admin.id)
        assert row.failed_login_attempts == 1
