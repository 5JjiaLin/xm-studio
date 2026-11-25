"""
认证 API 路由

提供 OAuth2 登录和 token 验证接口。
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import lib.db
from lib.db.models import User
from lib.identity.accounts import (
    EMPLOYEE_ROLES,
    change_employee_password,
    change_employee_role,
    create_employee,
    record_audit_event,
    set_employee_active,
)
from server.auth import (
    CurrentUser,
    CurrentUserInfo,
    check_credentials,
    create_employee_token,
    create_token,
    employee_auth_enabled,
    is_auth_enabled,
    oauth2_scheme_optional,
)
from server.i18n import Translator
from server.media_auth import clear_media_cookie, require_same_origin_browser, set_media_cookie

logger = logging.getLogger(__name__)

router = APIRouter()

# 公开端点：拿到 token 之前必须可达，注册时不挂 Bearer 依赖。
public_router = APIRouter()


# ==================== 响应模型 ====================


class TokenResponse(BaseModel):
    access_token: str
    token_type: str


class VerifyResponse(BaseModel):
    valid: bool
    username: str


class AuthStatusResponse(BaseModel):
    enabled: bool


class EmployeeInfo(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool
    failed_login_attempts: int
    locked_until: str | None


class CreateEmployeeRequest(BaseModel):
    username: str
    password: str
    role: str = "editor"


class UpdateEmployeeRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


# ==================== 路由 ====================


@public_router.get("/auth/status", response_model=AuthStatusResponse)
async def auth_status():
    """暴露 ``AUTH_ENABLED`` 状态供前端 bootstrap 判断是否需要登录拦截。

    前端 ``auth-store.initialize()`` 在 localStorage 无 token 时调用本接口：
    ``enabled=false`` 时跳过登录页直接进主界面；``enabled=true`` 时保留原
    登录链路。本接口本身**不要求认证**——一个 boolean 比 401 探针更直观，
    且实际"是否需要登录"通过 401/200 也能从外部观察到，因此不增量泄露。
    """
    return AuthStatusResponse(enabled=is_auth_enabled())


@public_router.post("/auth/token", response_model=TokenResponse)
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    _t: Translator,
    request: Request,
    response: Response,
):
    """用户登录

    使用 OAuth2 标准表单格式验证凭据，成功返回 access_token。
    ``AUTH_ENABLED=false`` 时跳过凭据校验，直接签发 token，让前端
    LoginPage 即便被打开也能正常跳转主界面。
    """
    if employee_auth_enabled():
        require_same_origin_browser(request)
        from lib.identity.accounts import authenticate_employee

        async with lib.db.async_session_factory() as session, session.begin():
            user = await authenticate_employee(
                session,
                username=form_data.username,
                password=form_data.password,
            )
            token = create_employee_token(user.id, user.username, user.session_version) if user else None
        # Commit failed-login counters before returning 401; raising inside begin() rolls them back.
        if user is None:
            raise HTTPException(
                status_code=401,
                detail=_t("unauthorized"),
                headers={"WWW-Authenticate": "Bearer"},
            )
        logger.info("员工登录成功: %s", user.username)
        set_media_cookie(response, token)
        return TokenResponse(access_token=token, token_type="bearer")

    if is_auth_enabled() and not check_credentials(form_data.username, form_data.password):
        logger.warning("登录失败: 用户名或密码错误 (用户: %s)", form_data.username)
        raise HTTPException(
            status_code=401,
            detail=_t("unauthorized"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_token(form_data.username)
    logger.info("用户登录成功: %s", form_data.username)
    return TokenResponse(access_token=token, token_type="bearer")


@router.post("/auth/media-session", status_code=204)
async def refresh_media_session(
    request: Request,
    response: Response,
    user: CurrentUser,
    token: Annotated[str | None, Depends(oauth2_scheme_optional)] = None,
) -> Response:
    """Restore browser media cookies from a current Bearer; never extend its expiry."""
    if employee_auth_enabled():
        require_same_origin_browser(request)
        if user.project_scoped:
            raise HTTPException(status_code=403, detail="项目凭据不能用于浏览器登录")
        if token is None or user.session_version is None:
            raise HTTPException(status_code=401, detail="需要员工会话")
        set_media_cookie(response, token)
    response.status_code = 204
    response.headers["Cache-Control"] = "no-store"
    return response


@public_router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> Response:
    """Forget browser media credentials; session revocation remains version-based."""
    require_same_origin_browser(request)
    clear_media_cookie(response)
    response.status_code = 204
    return response


@router.get("/auth/verify", response_model=VerifyResponse)
async def verify(
    current_user: CurrentUser,
):
    """验证 token 有效性

    使用 OAuth2 Bearer token 依赖自动提取和验证 token。
    """
    return VerifyResponse(valid=True, username=current_user.sub)


def _require_employee_admin(user: CurrentUserInfo) -> None:
    if not employee_auth_enabled():
        raise HTTPException(status_code=403, detail="员工账号管理尚未启用")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _employee_info(user: User) -> EmployeeInfo:
    return EmployeeInfo(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        failed_login_attempts=user.failed_login_attempts,
        locked_until=user.locked_until.isoformat() if user.locked_until else None,
    )


def _audit_actor(user: CurrentUserInfo) -> tuple[str, str]:
    return user.id, user.sub


@router.get("/users", response_model=list[EmployeeInfo])
async def list_employees(user: CurrentUser) -> list[EmployeeInfo]:
    _require_employee_admin(user)
    async with lib.db.async_session_factory() as session:
        rows = await session.scalars(select(User).where(User.password_hash.is_not(None)).order_by(User.username))
        return [_employee_info(row) for row in rows]


@router.post("/users", response_model=EmployeeInfo, status_code=201)
async def add_employee(body: CreateEmployeeRequest, user: CurrentUser) -> EmployeeInfo:
    _require_employee_admin(user)
    if body.role not in EMPLOYEE_ROLES:
        raise HTTPException(status_code=422, detail="role must be admin, editor, or viewer")
    actor_id, actor_label = _audit_actor(user)
    try:
        async with lib.db.async_session_factory() as session, session.begin():
            employee = await create_employee(session, username=body.username, password=body.password, role=body.role)
            await record_audit_event(
                session,
                action="employee.create",
                outcome="success",
                actor_user_id=actor_id,
                actor_label=actor_label,
                object_type="user",
                object_id=employee.id,
                attributes={"username": employee.username, "role": employee.role},
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    return _employee_info(employee)


@router.patch("/users/{employee_id}", response_model=EmployeeInfo)
async def update_employee(employee_id: str, body: UpdateEmployeeRequest, user: CurrentUser) -> EmployeeInfo:
    _require_employee_admin(user)
    changes = body.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="至少提供一个有效的变更字段")
    if "role" in changes and changes["role"] not in EMPLOYEE_ROLES:
        raise HTTPException(status_code=422, detail="role must be admin, editor, or viewer")
    if "password" in changes and len(changes["password"]) < 12:
        raise HTTPException(status_code=422, detail="password must contain at least 12 characters")
    if employee_id == user.id and (changes.get("is_active") is False or changes.get("role") not in (None, "admin")):
        raise HTTPException(status_code=409, detail="不能禁用或降权当前管理员账号")

    actor_id, actor_label = _audit_actor(user)
    async with lib.db.async_session_factory() as session, session.begin():
        employee = await session.scalar(select(User).where(User.id == employee_id))
        if employee is None or employee.password_hash is None:
            raise HTTPException(status_code=404, detail="员工账号不存在")
        removes_admin = (
            employee.role == "admin"
            and employee.is_active
            and (changes.get("role") not in (None, "admin") or changes.get("is_active") is False)
        )
        if removes_admin:
            active_admins = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == "admin", User.is_active.is_(True), User.password_hash.is_not(None))
            )
            if active_admins <= 1:
                raise HTTPException(status_code=409, detail="不能禁用或降权最后一个启用的管理员")

        if "password" in changes:
            await change_employee_password(session, user_id=employee_id, password=changes["password"])
        if "role" in changes:
            await change_employee_role(session, user_id=employee_id, role=changes["role"])
        if "is_active" in changes:
            await set_employee_active(session, user_id=employee_id, is_active=changes["is_active"])
        await session.flush()
        await session.refresh(employee)
        await record_audit_event(
            session,
            action="employee.update",
            outcome="success",
            actor_user_id=actor_id,
            actor_label=actor_label,
            object_type="user",
            object_id=employee.id,
            attributes={key: ("[changed]" if key == "password" else value) for key, value in changes.items()},
        )
    return _employee_info(employee)
