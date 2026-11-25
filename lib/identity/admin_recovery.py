"""Employee administrator operations for the offline, local-only operator CLI."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.models import User
from lib.identity.accounts import (
    change_employee_password,
    create_employee,
    normalize_username,
    record_audit_event,
)


async def change_local_admin(
    session: AsyncSession,
    *,
    action: str,
    username: str,
    password: str,
    operator: str,
    reason: str,
) -> User:
    """Caller must hold offline deployment locks and a serialized DB transaction.

    No HTTP route calls this function. OS access to the protected deployment is
    the recovery authority; it is recorded separately from employee identity.
    """
    normalized = normalize_username(username)
    if not operator.strip() or not 1 <= len(reason.strip()) <= 400:
        raise ValueError("An OS operator and a recovery reason of 1-400 characters are required")
    if action == "initialize":
        existing = await session.scalar(select(User.id).where(User.password_hash.is_not(None)).limit(1))
        if existing is not None:
            raise ValueError("Employee credentials already exist; use recovery for an existing administrator")
        if await session.scalar(select(User.id).where(User.username == normalized)) is not None:
            raise ValueError("Username already exists; choose a separate employee administrator name")
        employee = await create_employee(session, username=normalized, password=password, role="admin")
    elif action == "recover":
        employee = await session.scalar(select(User).where(User.username == normalized))
        if employee is None or employee.password_hash is None or employee.role != "admin":
            raise ValueError(
                "Recovery requires an existing employee administrator; shared and non-admin users cannot be promoted"
            )
        await change_employee_password(session, user_id=employee.id, password=password)
        employee.is_active = True
        await session.flush()
        await session.refresh(employee)
    else:
        raise ValueError("Action must be initialize or recover")
    await record_audit_event(
        session,
        action=f"employee.local_{action}",
        outcome="success",
        actor_label=f"local-os:{operator}",
        execution_identity=operator,
        object_type="user",
        object_id=employee.id,
        attributes={"reason": reason.strip(), "session_version": employee.session_version, "password": "[changed]"},
    )
    return employee
