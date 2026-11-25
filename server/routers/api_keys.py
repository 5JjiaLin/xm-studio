"""
API Key 管理路由

提供 API Key 的创建、列表查询和删除接口。
"""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from lib.db import async_session_factory
from lib.db.models import User
from lib.db.repositories.api_key_repository import ApiKeyRepository
from lib.identity.accounts import record_audit_event
from lib.identity.projects import ProjectAccessDenied, require_project_access
from server.auth import (
    API_KEY_PREFIX,
    CurrentUser,
    CurrentUserInfo,
    employee_auth_enabled,
    hash_api_key,
    invalidate_api_key_cache,
)
from server.i18n import Translator
from server.project_access import resolve_project_id

router = APIRouter()


def _require_jwt_auth(user: CurrentUserInfo, _t: Callable[..., str]) -> None:
    """确保请求通过 JWT 认证（非 API Key）。API Key 管理操作不允许由 API Key 本身执行。"""
    if user.api_key_id is not None or user.sub.startswith("apikey:"):
        raise HTTPException(status_code=403, detail=_t("jwt_auth_required"))
    if employee_auth_enabled() and user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理 API Key")


API_KEY_DEFAULT_EXPIRY_DAYS = 30


def _generate_api_key() -> str:
    """生成格式为 arc-<32位随机字符> 的 API Key。"""
    random_part = secrets.token_hex(16)  # 32 hex chars
    return f"{API_KEY_PREFIX}{random_part}"


def _default_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(days=API_KEY_DEFAULT_EXPIRY_DAYS)


class CreateApiKeyRequest(BaseModel):
    name: str
    expires_days: int | None = Field(None, ge=0)  # None 使用默认 30 天，0 表示不过期
    user_id: str | None = None
    project_name: str | None = None
    project_permission: Literal["read", "write"] = "read"


class CreateApiKeyResponse(BaseModel):
    id: int
    name: str
    key: str  # 完整 key，仅在创建时返回
    key_prefix: str
    created_at: str
    expires_at: str | None
    user_id: str | None = None
    project_id: str | None = None
    project_permission: str | None = None


class ApiKeyInfo(BaseModel):
    id: int
    name: str
    key_prefix: str
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    user_id: str | None = None
    project_id: str | None = None
    project_permission: str | None = None


@router.post("/api-keys", status_code=201)
async def create_api_key(
    body: CreateApiKeyRequest,
    user: CurrentUser,
    _t: Translator,
) -> CreateApiKeyResponse:
    """创建新 API Key。完整 key 仅在响应中出现一次，之后无法再查看。"""
    _require_jwt_auth(user, _t)
    employee_mode = employee_auth_enabled()
    project_id = None
    if employee_mode:
        if not body.project_name:
            raise HTTPException(status_code=422, detail="员工 API Key 必须指定项目")
        project_id = await resolve_project_id(body.project_name)
    elif body.project_name is not None or body.user_id is not None:
        raise HTTPException(status_code=422, detail="共享模式不支持员工项目 API Key")
    key = _generate_api_key()
    key_hash = hash_api_key(key)
    key_prefix = key[:8]  # e.g. "arc-abcd"

    if body.expires_days == 0:
        expires_at: datetime | None = None
    elif body.expires_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=body.expires_days)
    else:
        expires_at = _default_expires_at()

    try:
        async with async_session_factory() as session, session.begin():
            repo = ApiKeyRepository(session)
            bindings = {}
            if employee_mode:
                owner = await session.get(User, body.user_id or user.id)
                if owner is None:
                    raise HTTPException(status_code=422, detail="员工不存在")
                try:
                    await require_project_access(
                        session, user_id=owner.id, project_id=project_id, action=body.project_permission
                    )
                except ProjectAccessDenied as exc:
                    raise HTTPException(status_code=403, detail="员工无此项目权限") from exc
                bindings = {
                    "user_id": owner.id, "employee_session_version": owner.session_version,
                    "project_id": project_id, "project_permission": body.project_permission,
                }
            row = await repo.create(
                name=body.name,
                key_hash=key_hash,
                key_prefix=key_prefix,
                expires_at=expires_at,
                **bindings,
            )
            if employee_mode:
                await record_audit_event(
                    session, actor_user_id=user.id, actor_label=user.sub, project_id=project_id,
                    action="api_key.create", outcome="success", object_type="api_key", object_id=str(row["id"]),
                    attributes={"user_id": bindings["user_id"], "permission": body.project_permission},
                )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail=_t("api_key_name_exists", name=body.name)) from exc

    return CreateApiKeyResponse(
        id=row["id"],
        name=row["name"],
        key=key,
        key_prefix=row["key_prefix"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        user_id=row.get("user_id"),
        project_id=row.get("project_id"),
        project_permission=row.get("project_permission"),
    )


@router.get("/api-keys")
async def list_api_keys(
    user: CurrentUser,
    _t: Translator,
) -> list[ApiKeyInfo]:
    """查询所有 API Key 的元数据（不含完整 key）。"""
    _require_jwt_auth(user, _t)
    async with async_session_factory() as session, session.begin():
        repo = ApiKeyRepository(session)
        rows = await repo.list_all()

    return [ApiKeyInfo(**row) for row in rows]


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(
    key_id: int,
    user: CurrentUser,
    _t: Translator,
) -> None:
    """删除（吊销）指定 API Key，并立即清除内存缓存。"""
    _require_jwt_auth(user, _t)
    async with async_session_factory() as session, session.begin():
        repo = ApiKeyRepository(session)
        row = await repo.get_by_id(key_id)
        if row is None:
            raise HTTPException(status_code=404, detail=_t("api_key_not_found", key_id=key_id))
        key_hash = row["key_hash"]
        # 先失效缓存再删库：即使事务提交后崩溃，缓存也已清除，
        # 不会出现 DB 已删但缓存仍有效的宽限窗口。
        invalidate_api_key_cache(key_hash)
        deleted = await repo.delete(key_id)
        if employee_auth_enabled() and deleted:
            await record_audit_event(
                session, actor_user_id=user.id, actor_label=user.sub, project_id=row.get("project_id"),
                action="api_key.revoke", outcome="success", object_type="api_key", object_id=str(key_id),
            )

    if not deleted:
        raise HTTPException(status_code=404, detail=_t("api_key_not_found", key_id=key_id))
