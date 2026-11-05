"""Feedback request context and idempotency for production writes."""

from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

from starlette.responses import JSONResponse, Response

from lib.infra.async_thread import run_sync_transaction
from lib.infra.data_root_layout import DataRootLayout
from lib.production_feedback.context import operation
from lib.production_feedback.store import register_root


async def feedback_middleware(request, call_next):
    path = request.url.path
    if request.method not in {"POST", "PATCH", "PUT", "DELETE"} or not path.startswith("/api/v1/projects/"):
        return await call_next(request)
    operation_id = request.headers.get("X-Production-Operation-Id") or str(uuid4())
    if len(operation_id) > 128:
        return JSONResponse({"detail": "Invalid operation ID"}, status_code=400)
    source = "human" if request.headers.get("X-Production-Source") == "human" else "system"
    store = await run_sync_transaction(register_root, DataRootLayout.current().root)
    identity = await asyncio.to_thread(store.settings)
    if "application/json" not in request.headers.get("content-type", "") or any(
        part in path for part in ("preview", "workflow-plan", "estimate")
    ):
        with operation(operation_id=operation_id, source=source, action=path, settings_snapshot=identity):
            return await call_next(request)
    body = await request.body()
    fingerprint = hashlib.sha256(
        request.method.encode()
        + path.encode()
        + request.url.query.encode()
        + request.headers.get("authorization", "").encode()
        + body
    ).hexdigest()

    def reserve():
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM production_feedback_requests WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row:
                return dict(row)
            db.execute(
                "INSERT INTO production_feedback_requests(operation_id,fingerprint,state) VALUES(?,?,'pending')",
                (operation_id, fingerprint),
            )
            return None

    previous = await run_sync_transaction(reserve)
    if previous:
        if previous["fingerprint"] != fingerprint:
            return JSONResponse({"detail": "Operation ID has a different payload"}, status_code=409)
        if previous["state"] == "completed":
            return Response(previous["response"], status_code=previous["status"], media_type="application/json")
        return JSONResponse(
            {"detail": "Previous operation outcome is unknown; refresh the saved content before retrying"},
            status_code=409,
        )
    source = "human" if request.headers.get("X-Production-Source") == "human" else "system"
    try:
        with operation(operation_id=operation_id, source=source, action=path, settings_snapshot=identity):
            response = await call_next(request)
            # Only production JSON responses are memoized; streamed downloads bypass this write middleware.
            result = b"".join([chunk async for chunk in response.body_iterator])

        def complete():
            with store.connection() as db:
                if 200 <= response.status_code < 300:
                    db.execute(
                        "UPDATE production_feedback_requests SET state='completed',status=?,response=? WHERE operation_id=?",
                        (response.status_code, result.decode(), operation_id),
                    )
                else:
                    db.execute("DELETE FROM production_feedback_requests WHERE operation_id=?", (operation_id,))

        await run_sync_transaction(complete)
        return Response(
            result,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
    except BaseException:
        # Keep pending: a lost response must never cause a duplicate paid generation.
        raise
