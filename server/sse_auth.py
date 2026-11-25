"""Employee credentials and project access checks for open SSE subscriptions."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

from server.auth import CurrentUserInfo, employee_auth_enabled, get_current_user, oauth2_scheme_optional
from server.project_access import authorize_project

logger = logging.getLogger(__name__)

EMPLOYEE_STREAM_POLL_SECONDS = 1.0


def project_stream_authorizer(
    request: Request, project_name: str, original_user: CurrentUserInfo
) -> Callable[[], Awaitable[bool]] | None:
    """Keep the original credential bound to the original employee and session version.

    Check on idle wakeups as well as before every outgoing event. Reusing the
    HTTP authentication path also checks JWT expiry instead of treating a user
    snapshot captured when the connection opened as a permanent credential.
    """
    if not employee_auth_enabled():
        return None

    async def check() -> bool:
        try:
            token = await oauth2_scheme_optional(request)
            refreshed = await get_current_user(token=token)
            if refreshed.id != original_user.id or refreshed.session_version != original_user.session_version:
                raise HTTPException(status_code=401, detail="员工会话已变化")
            await authorize_project(refreshed, project_name, "read")
        except HTTPException:
            # The HTTP response has already begun. End the subscription without
            # sending an HTTP error or credential/permission details in the stream.
            logger.info("员工凭据或项目读取权限失效，关闭 SSE: %s", project_name)
            return False
        return True

    return check
