"""Annotated B1 decisions preserve story content through the real asset resolver."""

import copy
import json
from pathlib import Path

import pytest

from lib.script.plan_new_assets import resolve_new_assets

_ROOT = Path(__file__).resolve().parents[4]
_CASES = json.loads((_ROOT / "docs/production-mvp/fixtures/asset-rule-baselines/cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_annotated_decisions_keep_actions_and_only_register_independent_props(case):
    project = copy.deepcopy(case.get("project", {}))
    resolution = resolve_new_assets(project, case["assets"])
    rewritten = resolution.rewrite_entries([{"unit_id": "E1U1", "text": case["text"]}])[0]["text"]
    assert [item["name"] for item in resolution.registered() if item["type"] == "prop"] == case["props"]
    preserved_text = rewritten.replace("@[", "").replace("]", "")
    for fragment in case["preserve"]:
        assert fragment in preserved_text
    for asset in case["assets"]:
        if asset["decision"] == "skip":
            assert f"@[{asset['name']}]" not in rewritten
    resolution.apply_to_project(project)
    if case["id"] == "existing_reuse":
        assert project["props"]["铜钥匙"]["description"] == "旧图对应的铜钥匙"


def test_worn_restraints_form_one_character_appearance_without_losing_dialogue_or_carried_prop():
    project = {"characters": {"埃利亚斯": {"description": "青年"}}, "props": {"破旧木箱": {"description": "旧木箱"}}}
    resolution = resolve_new_assets(
        project,
        [
            {
                "type": "character",
                "name": "埃利亚斯（束缚）",
                "decision": "derivative",
                "target": "埃利亚斯",
                "asset_name": "束缚形态",
                "description": "手腕戴着手链、脚踝戴着脚链",
                "reason": "持续佩戴的完整外观",
            },
            {
                "type": "prop",
                "name": "铁链",
                "decision": "skip",
                "description": "",
                "reason": "归入人物束缚形态，无需独立图",
            },
        ],
    )
    original = "@[埃利亚斯（束缚）] 双手被@[铁链]束缚，仍拎着@[破旧木箱]。\n@[埃利亚斯（束缚）]{别碰我。}"

    rewritten = resolution.rewrite_entries([{"unit_id": "E1U1", "text": original}])[0]["text"]
    resolution.apply_to_project(project)

    assert rewritten == "@[埃利亚斯/束缚形态] 双手被铁链束缚，仍拎着@[破旧木箱]。\n@[埃利亚斯]{别碰我。}"
    assert set(project["props"]) == {"破旧木箱"}
    assert project["characters"]["埃利亚斯"]["derivatives"]["束缚形态"]["description"] == "手腕戴着手链、脚踝戴着脚链"
