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
    for fragment in case["preserve"]:
        assert fragment in rewritten
    for asset in case["assets"]:
        if asset["decision"] == "skip":
            assert f"@[{asset['name']}]" not in rewritten
    resolution.apply_to_project(project)
    if case["id"] == "existing_reuse":
        assert project["props"]["铜钥匙"]["description"] == "旧图对应的铜钥匙"
