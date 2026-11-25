"""Revocable employee/project authority for one embedded Agent connection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.db.models import User
from lib.identity.projects import ProjectAccessDenied, ProjectAction, require_project_access
from lib.project.identity import read_unique_project_identity
from server.auth import EMBEDDED_AGENT_SUB, CurrentUserInfo


@dataclass(frozen=True)
class EmployeeAgentAccess:
    user_id: str
    session_version: int
    project_id: str
    project_cwd: Path
    session_factory: Any

    @classmethod
    async def bind(cls, user_id: str, project_cwd: Path, session_factory: Any) -> EmployeeAgentAccess:
        project_id = await asyncio.to_thread(read_unique_project_identity, project_cwd)
        async with session_factory() as session:
            await require_project_access(session, user_id=user_id, project_id=project_id, action="write")
            user = await session.get(User, user_id)
            assert user is not None  # require_project_access resolved the same row.
            return cls(user_id, user.session_version, project_id, project_cwd, session_factory)

    async def authorize(self, action: ProjectAction = "write") -> CurrentUserInfo:
        # Captured project IDs must never follow a renamed/replaced directory.
        try:
            current_id = await asyncio.to_thread(read_unique_project_identity, self.project_cwd)
        except (ValueError, OSError) as exc:
            raise ProjectAccessDenied("助手项目身份不可用") from exc
        if current_id != self.project_id or action == "manage":
            raise ProjectAccessDenied("助手不能扩大项目授权或执行管理操作")
        async with self.session_factory() as session:
            user = await session.get(User, self.user_id)
            if user is None or user.session_version != self.session_version:
                raise ProjectAccessDenied("助手员工会话已失效")
            # Running assistants require editor access, even for their next read.
            # A membership downgrade must stop the previously accepted work.
            await require_project_access(session, user_id=self.user_id, project_id=self.project_id, action="write")
            await require_project_access(session, user_id=self.user_id, project_id=self.project_id, action=action)
            return CurrentUserInfo(
                id=user.id, sub=EMBEDDED_AGENT_SUB, role=user.role, session_version=self.session_version,
                project_id=self.project_id, project_permission="write",
            )

    async def tool_hook(self, input_data: dict, _tool_use_id: str | None, _context: Any) -> dict:
        try:
            await self.authorize()
            # Shell subprocesses cannot enforce per-employee filesystem access
            # on Windows. All project business operations have MCP tools.
            if input_data.get("tool_name") in {"Bash", "BashOutput", "KillBash", "Shell", "PowerShell"}:
                raise ProjectAccessDenied("员工助手请使用项目 MCP 工具；服务器命令执行仅供本机运维")
        except ProjectAccessDenied as exc:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": str(exc),
                },
            }
        return {"continue_": True}
