"""Archive isolation without deleting original scripts, assets, or media."""

import json

import pytest

from lib.artifacts.artifact_provenance import build_script_plan_request
from lib.episode.episode_management import create_episode
from lib.infra.json_io import atomic_write_json
from lib.project.archive_context import generation_project
from lib.project.production_management import set_archive
from lib.project.project_manager import ProjectManager
from lib.script.plan_new_assets import NewAssetsError, resolve_new_assets
from server.text_generation import _load_project_context, load_script_plan_source_with_basis


@pytest.fixture
def archived_project(tmp_path):
    pm = ProjectManager(tmp_path / "data")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Archive regression", "Anime", "drama")
    old = create_episode(pm, "demo", title="旧第一集", source_text="旧内容", source_kind="screenplay")
    active = create_episode(pm, "demo", title="新第一集", source_text="新内容", source_kind="screenplay")
    for name in ("旧主角", "共用角色", "手动角色"):
        pm.add_character("demo", name, f"{name}的原描述")
    pm.add_project_scene("demo", "旧场景", "旧场景外观")
    pm.add_prop("demo", "旧道具", "旧道具外观")
    path = pm.get_project_path("demo")
    atomic_write_json(path / "scripts" / f"episode_{old}.json", {
        "video_units": [{"text": "@[旧主角]在@[旧场景]拿着@[旧道具]与@[共用角色]交谈。"}],
    })
    atomic_write_json(path / "scripts" / f"episode_{active}.json", {
        "scenes": [{"characters_in_scene": ["共用角色"], "scenes": [], "props": []}],
    })
    set_archive(pm, "demo", True, old)
    return pm, path, old, active


def test_existing_archive_is_excluded_shared_assets_preserved_and_restore_is_reversible(archived_project):
    pm, path, old, active = archived_project
    before = pm.load_project("demo")
    files = {p: p.read_bytes() for p in (path / "scripts").glob("*.json")}
    view = generation_project(path, before, episode=active)
    assert set(view["characters"]) == {"共用角色", "手动角色"}
    assert not view["scenes"]
    assert not view["props"]
    assert generation_project(path, before, episode=old) == before
    assert pm.load_project("demo") == before
    assert all(p.read_bytes() == data for p, data in files.items())
    set_archive(pm, "demo", False, old)
    assert generation_project(path, pm.load_project("demo"))["characters"] == before["characters"]


def test_prompt_request_and_basis_use_identical_isolated_inputs(archived_project):
    pm, path, _, active = archived_project
    raw = pm.load_project("demo")
    # An archived next episode must not be fed into the new episode's outline.
    raw["episodes"].reverse()
    source, inputs, basis = load_script_plan_source_with_basis(path, None, raw, active, "drama")
    assert source == "新内容"
    assert "旧主角" not in inputs["characters"]
    assert inputs["next_episode_outline"] is None
    _, expected = build_script_plan_request(source, episode=active, project=generation_project(path, raw), expected_variant="drama")
    assert basis == expected
    assert "旧主角" not in _load_project_context(pm, "demo")[1]["characters"]


def test_replacement_same_name_gets_fresh_asset_and_keeps_archived_description(archived_project):
    pm, path, _, active = archived_project
    new = [{"type": "character", "name": "旧主角", "decision": "register", "description": "全新的外观", "reason": "新剧本"}]
    plan = path / "drafts" / f"episode_{active}" / "script_plan_normalized_script.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(plan, {"scenes": [{"characters_in_scene": ["旧主角"]}], "new_assets": new})
    raw = pm.load_project("demo")
    view = generation_project(path, raw)
    assert "旧主角" not in view["characters"]
    resolution = resolve_new_assets(view, new)
    rewritten = resolution.rewrite_entries([{"text": "@[旧主角]：你好。", "characters_in_scene": ["旧主角"]}])
    replacement = rewritten[0]["characters_in_scene"][0]
    assert replacement != "旧主角"
    assert f"@[{replacement}]" in rewritten[0]["text"]
    resolution.apply_to_project(raw)
    assert raw["characters"]["旧主角"]["description"] == "旧主角的原描述"
    assert raw["characters"][replacement]["description"] == "全新的外观"
    # Revalidation of the same plan must keep its new identity, not allocate another.
    repeat = resolve_new_assets(generation_project(path, raw), new)
    assert repeat.rewrite_entries([{"characters_in_scene": ["旧主角"]}])[0]["characters_in_scene"] == [replacement]
    assert not repeat.registrations


def test_merge_cannot_silently_target_archived_asset(archived_project):
    pm, path, _, _ = archived_project
    with pytest.raises(NewAssetsError, match="不是已登记"):
        resolve_new_assets(generation_project(path, pm.load_project("demo")), [
            {"type": "character", "name": "新称呼", "decision": "merge", "target": "旧主角", "reason": "合并"},
        ])


def test_temporary_aliases_for_same_new_registration_still_deduplicate(archived_project):
    pm, path, _, _ = archived_project
    view = generation_project(path, pm.load_project("demo"))
    resolution = resolve_new_assets(view, [
        {"type": "character", "name": alias, "asset_name": "旧主角", "decision": "register", "description": "新外观", "reason": "新稿"}
        for alias in ("称呼一", "称呼二")
    ])
    assert len(resolution.registrations["character"]) == 1
    rewritten = resolution.rewrite_entries([{"characters_in_scene": ["称呼一", "称呼二"]}])
    assert len(rewritten[0]["characters_in_scene"]) == 1


def test_concurrent_replacement_registrations_do_not_silently_merge(archived_project):
    pm, path, _, _ = archived_project
    raw = pm.load_project("demo")
    view = generation_project(path, raw)
    decisions = [
        [{"type": "character", "name": "旧主角", "decision": "register", "description": description, "reason": "新稿"}]
        for description in ("第一台电脑的外观", "第二台电脑的外观")
    ]
    first, second = [resolve_new_assets(view, items) for items in decisions]
    first.apply_to_project(raw)
    with pytest.raises(ValueError, match="被其他任务占用"):
        second.apply_to_project(raw)
    retry = resolve_new_assets(generation_project(path, raw), decisions[1])
    retry.apply_to_project(raw)
    assert {entry["description"] for entry in raw["characters"].values()}.issuperset({"第一台电脑的外观", "第二台电脑的外观"})


def test_sharing_updates_and_unreadable_files_are_not_silently_ignored(archived_project):
    pm, path, _, active = archived_project
    script = path / "scripts" / f"episode_{active}.json"
    atomic_write_json(script, {"video_units": [{"text": "@[旧主角/战斗形态]出现"}]})
    assert "旧主角" in generation_project(path, pm.load_project("demo"))["characters"]
    script.write_text("{invalid", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        generation_project(path, pm.load_project("demo"))


def test_archived_derivative_isolated_even_when_base_character_is_shared(archived_project):
    pm, path, old, _ = archived_project
    raw = pm.load_project("demo")
    raw["characters"]["共用角色"]["derivatives"] = {"旧形态": {"description": "旧外观"}}
    atomic_write_json(path / "scripts" / f"episode_{old}.json", {"video_units": [{"text": "@[共用角色/旧形态]出现"}]})
    view = generation_project(path, raw)
    assert view["characters"]["共用角色"]["derivatives"] == {}
    new = [{"type": "character", "name": "旧形态", "decision": "derivative", "target": "共用角色", "description": "新外观", "reason": "重新设计"}]
    resolution = resolve_new_assets(view, new)
    resolution.apply_to_project(raw)
    assert raw["characters"]["共用角色"]["derivatives"]["旧形态"]["description"] == "旧外观"
    assert len(raw["characters"]["共用角色"]["derivatives"]) == 2
    again = resolve_new_assets(generation_project(path, raw), new)
    assert not again.derivatives
