"""Read-only media credentials for native browser requests in employee mode."""

from __future__ import annotations

import time
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, Response

from server.auth import (
    CurrentUserInfo,
    employee_auth_enabled,
    get_current_user,
    get_token_secret,
    oauth2_scheme_optional,
    resolve_employee_user,
    verify_token,
)
from server.project_access import authorize_project

MEDIA_COOKIE_NAME = "arcreel_media"
# Only these two read-only media namespaces consume the cookie. The two paths
# keep the credential off ordinary API requests; those still require Bearer.
MEDIA_COOKIE_PATHS = ("/api/v1/files/", "/api/v1/global-assets/")
PRIVATE_MEDIA_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization"}


def set_media_cookie(response: Response, session_token: str) -> None:
    """Derive a purpose-bound cookie without extending the original login expiry."""
    payload = verify_token(session_token)
    if (
        payload is None or not isinstance(payload.get("uid"), str) or type(payload.get("sv")) is not int
        or payload.get("via") in {"embedded", "apikey"}
    ):
        raise ValueError("employee session required for media credential")
    media_token = jwt.encode(
        {"purpose": "media", "uid": payload["uid"], "sv": payload["sv"], "exp": payload["exp"]},
        get_token_secret(),
        algorithm="HS256",
    )
    for path in MEDIA_COOKIE_PATHS:
        response.set_cookie(
            MEDIA_COOKIE_NAME,
            media_token,
            path=path,
            max_age=max(0, int(payload["exp"] - time.time())),
            secure=True,
            httponly=True,
            samesite="strict",
        )
    response.headers["Cache-Control"] = "no-store"


def clear_media_cookie(response: Response) -> None:
    for path in MEDIA_COOKIE_PATHS:
        response.delete_cookie(MEDIA_COOKIE_NAME, path=path, secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"


def require_same_origin_browser(request: Request) -> None:
    """Reject browser login/logout CSRF; non-browser clients may omit Origin."""
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("sec-fetch-site") == "cross-site" or (origin is not None and origin != expected):
        raise HTTPException(status_code=403, detail="请从本站页面登录或退出", headers=PRIVATE_MEDIA_HEADERS)


async def get_current_media_user(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme_optional)] = None,
) -> CurrentUserInfo | None:
    """Accept a Bearer for API clients or a media cookie for native elements."""
    if not employee_auth_enabled():
        return None
    try:
        if "authorization" in request.headers:
            user = await get_current_user(token)
            if user.project_scoped and "project_name" not in request.path_params:
                raise HTTPException(status_code=403, detail="项目 API Key 不能读取全局资产库")
            return user
        cookie = request.cookies.get(MEDIA_COOKIE_NAME)
        if not cookie:
            raise HTTPException(status_code=401, detail="请先登录后访问媒体")
        try:
            payload = jwt.decode(
                cookie, get_token_secret(), algorithms=["HS256"], options={"require": ["exp", "uid", "sv"]}
            )
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail="媒体会话已失效，请重新登录") from exc
        if payload.get("purpose") != "media":
            raise HTTPException(status_code=401, detail="媒体凭据用途无效")
        return await resolve_employee_user(payload)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **PRIVATE_MEDIA_HEADERS}
        raise


async def require_project_media_access(
    project_name: str,
    user: Annotated[CurrentUserInfo | None, Depends(get_current_media_user)],
) -> None:
    if user is not None:
        try:
            await authorize_project(user, project_name, "read")
        except HTTPException as exc:
            exc.headers = {**(exc.headers or {}), **PRIVATE_MEDIA_HEADERS}
            raise
