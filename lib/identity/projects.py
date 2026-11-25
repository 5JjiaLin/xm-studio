"""Database-backed project permissions; callers own transaction boundaries."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.models import ProjectMembership, User
from lib.identity.accounts import EMPLOYEE_ROLES, record_audit_event
from lib.project.identity import validate_project_id

ProjectAction = Literal["read", "write", "download", "manage"]
MEMBERSHIP_PERMISSIONS = frozenset({"viewer", "editor"})


class ProjectAccessDenied(PermissionError):
    """The current employee or their membership does not allow this operation."""


async def _employee(session: AsyncSession, user_id: str) -> User:
    # Do not trust an ORM identity-map snapshot left by an earlier authorization.
    user = await session.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    if user is None or not user.is_active or user.password_hash is None or user.role not in EMPLOYEE_ROLES:
        raise ProjectAccessDenied("员工账号不可用")
    return user


async def _admin(session: AsyncSession, user_id: str) -> User:
    user = await _employee(session, user_id)
    if user.role != "admin":
        raise ProjectAccessDenied("仅管理员可管理项目成员")
    return user


async def require_project_access(
    session: AsyncSession, *, user_id: str, project_id: str, action: ProjectAction
) -> None:
    """Re-evaluate current role and membership; do not cache permission grants.

    Viewers can read project content. Downloads require both an editor account
    and an editor membership. Whole-project deletion and member management are
    admin operations even for the project creator.
    """
    validate_project_id(project_id)
    if action not in {"read", "write", "download", "manage"}:
        raise ValueError("unknown project action")
    user = await _employee(session, user_id)
    if user.role == "admin":
        return
    if action == "manage" or (action != "read" and user.role != "editor"):
        raise ProjectAccessDenied("没有此项目操作权限")
    permission = await session.scalar(
        select(ProjectMembership.permission).where(
            ProjectMembership.project_id == project_id, ProjectMembership.user_id == user_id
        )
    )
    if permission in MEMBERSHIP_PERMISSIONS and (action == "read" or permission == "editor"):
        return
    raise ProjectAccessDenied("没有此项目操作权限")


async def set_project_member(
    session: AsyncSession, *, actor_id: str, project_id: str, user_id: str, permission: str
) -> ProjectMembership:
    validate_project_id(project_id)
    if permission not in MEMBERSHIP_PERMISSIONS:
        raise ValueError("permission must be viewer or editor")
    actor = await _admin(session, actor_id)
    await _employee(session, user_id)
    member = await session.scalar(
        select(ProjectMembership)
        .where(ProjectMembership.project_id == project_id, ProjectMembership.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    previous = member.permission if member else None
    if member is None:
        member = ProjectMembership(project_id=project_id, user_id=user_id, permission=permission)
        session.add(member)
    else:
        member.permission = permission
    await session.flush()
    if previous != permission:
        await record_audit_event(
            session,
            actor_user_id=actor.id,
            actor_label=actor.username,
            project_id=project_id,
            action="project.member.set",
            outcome="success",
            object_type="user",
            object_id=user_id,
            attributes={"previous_permission": previous, "permission": permission},
        )
    return member


async def remove_project_member(
    session: AsyncSession, *, actor_id: str, project_id: str, user_id: str
) -> bool:
    validate_project_id(project_id)
    actor = await _admin(session, actor_id)
    member = await session.get(ProjectMembership, (project_id, user_id), populate_existing=True)
    if member is None:
        return False
    previous = member.permission
    await session.delete(member)
    await record_audit_event(
        session,
        actor_user_id=actor.id,
        actor_label=actor.username,
        project_id=project_id,
        action="project.member.remove",
        outcome="success",
        object_type="user",
        object_id=user_id,
        attributes={"previous_permission": previous},
    )
    return True


async def list_project_members(session: AsyncSession, *, actor_id: str, project_id: str) -> list[dict]:
    validate_project_id(project_id)
    await _admin(session, actor_id)
    rows = await session.execute(
        select(User.id, User.username, User.role, User.is_active, ProjectMembership.permission)
        .join(ProjectMembership, ProjectMembership.user_id == User.id)
        .where(ProjectMembership.project_id == project_id)
        .order_by(User.username)
    )
    return [dict(row._mapping) for row in rows]
