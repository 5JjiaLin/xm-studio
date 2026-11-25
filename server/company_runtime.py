"""Company deployment admission, drain reporting and local readiness checks."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy import text
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lib.db import async_session_factory
from lib.infra.data_root_layout import DataRootLayout
from lib.infra.server_runtime import runtime
from server.auth import EMBEDDED_AGENT_SUB, verify_token

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DRAIN_ACTION = re.compile(r"^/api/v1/projects/[^/]+/assistant/sessions/[^/]+/(?:interrupt|questions/[^/]+/answer)$")


def _local_agent_continuation(scope: Scope) -> bool:
    """Only an authenticated embedded Agent on the direct loopback connection."""
    headers = dict(scope.get("headers", []))
    if any(key in headers for key in (b"forwarded", b"x-forwarded-for", b"x-forwarded-proto")):
        return False
    try:
        local = ipaddress.ip_address(scope["client"][0]).is_loopback
    except (KeyError, ValueError, TypeError):
        return False
    token = headers.get(b"authorization", b"").decode("latin-1")
    if not local or not token.lower().startswith("bearer "):
        return False
    payload = verify_token(token[7:])
    return bool(payload and payload.get("sub") == EMBEDDED_AGENT_SUB)


def activity_snapshot(app) -> dict:
    worker = getattr(app.state, "generation_worker", None)
    manager = app.state.company_session_manager if hasattr(app.state, "company_session_manager") else None
    sessions = getattr(manager, "sessions", {})
    agents = sum(s.status == "running" or bool(getattr(s, "pending_questions", {})) for s in sessions.values())
    media = worker.active_count if worker is not None else 0
    activities = dict(runtime.activities)
    return {
        "maintenance": runtime.maintenance,
        "inspection": runtime.inspection,
        "storage_critical": runtime.storage_critical,
        "activities": activities,
        "agents": agents,
        "generation_tasks": media,
        "active": sum(activities.values()) + agents + media,
    }


def disk_status(root: Path) -> dict:
    usage = shutil.disk_usage(root)
    ratio = usage.free / usage.total
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "warning": ratio < 0.20 or usage.free < 100 * 1024**3,
        "critical": ratio < 0.10 or usage.free < 50 * 1024**3,
    }


async def readiness(app) -> dict:
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
    root = DataRootLayout.current().root

    def probe() -> dict:
        with tempfile.TemporaryFile(dir=root) as stream:
            stream.write(b"ready")
            stream.flush()
        return disk_status(root)

    disk = await asyncio.to_thread(probe)
    worker = getattr(app.state, "generation_worker", None)
    scheduler_ok = runtime.inspection or (worker is not None and worker.healthy)
    if not runtime.initialized or not scheduler_ok:
        raise RuntimeError("Application startup or scheduler is not ready")
    return {"ready": True, **activity_snapshot(app), "disk": disk, "checked_at": time.time()}


async def local_readiness(request: Request) -> dict:
    # A proxy is deliberately excluded, including one reached from localhost.
    forwarded = any(key in request.headers for key in ("forwarded", "x-forwarded-for", "x-forwarded-proto"))
    try:
        local = bool(request.client) and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if not local or forwarded:
        raise HTTPException(404)
    try:
        return await readiness(request.app)
    except Exception as exc:
        raise HTTPException(503, "Application is not ready") from exc


class MaintenanceMiddleware:
    """Count admitted mutations until ASGI completion, including background work."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        auth_session = scope["method"] == "POST" and path in {
            "/api/v1/auth/token",
            "/api/v1/auth/media-session",
            "/api/v1/auth/logout",
        }
        mutating = scope["method"] not in READ_METHODS and not auth_session
        # MCP uses POST for both reads and writes; pause the entire transport.
        mutating = mutating or path.startswith("/mcp")
        inspection_denied = runtime.inspection and path.startswith(("/api/", "/mcp"))
        # Let already admitted Agents save their output, and let people answer
        # their outstanding question or interrupt them. Authentication and the
        # endpoint's existing authorization checks still run normally.
        continuation = False
        if mutating and (runtime.maintenance or runtime.storage_critical) and not runtime.inspection:
            continuation = bool(_DRAIN_ACTION.fullmatch(path)) or _local_agent_continuation(scope)
        if inspection_denied or (mutating and (runtime.maintenance or runtime.inspection) and not continuation):
            response = JSONResponse(
                {"detail": "服务器维护中，已开始的任务正在保存结果，请稍后重试。", "code": "server_maintenance"},
                status_code=503,
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        # Keep reads/downloads, login, deletion and provider configuration
        # available; admitted work retains its result-save path.
        capacity_exempt = (
            scope["method"] == "DELETE"
            or (scope["method"] in {"PATCH", "PUT"} and path.startswith("/api/v1/providers/"))
            or path.endswith("/export/token")
        )
        if mutating and runtime.storage_critical and not continuation and not capacity_exempt:
            response = JSONResponse(
                {
                    "detail": "服务器磁盘空间不足，已暂停新增写入和任务，请管理员清理或扩容。",
                    "code": "storage_critical",
                },
                status_code=507,
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        if mutating:
            with runtime.activity("http_mutations"):
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)
