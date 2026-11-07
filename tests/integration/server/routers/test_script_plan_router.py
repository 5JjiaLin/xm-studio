"""Reason-guided replanning preserves saved instructions and remains a one-run request."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import script_plan
from server.services.project.episode_instructions import EpisodeInstructionKind, save_episode_instructions
from server.tool_runtime import ToolOutcome
from tests.auth_deps import AUTH_DEPENDENCIES


@pytest.fixture
def planning_http(tmp_path, monkeypatch):
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", "drama")
    pm.add_episode("demo", 1, "第一集", "scripts/episode_1.json")
    save_episode_instructions(pm, "demo", 1, EpisodeInstructionKind.SCRIPT_PLAN, "保留对白原文")
    command = AsyncMock(return_value=ToolOutcome(value={"batch_id": "replan-test", "members": []}))
    monkeypatch.setattr(script_plan, "get_project_manager", lambda: pm)
    monkeypatch.setattr(script_plan, "generate_script_plan", command)
    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="testuser", role="admin")
    app.include_router(script_plan.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, pm=pm, command=command)


def test_replan_preserves_saved_instructions_without_persisting_its_reason(planning_http):
    before = planning_http.pm.load_project("demo")
    reason = "将手链脚链合为人物束缚形态"

    response = planning_http.client.post(
        "/api/v1/projects/demo/episodes/1/script-plan", json={"replan_reason": reason}
    )

    assert response.status_code == 200, response.text
    request = planning_http.command.await_args.args[0].value
    assert request.instructions == "保留对白原文"
    assert request.replan_reason == reason
    assert planning_http.pm.load_project("demo") == before


@pytest.mark.parametrize("instructions", [None, "按动作衔接"])
def test_replan_can_explicitly_clear_or_replace_saved_instructions(planning_http, instructions):
    response = planning_http.client.post(
        "/api/v1/projects/demo/episodes/1/script-plan",
        json={"replan_reason": "调整人物造型", "instructions": instructions},
    )

    assert response.status_code == 200, response.text
    assert planning_http.command.await_args.args[0].value.instructions == instructions
    episode = planning_http.pm.load_project("demo")["episodes"][0]
    assert episode.get("script_plan_instructions") == instructions
    assert "replan_reason" not in episode


@pytest.mark.parametrize("path", ["missing/episodes/1", "demo/episodes/9"])
def test_replan_for_a_missing_project_or_episode_does_not_submit(planning_http, path):
    response = planning_http.client.post(
        f"/api/v1/projects/{path}/script-plan", json={"replan_reason": "修正束缚状态"}
    )

    assert response.status_code == 404
    planning_http.command.assert_not_awaited()


def test_oversized_replan_reason_is_rejected_without_submission(planning_http):
    response = planning_http.client.post(
        "/api/v1/projects/demo/episodes/1/script-plan", json={"replan_reason": "错" * 4001}
    )

    assert response.status_code == 422
    planning_http.command.assert_not_awaited()
