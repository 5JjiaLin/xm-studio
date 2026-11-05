"""Stored costs include failed/unused calls without double-counting rerolls."""

from lib.billing.production_costs import aggregate_costs, task_attribution


def row(**kwargs):
    return dict(
        call_type="image",
        purpose="generation_task",
        episode_id=1,
        generation_target="character:hero",
        status="success",
        cost_amount=0.1,
        currency="CNY",
        **kwargs,
    )


def test_totals_rerolls_pending_unknown_and_currencies():
    first = row()
    retry = {**first, "cost_amount": 0.2, "status": "failed"}
    ep2 = {**first, "episode_id": 2, "cost_amount": 0.3}
    video = {**first, "call_type": "video", "generation_target": "video:one", "cost_amount": 2, "currency": "USD"}
    pending = {**video, "status": "pending", "cost_amount": 0}
    unpriced = {**video, "generation_target": "video:two", "cost_amount": None}
    result = aggregate_costs([first, retry, ep2, video, pending, unpriced], {"one.json": 1, "two.json": 2})
    one, two = result["episodes"]
    assert one["amounts"] == {"CNY": 0.3, "USD": 2}
    assert one["reroll_amounts"] == {"CNY": 0.2}
    assert one["reroll_count"] == 2
    assert one["pending_count"] == one["unpriced_count"] == 1
    assert two["amounts"] == {"CNY": 0.3}
    assert two["reroll_count"] == 1
    assert result["unassigned"]["call_count"] == 0


def test_legacy_exact_script_evidence_only_and_frozen_unknown():
    base = {
        **row(),
        "episode_id": None,
        "generation_target": None,
        "task_type": "reference_video",
        "resource_id": "E999U1",
        "script_file": "scripts/custom.json",
        "payload_json": None,
    }
    unknown = {**base, "script_file": "elsewhere.json"}
    frozen = {**base, "generation_target": "", "payload_json": '{"episode_id": 1}'}
    result = aggregate_costs([base, unknown, frozen], {"custom.json": 2})
    assert result["episodes"][0]["amounts"] == {"CNY": 0.1}
    assert result["unassigned"]["amounts"] == {"CNY": 0.2}
    assert result["unassigned"]["unidentified_target_count"] == 1


def test_missing_episode_and_nonproduction_calls_are_not_assigned():
    result = aggregate_costs(
        [row(), {**row(), "purpose": "endpoint_trial"}, {**row(), "purpose": "assistant_session"}], {"two.json": 2}
    )
    assert result["unassigned"]["call_count"] == 1
    assert result["episodes"][0]["call_count"] == 0


def test_attribution_uses_top_level_explicit_identity_and_stable_target():
    task = {
        "task_type": "character",
        "resource_id": "hero",
        "payload_json": '{"billing_episode_id": 2, "reference": {"episode_id": 1}}',
    }
    ep, target = task_attribution(task)
    assert ep == 2
    assert task_attribution({**task, "payload_json": '{"billing_episode_id": 3}'}) == (3, target)
    assert task_attribution({"payload_json": '{"reference":{"episode_id":1}}'})[0] is None
    assert task_attribution({"payload_json": "bad json"})[0] is None
