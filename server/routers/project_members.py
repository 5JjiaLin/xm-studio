"""Administrator-only project membership management."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import lib.db
from lib.identity.projects import (
    ProjectAccessDenied,
    list_project_members,
    remove_project_member,
    set_project_member,
)
from lib.project.identity import read_unique_project_identity
from lib.project.project_manager import get_project_manager
from server.auth import CurrentUser, employee_auth_enabled

router = APIRouter()


class MemberRequest(BaseModel):
    permission: Literal["viewer", "editor"]


class MemberInfo(MemberRequest):
    id: str
    username: str
    role: str
    is_active: bool


async def _project_id(project_name: str, user: CurrentUser) -> str:
    if not employee_auth_enabled() or user.role != "admin":
        raise HTTPException(status_code=403, detail="仅员工管理员可管理项目成员")

    try:
        project_dir = await asyncio.to_thread(get_project_manager().get_project_path, project_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="项目名称无效") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    try:
        return await asyncio.to_thread(read_unique_project_identity, project_dir)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail="项目身份异常，请先修复项目数据") from exc


async def _session() -> AsyncGenerator[AsyncSession]:
    try:
        async with lib.db.async_session_factory() as session, session.begin():
            yield session
    except ProjectAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="项目成员发生并发变更，请重试") from exc


ProjectID = Annotated[str, Depends(_project_id)]
MemberSession = Annotated[AsyncSession, Depends(_session)]


@router.get("/projects/{project_name}/members", response_model=list[MemberInfo])
async def get_members(project_id: ProjectID, session: MemberSession, user: CurrentUser) -> list[dict]:
    return await list_project_members(session, actor_id=user.id, project_id=project_id)


@router.put("/projects/{project_name}/members/{user_id}", response_model=list[MemberInfo])
async def put_member(
    user_id: str, body: MemberRequest, project_id: ProjectID, session: MemberSession, user: CurrentUser
) -> list[dict]:
    await set_project_member(
        session, actor_id=user.id, project_id=project_id, user_id=user_id, permission=body.permission
    )
    return await list_project_members(session, actor_id=user.id, project_id=project_id)


@router.delete("/projects/{project_name}/members/{user_id}", status_code=204)
async def delete_member(user_id: str, project_id: ProjectID, session: MemberSession, user: CurrentUser) -> Response:
    await remove_project_member(session, actor_id=user.id, project_id=project_id, user_id=user_id)
    return Response(status_code=204)
