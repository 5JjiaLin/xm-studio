"""内容确认时登记本集新增资产：按处理决定登记、改写引用，同名归并，「不登记」的改写。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.config.resolver import ConfigResolver
from lib.infra.json_io import atomic_write_json
from lib.project.project_manager import ProjectManager
from server.draft_workflow import DraftContext, DraftWorkflow
from server.services.project.script_review import ScriptReviewError, ScriptReviewService
from server.text_generation import TextGenerationError, TextGenerationRequest, generate_reference_script_plan
from tests.fakes import (
    FakeConfigResolver,
    FakeTextGenerator,
    use_generation_context,
    use_project_manager,
    use_text_generator,
)

_PLAN_FILES = {
    "drama": "script_plan_normalized_script.json",
    "narration": "script_plan_segments.json",
    "reference_video": "script_plan_reference_units.json",
}


def _project(tmp_path: Path, content_mode: str, *, generation_mode: str | None = None) -> ProjectManager:
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", content_mode)
    pm.add_character("demo", "阿离", "少女，青衣")
    pm.add_character("demo", "裴与", "将军，银甲")
    pm.add_episode("demo", 1, "第一集", "scripts/episode_1.json")
    if generation_mode is not None:
        pm.update_project("demo", lambda project: project.update(generation_mode=generation_mode))
    return pm


def _write_plan(pm: ProjectManager, route: str, content: dict[str, Any]) -> None:
    project_path = pm.get_project_path("demo")
    drafts = project_path / "drafts" / "episode_1"
    drafts.mkdir(parents=True, exist_ok=True)
    atomic_write_json(drafts / _PLAN_FILES[route], content)
    source = project_path / "source" / "episode_1.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("原文", encoding="utf-8")
    activate_artifact_target_state(project_path, bump_schema=False)


def _narration_plan(characters: list[str], new_assets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "episode": 1,
        "segments": [
            {
                "segment_id": "E1S01",
                "novel_text": "裴与身边跟着一个小丫头。",
                "duration_seconds": 6,
                "segment_break": False,
                "characters_in_segment": characters,
                "scenes": [],
                "props": [],
            }
        ],
        "new_assets": new_assets,
    }


def _new(name: str, decision: str, **fields: Any) -> dict[str, Any]:
    return {"type": "character", "name": name, "decision": decision, "reason": "依据", **fields}


async def _confirm(pm: ProjectManager) -> None:
    service = ScriptReviewService(pm, config_resolver=cast(ConfigResolver, FakeConfigResolver()))
    await service.confirm("demo", 1, plan_fingerprint=_review_fingerprint(pm))


def _formal(pm: ProjectManager) -> dict[str, Any]:
    return json.loads((pm.get_project_path("demo") / "scripts" / "episode_1.json").read_text(encoding="utf-8"))


async def test_a_new_character_is_registered_with_its_aliases(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(["裴与", "小桃"], [_new("小桃", "register", description="十岁女童，双髻", aliases=["桃丫头"])]),
    )

    await _confirm(pm)

    character = pm.load_project("demo")["characters"]["小桃"]
    assert character["description"] == "十岁女童，双髻"
    assert character["aliases"] == ["桃丫头"]
    assert _formal(pm)["segments"][0]["characters_in_segment"] == ["裴与", "小桃"]


async def test_a_renamed_new_asset_keeps_the_planned_name_as_an_alias(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(["桃丫头"], [_new("桃丫头", "register", description="十岁女童", asset_name="小桃")]),
    )

    await _confirm(pm)

    characters = pm.load_project("demo")["characters"]
    assert "桃丫头" not in characters
    assert characters["小桃"]["aliases"] == ["桃丫头"]
    assert _formal(pm)["segments"][0]["characters_in_segment"] == ["小桃"]


async def test_merging_into_an_existing_asset_rewrites_references_and_records_the_alias(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(["将军"], [_new("将军", "merge", target="裴与", description="被忽略的描述")]),
    )

    await _confirm(pm)

    characters = pm.load_project("demo")["characters"]
    assert "将军" not in characters
    assert characters["裴与"]["description"] == "将军，银甲"
    assert characters["裴与"]["aliases"] == ["将军"]
    assert _formal(pm)["segments"][0]["characters_in_segment"] == ["裴与"]


async def test_a_new_asset_named_like_a_registered_one_merges_without_changing_its_description(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(["裴与"], [_new(" 裴与", "register", description="另一份描述", aliases=["裴将军"])]),
    )

    await _confirm(pm)

    characters = pm.load_project("demo")["characters"]
    assert list(characters) == ["阿离", "裴与"]
    assert characters["裴与"]["description"] == "将军，银甲"
    assert characters["裴与"]["aliases"] == ["裴将军"]


async def test_same_named_new_assets_register_once(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(
            ["小桃"],
            [
                _new("小桃", "register", description="十岁女童"),
                _new("小桃", "register", description="十岁女童"),
            ],
        ),
    )

    await _confirm(pm)

    assert pm.load_project("demo")["characters"]["小桃"]["description"] == "十岁女童"


async def test_merging_into_another_new_asset_follows_its_registration(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(
            ["小桃", "桃丫头"],
            [_new("小桃", "register", description="十岁女童"), _new("桃丫头", "merge", target="小桃")],
        ),
    )

    await _confirm(pm)

    assert pm.load_project("demo")["characters"]["小桃"]["aliases"] == ["桃丫头"]
    assert _formal(pm)["segments"][0]["characters_in_segment"] == ["小桃"]


async def test_merging_into_another_new_asset_by_its_registered_name(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(
            ["桃丫头", "小桃儿"],
            [
                _new("桃丫头", "register", description="十岁女童", asset_name="小桃"),
                _new("小桃儿", "merge", target="小桃"),
            ],
        ),
    )

    await _confirm(pm)

    assert pm.load_project("demo")["characters"]["小桃"]["aliases"] == ["桃丫头", "小桃儿"]
    assert _formal(pm)["segments"][0]["characters_in_segment"] == ["小桃"]


@pytest.mark.parametrize("decision", ["skip", "derivative"])
async def test_a_new_asset_named_like_a_registered_one_merges_whatever_its_decision(
    tmp_path: Path, video_request_facts, decision: str
) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(
        pm,
        "narration",
        _narration_plan(["裴与"], [_new("裴与", decision, target="阿离", asset_name="少年", description="少年")]),
    )

    await _confirm(pm)

    characters = pm.load_project("demo")["characters"]
    assert characters["阿离"]["derivatives"] == {}
    assert characters["裴与"]["description"] == "将军，银甲"
    assert _formal(pm)["segments"][0]["characters_in_segment"] == ["裴与"]


async def test_a_derivative_is_registered_under_its_base_character(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "drama")
    scene = {
        "scene_id": "E1S01",
        "duration_seconds": 8,
        "segment_break": False,
        "characters_in_scene": ["少年裴与"],
        "scenes": [],
        "props": [],
        "scene_description": "少年裴与练枪",
        "utterances": [{"kind": "dialogue", "speaker": "少年裴与", "text": "再来！"}],
        "source_text": "少年裴与练枪。",
    }
    _write_plan(
        pm,
        "drama",
        {
            "title": "第一集",
            "scenes": [scene],
            "new_assets": [
                _new("少年裴与", "derivative", target="裴与", asset_name="少年", description="十五岁，布衣束发")
            ],
        },
    )

    await _confirm(pm)

    base = pm.load_project("demo")["characters"]["裴与"]
    assert base["derivatives"]["少年"]["description"] == "十五岁，布衣束发"
    assert base["description"] == "将军，银甲"
    [formal_scene] = _formal(pm)["scenes"]
    assert formal_scene["characters_in_scene"] == ["裴与/少年"]
    assert formal_scene["utterances"][0]["speaker"] == "裴与"


async def test_a_skipped_drama_character_leaves_the_references_and_keeps_its_lines(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "drama")
    scene = {
        "scene_id": "E1S01",
        "duration_seconds": 8,
        "segment_break": False,
        "characters_in_scene": ["阿离", "路人"],
        "scenes": [],
        "props": [],
        "scene_description": "路人拦住阿离",
        "utterances": [{"kind": "dialogue", "speaker": "路人", "text": "借过。"}],
        "source_text": "路人拦住阿离。",
    }
    _write_plan(
        pm,
        "drama",
        {"title": "第一集", "scenes": [scene], "new_assets": [_new("路人", "skip", description="挑夫")]},
    )

    await _confirm(pm)

    assert "路人" not in pm.load_project("demo")["characters"]
    [formal_scene] = _formal(pm)["scenes"]
    assert formal_scene["characters_in_scene"] == ["阿离"]
    assert formal_scene["utterances"][0]["speaker"] == "路人"


async def test_a_skipped_reference_video_asset_becomes_plain_text_but_keeps_its_speaker_mark(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    _write_plan(
        pm,
        "reference_video",
        {
            "units": [
                {
                    "unit_id": "E1U01",
                    "text": "@[路人] 拦住 @[阿离]。\n@[路人]{借过。}",
                    "duration_seconds": 8,
                    "source_text": "路人拦住阿离。",
                }
            ],
            "new_assets": [_new("路人", "skip")],
        },
    )

    await _confirm(pm)

    [unit] = _formal(pm)["video_units"]
    assert unit["text"] == "路人 拦住 @[阿离]。\n@[路人]{借过。}"
    assert "路人" not in pm.load_project("demo")["characters"]


async def test_reference_video_mentions_follow_merges_and_derivatives(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    _write_plan(
        pm,
        "reference_video",
        {
            "units": [
                {
                    "unit_id": "E1U01",
                    "text": "@[将军] 看着 @[小离]。\n@[小离]{你回来了。}",
                    "duration_seconds": 8,
                    "source_text": "将军看着小离。",
                }
            ],
            "new_assets": [
                _new("将军", "merge", target="裴与"),
                _new("小离", "derivative", target="阿离", asset_name="幼年", description="六岁"),
            ],
        },
    )

    await _confirm(pm)

    [unit] = _formal(pm)["video_units"]
    assert unit["text"] == "@[裴与] 看着 @[阿离/幼年]。\n@[阿离]{你回来了。}"


async def test_reference_plan_recovers_and_confirms_with_the_same_asset_identities_without_regeneration(
    tmp_path: Path, monkeypatch, video_request_facts
) -> None:
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    project_path = pm.get_project_path("demo")
    source = project_path / "source" / "episode_1.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("小桃穿着雨衣等人。", encoding="utf-8")
    original_text = "@[桃桃] 穿着@[小桃/雨衣]等人。\n@[桃桃]{你回来了。}"
    fake = FakeTextGenerator(
        json.dumps(
            {
                "units": [{"text": original_text, "duration_seconds": 8, "source_text": "小桃穿着雨衣等人。"}],
                "new_assets": [
                    _new("小桃", "register", description="女童"),
                    _new("雨衣", "derivative", target="小桃", description="披着雨衣"),
                    _new("桃桃", "merge", target="不存在的人"),
                ],
            },
            ensure_ascii=False,
        )
    )
    use_text_generator(monkeypatch, fake.create)
    resolver = cast(ConfigResolver, FakeConfigResolver())

    with pytest.raises(TextGenerationError, match="new_asset_target_unresolved"):
        await generate_reference_script_plan(
            TextGenerationRequest(episode=1), project_name="demo", projects=pm, config_resolver=resolver
        )
    workflow = DraftWorkflow(DraftContext("demo", pm.data_root, pm, resolver))
    draft = await workflow.open(1, "reference_script_plan")
    assert [v["code"] for v in draft["violations"]] == ["new_asset_target_unresolved"]
    assert draft["content"]["units"][0]["text"] == original_text
    draft["content"]["new_assets"][2]["target"] = "小桃"

    repaired = await workflow.patch(1, "reference_script_plan", draft["content"], draft["revision"])
    await workflow.promote(1, "reference_script_plan", repaired["revision"])

    plan = json.loads((project_path / "drafts" / "episode_1" / _PLAN_FILES["reference_video"]).read_text())
    assert plan["units"][0]["text"] == original_text
    assert set(pm.load_project("demo")["characters"]) == {"阿离", "裴与"}
    await _confirm(pm)
    assert _formal(pm)["video_units"][0]["text"] == "@[小桃] 穿着@[小桃/雨衣]等人。\n@[小桃]{你回来了。}"
    characters = pm.load_project("demo")["characters"]
    assert set(characters) == {"阿离", "裴与", "小桃"}
    assert characters["小桃"]["derivatives"]["雨衣"]["description"] == "披着雨衣"
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "item",
    [
        _new("将军", "merge", target="不存在的人"),
        _new("少年", "derivative", target="不存在的人", description="十五岁"),
        {"type": "scene", "name": "旧宅", "decision": "derivative", "reason": "依据", "target": "阿离", "description": "旧宅变化"},
    ],
    ids=["unknown-merge-target", "unknown-derivative-base", "derivative-of-a-scene"],
)
async def test_an_unresolvable_decision_refuses_the_confirmation_untouched(
    tmp_path: Path, video_request_facts, item: dict[str, Any]
) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(pm, "narration", _narration_plan([], [item]))
    before = pm.load_project("demo")

    with pytest.raises(ScriptReviewError) as exc_info:
        await _confirm(pm)

    assert exc_info.value.code == "invalid_new_assets"
    assert pm.load_project("demo")["characters"] == before["characters"]
    assert not (pm.get_project_path("demo") / "scripts" / "episode_1.json").exists()


def _drama_plan(scene: dict[str, Any], new_assets: list[dict[str, Any]]) -> dict[str, Any]:
    base = {
        "scene_id": "E1S01",
        "duration_seconds": 8,
        "segment_break": False,
        "characters_in_scene": [],
        "scenes": [],
        "props": [],
        "scene_description": "画面",
        "utterances": [],
        "source_text": "原文。",
    }
    return {"title": "第一集", "scenes": [{**base, **scene}], "new_assets": new_assets}


def _assert_untouched(pm: ProjectManager, before: dict[str, Any]) -> None:
    after = pm.load_project("demo")
    assert after["characters"] == before["characters"]
    assert after.get("scenes", {}) == before.get("scenes", {})
    assert not (pm.get_project_path("demo") / "scripts" / "episode_1.json").exists()


async def test_a_reference_to_a_name_neither_registered_nor_registered_this_episode_refuses_the_confirmation(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(pm, "narration", _narration_plan(["裴与", "王五"], [_new("小桃", "register", description="女童")]))
    before = pm.load_project("demo")

    with pytest.raises(ScriptReviewError) as exc_info:
        await _confirm(pm)

    assert exc_info.value.code == "unregistered_references"
    assert "E1S01" in exc_info.value.message
    assert "王五" in exc_info.value.message
    assert "裴与" not in exc_info.value.message
    _assert_untouched(pm, before)


async def test_unknown_scene_and_prop_references_are_listed_per_item(tmp_path: Path, video_request_facts) -> None:
    pm = _project(tmp_path, "drama")
    _write_plan(pm, "drama", _drama_plan({"scenes": ["旧宅"], "props": ["铜镜"]}, []))
    before = pm.load_project("demo")

    with pytest.raises(ScriptReviewError) as exc_info:
        await _confirm(pm)

    assert exc_info.value.code == "unregistered_references"
    assert "旧宅" in exc_info.value.message
    assert "铜镜" in exc_info.value.message
    _assert_untouched(pm, before)


async def test_references_to_this_episodes_registered_new_assets_and_known_derivatives_confirm(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "drama")
    pm.update_project(
        "demo",
        lambda project: project["characters"]["阿离"].update(
            derivatives={"幼年": {"description": "六岁", "character_sheet": ""}}
        ),
    )
    _write_plan(
        pm,
        "drama",
        _drama_plan(
            {
                "characters_in_scene": ["阿离/幼年", "小桃", "将军"],
                "scenes": ["旧宅"],
                "utterances": [{"kind": "dialogue", "speaker": "店小二", "text": "客官里边请。"}],
            },
            [
                _new("小桃", "register", description="女童"),
                _new("将军", "merge", target="裴与"),
                {"type": "scene", "name": "旧宅", "decision": "register", "reason": "依据", "description": "破败院落"},
            ],
        ),
    )

    await _confirm(pm)

    [scene] = _formal(pm)["scenes"]
    assert scene["characters_in_scene"] == ["阿离/幼年", "小桃", "裴与"]
    assert scene["scenes"] == ["旧宅"]
    assert scene["utterances"][0]["speaker"] == "店小二"


async def test_a_derivative_the_character_does_not_have_refuses_the_confirmation(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "narration")
    _write_plan(pm, "narration", _narration_plan(["阿离/不存在"], []))

    with pytest.raises(ScriptReviewError) as exc_info:
        await _confirm(pm)

    assert exc_info.value.code == "unregistered_references"
    assert "阿离/不存在" in exc_info.value.message


async def test_a_reference_video_mention_of_an_unknown_name_refuses_the_confirmation(
    tmp_path: Path, video_request_facts
) -> None:
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    _write_plan(
        pm,
        "reference_video",
        {
            "units": [
                {
                    "unit_id": "E1U01",
                    "text": "@[王五] 拦住 @[阿离]。\n@[阿离]{借过。}",
                    "duration_seconds": 8,
                    "source_text": "王五拦住阿离。",
                }
            ],
        },
    )
    before = pm.load_project("demo")

    with pytest.raises(ScriptReviewError) as exc_info:
        await _confirm(pm)

    assert exc_info.value.code == "unregistered_references"
    assert "E1U01" in exc_info.value.message
    assert "王五" in exc_info.value.message
    _assert_untouched(pm, before)


def _review_fingerprint(pm):
    from lib.script import script_review
    path = script_review.script_plan_path(pm.get_project_path("demo"), pm.load_project("demo"), 1)
    return script_review.content_fingerprint(path) if path is not None else None


@pytest.mark.parametrize("response", [
    '{"units": []}',
    '{"units": [{"duration_seconds": 8, "source_text": "原文", "text": 42}]}',
    '{"units": [{"duration_seconds": 8, "source_text": "原文", "text": "门口。"}], "new_assets": [{"type":"character","name":"小桃","decision":"register","reason":"人物","description":""}]}',
    '{"units": [], "units": []}',
    '{"units": [',
    '{"units": [{"duration_seconds": 8, "source_span_start": "S9999", "source_span_end": "S0001", "text": "@[阿离]等候。"}]}',
    '{"units": [{"duration_seconds": 8, "source_span_start": "S0002", "source_span_end": "S0001", "text": "@[阿离]等候。"}]}',
])
async def test_failed_candidate_survives_reload_and_manual_repair_without_model_retry(tmp_path, monkeypatch, video_request_facts, response):
    from lib.script.draft_quarantine import QUARANTINE_KIND_SCRIPT_PLAN, read_quarantine
    from server.services.project.episode_drafts import EpisodeDraftService

    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "false")
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    _write_plan(pm, "reference_video", {"units": [{"unit_id": "E1U01", "text": "@[阿离]等候。", "duration_seconds": 8, "source_text": "原文"}]})
    root = pm.get_project_path("demo")
    (root / "source/episode_1.txt").write_text("阿离等候。裴与进门。", encoding="utf-8")
    original = (root / "drafts/episode_1/script_plan_reference_units.json").read_bytes()
    fake = FakeTextGenerator(response)
    use_text_generator(monkeypatch, fake.create)
    resolver = cast(ConfigResolver, FakeConfigResolver())
    with pytest.raises(TextGenerationError):
        await generate_reference_script_plan(TextGenerationRequest(episode=1), project_name="demo", projects=pm, config_resolver=resolver)
    persisted = read_quarantine(root, 1, QUARANTINE_KIND_SCRIPT_PLAN)
    assert persisted is not None
    assert persisted.meta["raw_response"] == response
    service = EpisodeDraftService(ProjectManager(pm.data_root), config_resolver=resolver)
    view = await service.get_draft("demo", 1, "reference_script_plan")
    second = await service.get_draft("demo", 1, "reference_script_plan")
    assert second["revision"] == view["revision"]
    assert second["violations"] == view["violations"]
    assert (root / "drafts/episode_1/script_plan_reference_units.json").read_bytes() == original
    repaired = {"units": [{"text": "@[阿离]等候。", "duration_seconds": 8, "source_text": "阿离等候。"}], "new_assets": []}
    result = await service.save_draft("demo", 1, "reference_script_plan", repaired, view["revision"])
    assert result["adopted"] is True
    await _confirm(pm)
    assert _formal(pm)["video_units"][0]["text"] == repaired["units"][0]["text"]
    assert len(fake.requests) == 1


@pytest.mark.parametrize("content_validation", ["true", "false"])
async def test_planned_reference_uses_image_tier_before_images_and_limits_before_confirmation(tmp_path, monkeypatch, set_video_request_facts, content_validation):
    from server.text_generation import collect_reference_flat_violations, fetch_reference_split_caps
    from tests.factories import make_video_request_facts

    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", content_validation)
    set_video_request_facts({
        "r2v": make_video_request_facts(route="reference_video", generation_type="r2v", allowed_durations=(8,), max_reference_images=1),
        "i2v": make_video_request_facts(route="reference_video", generation_type="i2v", allowed_durations=(4, 6, 8)),
    })
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    project = pm.load_project("demo")
    caps = await fetch_reference_split_caps(project, config_resolver=cast(ConfigResolver, FakeConfigResolver()))
    unit = {"text": "@[阿离]等候。", "duration_seconds": 4, "source_text": "原文"}
    problems = collect_reference_flat_violations([unit], project, project_path=pm.get_project_path("demo"), episode=1, novel_text="原文", caps=caps, source_language="zh", new_assets=[])
    assert [v.code for v in problems] == ["duration_off_tier"]
    unit["duration_seconds"] = 8
    assert not collect_reference_flat_violations([unit], project, project_path=pm.get_project_path("demo"), episode=1, novel_text="原文", caps=caps, source_language="zh", new_assets=[])
    unit["text"] = "@[阿离]看着@[裴与]。"
    _write_plan(pm, "reference_video", {"units": [{"unit_id": "E1U01", **unit}]})
    with pytest.raises(ScriptReviewError, match="参考图数"):
        await _confirm(pm)
    assert not (pm.get_project_path("demo") / "scripts/episode_1.json").exists()


async def test_revising_confirmed_plan_preserves_formal_until_explicit_reconfirmation(tmp_path, monkeypatch, video_request_facts):
    from server.services.project.episode_drafts import EpisodeDraftService

    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "false")
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    _write_plan(pm, "reference_video", {"units": [{"unit_id": "E1U01", "text": "@[阿离]等候。", "duration_seconds": 8, "source_text": "原文"}]})
    await _confirm(pm)
    formal = _formal(pm)
    resolver = cast(ConfigResolver, FakeConfigResolver())
    review = ScriptReviewService(pm, config_resolver=resolver)
    fingerprint = _review_fingerprint(pm)
    await review.start_revision("demo", 1, fingerprint)
    drafts = EpisodeDraftService(pm, config_resolver=resolver)
    view = await drafts.get_draft("demo", 1, "reference_script_plan")
    view["content"]["units"][0]["text"] = "@[阿离]转身。"
    assert (await drafts.save_draft("demo", 1, "reference_script_plan", view["content"], view["revision"]))["adopted"]
    assert _formal(pm) == formal
    state = await review.get_state("demo", 1)
    assert state["status"] == "pending_review"
    with pytest.raises(ScriptReviewError) as error:
        await review.confirm("demo", 1, plan_fingerprint=state["fingerprint"])
    assert error.value.code == "overwrite_required"
    assert _formal(pm) == formal
    await review.confirm("demo", 1, plan_fingerprint=state["fingerprint"], overwrite_revision=state["script_overwrite"]["revision"])
    assert _formal(pm)["video_units"][0]["text"] == "@[阿离]转身。"


@pytest.mark.parametrize("batch", [False, True])
async def test_manual_prompt_survives_save_registration_preview_and_task(
    tmp_path, monkeypatch, video_request_facts, session_factory, batch
):
    from lib.artifacts.generation_input import asset_sheet_input, project_input_observation
    from lib.db.base import DEFAULT_USER_ID
    from lib.generation.generation_queue import GenerationQueue
    from server.services.admission.asset_sheet_batch import (
        AssetSheetScope,
        load_asset_sheet_batch_plan,
        submit_asset_sheet_batch,
    )
    from server.services.tasks import generation_tasks
    from tests.integration.server.services.tasks.generation_tasks_support import FakeGenerator, fake_resolve_ctx

    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "false")
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    content = {"units": [{"unit_id": "E1U01", "duration_seconds": 8, "source_text": "原文", "text": "@[小桃] 等候。"}]}
    _write_plan(pm, "reference_video", content)
    service = ScriptReviewService(pm, config_resolver=cast(ConfigResolver, FakeConfigResolver()))
    custom = "完整人工提示词：四格同一少女，蓝衣，白底；Avoid: 文字。"
    content["new_assets"] = [_new("小桃", "register", description="", generation_prompt=custom)]
    saved = await service.save_content("demo", 1, content, _review_fingerprint(pm))
    assert saved["content"]["new_assets"][0]["description"] == ""
    await service.confirm("demo", 1, plan_fingerprint=saved["fingerprint"])
    project = pm.load_project("demo")
    assert project["characters"]["小桃"]["generation_prompt"] == custom
    assert project["characters"]["小桃"]["description"] == ""
    project_path = pm.get_project_path("demo")
    preview = asset_sheet_input(project, asset_type="character", name="小桃", observation=project_input_observation(project_path))
    assert preview.refusal is None
    assert preview.render(max_reference_images=0, model="").prompt == custom
    planning_context = await generate_reference_script_plan(TextGenerationRequest(episode=1, dry_run=True), project_name="demo", projects=pm, config_resolver=cast(ConfigResolver, FakeConfigResolver()))
    assert custom in planning_context.message
    assert "当前人工完整生图提示词" in planning_context.message
    generator = FakeGenerator(project_path)
    use_project_manager(monkeypatch, pm)
    use_generation_context(monkeypatch, fake_resolve_ctx(generator))
    payload = {}
    if batch:
        queue = GenerationQueue(session_factory=session_factory, project_manager=pm)
        await queue.acquire_or_renew_worker_lease(name="default", owner_id="test", ttl_seconds=60)
        _project_view, plan = await load_asset_sheet_batch_plan(pm, queue, project_name="demo", scope=AssetSheetScope(episode_id=1), user_id=DEFAULT_USER_ID)
        assert plan.target_ids == ["character/小桃"]
        assert plan.missing_description_ids == []
        _batch, submitted = await submit_asset_sheet_batch(plan, project_name="demo", queue=queue, source="webui", user_id=DEFAULT_USER_ID)
        assert len(submitted) == 1
        task = await queue.claim_next_task("image")
        assert task is not None
        payload = task["payload"]
    assert generator.image_calls == []  # 保存、确认、预览和入队没有调用供应商。
    await generation_tasks.execute_asset_sheet_task("character", "demo", "小桃", payload)
    assert len(generator.image_calls) == 1
    assert generator.image_calls[0]["prompt"] == custom


async def test_generation_cannot_use_manual_prompt_exception(tmp_path, monkeypatch, video_request_facts):
    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "false")
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    _write_plan(pm, "reference_video", {"units": [{"unit_id": "E1U01", "duration_seconds": 8, "source_text": "原文", "text": "门开了。"}]})
    response = {"units": [{"duration_seconds": 8, "source_text": "原文", "text": "@[小桃] 等候。"}], "new_assets": [_new("小桃", "register", generation_prompt="完整人工例外不适用于模型输出") ]}
    fake = FakeTextGenerator(json.dumps(response, ensure_ascii=False))
    async def create(*_args, **_kwargs):
        return fake
    use_text_generator(monkeypatch, create)
    with pytest.raises(TextGenerationError, match="new_asset_description_required"):
        await generate_reference_script_plan(TextGenerationRequest(episode=1), project_name="demo", projects=pm, config_resolver=cast(ConfigResolver, FakeConfigResolver()))
    view = await ScriptReviewService(pm, config_resolver=cast(ConfigResolver, FakeConfigResolver())).get_quarantine_info("demo", 1)
    assert view is not None
    assert [v["code"] for v in view["violations"]] == ["new_asset_description_required"]
    assert "小桃" not in pm.load_project("demo")["characters"]
    assert len(fake.requests) == 1


async def test_saving_a_dangling_dependency_is_rejected_without_writes(tmp_path, video_request_facts):
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    content = {"units": [{"unit_id": "E1U01", "duration_seconds": 8, "text": "门开了。"}]}
    _write_plan(pm, "reference_video", content)
    before = (pm.get_project_path("demo") / "drafts/episode_1/script_plan_reference_units.json").read_bytes()
    content["new_assets"] = [_new("雨衣", "derivative", target="已删除的本体", description="换上雨衣")]
    with pytest.raises(ScriptReviewError, match="本体"):
        await ScriptReviewService(pm).save_content("demo", 1, content, _review_fingerprint(pm))
    assert (pm.get_project_path("demo") / "drafts/episode_1/script_plan_reference_units.json").read_bytes() == before


async def test_same_shape_name_on_two_characters_requires_distinct_temporary_names(tmp_path, video_request_facts):
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    content = {"units": [{"unit_id": "E1U01", "duration_seconds": 8, "text": "@[青衣雨装]与@[银甲雨装]等候。"}],
               "new_assets": [_new("雨衣", "derivative", target="阿离", description="青色雨衣"),
                              _new("雨衣", "derivative", target="裴与", description="黑色雨衣")]}
    _write_plan(pm, "reference_video", content)
    before = pm.load_project("demo")
    with pytest.raises(ScriptReviewError, match="临时名称"):
        await _confirm(pm)
    assert pm.load_project("demo") == before
    content["new_assets"][0].update(name="青衣雨装", asset_name="雨衣")
    content["new_assets"][1].update(name="银甲雨装", asset_name="雨衣")
    _write_plan(pm, "reference_video", content)
    await _confirm(pm)
    characters = pm.load_project("demo")["characters"]
    assert characters["阿离"]["derivatives"]["雨衣"]["description"] == "青色雨衣"
    assert characters["裴与"]["derivatives"]["雨衣"]["description"] == "黑色雨衣"
    assert _formal(pm)["video_units"][0]["text"] == "@[阿离/雨衣]与@[裴与/雨衣]等候。"


async def test_late_structural_failure_cannot_replace_newer_candidate(tmp_path, monkeypatch, video_request_facts):
    import asyncio

    from lib.backends.text_backends.base import TextGenerationResult
    from lib.script.draft_quarantine import QUARANTINE_KIND_SCRIPT_PLAN, quarantine_path, write_quarantine
    pm = _project(tmp_path, "drama", generation_mode="reference_video")
    content = {"units": [{"unit_id": "E1U01", "duration_seconds": 8, "text": "门开了。"}]}
    _write_plan(pm, "reference_video", content)
    path = quarantine_path(pm.get_project_path("demo"), 1, QUARANTINE_KIND_SCRIPT_PLAN)
    calls = []
    newer = []
    class LateGenerator:
        async def generate(self, request, **kwargs):
            calls.append(request)
            def write_newer():
                write_quarantine(pm.get_project_path("demo"), 1, QUARANTINE_KIND_SCRIPT_PLAN, content={"units": [], "marker": "newer"}, violations=[], meta={"source": None})
                newer.append(path.read_bytes())
            await asyncio.to_thread(write_newer)
            return TextGenerationResult(text='{"units": [', provider="fake", model="fake")
    async def create(*args, **kwargs):
        return LateGenerator()
    use_text_generator(monkeypatch, create)
    with pytest.raises(TextGenerationError, match="draft_revision_conflict"):
        await generate_reference_script_plan(TextGenerationRequest(episode=1), project_name="demo", projects=pm, config_resolver=cast(ConfigResolver, FakeConfigResolver()))
    assert path.read_bytes() == newer[0]
    assert len(calls) == 1
    assert json.loads((pm.get_project_path("demo") / "drafts/episode_1/script_plan_reference_units.json").read_text()) == content
