"""Database-backed employee account operations."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pwdlib import PasswordHash
from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.models import AuditEvent, User

PASSWORD_MIN_LENGTH = 12
LOGIN_FAILURE_LIMIT = 8
LOGIN_LOCKOUT = timedelta(minutes=15)
EMPLOYEE_ROLES = frozenset({"admin", "editor", "viewer"})
_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_password_hash = PasswordHash.recommended()
_dummy_password_hash: str | None = None


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not _USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("username must be 3-64 lowercase letters, digits, dots, underscores, or hyphens")
    return normalized


async def _hash_password(password: str) -> str:
    return await asyncio.to_thread(_password_hash.hash, password)


async def _verify_password(password: str, encoded: str) -> bool:
    return await asyncio.to_thread(_password_hash.verify, password, encoded)


async def _get_dummy_hash() -> str:
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = await _hash_password("not-a-valid-employee-password")
    return _dummy_password_hash


async def create_employee(
    session: AsyncSession, *, username: str, password: str, role: str = "editor"
) -> User:
    """Add an employee with a stable ID. The caller owns the transaction."""
    normalized = normalize_username(username)
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"password must contain at least {PASSWORD_MIN_LENGTH} characters")
    if role not in EMPLOYEE_ROLES:
        raise ValueError("role must be admin, editor, or viewer")

    user = User(
        id=str(uuid4()),
        username=normalized,
        role=role,
        is_active=True,
        password_hash=await _hash_password(password),
    )
    session.add(user)
    await session.flush()
    return user


async def authenticate_employee(session: AsyncSession, *, username: str, password: str) -> User | None:
    """Authenticate an active employee and persist throttling state on failure."""
    normalized = username.strip().lower()
    user = await session.scalar(select(User).where(User.username == normalized))
    encoded = user.password_hash if user and user.password_hash else await _get_dummy_hash()
    password_ok = await _verify_password(password, encoded)
    now = datetime.now(UTC)

    if user is None or not user.password_hash or not user.is_active or not password_ok:
        if user is not None and user.password_hash and user.is_active:
            lock_until = now + LOGIN_LOCKOUT
            await session.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    failed_login_attempts=User.failed_login_attempts + 1,
                    locked_until=case(
                        (User.failed_login_attempts + 1 >= LOGIN_FAILURE_LIMIT, lock_until),
                        else_=User.locked_until,
                    ),
                )
            )
            await session.flush()
        return None

    locked_until = user.locked_until
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until > now:
            return None

    await session.execute(
        update(User).where(User.id == user.id).values(failed_login_attempts=0, locked_until=None)
    )
    await session.flush()
    return user


async def change_employee_password(session: AsyncSession, *, user_id: str, password: str) -> bool:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"password must contain at least {PASSWORD_MIN_LENGTH} characters")
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            password_hash=await _hash_password(password),
            session_version=User.session_version + 1,
            failed_login_attempts=0,
            locked_until=None,
        )
    )
    return bool(result.rowcount)


async def set_employee_active(session: AsyncSession, *, user_id: str, is_active: bool) -> bool:
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(is_active=is_active, session_version=User.session_version + 1)
    )
    return bool(result.rowcount)


async def change_employee_role(session: AsyncSession, *, user_id: str, role: str) -> bool:
    if role not in EMPLOYEE_ROLES:
        raise ValueError("role must be admin, editor, or viewer")
    result = await session.execute(
        update(User).where(User.id == user_id).values(role=role, session_version=User.session_version + 1)
    )
    return bool(result.rowcount)


async def record_audit_event(
    session: AsyncSession,
    *,
    action: str,
    outcome: str,
    actor_user_id: str | None = None,
    actor_label: str = "unknown",
    execution_identity: str | None = None,
    project_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    operation_id: str | None = None,
    detail: str | None = None,
    attributes: dict | None = None,
) -> AuditEvent:
    """Record an attributable event; legacy events should use actor_label='unknown'."""
    event = AuditEvent(
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        execution_identity=execution_identity,
        project_id=project_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        operation_id=operation_id,
        outcome=outcome,
        detail=detail,
        attributes=attributes,
    )
    session.add(event)
    await session.flush()
    return event
