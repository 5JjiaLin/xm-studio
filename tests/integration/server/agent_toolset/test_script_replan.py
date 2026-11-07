"""Reason-guided replanning uses the reviewed snapshot through the original queue and model boundary."""

import json

import pytest

from lib.script import script_review
from lib.script.draft_quarantine import write_quarantine
from server.text_generation import TextGenerationRequest
from tests.fakes import FakeTextGenerator, use_text_generator
from tests.integration.server.agent_tool_support import (
    ToolHarness,
    drama_project,
    nr_source,
    run_declared_tool,
    rv_script_plan_path,
    rv_source,
    rv_unit,
    said,
)


@pytest.mark.parametrize("mode", ["reference", "drama", "narration"])
async def test_replan_prompt_uses_reason_and_current_plan(fake_ctx: ToolHarness, video_request_facts, mode):
    {"reference": rv_source, "drama": drama_project, "narration": nr_source}[mode](fake_ctx)
    path = script_review.script_plan_path(fake_ctx.project_path, fake_ctx.pm.project_payload, 1)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {"reviewed_marker": "待纠正的铁链独立资产"}
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    result = await run_declared_tool(
        "generate_script_plan",
        fake_ctx,
        {
            "episode_id": 1,
            "dry_run": True,
            "instructions": "保持对白原文",
            "replan_reason": "将手链脚链纳入人物束缚形态",
        },
    )

    assert result.problem is None, result
    prompt = said(result)
    assert "将手链脚链纳入人物束缚形态" in prompt
    assert "待纠正的铁链独立资产" in prompt
    assert "保持对白原文" in prompt
    assert "不能只改 reason" in prompt
    assert path.read_bytes() == before  # Preview must not replace the plan.


async def test_replan_prefers_visible_quarantine_to_previous_plan(fake_ctx: ToolHarness, video_request_facts):
    rv_source(fake_ctx)
    path = rv_script_plan_path(fake_ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"marker": "旧已通过规划"}', encoding="utf-8")
    kind = script_review.script_plan_quarantine_kind(fake_ctx.pm.project_payload)
    assert kind is not None
    write_quarantine(fake_ctx.project_path, 1, kind, content={"marker": "当前待修复规划"}, violations=[])

    result = await run_declared_tool(
        "generate_script_plan", fake_ctx, {"episode_id": 1, "dry_run": True, "replan_reason": "修正束缚形态"}
    )

    assert result.problem is None, result
    assert "当前待修复规划" in said(result)
    assert "旧已通过规划" not in said(result)


async def test_reason_and_snapshot_reach_model_via_queue_without_registering_assets(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
):
    rv_source(fake_ctx)
    source = "张三双手戴着手链，双脚戴着脚链，在村口说：别过来。"
    (fake_ctx.project_path / "source/episode_1.txt").write_text(source, encoding="utf-8")
    path = rv_script_plan_path(fake_ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = {"units": [rv_unit("@[张三] 戴着 @[铁链]。", source_text=source)], "new_assets": []}
    path.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
    reason = "将手链和脚链合为张三的束缚形态，保留束缚状态，不独立登记铁链道具。"
    result_content = {
        "units": [
            rv_unit("@[张三/束缚形态] 在 @[村口]，双手戴着手链、双脚戴着脚链。\n@[张三]{别过来。}", source_text=source)
        ],
        "new_assets": [
            {
                "type": "character",
                "name": "张三（束缚形态）",
                "decision": "derivative",
                "target": "张三",
                "asset_name": "束缚形态",
                "description": "双手戴着手链，双脚戴着脚链",
                "reason": "持续佩戴的束缚状态归入完整人物外观",
            }
        ],
    }
    generator = FakeTextGenerator(json.dumps(result_content, ensure_ascii=False))
    use_text_generator(monkeypatch, generator.create)

    result = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "replan_reason": reason})

    assert result.problem is None, result
    prompt = generator.requests[0].prompt
    assert reason in prompt
    assert "@[铁链]" in prompt  # The old result is supplied for correction, not silently discarded.
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["units"][0]["text"] == result_content["units"][0]["text"]
    assert saved["new_assets"][0]["decision"] == "derivative"
    assert "铁链" not in fake_ctx.pm.project_payload["props"]
    assert "束缚形态" not in fake_ctx.pm.project_payload["characters"]["张三"].get("derivatives", {})
    tasks = await fake_ctx.queue.list_tasks(project_name=fake_ctx.project_name)
    payload = tasks["items"][0]["payload"]
    restored = TextGenerationRequest(**payload)
    assert restored.replan_reason == reason
    assert json.loads(restored.replan_context)["content"] == previous


async def test_first_failed_attempt_can_retry_with_reason_without_snapshot(fake_ctx: ToolHarness, video_request_facts):
    rv_source(fake_ctx)
    result = await run_declared_tool(
        "generate_script_plan", fake_ctx, {"episode_id": 1, "dry_run": True, "replan_reason": "上次超时，请重试"}
    )
    assert result.problem is None, result
    assert "上次超时，请重试" in said(result)
    assert "尚无可用拆解结果" in said(result)


def test_legacy_text_task_payload_is_unchanged_without_replan():
    payload = TextGenerationRequest(episode=1).to_payload()
    assert "replan_reason" not in payload
    assert "replan_context" not in payload
    assert TextGenerationRequest(**payload).to_payload() == payload
