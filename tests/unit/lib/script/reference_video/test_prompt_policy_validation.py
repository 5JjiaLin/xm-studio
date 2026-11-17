import pytest

from lib.script.reference_video.prompt_policy_validation import (
    INHERIT_HANDOFF,
    compile_policy_handoffs,
    validate_policy_asset_descriptions,
    validate_policy_handoffs,
    validate_policy_text,
)


def _text(opening: str, ending: str, relation: str = "连续") -> str:
    return "\n".join(
        [
            f"【本组设定】\n叙事关系：{relation}\n场景。",
            f"【开场承接】\n{opening}",
            "【分段时间轴】\n【0-8s】\n动作。",
            f"【组尾交接】\n{ending}",
            "【必要限制】\n无。",
        ]
    )


def test_template_only_mode_keeps_handoff_filling_without_rejecting_content(monkeypatch):
    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "false")
    assert validate_policy_asset_descriptions([
        {"name": "木箱", "type": "prop", "decision": "register", "description": "旧木箱，尺寸待确认。"},
    ]) == []
    assert validate_policy_text("2", "自由分镜文字", required_source_text="@[木箱]") == []
    previous = {"unit_id": "1", "prompt_policy_version": "drama_detail_v1",
                "text": _text("起点。", "<A>在左侧。", "首组")}
    following = {"unit_id": "2", "prompt_policy_version": "drama_detail_v1",
                 "text": _text("<A>在右侧。", "终点。")}
    assert compile_policy_handoffs([following], [previous, following]) == [following]
    assert validate_policy_handoffs([following], [previous, following]) == []
    placeholder = {**following, "text": _text(INHERIT_HANDOFF, "终点。")}
    (compiled,) = compile_policy_handoffs([placeholder], [previous, placeholder])
    assert "【开场承接】\n<A>在左侧。\n" in compiled["text"]
    assert INHERIT_HANDOFF not in compiled["text"]


def test_state_sections_reject_bindings_and_require_each_section_once():
    text = _text("@[A] 在左侧。", "A 站定。")
    assert {issue.code for issue in validate_policy_text("E1U1", text)} == {"prompt_policy_state_binding"}


def test_existing_v1_without_narrative_relation_stays_readable():
    text = _text("起点。", "终点.").replace("叙事关系：连续", "场景描述里提到换场")

    issues = validate_policy_text("E1U1", text)

    assert issues == []


def test_explicit_narrative_relation_rejects_invalid_enum():
    text = _text("起点。", "终点。", "没有换场")

    issues = validate_policy_text("E1U1", text)

    assert "prompt_policy_narrative_relation" in {issue.code for issue in issues}


def test_first_existing_v1_without_narrative_relation_does_not_require_migration():
    text = _text("起点。", "终点。").replace("叙事关系：连续\n", "")
    unit = {"unit_id": "E1U1", "prompt_policy_version": "drama_detail_v1", "text": text}

    assert validate_policy_handoffs([unit], [unit]) == []


def test_required_asset_mentions_are_reported_instead_of_appended_as_a_list():
    source = "@[黑色石殿] 内，@[吉迪恩] 推开 @[旧木门]。@[吉迪恩]{Come here.}"
    authored = "【本组设定】\n石殿内，吉迪恩推开木门。\n【开场承接】\n<吉迪恩>站在门边。"

    issues = validate_policy_text("E1U1", authored, required_source_text=source)

    assert "prompt_policy_asset_reference_missing" in {issue.code for issue in issues}
    assert "本组引用资产" not in authored


def test_required_asset_mentions_fail_validation_when_setup_section_is_missing():
    source = "@[酒馆] 内 @[主角] 等待。"
    authored = "【分段时间轴】\n主角等待。"

    issues = validate_policy_text("E1U1", authored, required_source_text=source)

    assert "prompt_policy_asset_reference_missing" in {issue.code for issue in issues}


def test_registered_prop_requires_a_measurable_scale_description():
    assets = [
        {"type": "prop", "decision": "register", "name": "旧木箱", "description": "破旧木箱，边角磨损。"},
        {"type": "prop", "decision": "register", "name": "银针", "description": "約12厘米长的银针，直径约3毫米。"},
        {"type": "prop", "decision": "register", "name": "不明器物", "description": "轮廓待确认的石质物件。", "reason": "尺寸无法合理推断，待确认"},
        {"type": "prop", "decision": "merge", "name": "旧箱", "description": ""},
    ]

    issues = validate_policy_asset_descriptions(assets)

    assert [(issue.label, issue.code) for issue in issues] == [
        ("new_assets[0]", "prompt_policy_prop_scale_missing"),
        ("new_assets[2]", "prompt_policy_asset_design_incomplete"),
        ("new_assets[2]", "prompt_policy_prop_scale_missing"),
    ]
    assert all(issue.item_index is None for issue in issues)  # Asset indices must not address video units.


def test_opted_in_adjacent_handoff_requires_matching_opening():
    context = [
        {"unit_id": "E1U1", "prompt_policy_version": "drama_detail_v1", "text": _text("起点。", "A 左 B 右。")},
        {"unit_id": "E1U2", "prompt_policy_version": "drama_detail_v1", "text": _text("A 右 B 左。", "终点。")},
    ]
    issues = validate_policy_handoffs([context[1]], context)
    assert len(issues) == 1
    assert issues[0].code == "prompt_policy_handoff_mismatch"


def test_arbitrary_discontinuity_word_does_not_skip_handoff_validation():
    context = [
        {"unit_id": "E1U1", "prompt_policy_version": "drama_detail_v1", "text": _text("起点。", "A 左 B 右。", "首组")},
        {"unit_id": "E1U2", "prompt_policy_version": "drama_detail_v1", "text": _text("没有换场，A 仍在右侧。", "终点。")},
    ]

    issues = validate_policy_handoffs([context[1]], context)

    assert len(issues) == 1
    assert issues[0].code == "prompt_policy_handoff_mismatch"


def test_non_contiguous_units_only_compare_immediate_predecessor():
    context = [
        {"unit_id": "E1U1", "prompt_policy_version": "drama_detail_v1", "text": _text("起点。", "A 左 B 右。")},
        {"unit_id": "E1U2", "prompt_policy_version": None, "text": "legacy"},
        {"unit_id": "E1U3", "prompt_policy_version": "drama_detail_v1", "text": _text("新场景。", "B 离开。", "换场")},
    ]
    assert validate_policy_handoffs([context[2]], context) == []


@pytest.mark.parametrize(("name", "description", "reason", "valid"), [
    ("木箱", "长40厘米、宽25厘米、高30厘米，木质箱体、铁质合页。", "设计约数", True),
    ("木箱", "40×25×30cm，旧木质。", "设计约数", True),
    ("木箱", "长40厘米，掌宽。", "", False),
    ("银针", "总长12厘米，针杆直径3毫米，银质。", "设计约数", True),
    ("银针", "约一掌长，纤细银针。", "", False),
    ("黑雾", "流动半透明黑雾。", "无固定形体，非实体能量", True),
])
def test_prop_dimensions_fit_the_shape(name, description, reason, valid):
    issues = validate_policy_asset_descriptions([{
        "type": "prop", "decision": "register", "name": name,
        "description": description, "reason": reason,
    }])
    assert (not issues) == valid


def test_character_design_can_be_completed_artistically_without_pending_placeholders():
    complete = "约35岁男性，宽肩健硕、腰身利落，颧骨清晰、血红眼睛、硬朗下颌，浅色皮肤保留毛孔，黑色短发后梳，黑呢长款军大衣带银扣，黑皮手套与皮鞋。"
    for description, expected in [(complete, set()), ("刀削般面孔，穿黑色军大衣。", {"prompt_policy_character_design_incomplete"})]:
        issues = validate_policy_asset_descriptions([{
            "type": "character", "decision": "register", "name": "吉迪恩",
            "description": description, "reason": "视觉年龄、肤色、发型及服装细节为设计补全",
        }])
        assert {issue.code for issue in issues} == expected


@pytest.mark.parametrize("description", [
    "体长约3.2米、肩高约1.5米的巨型四足狼形魔兽，肩胛高耸、胸腔宽阔、四肢粗壮；"
    "狭长狼头，三角耳直立，深陷眼窝与暗红眼睛，吻部宽而厚，血红利口内露出两排尖锐獠牙；"
    "粗长尾巴垂向地面；全身覆盖粗硬黑色短毛，黑紫色黏液沿毛束结成黏稠条块，"
    "肩背与腹部的积液颜色更深，爪垫和獠牙为暗灰材质。",
    "成年乌鸦，身体纤细，双翼收拢，尖长鸟喙与圆形黑眼；全身黑色羽毛，翼尖羽片呈蓝紫金属光泽，尾羽呈扇形。",
    "长约2米的蛇形灵兽，躯干细长盘成两圈，三角头与琥珀色竖瞳，青色细密鳞片覆盖全身，背部有白色菱形花纹。",
    "高约1.8米的机器人，窄腰宽肩，球形头部装有圆形蓝色传感器，银灰金属外壳有磨砂肌理，头顶两根短触角，肘部关节呈黑色环状。",
    "直径约1米的不规则球状雾灵，内部由三层半透明薄雾环绕明亮核心，外缘伸出细长丝状触须，淡蓝色渐变为白色，边缘光晕柔和。",
])
@pytest.mark.parametrize("reason", ["", "原文角色，细节为设计补全。"])
def test_nonhuman_design_uses_visible_anatomy_without_magic_reason_words(description, reason):
    # The display name is intentionally uninformative: morphology is not a name whitelist.
    asset = {"type": "character", "decision": "register", "name": "角色甲",
             "description": description, "reason": reason}
    assert validate_policy_asset_descriptions([asset]) == []


@pytest.mark.parametrize("description", ["一只黑狼。", "银色机器人。", "刀削般面孔，穿黑色军大衣。"])
@pytest.mark.parametrize("reason", ["", "非人形", "无固定形体"])
def test_reason_cannot_exempt_an_unfinished_character_design(description, reason):
    issues = validate_policy_asset_descriptions([{
        "type": "character", "decision": "register", "name": "角色甲",
        "description": description, "reason": reason,
    }])
    assert {issue.code for issue in issues} == {"prompt_policy_character_design_incomplete"}


def test_setup_asset_list_is_not_a_timed_action_and_angle_is_same_identity():
    text = _text("A站定。", "A站定。", "首组").replace("场景。", "@[石殿] @[A] @[银针]")
    text = text.replace("动作。", "@[A/视角-正面特写] 取出 @[银针]。")
    assert validate_policy_text("U1", text, required_source_text="@[石殿] @[A] @[银针]", scene_names=["石殿"]) == []
    missing = text.replace("@[银针]。", "银针。")
    assert "prompt_policy_asset_reference_missing" in {v.code for v in validate_policy_text("U1", missing, required_source_text="@[银针]")}
    assert "prompt_policy_asset_reference_missing" in {v.code for v in validate_policy_text("U1", text, required_source_text="@[A/战斗装]")}


def _unit(uid, text, **extra):
    return {"unit_id": uid, "prompt_policy_version": "drama_detail_v1", "text": text, **extra}


def test_compiler_passes_tail_in_order_across_batch_and_manual_predecessor():
    first = _unit("U1", _text("起点。", "旧尾态。", "首组"), generation_prompt=_text("起点。", "<A>在画面左侧，<B>在画面右侧；A持木箱。", "首组"))
    second = _unit("U2", _text(INHERIT_HANDOFF, "A放下木箱。"))
    third = _unit("U3", _text(INHERIT_HANDOFF, "A离开。"))
    result = compile_policy_handoffs([third, second], [first, second, third])
    by_id = {item["unit_id"]: item for item in result}
    assert "A持木箱。" in by_id["U2"]["text"]
    assert "A放下木箱。" in by_id["U3"]["text"]
    assert validate_policy_handoffs(result, [first, second, third]) == []
    assert INHERIT_HANDOFF in second["text"]  # Input snapshots are immutable.


def test_compiler_never_overwrites_same_sides_with_different_holding_state_or_manual_body():
    first = _unit("U1", _text("起点。", "<A>在画面左侧，手持木箱。", "首组"))
    text = _text("<A>在画面左侧，双手空着。", "结束。")
    second = _unit("U2", text)
    assert compile_policy_handoffs([second], [first, second]) == [second]
    assert validate_policy_handoffs([second], [first, second])
    manual = _unit("U2", _text(INHERIT_HANDOFF, "结束。"), generation_prompt=text)
    assert compile_policy_handoffs([manual], [first, manual]) == [manual]
    assert validate_policy_handoffs([manual], [first, manual])


def test_explicit_start_position_conflict_is_not_hidden_by_the_inheritance_slot():
    first = _unit("U1", _text("起点。", "<A>在画面左侧。", "首组"))
    second = _unit("U2", _text(INHERIT_HANDOFF, "结束。").replace("动作。", "@[A]在画面右侧站立。"))
    with pytest.raises(Exception, match="左右位置"):
        compile_policy_handoffs([second], [first, second])


def test_changing_manual_tail_flags_the_already_authored_neighbour_without_rewriting():
    first = _unit("U1", _text("起点。", "A站定。", "首组"), generation_prompt=_text("起点。", "A离开。", "首组"))
    second = _unit("U2", _text("A站定。", "B站定。"))
    issues = validate_policy_handoffs([first], [first, second])
    assert len(issues) == 1
    assert issues[0].label == "unit U2"
    assert "A站定。" in second["text"]
