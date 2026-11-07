"""Sequential batches reconcile accepted tasks and never restart unknown submissions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib.episode.episode_management import create_episode
from lib.episode.script_batch_state import require_batch_idle
from lib.episode.whole_script import divide_script, import_script, project_basis
from lib.project.project_manager import ProjectManager
from lib.script import script_review
from server.services.project.whole_script_batch import WholeScriptBatchService
from server.tool_runtime import ToolOutcome


@pytest.fixture
def setup_batch(tmp_path, monkeypatch):
    pm = ProjectManager(tmp_path)
    pm.create_project("demo", content_mode="drama")
    pm.create_project_metadata("demo", content_mode="drama")
    p = divide_script("第一集 开始\n甲说你好。\n第二集 继续\n甲换上盔甲。")
    imported = import_script(
        pm,
        "demo",
        p["text"],
        [{k: e[k] for k in ("start", "title", "number")} for e in p["episodes"]],
        p["fingerprint"],
        project_basis(pm.load_project("demo")),
    )
    queue = SimpleNamespace(
        list_tasks=AsyncMock(return_value={"items": []}),
        get_generation_batch=AsyncMock(return_value=SimpleNamespace(done=False)),
        get_task=AsyncMock(),
    )
    services = SimpleNamespace(queue=queue)
    service = WholeScriptBatchService(pm, services)
    calls = []

    async def submit(request, scope, caller, services, *, batch_context):
        calls.append((request.value.episode_id, batch_context))
        return ToolOutcome(value=SimpleNamespace(batch_id=f"b{len(calls)}"))

    monkeypatch.setattr("server.services.project.whole_script_batch.generate_script_plan", submit)
    return service, imported["import_id"], calls


def draft(service, episode, *, name="甲"):
    path = script_review.script_plan_path(service._dir("demo"), service.projects.load_project("demo"), episode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"new_assets": [{"type": "character", "name": name, "description": "黑发青年"}], "scenes": []},
            ensure_ascii=False,
        )
    )


def done(service):
    service.services.queue.get_generation_batch.return_value = SimpleNamespace(
        done=True, counts=SimpleNamespace(total=1, succeeded=1, failed=0, cancelled=0, blocked=0)
    )


async def test_serial_success_shares_hints_and_stops_for_review(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    await service.advance("demo")
    assert len(calls) == 1
    draft(service, 1)
    done(service)
    await service.advance("demo")
    await service.advance("demo")
    assert [ep for ep, _ in calls] == [1, 2]
    assert json.loads(calls[1][1][2])[0]["name"] == "甲"
    assert service.projects.load_project("demo").get("characters", {}) == {}
    draft(service, 2)
    await service.advance("demo")
    await service.advance("demo")
    assert (await service.status("demo"))["status"] == "completed"
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    assert len(calls) == 2


async def test_stop_finishes_current_then_continue_without_replaying_success(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    await service.control("demo", "stop", "u")
    with pytest.raises(ValueError, match="逐集拆解"):
        create_episode(service.projects, "demo")
    await service.advance("demo")
    assert len(calls) == 1
    draft(service, 1)
    done(service)
    await service.advance("demo")
    await service.advance("demo")
    assert (await service.status("demo"))["status"] == "stopped"
    await service.control("demo", "continue", "u")
    await service.advance("demo")
    assert [ep for ep, _ in calls] == [1, 2]


async def test_failed_episode_retries_only_after_explicit_continue_with_new_attempt(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    service.services.queue.get_generation_batch.return_value = SimpleNamespace(
        done=True, counts=SimpleNamespace(failed=1, cancelled=0, blocked=0), members=[]
    )
    await service.advance("demo")
    await service.advance("demo")
    assert len(calls) == 1
    assert (await service.status("demo"))["status"] == "failed"
    await service.control("demo", "continue", "u")
    await service.advance("demo")
    assert [ep for ep, _ in calls] == [1, 1]
    assert calls[0][1][1] != calls[1][1][1]


async def test_restart_claims_exact_accepted_attempt_without_resubmission(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    state = service._read("demo")
    state["items"][0].update(status="submitting", attempt="attempt")
    await service._write("demo", state)
    service.services.queue.list_tasks.return_value = {
        "items": [
            {
                "task_id": "old",
                "user_id": "u",
                "payload": {"production_batch_id": state["id"], "production_attempt": "older", "episode": 1},
            },
            {
                "task_id": "accepted",
                "user_id": "u",
                "payload": {"production_batch_id": state["id"], "production_attempt": "attempt", "episode": 1},
            },
        ]
    }
    service.services.queue.get_task.return_value = {"status": "running"}
    restarted = WholeScriptBatchService(service.projects, service.services)
    await restarted.advance("demo")
    assert restarted._read("demo")["items"][0]["task_id"] == "accepted"
    assert not calls
    draft(service, 1)
    service.services.queue.get_task.return_value = {"status": "succeeded"}
    await restarted.advance("demo")
    assert restarted._read("demo")["items"][0]["status"] == "completed"


async def test_unknown_submission_remains_blocked_on_refresh_and_stop(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    state = service._read("demo")
    state["items"][0].update(status="submitting", attempt="lost")
    await service._write("demo", state)
    for _ in range(2):
        await service.advance("demo")
    assert (await service.status("demo"))["status"] == "unknown"
    await service.control("demo", "stop", "u")
    with pytest.raises(ValueError, match="结果未知"):
        await service.control("demo", "continue", "u")
    assert not calls


async def test_context_change_pauses_before_next_episode_and_is_explicitly_acknowledged(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    draft(service, 1)
    done(service)
    await service.advance("demo")
    draft(service, 1, name="甲修订")
    await service.advance("demo")
    assert len(calls) == 1
    assert (await service.status("demo"))["status"] == "failed"
    await service.control("demo", "continue", "u")
    await service.advance("demo")
    assert "甲修订" in calls[-1][1][2]


@pytest.mark.parametrize(
    "patch",
    [
        {"style": "新风格"},
        {"episode_target_duration": 120},
        {"speech_rate_units_per_second": 4.0},
        {"text_backend_script": "test/new-model"},
        {"video_backend": "test/new-video"},
        {"default_duration": 8},
    ],
)
async def test_project_settings_changes_require_current_revision(setup_batch, patch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    service.projects.update_project("demo", lambda p: p.update(patch))
    await service.advance("demo")
    with pytest.raises(ValueError, match="输入已变化"):
        await service.control("demo", "continue", "u")
    status = await service.status("demo")
    await service.control("demo", "continue", "u", status["input_revision"])
    await service.advance("demo")
    assert len(calls) == 1


async def test_changed_outline_pauses_without_replaying_completed_episode(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    draft(service, 1)
    done(service)
    await service.advance("demo")
    service.projects.update_project("demo", lambda p: p["episodes"][1].update(outline={"story_beats": ["新增转折"]}))
    await service.advance("demo")
    status = await service.status("demo")
    assert status["status"] == "failed"
    assert status["input_changed"]
    assert len(calls) == 1
    await service.control("demo", "continue", "u", status["input_revision"])
    await service.advance("demo")
    assert [ep for ep, _ in calls] == [1, 2]


async def test_other_project_batch_and_assets_are_isolated(setup_batch):
    service, import_id, _calls = setup_batch
    service.projects.create_project("other", content_mode="drama")
    await service.start("demo", import_id, "u")
    require_batch_idle(service._dir("other"))
    assert (await service.status("other"))["status"] == "idle"
    assert service.projects.load_project("other").get("characters", {}) == {}


async def test_stopped_batch_can_switch_import_and_preserves_history(setup_batch):
    service, import_id, calls = setup_batch
    old = await service.start("demo", import_id, "u")
    await service.control("demo", "stop", "u")
    p = divide_script("第三集\n新剧情。")
    imported = import_script(
        service.projects,
        "demo",
        p["text"],
        [{k: e[k] for k in ("start", "title", "number")} for e in p["episodes"]],
        p["fingerprint"],
        project_basis(service.projects.load_project("demo")),
    )
    current = await service.start("demo", imported["import_id"], "u")
    assert current["items"] == [{"episode": 3, "status": "pending"}]
    history = service._dir("demo") / "production" / "script_batch_history" / f"{old['id']}.json"
    assert json.loads(history.read_text())["status"] == "stopped"
    assert not calls


async def test_external_source_edits_cannot_be_accepted_on_continue(setup_batch):
    from lib.episode.episode_paths import episode_source_path

    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    episode_source_path(service._dir("demo"), 1).write_text("外部修改")
    await service.advance("demo")
    status = await service.status("demo")
    assert status["status"] == "failed"
    assert status["input_changed"]
    with pytest.raises(ValueError, match="已被修改"):
        await service.control("demo", "continue", "u", status.get("input_revision"))
    assert not calls


async def test_real_queue_persists_internal_context_and_restart_claims_once(setup_batch, db_factory, monkeypatch):
    from lib.generation.generation_queue import GenerationQueue
    from server import tool_runtime

    service, import_id, _ = setup_batch
    queue = GenerationQueue(session_factory=db_factory, project_manager=service.projects)
    service.services = SimpleNamespace(queue=queue, projects=service.projects)
    monkeypatch.setattr(
        "server.services.project.whole_script_batch.generate_script_plan", tool_runtime.generate_script_plan
    )
    await queue.acquire_or_renew_worker_lease(name="default", owner_id="test-worker", ttl_seconds=60)
    state = await service.start("demo", import_id, "u")
    await service.advance("demo")
    persisted = service._read("demo")
    assert persisted["items"][0]["status"] == "active", persisted.get("error")
    tasks = (await queue.list_tasks(project_name="demo"))["items"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["payload"]["production_batch_id"] == state["id"]
    assert task["payload"]["production_attempt"] == persisted["items"][0]["attempt"]
    assert task["payload"]["batch_asset_context"] == "[]"
    # Simulate loss of the accepted response, keeping the persisted submission token.
    persisted["items"][0].update(status="submitting")
    persisted["items"][0].pop("batch_id")
    await service._write("demo", persisted)
    restarted = WholeScriptBatchService(service.projects, service.services)
    await restarted.advance("demo")
    assert restarted._read("demo")["items"][0]["task_id"] == task["task_id"]
    denied = await tool_runtime.generate_script_plan(
        tool_runtime.ToolRequest(tool_runtime.GenerateScriptPlanRequest(episode_id=2)),
        tool_runtime.ProjectScope(project_name="demo", data_root=service.projects.data_root),
        tool_runtime.CallerContext(user_id="u", source="webui"),
        service.services,
    )
    assert denied.problem
    assert denied.problem.code == "generation_refused"
    assert len((await queue.list_tasks(project_name="demo"))["items"]) == 1


async def test_success_with_missing_draft_never_replays_paid_task(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    done(service)
    await service.advance("demo")
    state = await service.status("demo")
    assert state["status"] == "failed"
    assert state["items"][0]["status"] == "completed"
    with pytest.raises(ValueError, match="缺失"):
        await service.control("demo", "continue", "u")
    draft(service, 1)
    await service.control("demo", "continue", "u")
    await service.advance("demo")
    assert [ep for ep, _ in calls] == [1, 2]


async def test_task_lookup_failure_preserves_handle_and_cannot_retry(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    service.services.queue.get_generation_batch.side_effect = ValueError("temporarily missing")
    await service.advance("demo")
    assert (await service.status("demo"))["status"] == "unknown"
    assert service._read("demo")["items"][0]["batch_id"] == "b1"
    await service.control("demo", "continue", "u")
    await service.advance("demo")
    assert len(calls) == 1
    service.services.queue.get_generation_batch.side_effect = None
    draft(service, 1)
    done(service)
    await service.advance("demo")
    await service.advance("demo")
    assert [ep for ep, _ in calls] == [1, 2]


async def test_empty_finished_batch_is_unresolved_not_success(setup_batch):
    service, import_id, calls = setup_batch
    await service.start("demo", import_id, "u")
    await service.advance("demo")
    service.services.queue.get_generation_batch.return_value = SimpleNamespace(
        done=True, counts=SimpleNamespace(total=0, succeeded=0, failed=0, cancelled=0, blocked=0)
    )
    await service.advance("demo")
    assert (await service.status("demo"))["status"] == "unknown"
    await service.advance("demo")
    assert len(calls) == 1
