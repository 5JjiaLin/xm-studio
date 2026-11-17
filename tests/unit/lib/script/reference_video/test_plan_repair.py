from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from lib.infra.content_digest import prefixed_canonical_json_digest
from lib.script.draft_violation import DraftViolation
from lib.script.reference_video.plan_repair import (
    PlanRepairError,
    build_repair_prompt,
    build_repair_schema,
    merge_repair,
    parse_repair_response,
    repair_targets,
)


def unit(source: str, text: str, duration: int = 6) -> dict:
    return {"source_text": source, "text": text, "duration_seconds": duration}


def response(index: int, original: dict, *units: dict) -> dict:
    return {"item_index": index, "content_digest": prefixed_canonical_json_digest(original), "units": list(units)}


def issue(index: int, code: str = "dialogue_overload") -> DraftViolation:
    return DraftViolation("too long", code=code, item_index=index)


def test_two_nonadjacent_targets_keep_middle_and_assets():
    units = [unit("甲说话", "@[甲]{原话一。}"), unit("乙等待", "@[乙]等候。"), unit("丙发问", "@[丙]{你是谁？}")]
    content = {"units": units, "new_assets": [{"name": "甲", "type": "character"}]}
    original = copy.deepcopy(content)
    targets = repair_targets(content, [issue(0), issue(2)], "甲说话。乙等待。丙发问。")
    reply = {"replacements": [response(2, units[2], {**units[2], "duration_seconds": 12}), response(0, units[0], {**units[0], "duration_seconds": 12})]}
    candidate = merge_repair(content, targets, reply)
    assert candidate["units"][1] == original["units"][1]
    assert candidate["new_assets"] == original["new_assets"]
    assert content == original


def test_single_sentence_can_split_without_losing_speaker_or_actions():
    original = unit("他问你究竟是谁", "@[甲]抬头。\n@[甲]{你究竟是谁}")
    content = {"units": [original]}
    targets = repair_targets(content, [issue(0)], "他问你究竟是谁")
    reply = {"replacements": [response(0, original, unit("他问你", "@[甲]抬头。\n@[甲]{你}"), unit("究竟是谁", "@[甲]{究竟是谁}"))]}
    assert len(merge_repair(content, targets, reply)["units"]) == 2


@pytest.mark.parametrize("text", ["@[甲]{你是谁}", "@[乙]{你究竟是谁}", "{你究竟是谁}", "@[甲]{你究竟是谁}快说。", "@[甲]{你是谁究竟}"])
def test_content_changes_are_rejected(text):
    original = unit("问身份", "@[甲]{你究竟是谁}")
    with pytest.raises(PlanRepairError, match="repair_content_changed"):
        merge_repair({"units": [original]}, [{"item_index": 0, "content_digest": prefixed_canonical_json_digest(original)}], {"replacements": [response(0, original, unit("问身份", text))]})


@pytest.mark.parametrize("anchors", [["他问你", "你是谁"], ["他问", "是谁"], ["是谁", "他问你"]])
def test_source_overlap_loss_or_reordering_rejected(anchors):
    original = unit("他问你是谁", "@[甲]{原话}")
    replacements = [unit(a, "@[甲]{原话}") for a in anchors]
    with pytest.raises(PlanRepairError, match="repair_source_changed"):
        merge_repair({"units": [original]}, [{"item_index": 0, "content_digest": prefixed_canonical_json_digest(original)}], {"replacements": [response(0, original, *replacements)]})


@pytest.mark.parametrize("indexes", [[0, 0], [1], [], [0, 1]])
def test_missing_duplicate_or_outside_target_rejected(indexes):
    original = unit("来源", "动作")
    targets = [{"item_index": 0, "content_digest": prefixed_canonical_json_digest(original)}]
    with pytest.raises(PlanRepairError, match="repair_scope_changed"):
        merge_repair({"units": [original]}, targets, {"replacements": [response(i, original, original) for i in indexes]})


@pytest.mark.parametrize("code", ["source_text_not_verbatim", "unregistered_asset", "schema_invalid", "formal_revision_conflict"])
def test_nonwhitelisted_error_cannot_trigger_call(code):
    with pytest.raises(PlanRepairError, match="repair_not_supported"):
        repair_targets({"units": [unit("来源", "动作")]}, [issue(0, code)], "来源")


def test_ambiguous_or_overlapping_sources_cannot_trigger_call():
    with pytest.raises(PlanRepairError, match="repair_source_ambiguous"):
        repair_targets({"units": [unit("重复", "动作")]}, [issue(0)], "重复。重复。")
    with pytest.raises(PlanRepairError, match="repair_source_overlap"):
        repair_targets({"units": [unit("他问", "动作"), unit("他问身份", "动作")]}, [issue(0)], "他问身份")


def test_schema_forbids_extra_fields_invalid_tier_and_coercion():
    schema = build_repair_schema([6, 12])
    original = unit("来源", "动作")
    valid = {"status": "repaired", "replacements": [response(0, original, original)], "reason_code": None}
    assert schema.model_validate(valid).model_dump() == valid
    for change in ({"new_assets": []}, {"replacements": [response(0, original, unit("来源", "动作", 8))]}, {"replacements": [{**response(0, original, original), "item_index": "0"}]}):
        with pytest.raises(ValidationError):
            schema.model_validate({**valid, **change})


def test_unrepairable_is_a_valid_bounded_response_and_cannot_be_merged():
    schema = build_repair_schema([6, 12])
    refusal = parse_repair_response(
        schema,
        '{"status":"unrepairable","replacements":[],"reason_code":"cannot_preserve_content"}',
    )
    assert refusal["status"] == "unrepairable"
    with pytest.raises(PlanRepairError, match="repair_response_unrepairable"):
        merge_repair({"units": [unit("来源", "动作")]}, [{"item_index": 0, "content_digest": "digest"}], refusal)


@pytest.mark.parametrize("value", [
    {"status": "repaired", "replacements": [], "reason_code": None},
    {"status": "unrepairable", "replacements": [{"item_index": 0, "content_digest": "x", "units": [unit("a", "b")]}], "reason_code": "cannot_preserve_content"},
    {"status": "unrepairable", "replacements": [], "reason_code": None},
])
def test_repair_response_status_must_match_replacement_contract(value):
    schema = build_repair_schema([6])
    with pytest.raises((PlanRepairError, ValidationError)):
        parse_repair_response(schema, json.dumps(value))


def test_long_context_not_silently_truncated():
    with pytest.raises(PlanRepairError, match="repair_context_too_large"):
        build_repair_prompt({"source": "长" * 180_000})
