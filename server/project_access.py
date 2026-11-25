"""Employee authorization shared by project HTTP entry points."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request
from sqlalchemy import select

import lib.db
from lib.db.models import ProjectMembership, User
from lib.identity.accounts import EMPLOYEE_ROLES, record_audit_event
from lib.identity.projects import ProjectAccessDenied, ProjectAction, require_project_access
from lib.project.identity import read_unique_project_identity
from lib.project.project_manager import ProjectManager, get_project_manager
from server.auth import CurrentUser, CurrentUserInfo, employee_auth_enabled


async def resolve_project_id(name: str, manager: ProjectManager | None = None) -> str:
    manager = manager or get_project_manager()
    try:
        directory = await asyncio.to_thread(manager.get_project_path, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="项目名称无效") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    try:
        return await asyncio.to_thread(read_unique_project_identity, directory)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail="项目身份异常，请先修复项目数据") from exc


async def authorize_project(
    user: CurrentUserInfo, name: str, action: ProjectAction, *, manager: ProjectManager | None = None
) -> str:
    project_id = await resolve_project_id(name, manager)
    if user.project_scoped and (user.project_id != project_id or action == "manage" or (
        action != "read" and user.project_permission != "write"
    )):
        raise HTTPException(status_code=403, detail="API Key 不允许此项目操作")
    try:
        async with lib.db.async_session_factory() as session:
            await require_project_access(session, user_id=user.id, project_id=project_id, action=action)
    except ProjectAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return project_id


async def require_project_request_access(request: Request, user: CurrentUser) -> None:
    """Authorize project paths and administrator-only shared settings.

    This dependency intentionally uses the registered route template rather
    than guessing from arbitrary ``name`` parameters. Other resource policies
    still belong to their own routers.
    """
    if not employee_auth_enabled():
        return

    route = request.scope.get("route")
    route_path = getattr(route, "path", "")
    name_param = next(
        (
            key
            for key in ("project_name", "name")
            if key in request.path_params and f"/projects/{{{key}}}" in route_path
        ),
        None,
    )
    if name_param is None:
        if user.project_scoped and not (
            (route_path.removeprefix("/api/v1") == "/projects" and request.method == "GET")
            or (route_path.removeprefix("/api/v1") == "/auth/verify" and request.method == "GET")
        ):
            raise HTTPException(status_code=403, detail="API Key 仅允许访问已授权项目")
        if _requires_global_admin(route_path, request.method) and user.role != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可管理全局凭据和配置")
        return

    name = request.path_params[name_param]
    suffix = route_path.split(f"/projects/{{{name_param}}}", 1)[1]
    if user.project_scoped and suffix.startswith("/assistant"):
        # Assistant execution identity still needs its separate session binding.
        raise HTTPException(status_code=403, detail="请使用员工会话操作内嵌助手")
    method = request.method.upper()
    if (suffix == "" and method == "DELETE") or suffix.startswith(("/members", "/trash")):
        action: ProjectAction = "manage"
    elif suffix == "/export/token":
        action = "download"
    elif method in {"GET", "HEAD", "OPTIONS"}:
        action = "read"
    else:
        action = "write"
    await authorize_project(user, name, action)


def _requires_global_admin(route_path: str, method: str) -> bool:
    """Keep shared credentials and deployment-wide settings administrator-only."""
    route_path = route_path.removeprefix("/api/v1")
    method = method.upper()
    if any(
        route_path == suffix or route_path.startswith(f"{suffix}/")
        for suffix in (
            "/api-keys",
            "/custom-providers",
            "/custom-endpoints",
            "/deleted-projects",
            "/projects/import",
            "/tasks",
            "/usage",
        )
    ):
        return True
    if route_path.startswith("/assets"):
        return method not in {"GET", "HEAD", "OPTIONS"}
    if route_path.endswith("/agent/credentials") or "/agent/credentials/" in route_path:
        return True
    if route_path.endswith("/system/config"):
        return method != "GET"
    if route_path.startswith("/providers/"):
        segments = route_path.strip("/").split("/")
        return len(segments) >= 3 and segments[2] in {"config", "multiplier", "credentials"}
    return False


async def authorize_project_download(payload: dict, name: str) -> None:
    """Re-check the employee and membership when a signed URL is redeemed."""
    if not employee_auth_enabled():
        return
    user_id, version = payload.get("uid"), payload.get("sv")
    if not isinstance(user_id, str) or type(version) is not int:
        raise HTTPException(status_code=401, detail="员工下载凭证已失效，请重新获取")
    project_id = await resolve_project_id(name)
    if payload.get("project_id") != project_id:
        raise HTTPException(status_code=403, detail="下载凭证对应的项目身份已变化")
    try:
        async with lib.db.async_session_factory() as session:
            if "api_key_id" in payload:
                from datetime import UTC, datetime

                from lib.db.models import ApiKey

                key_id = payload["api_key_id"]
                key = await session.get(ApiKey, key_id) if type(key_id) is int else None
                if (
                    key is None or key.user_id != user_id or key.employee_session_version != version
                    or key.project_id != project_id or key.project_permission != "write"
                    or (key.expires_at is not None and key.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC))
                ):
                    raise HTTPException(status_code=401, detail="下载所用 API Key 已失效")
            user = await session.get(User, user_id)
            if user is None or user.session_version != version:
                raise HTTPException(status_code=401, detail="员工下载凭证已失效，请重新获取")
            await require_project_access(session, user_id=user_id, project_id=project_id, action="download")
    except ProjectAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def require_core_project_access(request: Request, user: CurrentUser) -> None:
    """Protect the core project router, including its non-project admin routes.

    Keep this explicit: POST workflow-plan computes a preview; POST export/token
    grants download access; DELETE of the project itself requires an admin.
    Other mutations edit the current project. Unknown collection operations are
    denied to non-admins until their own resource policy is implemented.
    """
    if not employee_auth_enabled():
        return
    route = request.scope["route"].path
    if user.project_scoped and route.endswith("/projects") and request.method != "GET":
        raise HTTPException(status_code=403, detail="API Key 不能创建项目或扩大授权范围")
    name = request.path_params.get("name")
    if name is not None and "/projects/{name}" in route:
        suffix = route.split("/projects/{name}", 1)[1]
        action: ProjectAction = "read" if request.method in {"GET", "HEAD"} else "write"
        if suffix == "/workflow-plan":
            action = "read"
        elif suffix == "/export/token":
            action = "download"
        elif request.method == "DELETE" and not suffix:
            action = "manage"
        await authorize_project(user, name, action)
        return
    if route.endswith("/projects"):
        if request.method == "GET":
            return  # list_projects filters before loading summaries and covers
        if request.method == "POST" and user.role in {"admin", "editor"}:
            return
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此项目管理操作")


async def visible_project_names(user: CurrentUserInfo, manager: ProjectManager) -> set[str] | None:
    """Return an allowlist before expensive metadata reads or error fallbacks."""
    if not employee_auth_enabled():
        return None
    async with lib.db.async_session_factory() as session:
        employee = await session.get(User, user.id)
        if (
            employee is None or not employee.is_active or employee.password_hash is None
            or employee.role not in EMPLOYEE_ROLES
        ):
            raise HTTPException(status_code=403, detail="员工账号不可用")
        if employee.role == "admin" and not user.project_scoped:
            return None
        allowed = set(await session.scalars(select(ProjectMembership.project_id).where(
            ProjectMembership.user_id == user.id,
            ProjectMembership.permission.in_(["viewer", "editor"]),
        )))
        if user.project_scoped:
            allowed = {user.project_id} if employee.role == "admin" else allowed & {user.project_id}
    names = await asyncio.to_thread(manager.list_projects)
    visible: set[str] = set()
    for name in names:
        if await resolve_project_id(name, manager) in allowed:
            visible.add(name)
    return visible


async def grant_project_creator(user: CurrentUserInfo, project_id: str) -> None:
    """A newly created stable identity belongs to its actual employee creator.

    This is only called with the result of server-side project creation. If the
    database write fails, the project stays admin-only for recovery.
    """
    if not employee_auth_enabled():
        return
    async with lib.db.async_session_factory() as session, session.begin():
        employee = await session.get(User, user.id)
        if (
            employee is None or not employee.is_active or employee.password_hash is None
            or employee.role not in {"admin", "editor"}
        ):
            raise HTTPException(status_code=403, detail="员工账号不能创建项目")
        session.add(ProjectMembership(project_id=project_id, user_id=user.id, permission="editor"))
        await record_audit_event(
            session, actor_user_id=user.id, actor_label=employee.username, project_id=project_id,
            action="project.create", outcome="success", object_type="project", object_id=project_id,
        )
