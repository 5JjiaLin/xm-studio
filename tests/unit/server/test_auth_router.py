"""
登录 API 路由测试

测试 server.routers.auth 中的登录和 token 验证路由。
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import lib.db
import lib.identity.accounts as accounts
import server.auth as auth_module
from server.routers import auth as auth_router
from tests.auth_deps import AUTH_DEPENDENCIES


@pytest.fixture
def auth_router_client():
    """创建测试客户端，设置固定的认证环境变量"""
    auth_module._cached_token_secret = None
    auth_module._cached_password_hash = None
    with patch.dict(
        os.environ,
        {
            "AUTH_USERNAME": "testuser",
            "AUTH_PASSWORD": "testpass",
            "AUTH_TOKEN_SECRET": "test-router-secret-key-at-least-32-bytes-long",
        },
    ):
        app = FastAPI()
        app.include_router(auth_router.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
        app.include_router(auth_router.public_router, prefix="/api/v1")
        with TestClient(app) as c:
            yield c


class TestLoginRoute:
    def test_login_success(self, auth_router_client):
        """正确凭据返回 200 + access_token"""
        resp = auth_router_client.post(
            "/api/v1/auth/token",
            data={"username": "testuser", "password": "testpass"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert len(data["access_token"]) > 0

    def test_login_wrong_password(self, auth_router_client):
        """错误密码返回 401"""
        resp = auth_router_client.post(
            "/api/v1/auth/token",
            data={"username": "testuser", "password": "wrongpass"},
        )
        assert resp.status_code == 401

    def test_login_wrong_username(self, auth_router_client):
        """错误用户名返回 401"""
        resp = auth_router_client.post(
            "/api/v1/auth/token",
            data={"username": "wronguser", "password": "testpass"},
        )
        assert resp.status_code == 401

    def test_employee_login_issues_user_bound_token(self, auth_router_client, monkeypatch):
        class SessionContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def begin(self):
                return self

        async def authenticate(_session, *, username, password):
            assert (username, password) == ("alice", "long-password")
            return SimpleNamespace(id="employee-1", username="alice", session_version=3)

        monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_ENABLED", "false")
        monkeypatch.setattr(lib.db, "async_session_factory", SessionContext)
        monkeypatch.setattr(accounts, "authenticate_employee", authenticate)

        response = auth_router_client.post(
            "/api/v1/auth/token",
            data={"username": "alice", "password": "long-password"},
        )

        assert response.status_code == 200
        payload = auth_module.verify_token(response.json()["access_token"])
        assert payload is not None
        assert (payload["sub"], payload["uid"], payload["sv"]) == ("alice", "employee-1", 3)


class TestVerifyRoute:
    def test_verify_valid_token(self, auth_router_client):
        """有效 token 验证通过"""
        login_resp = auth_router_client.post(
            "/api/v1/auth/token",
            data={"username": "testuser", "password": "testpass"},
        )
        token = login_resp.json()["access_token"]

        resp = auth_router_client.get(
            "/api/v1/auth/verify",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "testuser"

    def test_verify_no_token(self, auth_router_client):
        """缺少 token 返回 401"""
        resp = auth_router_client.get("/api/v1/auth/verify")
        assert resp.status_code == 401

    def test_verify_invalid_token(self, auth_router_client):
        response = auth_router_client.get("/api/v1/auth/verify", headers={"Authorization": "Bearer invalid-token"})
        assert response.status_code == 401


class TestEmployeeManagementAuthorization:
    @pytest.mark.parametrize("auth_enabled", ["true", "false"])
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/api/v1/users", None),
            ("POST", "/api/v1/users", {"username": "newuser", "password": "long-test-password"}),
            ("PATCH", "/api/v1/users/employee-id", {"role": "viewer"}),
        ],
    )
    def test_employee_management_is_disabled_with_legacy_or_anonymous_auth(
        self, auth_router_client, monkeypatch, auth_enabled, method, path, body
    ):
        monkeypatch.setenv("AUTH_ENABLED", auth_enabled)
        monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
        token = auth_module.create_token("testuser")

        response = auth_router_client.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "员工账号管理尚未启用"
