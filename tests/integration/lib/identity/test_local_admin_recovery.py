"""Offline administrator changes against an explicit, existing SQLite database."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from lib.db.base import Base
from lib.db.models import AuditEvent, User
from lib.identity.accounts import authenticate_employee, create_employee
from scripts.company_admin import apply_change

PASSWORD = "local-admin-test-password"


@pytest.fixture
async def deployment(tmp_path, monkeypatch):
    def prepare():
        root = tmp_path / "deployment"
        (root / "data").mkdir(parents=True)
        (root / "ops").mkdir()
        return root

    root = await asyncio.to_thread(prepare)
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'data' / 'arcreel.db').as_posix()}", poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(conn, tables=[User.__table__, AuditEvent.__table__])
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add(User(id="default", username="default", role="admin", is_active=True))
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    try:
        yield root, factory
    finally:
        await engine.dispose()


async def change(root, action="initialize", username="employee-admin", password=PASSWORD):
    return await apply_change(
        root / "data" / "arcreel.db",
        action=action,
        username=username,
        password=password,
        operator="server\\operator (S-1-test)",
        reason="IT-2026-001",
    )


async def test_initialize_preserves_shared_user_and_records_os_actor(deployment):
    root, factory = deployment
    result = await change(root)
    async with factory() as session:
        employee = await authenticate_employee(session, username="employee-admin", password=PASSWORD)
        assert employee is not None
        assert employee.id == result["id"]
        assert employee.id != "default"
        assert employee.role == "admin"
        assert employee.password_hash != PASSWORD
        shared = await session.get(User, "default")
        assert shared.password_hash is None
        assert shared.session_version == 0
        event = await session.scalar(select(AuditEvent))
        assert event.actor_user_id is None
        assert event.actor_label == "local-os:server\\operator (S-1-test)"
        assert event.object_id == employee.id
        assert event.attributes["reason"] == "IT-2026-001"
        assert PASSWORD not in str(event.attributes)


async def test_initialization_cannot_create_another_admin_or_promote_shared_user(deployment):
    root, factory = deployment
    with pytest.raises(ValueError, match="Username already exists"):
        await change(root, username="default")
    await change(root)
    with pytest.raises(ValueError, match="credentials already exist"):
        await change(root, username="another-admin")
    async with factory() as session:
        assert len((await session.scalars(select(User))).all()) == 2
        assert len((await session.scalars(select(AuditEvent))).all()) == 1


async def test_recovery_reenables_unlocks_and_revokes_old_session(deployment):
    root, factory = deployment
    old = await change(root)
    async with factory() as session, session.begin():
        employee = await session.get(User, old["id"])
        employee.is_active = False
        employee.failed_login_attempts = 8
        employee.locked_until = datetime.now(UTC) + timedelta(hours=1)
    recovered = await change(root, action="recover", password="recovered-admin-password")
    assert recovered["id"] == old["id"]
    assert recovered["session_version"] == old["session_version"] + 1
    async with factory() as session:
        assert await authenticate_employee(session, username="employee-admin", password=PASSWORD) is None
        employee = await authenticate_employee(session, username="employee-admin", password="recovered-admin-password")
        assert employee is not None
        assert employee.is_active
        assert employee.failed_login_attempts == 0
        assert employee.locked_until is None
        event = await session.scalar(select(AuditEvent).where(AuditEvent.action == "employee.local_recover"))
        assert event.object_id == employee.id
        assert event.attributes["session_version"] == recovered["session_version"]


@pytest.mark.parametrize("username", ["missing-admin", "default", "employee-viewer"])
async def test_recovery_rejects_missing_shared_and_non_admin_accounts(deployment, username):
    root, factory = deployment
    async with factory() as session, session.begin():
        await create_employee(session, username="employee-viewer", password=PASSWORD, role="viewer")
    with pytest.raises(ValueError, match="existing employee administrator"):
        await change(root, action="recover", username=username)
    async with factory() as session:
        assert not (await session.scalars(select(AuditEvent))).all()


async def test_audit_failure_rolls_back_the_admin_password(deployment, monkeypatch):
    from lib.identity import admin_recovery

    root, factory = deployment
    original = await change(root)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(admin_recovery, "record_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await change(root, action="recover", password="must-not-be-committed")
    async with factory() as session:
        employee = await authenticate_employee(session, username="employee-admin", password=PASSWORD)
        assert employee is not None
        assert employee.session_version == original["session_version"]
        assert len((await session.scalars(select(AuditEvent))).all()) == 1


async def test_missing_database_is_not_created_and_other_environment_is_ignored(deployment, monkeypatch):
    root, factory = deployment
    unrelated = root / "unrelated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{unrelated.as_posix()}")
    await change(root)
    assert not await asyncio.to_thread(unrelated.exists)
    missing = root / "data" / "missing.db"
    with pytest.raises(OperationalError, match="unable to open database"):
        await apply_change(
            missing, action="initialize", username="test-admin", password=PASSWORD, operator="local", reason="test"
        )
    assert not await asyncio.to_thread(missing.exists)
    async with factory() as session:
        assert len((await session.scalars(select(User))).all()) == 2


async def test_outdated_schema_fails_without_migrating_or_changing_shared_user(deployment):
    root, factory = deployment
    async with factory() as session, session.begin():
        from sqlalchemy import text

        await session.execute(text("DROP TABLE audit_events"))
    with pytest.raises(ValueError, match="schema is not ready"):
        await change(root)
    async with factory() as session:
        assert len((await session.scalars(select(User))).all()) == 1


async def test_concurrent_initialization_cannot_create_multiple_admins(deployment):
    root, factory = deployment
    results = await asyncio.gather(
        change(root, username="first-admin"),
        change(root, username="second-admin"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, (ValueError, OperationalError)) for result in results) == 1
    async with factory() as session:
        assert len((await session.scalars(select(User).where(User.password_hash.is_not(None)))).all()) == 1
        assert len((await session.scalars(select(AuditEvent))).all()) == 1


async def test_interactive_cli_roundtrip_uses_real_offline_locks_and_database(deployment, monkeypatch, capsys):
    from scripts import company_admin

    root, factory = deployment
    monkeypatch.setattr(company_admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(company_admin, "prompt_password", lambda: PASSWORD)
    replies = iter(["IT-bootstrap", "initialize employee-admin", "IT-recovery", "recover employee-admin"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    for action in ("initialize", "recover"):
        assert (
            await asyncio.to_thread(company_admin.main, [action, "--root", str(root), "--username", "employee-admin"])
            == 0
        )
    async with factory() as session:
        events = (await session.scalars(select(AuditEvent).order_by(AuditEvent.id))).all()
        assert [event.action for event in events] == ["employee.local_initialize", "employee.local_recover"]
        assert events[-1].attributes["session_version"] == 1
        assert "S-1-" in events[-1].execution_identity if os.name == "nt" else "uid=" in events[-1].execution_identity
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert "Audit committed" in output.out
