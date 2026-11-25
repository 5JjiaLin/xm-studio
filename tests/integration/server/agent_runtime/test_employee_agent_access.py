"""Live employee revocation across SDK hooks, embedded MCP and local HTTP.

The database, project files, options assembly and tool handlers are real. No
provider or SDK subprocess is needed to exercise the long-lived authority.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from mcp import types

from lib.identity.accounts import (
    change_employee_password,
    change_employee_role,
    create_employee,
    set_employee_active,
)
from lib.identity.projects import remove_project_member, set_project_member
from lib.project.project_manager import ProjectManager
from server import auth, project_access
from server.agent_runtime.agent_access_policy import AgentAccessPolicy
from server.agent_runtime.arcreel_mcp import build_arcreel_mcp_server
from server.agent_runtime.options_assembler import OptionsAssembler
from server.project_access import require_project_request_access
from server.routers import auth as auth_router
from tests.fakes import bind_session_factory


async def call_tool(options, name, arguments):
    server = options.mcp_servers["arcreel"]["instance"]
    request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
    result = (await server.request_handlers[types.CallToolRequest](request)).root
    assert isinstance(result, types.CallToolResult)
    return result


def result_payload(result):
    return json.loads(result.content[-1].text)


async def hook_denied(options, *, tool_name="Read", tool_input=None):
    decisions = [
        await callback({"tool_name": tool_name, "tool_input": tool_input or {}}, "test-call", None)
        for callback in options.hooks["PreToolUse"][0].hooks
    ]
    return any(
        decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        for decision in decisions
    )


@pytest.fixture
async def employee_agent(tmp_path, concurrent_session_factory, monkeypatch):
    factory = concurrent_session_factory
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    bind_session_factory(monkeypatch, factory)
    manager = ProjectManager(tmp_path / "data")
    for name in ("alpha", "beta"):
        manager.create_project(name)
        manager.create_project_metadata(name, name.title(), "", "drama")
    monkeypatch.setattr(project_access, "get_project_manager", lambda: manager)
    alpha_id = manager.load_project("alpha")["project_id"]
    async with factory() as session, session.begin():
        admin = await create_employee(session, username="agent-admin", password="long-test-password", role="admin")
        editor = await create_employee(session, username="agent-editor", password="long-test-password", role="editor")
        for name in ("alpha", "beta"):
            await set_project_member(
                session, actor_id=admin.id, project_id=manager.load_project(name)["project_id"],
                user_id=editor.id, permission="editor",
            )

    async def provider_env():
        return {}

    policy = AgentAccessPolicy(
        project_root=tmp_path / "repo", data_root=manager.data_root,
        agent_profile_root=tmp_path / "profile", sandbox_enabled=False,
    )
    assembler = OptionsAssembler(
        data_root=manager.data_root, allowed_tools=["Read", "Write", "Edit", "Bash", "Task"],
        setting_sources=["project"], access_policy_provider=lambda: policy,
        max_turns_provider=lambda: None, resolve_project_cwd=manager.get_project_path,
        provider_env_loader=provider_env, session_factory_provider=lambda: factory,
    )
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")

    @app.get("/api/v1/projects/{project_name}/agent-probe", dependencies=[Depends(require_project_request_access)])
    @app.post("/api/v1/projects/{project_name}/agent-probe", dependencies=[Depends(require_project_request_access)])
    async def probe(project_name: str, user: auth.CurrentUser):
        return {"project": project_name, "user_id": user.id, "sub": user.sub}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        yield SimpleNamespace(
            manager=manager, factory=factory, admin=admin, editor=editor, alpha_id=alpha_id,
            assembler=assembler, client=client,
        )


@pytest.mark.parametrize("revocation", ["disable", "password", "role", "membership", "permission"])
async def test_connected_agent_rechecks_revocation_before_every_operation(employee_agent, revocation):
    ctx = employee_agent
    options = await ctx.assembler.build("alpha", user_id=ctx.editor.id)
    headers = {"Authorization": "Bearer " + options.env["ARCREEL_API_TOKEN"]}
    note = {"file_path": str(ctx.manager.get_project_path("alpha") / "notes.md")}
    assert not await hook_denied(options, tool_input=note)
    assert (await ctx.client.post("/api/v1/projects/alpha/agent-probe", headers=headers)).status_code == 200
    uploaded = await call_tool(options, "upload_source", {"filename": "before.txt", "content": "accepted"})
    assert not uploaded.isError, result_payload(uploaded)
    assert not (await call_tool(options, "list_projects", {})).isError

    async with ctx.factory() as session, session.begin():
        if revocation == "disable":
            await set_employee_active(session, user_id=ctx.editor.id, is_active=False)
        elif revocation == "password":
            await change_employee_password(session, user_id=ctx.editor.id, password="replacement-password")
        elif revocation == "role":
            await change_employee_role(session, user_id=ctx.editor.id, role="viewer")
        elif revocation == "membership":
            await remove_project_member(
                session, actor_id=ctx.admin.id, project_id=ctx.alpha_id, user_id=ctx.editor.id,
            )
        else:
            await set_project_member(
                session, actor_id=ctx.admin.id, project_id=ctx.alpha_id, user_id=ctx.editor.id, permission="viewer",
            )

    # Reuse the exact connection, hooks and token built before revocation.
    assert await hook_denied(options, tool_input=note)
    for name, arguments in (
        ("list_projects", {}),
        ("upload_source", {"filename": "after.txt", "content": "must never be written"}),
    ):
        rejected = await call_tool(options, name, arguments)
        assert rejected.isError
        assert result_payload(rejected)["problem"]["code"] == "permission_denied"
    assert not (ctx.manager.get_project_path("alpha") / "source" / "after.txt").exists()
    response = await ctx.client.post("/api/v1/projects/alpha/agent-probe", headers=headers)
    assert response.status_code == (403 if revocation in {"membership", "permission"} else 401)


@pytest.mark.parametrize("owner", ["editor", "admin"])
async def test_embedded_identity_cannot_expand_project_or_become_browser_login(employee_agent, owner):
    ctx = employee_agent
    user = getattr(ctx, owner)
    options = await ctx.assembler.build("alpha", user_id=user.id)
    headers = {"Authorization": "Bearer " + options.env["ARCREEL_API_TOKEN"]}
    response = await ctx.client.get("/api/v1/projects/alpha/agent-probe", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"project": "alpha", "user_id": user.id, "sub": auth.EMBEDDED_AGENT_SUB}
    assert (await ctx.client.get("/api/v1/projects/beta/agent-probe", headers=headers)).status_code == 403
    response = await ctx.client.post("/api/v1/auth/media-session", headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    listed = await call_tool(options, "list_projects", {})
    assert [project["name"] for project in result_payload(listed)["projects"]] == ["alpha"]
    rejected = await call_tool(options, "create_project", {"name": "unauthorized"})
    assert result_payload(rejected)["problem"]["code"] == "permission_denied"
    assert not (ctx.manager.projects_dir / "unauthorized").exists()
    for tool in ("Bash", "BashOutput", "KillBash", "Shell", "PowerShell"):
        assert await hook_denied(options, tool_name=tool, tool_input={"command": "whoami"})


@pytest.mark.parametrize("replacement", ["different", "duplicate", "missing"])
async def test_running_agent_refuses_replaced_or_ambiguous_project_identity(employee_agent, replacement):
    ctx = employee_agent
    options = await ctx.assembler.build("alpha", user_id=ctx.editor.id)
    metadata_path = ctx.manager.get_project_path("alpha") / "project.json"
    project = json.loads(metadata_path.read_text(encoding="utf-8"))
    if replacement == "missing":
        metadata_path.unlink()
    else:
        project["project_id"] = (
            str(uuid4()) if replacement == "different" else ctx.manager.load_project("beta")["project_id"]
        )
        metadata_path.write_text(json.dumps(project), encoding="utf-8")
    assert await hook_denied(options, tool_input={"file_path": str(metadata_path)})
    rejected = await call_tool(options, "upload_source", {"filename": "after.txt", "content": "rejected"})
    assert result_payload(rejected)["problem"]["code"] == "permission_denied"
    assert not (metadata_path.parent / "source" / "after.txt").exists()


async def test_employee_mode_rejects_unbound_embedded_server(employee_agent):
    ctx = employee_agent
    server = build_arcreel_mcp_server(project_name="alpha", data_root=ctx.manager.data_root)["instance"]
    request = types.CallToolRequest(params=types.CallToolRequestParams(
        name="upload_source", arguments={"filename": "after.txt", "content": "rejected"},
    ))
    result = (await server.request_handlers[types.CallToolRequest](request)).root
    assert result.isError
    assert result_payload(result)["problem"]["code"] == "permission_denied"
    assert not (ctx.manager.get_project_path("alpha") / "source" / "after.txt").exists()
