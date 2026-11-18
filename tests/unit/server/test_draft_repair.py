"""AI 修复的范围判定与回复合并：条目模式只放回违约条目，整集模式只接受原草稿已有的字段。"""

from __future__ import annotations

import json

import pytest

from lib.script.draft_quarantine import QUARANTINE_KIND_NARRATION_SCRIPT_PLAN, QUARANTINE_KIND_SCRIPT_PLAN
from server.draft_repair import RepairScope, build_repair_prompt, merge_repair, repair_scope

_CONTENT = {"episode": 1, "segments": [{"segment_id": "E1S01"}, {"segment_id": "E1S02"}]}


def test_item_scoped_repair_sends_all_diagnostics_without_expanding_writable_scope():
    content = {"units": [{"text": f"shot {i}"} for i in range(20)]}
    violations = [{"item_index": i, "message": f"诊断 {i}"} for i in range(12)]
    violations += [
        {"message": "衔接异常", "locations": [{"unit_id": "E1U17"}, {"unit_id": "E1U18"}]},
        {"message": "衔接异常", "locations": [{"unit_id": "E1U18"}, {"unit_id": "E1U19"}]},
    ]
    scope = RepairScope("units", tuple(range(12)))
    prompt = build_repair_prompt(QUARANTINE_KIND_SCRIPT_PLAN, content, scope, violations,
                                 source_text="正式原词", instructions=None)
    assert json.loads(prompt.split("<diagnostics>")[1].split("</diagnostics>")[0]) == violations
    merged = merge_repair(content, scope, {"units": [{"text": "fixed"}] * 12})
    assert merged["units"][12:] == content["units"][12:]


def test_item_mode_puts_back_only_the_violating_items() -> None:
    scope = repair_scope(QUARANTINE_KIND_NARRATION_SCRIPT_PLAN, _CONTENT, [{"item_index": 1}, {"item_index": 1}])
    assert scope == RepairScope("segments", (1,))

    merged = merge_repair(_CONTENT, scope, {"segments": [{"segment_id": "E1S02", "fixed": True}]})

    assert merged["segments"] == [{"segment_id": "E1S01"}, {"segment_id": "E1S02", "fixed": True}]
    assert _CONTENT["segments"][1] == {"segment_id": "E1S02"}


@pytest.mark.parametrize("violation", [{"item_index": None}, {"item_index": 5}, {"item_index": True}])
def test_unlocated_violation_switches_to_whole_draft_mode(violation: dict) -> None:
    scope = repair_scope(QUARANTINE_KIND_NARRATION_SCRIPT_PLAN, _CONTENT, [{"item_index": 0}, violation])
    assert scope.indices is None


def test_whole_draft_mode_keeps_only_the_draft_fields() -> None:
    scope = RepairScope("segments", None)

    merged = merge_repair(_CONTENT, scope, {"segments": [{"segment_id": "E1S01"}], "notes": "已合并"})

    assert merged == {"episode": 1, "segments": [{"segment_id": "E1S01"}]}


@pytest.mark.parametrize(
    ("scope", "reply", "match"),
    [
        (RepairScope("segments", (1,)), {"segments": []}, "条目数"),
        (RepairScope("segments", (1,)), {"segments": ["not an object"]}, "条目不是 JSON 对象"),
        (RepairScope("segments", None), {"segments": []}, "缺少非空的 segments 数组"),
        (RepairScope("segments", None), ["not an object"], "模型回复不是 JSON 对象"),
    ],
    ids=["item_count_mismatch", "item_not_object", "empty_whole_draft", "reply_not_object"],
)
def test_malformed_reply_is_rejected(scope: RepairScope, reply: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        merge_repair(_CONTENT, scope, reply)


def test_unresolved_speaker_repairs_only_character_aliases_and_preserves_the_episode():
    content = {"units": [{"text": "@[吉迪恩]{Come here.}"}], "new_assets": [
        {"type": "scene", "name": "石殿", "description": "原空间"},
        {"type": "character", "name": "吉迪恩", "decision": "register", "description": "原外观", "aliases": []},
    ]}
    violations = [{"code": "screenplay_speaker_unresolved", "message": "GIDEON 缺别名"}]
    scope = repair_scope(QUARANTINE_KIND_SCRIPT_PLAN, content, violations)
    reply = {"new_assets": [{"name": "吉迪恩", "aliases": ["GIDEON"], "description": "不该覆盖"}]}
    merged = merge_repair(content, scope, reply)
    assert merged == {**content, "new_assets": [content["new_assets"][0],
        {**content["new_assets"][1], "aliases": ["GIDEON"]}]}
    assert content["new_assets"][1]["aliases"] == []
    prompt = build_repair_prompt(QUARANTINE_KIND_SCRIPT_PLAN, content, scope, violations,
                                 source_text="GIDEON: Come here.", instructions=None)
    assert '只返回人物的 name、aliases' in prompt
    assert '只输出一个 JSON 对象：`{"new_assets": [...]}`' in prompt


def test_prop_scale_repair_only_changes_the_reported_registered_prop_description():
    content = {
        "units": [{"unit_id": "E1U1", "text": "原视频单元"}],
        "new_assets": [
            {"type": "character", "name": "甲", "description": "原人物"},
            {"type": "prop", "name": "传音符", "decision": "register", "reason": "原依据", "description": "约掌心大小的符纸"},
            {"type": "prop", "name": "木箱", "decision": "register", "reason": "箱体", "description": "原木箱"},
        ],
    }
    violations = [{
        "code": "prompt_policy_prop_scale_missing",
        "label": "new_assets[1]",
        "message": "new_assets[1] 缺少可执行尺寸",
    }]
    scope = repair_scope(QUARANTINE_KIND_SCRIPT_PLAN, content, violations)
    reply = {"new_assets": [{
        "name": "传音符",
        "type": "prop",
        "description": "约长9厘米、宽6厘米的黄色符纸，边缘朱砂绘纹，纸面轻薄。",
    }]}

    merged = merge_repair(content, scope, reply)

    assert scope == RepairScope("new_assets", (1,), prop_descriptions_only=True)
    assert merged["new_assets"][1]["description"] == reply["new_assets"][0]["description"]
    assert merged["new_assets"][1]["reason"] == "原依据"
    assert merged["new_assets"][0] == content["new_assets"][0]
    assert merged["new_assets"][2] == content["new_assets"][2]
    assert merged["units"] == content["units"]
    prompt = build_repair_prompt(
        QUARANTINE_KIND_SCRIPT_PLAN, content, scope, violations,
        source_text="传音符。", instructions=None,
    )
    assert "只修复被指出尺寸缺失的实体道具 description" in prompt
    assert "new_assets[1] 缺少可执行尺寸" in prompt


@pytest.mark.parametrize("reply", [
    {"name": "木箱", "type": "prop", "description": "长40厘米。"},
    {"name": "传音符", "type": "character", "description": "长9厘米。"},
    {"name": "传音符", "type": "prop", "description": "长9厘米。", "reason": "改理由"},
])
def test_prop_scale_repair_rejects_asset_identity_or_scope_changes(reply):
    content = {"units": [{}], "new_assets": [
        {"type": "prop", "name": "传音符", "decision": "register", "reason": "原理由", "description": "掌心大小"},
    ]}
    scope = repair_scope(QUARANTINE_KIND_SCRIPT_PLAN, content, [{
        "code": "prompt_policy_prop_scale_missing", "label": "new_assets[0]",
    }])

    with pytest.raises(ValueError, match="道具尺寸修复"):
        merge_repair(content, scope, {"new_assets": [reply]})


@pytest.mark.parametrize("reply", [
    {"name": "另一个人", "aliases": ["GIDEON"]},
    {"name": "吉迪恩", "aliases": "GIDEON"},
    {"name": "吉迪恩", "aliases": [42]},
])
def test_alias_repair_rejects_identity_changes_and_malformed_aliases(reply):
    content = {"units": [{}], "new_assets": [{"type": "character", "name": "吉迪恩", "decision": "register", "aliases": []}]}
    scope = repair_scope(QUARANTINE_KIND_SCRIPT_PLAN, content, [{"code": "screenplay_speaker_unresolved"}])
    with pytest.raises(ValueError, match="别名修复"):
        merge_repair(content, scope, {"new_assets": [reply]})


@pytest.mark.parametrize('change', [None, 'manual', 'legacy_manual', 'bad_ids'])
def test_structural_scope_protects_successes_and_rejects_stale_raw_response(tmp_path, change):
    from lib.infra.content_digest import canonical_json_digest
    from lib.script.draft_quarantine import QUARANTINE_KIND_PROMPT_AUTHORING, QuarantinedDraft
    from server.draft_repair import reference_structure_repair_scope

    sources = [{'unit_id': '1', 'text': 'source1', 'pending_authoring': True},
               {'unit_id': '2', 'text': 'source2', 'pending_authoring': True}]
    content = {'title': 't', 'units': [{'text': 'successful1'}, {'text': 'source2'}]}
    diagnostic = {'failed_unit_ids': ['2'], 'fallback_digest': canonical_json_digest({
        'title': 't', 'units': [content['units'][1]],
    })}
    if change == 'manual':
        content['units'][1]['text'] = 'manual2'
    elif change == 'legacy_manual':
        diagnostic = {}
        content['units'][1]['text'] = 'manual2'
    elif change == 'bad_ids':
        diagnostic['failed_unit_ids'] = ['missing']
    draft = QuarantinedDraft(
        kind=QUARANTINE_KIND_PROMPT_AUTHORING, episode=1, content=content, violations=[],
        meta={'response_diagnostic': diagnostic}, schema_version=1, path=tmp_path / 'unused.json',
    )
    if change:
        with pytest.raises(ValueError, match=r"失败单元范围无效|草稿已被编辑|无法安全定位失败批次"):
            reference_structure_repair_scope(draft, sources, ['1', '2'])
    else:
        scope = reference_structure_repair_scope(draft, sources, ['1', '2'])
        assert scope.indices == (1,)
        assert merge_repair(content, scope, {'units': [{'text': 'repaired2'}]})['units'] == [
            {'text': 'successful1'}, {'text': 'repaired2'},
        ]
