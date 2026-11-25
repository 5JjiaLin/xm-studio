"""Employee account service coverage."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import lib.db
from lib.db.models import AuditEvent, User
from lib.identity.accounts import (
    LOGIN_FAILURE_LIMIT,
    authenticate_employee,
    change_employee_password,
    change_employee_role,
    create_employee,
    record_audit_event,
    set_employee_active,
)


async def test_employee_password_and_session_invalidation(db_session):
    user = await create_employee(db_session, username="Alice.Example", password="long-test-password", role="editor")
    await db_session.commit()
    assert user.username == "alice.example"
    assert user.id != "default"
    assert user.password_hash != "long-test-password"

    authenticated = await authenticate_employee(db_session, username="ALICE.EXAMPLE", password="long-test-password")
    assert authenticated is not None

    assert await change_employee_password(db_session, user_id=user.id, password="another-long-password")
    await db_session.refresh(user)
    assert user.session_version == 1
    assert await authenticate_employee(db_session, username=user.username, password="long-test-password") is None
    assert await authenticate_employee(db_session, username=user.username, password="another-long-password") is user

    assert await change_employee_role(db_session, user_id=user.id, role="viewer")
    await db_session.refresh(user)
    assert user.role == "viewer"
    assert user.session_version == 2

    assert await set_employee_active(db_session, user_id=user.id, is_active=False)
    await db_session.refresh(user)
    assert user.session_version == 3
    assert await authenticate_employee(db_session, username=user.username, password="another-long-password") is None


async def test_employee_login_failures_lock_account(db_session):
    user = await create_employee(db_session, username="lock-test", password="long-test-password")
    await db_session.commit()

    for _ in range(LOGIN_FAILURE_LIMIT):
        assert await authenticate_employee(db_session, username=user.username, password="wrong-password") is None

    await db_session.refresh(user)
    assert user.failed_login_attempts == LOGIN_FAILURE_LIMIT
    assert user.locked_until is not None
    assert await authenticate_employee(db_session, username=user.username, password="long-test-password") is None

    user.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert await authenticate_employee(db_session, username=user.username, password="long-test-password") is user
    await db_session.refresh(user)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


async def test_audit_event_keeps_unknown_actor_and_execution_identity(db_session):
    event = await record_audit_event(
        db_session,
        action="legacy-import",
        outcome="unattributed",
        execution_identity="company-service",
        project_id="stable-project-id",
        operation_id="op-123",
    )
    await db_session.commit()

    loaded = await db_session.scalar(select(AuditEvent).where(AuditEvent.id == event.id))
    assert loaded is not None
    assert loaded.actor_user_id is None
    assert loaded.actor_label == "unknown"
    assert loaded.execution_identity == "company-service"
    assert loaded.project_id == "stable-project-id"
    assert loaded.operation_id == "op-123"


async def test_legacy_shared_user_is_not_employee_login(db_session):
    legacy = User(id="default", username="admin", role="admin", is_active=True)
    db_session.add(legacy)
    await db_session.commit()

    assert await authenticate_employee(db_session, username="admin", password="any-password") is None


async def test_employee_session_resolves_current_role_and_revokes_old_token(db_session, monkeypatch):
    from lib.identity.accounts import change_employee_role
    from server.auth import create_employee_token, get_current_user

    user = await create_employee(db_session, username="session-test", password="long-test-password")
    await db_session.commit()

    @asynccontextmanager
    async def use_test_session():
        yield db_session

    monkeypatch.setattr(lib.db, "async_session_factory", use_test_session)
    token = create_employee_token(user.id, user.username, user.session_version)

    current = await get_current_user(token)
    assert current.id == user.id
    assert current.sub == user.username
    assert current.role == "editor"

    assert await change_employee_role(db_session, user_id=user.id, role="viewer")
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(token)
    assert exc_info.value.status_code == 401
