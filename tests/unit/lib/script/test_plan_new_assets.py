"""脚本规划带出的新增资产：登记名与项目里已有资产或本次另一项新增只差大小写时视为重名。"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from lib.references.reference_catalog import build_reference_catalog
from lib.script.plan_new_assets import NewAssetsError, plan_reference_validation, planning_project, resolve_new_assets


def _new(name: str, asset_type: str = "character", **fields: Any) -> dict[str, Any]:
    return {
        "type": asset_type,
        "name": name,
        "decision": "register",
        "reason": "原文第一次出现",
        "description": "原文可确认的视觉外观",
        **fields,
    }


def _problems(project: dict[str, Any], items: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    with pytest.raises(NewAssetsError) as exc_info:
        resolve_new_assets(project, items)
    return [(problem.code, problem.index, problem.message) for problem in exc_info.value.problems]


def _problem_codes(project: dict[str, Any], items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(code, index) for code, index, _ in _problems(project, items)]


@pytest.mark.parametrize(("asset_type", "name"), [("character", "alice"), ("scene", "ALICE")])
def test_a_registration_differing_only_in_case_from_a_registered_asset_is_taken(asset_type: str, name: str) -> None:
    project = {"characters": {"Alice": {"description": "主角"}}}

    assert _problem_codes(project, [_new(name, asset_type, description="另一个人")]) == [("new_asset_name_taken", 0)]


def test_a_registration_name_differing_only_in_case_from_a_registered_asset_is_taken() -> None:
    project = {"characters": {"Alice": {"description": "主角"}}}

    assert _problem_codes(project, [_new("小爱", asset_name="ALICE", description="另一个人")]) == [
        ("new_asset_name_taken", 0)
    ]


def test_two_new_assets_differing_only_in_case_cannot_both_register() -> None:
    items = [_new("Bob", description="一个人"), _new("BOB", "prop", description="一样东西")]

    assert _problem_codes({}, items) == [("new_asset_name_taken", 1)]


def test_register_without_visual_description_is_rejected_with_actionable_error() -> None:
    problems = _problems({}, [_new("破旧木箱", "prop", description="")])

    assert len(problems) == 1
    assert problems[0][0:2] == ("new_asset_invalid", 0)
    assert "register 必须提供非空外观描述" in problems[0][2]


def test_continuous_carried_wooden_box_registers_and_remains_in_shot_references() -> None:
    project: dict[str, Any] = {"props": {}}
    item = _new(
        "破旧木箱",
        "prop",
        description="破旧木箱，木质箱体陈旧磨损；剧本未说明颜色、尺寸、纹样、箱内物品或特殊结构。",
        reason="连续镜头中由埃利亚斯持续拎持，需保持箱体外形与持物关系一致",
    )

    resolution = resolve_new_assets(project, [item])
    rewritten = resolution.rewrite_entries([{"unit_id": "E1U1", "text": "埃利亚斯仍拎着@[破旧木箱]。"}])[0]["text"]
    resolution.apply_to_project(project)

    assert "@[破旧木箱]" in rewritten
    assert resolution.registered() == [{"type": "prop", "name": "破旧木箱"}]
    assert project["props"]["破旧木箱"]["description"] == item["description"]


def test_a_registration_exactly_matching_a_registered_asset_still_merges_into_it() -> None:
    project = {"characters": {"Alice": {"description": "主角"}}}

    resolution = resolve_new_assets(project, [_new(" Alice ", description="重复登记")])

    assert resolution.registrations == {}
    assert resolution.auto_merged == [("character", " Alice ", "Alice")]


def _derivative(name: str, target: str = "Alice", **fields: Any) -> dict[str, Any]:
    return _new(name, decision="derivative", target=target, description="换装", **fields)


def test_planning_accepts_resolved_derivative_without_mutating_project() -> None:
    project = {"characters": {"Alice": {"description": "主角", "derivatives": {}}}}
    planning = planning_project(project, [_derivative("赤裸上身")])

    catalog = build_reference_catalog(planning)
    assert "Alice/赤裸上身" in catalog.reference_names("character")
    assert "赤裸上身" in catalog.reference_names("character")
    assert project["characters"]["Alice"]["derivatives"] == {}


@pytest.mark.parametrize("derivative_first", [False, True])
@pytest.mark.parametrize("base_name", ["José", "Jose\u0301"])
def test_plan_validation_preserves_the_draft_and_resolves_chained_names_and_derivatives(
    derivative_first: bool, base_name: str
) -> None:
    project: dict[str, Any] = {"characters": {}, "scenes": {}, "props": {}}
    items = [
        _new(base_name),
        _derivative("雨衣", target=base_name),
        _new("少年", decision="merge", target=base_name),
        _new("小哥", decision="merge", target="少年"),
        _new("路人", decision="skip"),
    ]
    if derivative_first:
        items.reverse()
    entries = [{"text": "@[小哥] 穿着@[雨衣]走过@[路人]。\n@[雨衣]{等我回来。}\n@[路人]{好。}"}]
    original = copy.deepcopy((project, items, entries))

    validation = plan_reference_validation(project, items, entries)

    assert not validation.asset_problems
    assert validation.entries[0]["text"] == ("@[José] 穿着@[José/雨衣]走过路人。\n@[José]{等我回来。}\n@[路人]{好。}")
    assert validation.tolerated_speakers == ("路人",)
    assert set(validation.project["characters"]) == {"José"}
    assert "雨衣" in validation.project["characters"]["José"]["derivatives"]
    assert (project, items, entries) == original


@pytest.mark.parametrize("derivative_first", [False, True])
@pytest.mark.parametrize("registered_name", ["Alice", "Álice"])
def test_planning_keeps_derivative_of_a_base_registered_in_the_same_plan(
    derivative_first: bool, registered_name: str
) -> None:
    project: dict[str, Any] = {"characters": {}, "scenes": {}, "props": {}}
    items = [
        _new(registered_name),
        _derivative("换装形态", target=registered_name),
    ]
    if derivative_first:
        items.reverse()

    planning = planning_project(project, items)
    catalog = build_reference_catalog(planning)

    assert f"{registered_name}/换装形态" in catalog.reference_names("character")
    assert {registered_name, "换装形态"} <= catalog.reference_names("character")
    assert planning["characters"][registered_name]["derivatives"]["换装形态"]["description"] == "换装"
    assert project == {"characters": {}, "scenes": {}, "props": {}}


def test_a_derivative_differing_only_in_case_from_an_existing_derivative_is_taken() -> None:
    project = {"characters": {"Alice": {"description": "主角", "derivatives": {"Coat": {"description": "大衣"}}}}}

    assert _problems(project, [_derivative("coat")]) == [
        ("new_asset_name_taken", 0, "new_assets[0]：衍生名「coat」与「Alice」已有的衍生「Coat」重名")
    ]
    assert _problem_codes(project, [_derivative("外套", asset_name="COAT")]) == [("new_asset_name_taken", 0)]


def test_two_new_derivatives_differing_only_in_case_under_one_base_cannot_both_register() -> None:
    project = {"characters": {"Alice": {"description": "主角"}}}

    assert _problems(project, [_derivative("Coat"), _derivative("coat")]) == [
        ("new_asset_name_taken", 1, "new_assets[1]：衍生名「coat」与「Alice」本集新增的衍生「Coat」重名")
    ]


def test_a_derivative_exactly_matching_an_existing_derivative_still_resolves_to_it() -> None:
    project = {
        "characters": {
            "Alice": {
                "description": "主角",
                "derivatives": {"Coat": {"description": "大衣"}, "coat": {"description": "旧版登记的另一件"}},
            }
        }
    }

    resolution = resolve_new_assets(project, [_derivative(" coat ")])

    assert resolution.derivatives == []
    assert resolution.outcomes[("character", "coat")].visual == "Alice/coat"


def test_writing_a_derivative_rejects_a_case_variant_registered_after_resolution() -> None:
    resolution = resolve_new_assets({"characters": {"Alice": {"description": "主角"}}}, [_derivative("Coat")])
    project = {"characters": {"Alice": {"description": "主角", "derivatives": {"coat": {"description": "大衣"}}}}}

    with pytest.raises(ValueError, match="重名"):
        resolution.apply_to_project(project)

    assert list(project["characters"]["Alice"]["derivatives"]) == ["coat"]
