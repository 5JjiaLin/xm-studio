"""Continuation failures never replay paid submissions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib.generation.generation_batch import GenerationBatchReadModel
from lib.project.project_manager import ProjectManager
from server.services.admission.cost_estimation import BatchCostEstimate
from server.services.project.production import ProductionService
from server.tool_runtime import ToolOutcome


def batch(batch_id="batch1", *, status="running"):
    return GenerationBatchReadModel.model_validate(
        {
            "batch_id": batch_id,
            "project": "demo",
            "operation": "generate_videos",
            "created_at": "2026-01-01T00:00:00Z",
            "done": status == "succeeded",
            "counts": {"total": 1, status: 1},
            "members": [{"unit_id": "E1U1", "status": status}],
        }
    )


@pytest.fixture
def production(tmp_path):
    projects = ProjectManager(tmp_path)
    (projects.data_root / "projects" / "demo" / "production").mkdir(parents=True)
    return ProductionService(projects, SimpleNamespace(queue=SimpleNamespace(get_generation_batch=AsyncMock())))


@pytest.mark.asyncio
async def test_refresh_and_restart_do_not_resubmit_ambiguous_operation(production, monkeypatch):
    state = {"episode": 1, "action": "assets", "status": "submitting", "stage": "prompts", "user_id": "default"}
    await production._write("demo", 1, state)
    submit = AsyncMock()
    monkeypatch.setattr(production, "_submit", submit)
    restarted = ProductionService(production.projects, production.services)
    monkeypatch.setattr(restarted, "_submit", submit)
    await restarted.advance("demo", 1)
    assert (await restarted.status("demo", 1))["status"] == "unknown"
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_query_failure_recovers_only_the_original_batch(production, monkeypatch):
    await production._write(
        "demo", 1, {"episode": 1, "status": "active", "stage": "videos", "user_id": "default", "batch_id": "batch1"}
    )
    production.services.queue.get_generation_batch.side_effect = RuntimeError("database unavailable")
    submit = AsyncMock()
    monkeypatch.setattr(production, "_submit", submit)
    await production.advance("demo", 1)
    production.services.queue.get_generation_batch.side_effect = None
    production.services.queue.get_generation_batch.return_value = batch(status="running")
    await production.advance("demo", 1)
    state = await production.status("demo", 1)
    assert state["status"] == "active"
    assert state["batch_id"] == "batch1"
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_failure_stops_before_next_paid_stage(production, monkeypatch):
    await production._write(
        "demo", 1, {"episode": 1, "status": "active", "stage": "prompts", "user_id": "default", "batch_id": "batch1"}
    )
    production.services.queue.get_generation_batch.return_value = SimpleNamespace(
        done=True, counts=SimpleNamespace(failed=1, cancelled=0, blocked=0), members=[]
    )
    submit = AsyncMock()
    monkeypatch.setattr(production, "_submit", submit)
    await production.advance("demo", 1)
    assert (await production.status("demo", 1))["status"] == "failed"
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_prompts_stop_for_review_without_media_submission(production, monkeypatch):
    await production._write(
        "demo", 1, {"episode": 1, "status": "active", "stage": "prompts", "user_id": "default", "batch_id": "batch1"}
    )
    production.services.queue.get_generation_batch.return_value = SimpleNamespace(
        done=True, counts=SimpleNamespace(failed=0, cancelled=0, blocked=0), members=[]
    )
    monkeypatch.setattr(production, "_script_hash", lambda *_: "basis")

    async def submitted(project, episode, state, stage):
        state.update(stage=stage, batch_id="batch2")
        await production._write(project, episode, state)

    submit = AsyncMock(side_effect=submitted)
    monkeypatch.setattr(production, "_submit", submit)
    await production.advance("demo", 1)
    production.services.queue.get_generation_batch.return_value = SimpleNamespace(done=False)
    await production.advance("demo", 1)
    submit.assert_not_awaited()
    state = await production.status("demo", 1)
    assert state["status"] == "completed"
    assert state["stage"] == "prompts"


@pytest.mark.asyncio
async def test_changed_script_prevents_render(production, monkeypatch):
    await production._write(
        "demo",
        1,
        {
            "episode": 1,
            "status": "active",
            "stage": "videos",
            "user_id": "default",
            "batch_id": "batch1",
            "basis": "old",
        },
    )
    production.services.queue.get_generation_batch.return_value = SimpleNamespace(
        done=True, counts=SimpleNamespace(failed=0, cancelled=0, blocked=0), members=[]
    )
    monkeypatch.setattr(production, "_script_hash", lambda *_: "new")
    submit = AsyncMock()
    monkeypatch.setattr(production, "_submit", submit)
    await production.advance("demo", 1)
    assert (await production.status("demo", 1))["status"] == "failed"
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_submission_preserves_selected_ids_and_duration_confirmation(production, monkeypatch):
    monkeypatch.setattr(
        production.projects,
        "load_project",
        lambda *_: {"episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}]},
    )
    generate = AsyncMock(return_value=ToolOutcome(value=batch()))
    monkeypatch.setattr("server.services.project.production.generate_videos", generate)
    state = {"episode": 1, "user_id": "default", "unit_ids": ["E1U1"], "durations": {"E1U1": 8}}
    await production._submit("demo", 1, state, "videos")
    request, _, caller, _ = generate.call_args.args
    assert request.value.target.ids == ["E1U1"]
    assert request.value.force is True
    assert request.value.confirmed_request_durations == {"E1U1": 8}
    assert caller.source == "mcp"
    assert state["batch_id"] == "batch1"
    assert state["status"] == "active"


@pytest.mark.asyncio
async def test_local_rebuild_never_calls_video_or_asset_provider(production, monkeypatch):
    monkeypatch.setattr(production, "_script_hash", lambda *_: "basis")
    submit = AsyncMock()
    monkeypatch.setattr(production, "_submit", submit)
    await production.start_render("demo", 1, "default")
    assert submit.call_args.args[-1] == "render"
    assert (await production.status("demo", 1))["stage"] == "render"


@pytest.mark.asyncio
async def test_active_member_progress_comes_from_durable_batch(production):
    await production._write(
        "demo", 1, {"episode": 1, "status": "active", "stage": "videos", "user_id": "default", "batch_id": "batch1"}
    )
    production.services.queue.get_generation_batch.return_value = batch(status="running")
    state = await production.status("demo", 1)
    assert state["members"] == [
        {
            "unit_id": "E1U1",
            "status": "running",
            "task_id": None,
            "task_type": None,
            "deduped": False,
            "problem": None,
            "admission": {},
        }
    ]


@pytest.mark.asyncio
async def test_restarted_service_advances_existing_video_batch_without_resubmitting_it(production, monkeypatch):
    await production._write(
        "demo",
        1,
        {
            "episode": 1,
            "action": "videos",
            "status": "active",
            "stage": "videos",
            "user_id": "default",
            "batch_id": "batch1",
            "basis": "basis",
        },
    )
    production.services.queue.get_generation_batch.return_value = batch(status="succeeded")
    restarted = ProductionService(production.projects, production.services)
    monkeypatch.setattr(restarted, "_script_hash", lambda *_: "basis")
    submit = AsyncMock()
    monkeypatch.setattr(restarted, "_submit", submit)
    await restarted.advance("demo", 1)
    assert submit.call_args.args[-1] == "render"
    assert (await restarted.status("demo", 1))["stage"] == "videos"
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_asset_preview_uses_explicit_admission_without_submitting(production, monkeypatch):
    loader = AsyncMock(
        return_value=(
            {"aspect_ratio": "9:16"},
            SimpleNamespace(
                new_task_ids=["character/阿明"],
                units={"character/阿明": SimpleNamespace(image_to_image=False)},
                preflight=SimpleNamespace(items=[]),
            ),
        )
    )
    estimate = AsyncMock(return_value=BatchCostEstimate(total={"CNY": 0.2}, unpriced_count=0))
    submit = AsyncMock()
    monkeypatch.setattr("server.services.project.production.load_asset_sheet_batch_plan", loader)
    monkeypatch.setattr("server.services.project.production.estimate_image_batch_cost", estimate)
    monkeypatch.setattr(production, "_submit", submit)
    quote = await production.preview_asset("demo", "character", "阿明", "default")
    assert quote == {"estimated_cost": {"CNY": 0.2}, "unpriced_count": 0, "submittable": True, "problems": []}
    assert loader.call_args.kwargs["scope"].names == ("阿明",)
    assert estimate.call_args.args[1] == ["t2i"]
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_asset_generation_starts_images_and_keeps_same_action_idempotent(production, monkeypatch):
    review = SimpleNamespace(
        get_state=AsyncMock(return_value={"fingerprint": "confirmed", "status": "confirmed"}), confirm=AsyncMock()
    )
    monkeypatch.setattr("server.services.project.production.ScriptReviewService", lambda *_: review)
    monkeypatch.setattr(production, "_script_hash", lambda *_: "basis")
    submit = AsyncMock()
    monkeypatch.setattr(production, "_submit", submit)
    state = await production.start_assets("demo", 1, "default", "confirmed")
    assert submit.call_args.args[-1] == "assets"
    review.confirm.assert_not_awaited()
    assert state["action"] == "assets"
    state.update(status="completed")
    await production._write("demo", 1, state)
    await production.start_assets("demo", 1, "default", "confirmed")
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_video_generation_negotiates_durations_and_dedupes_active(production, monkeypatch):
    monkeypatch.setattr(production, "_script_hash", lambda *_: "basis")
    preview = AsyncMock(return_value={"submittable": True, "confirmed_request_durations": {"E1U1": 8}})
    submit = AsyncMock()
    monkeypatch.setattr(production, "preview_videos", preview)
    monkeypatch.setattr(production, "_submit", submit)
    state = await production.start_videos("demo", 1, "default", unit_ids=["E1U1"])
    assert state["durations"] == {"E1U1": 8}
    await production.start_videos("demo", 1, "default", unit_ids=["E1U1"])
    preview.assert_awaited_once()
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_video_generation_retains_admission_rejection(production, monkeypatch):
    from server.services.project.production import ProductionError

    monkeypatch.setattr(production, "_script_hash", lambda *_: "basis")
    monkeypatch.setattr(production, "preview_videos", AsyncMock(return_value={"submittable": False}))
    submit = AsyncMock()
    monkeypatch.setattr(production, "_submit", submit)
    with pytest.raises(ProductionError):
        await production.start_videos("demo", 1, "default")
    submit.assert_not_awaited()
