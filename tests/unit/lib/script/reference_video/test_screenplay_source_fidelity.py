import pytest

from lib.script.reference_video.screenplay_coverage import (
    screenplay_body_start,
    screenplay_dialogue,
    screenplay_source_dialogue,
    screenplay_speaker_aliases,
    validate_screenplay_coverage,
)

SCRIPT = """Episode 1

1-1 NIGHT — INT. OBSIDIAN HALL

人物：吉迪恩、埃利亚斯

【场景：黑色石殿，中央一座大玉台】

△吉迪恩转头，解开军大衣银扣。

GIDEON: Come here.

△埃利亚斯双脚铁链撞响。

ELIAS (to Gideon): Lord Gideon, you actually kidnapped a Star Mark wielder by force

△银色纹饰针从埃利亚斯身上掉落。

GIDEON (cold and lethal): Tonight, you shall nevere scapeme for a slong as you live.

△埃利亚斯瘫坐，盯着地上的银色纹饰针。
"""


def test_extracts_screenplay_dialogue_verbatim():
    assert screenplay_dialogue(SCRIPT) == [
        ("GIDEON", "Come here."),
        ("ELIAS", "Lord Gideon, you actually kidnapped a Star Mark wielder by force"),
        ("GIDEON", "Tonight, you shall nevere scapeme for a slong as you live."),
    ]


@pytest.mark.parametrize(
    "cue",
    ["林铮os：我不会退。", "林铮 OS: 我不会退。", "林铮（OS）：我不会退。", "林铮（画外音）：我不会退。"],
)
def test_extracts_chinese_os_and_voiceover_cues_without_changing_speaker(cue):
    assert screenplay_dialogue(cue) == [("林铮", "我不会退。")]


@pytest.mark.parametrize(
    "heading",
    ["4-1掌剑峰柴房日外", "4-2 掌剑峰藏宝阁 日外", "4－3·山门夜·内"],
)
def test_unspaced_chinese_scene_headings_do_not_count_as_uncovered_story_content(heading):
    issues = validate_screenplay_coverage([], heading)

    assert "screenplay_source_coverage" not in {issue.code for issue in issues}


def test_rejects_partial_screenplay_even_when_first_unit_is_valid():
    units = [
        {
            "source_text": "【场景：黑色石殿，中央一座大玉台】\n\n△吉迪恩转头，解开军大衣银扣。",
            "text": "@[黑色石殿] 内，@[吉迪恩] 解开军大衣银扣。",
        }
    ]

    issues = validate_screenplay_coverage(units, SCRIPT)

    assert {issue.code for issue in issues} == {
        "screenplay_source_coverage",
        "screenplay_dialogue_coverage",
    }


def test_accepts_full_ordered_coverage_and_original_dialogue():
    anchors = [
        "【场景：黑色石殿，中央一座大玉台】\n\n△吉迪恩转头，解开军大衣银扣。",
        "GIDEON: Come here.\n\n△埃利亚斯双脚铁链撞响。",
        "ELIAS (to Gideon): Lord Gideon, you actually kidnapped a Star Mark wielder by force\n\n△银色纹饰针从埃利亚斯身上掉落。",
        "GIDEON (cold and lethal): Tonight, you shall nevere scapeme for a slong as you live.\n\n△埃利亚斯瘫坐，盯着地上的银色纹饰针。",
    ]
    texts = [
        "@[黑色石殿] 内，@[吉迪恩] 转头解开军大衣银扣。",
        "@[吉迪恩]{Come here.} @[埃利亚斯] 双脚铁链撞响。",
        "@[埃利亚斯]{Lord Gideon, you actually kidnapped a Star Mark wielder by force} 银色纹饰针掉落。",
        "@[吉迪恩]{Tonight, you shall nevere scapeme for a slong as you live.} @[埃利亚斯] 瘫坐并盯着银色纹饰针。",
    ]
    units = [{"source_text": anchor, "text": text} for anchor, text in zip(anchors, texts, strict=True)]

    assert validate_screenplay_coverage(units, SCRIPT) == []


@pytest.mark.parametrize("body_heading", ["第1集", "**Episode 1**", "4-1 姜家书房 夜 内", "**8-1 NIGHT — INT. HALL**"])
def test_leading_recap_is_context_without_weakening_episode_coverage(body_heading):
    recap = "\n## 【前情提要】\n姜家：人物背景。\n他曾离开。\n\n"
    action = "△甲推开门。"
    speech = "ALICE: Stop."
    source = f"{recap}{body_heading}\n{action}\n{speech}"
    assert screenplay_body_start(source) == len(recap)
    assert screenplay_dialogue(source) == [("ALICE", "Stop.")]
    units = [
        {"source_text": action, "text": action},
        {"source_text": speech, "text": "@[ALICE]{Stop.}"},
    ]

    assert validate_screenplay_coverage(units, source) == []
    missing_action = validate_screenplay_coverage(units[1:], source)
    assert [issue.code for issue in missing_action] == ["screenplay_source_coverage"]
    assert action in str(missing_action[0])
    assert "人物背景" not in str(missing_action[0])
    assert [issue.code for issue in validate_screenplay_coverage(
        [units[0], {"source_text": speech, "text": "甲站定。"}], source,
    )] == ["screenplay_dialogue_coverage"]


@pytest.mark.parametrize("opening", [
    "前情提要\n他曾离开。",
    "前情提要\n他曾离开。\n出场人物：甲、乙",
    "△甲进屋。\n前情提要\n他曾离开。\n第1集",
    "前情提要\n他曾离开。\n△甲进屋。\n第1集",
    "前情提要\n他曾离开。\n【闪回】\n画面：甲进屋。\n4-1 大殿 夜 内",
])
def test_recap_exemption_requires_a_leading_recap_and_a_boundary_before_action(opening):
    action = "△甲推开门。"
    issues = validate_screenplay_coverage([{"source_text": action, "text": action}], f"{opening}\n{action}")

    assert [issue.code for issue in issues] == ["screenplay_source_coverage"]
    assert "他曾离开" in str(issues[0])
    assert screenplay_body_start(opening) == 0


def test_recap_does_not_skip_body_flashback_narration_or_first_scene_in_search_for_episode_heading():
    recap = "前情提要\n反派：她已离开。\n"
    body = "4-1 书房 夜 内\n【闪回】\n△她打开木盒。\n旁白：那是三年前。\n第5集"
    assert screenplay_body_start(recap + body) == len(recap)
    assert screenplay_dialogue(recap + body) == [("", "那是三年前。")]
    advisories = []
    assert validate_screenplay_coverage(
        [{"source_text": "旁白：那是三年前。", "text": "{那是三年前。}"}],
        recap + body, coverage_advisories=advisories,
    ) == []
    assert any("她打开木盒" in line for line in advisories)
    assert all("她已离开" not in line for line in advisories)


@pytest.mark.parametrize("title", ["仙侠视觉体系（制作参考）", "**制作参考**"])
def test_production_reference_requires_only_body_with_original_dialogue_coordinates(title):
    prefix = f"{title}\n一、场景总览\n布景：雪地。\n四、全剧故事梗概\n旧城风起。\n五、剧本正文\n\n第1集\n"
    action, speech = "▲她推开木门。", "甲：回来了。"
    source = prefix + action + "\n" + speech
    units = [{"source_text": action, "text": action}, {"source_text": speech, "text": "@[甲]{回来了。}"}]
    assert validate_screenplay_coverage(units, source) == []
    (line,), _ = screenplay_source_dialogue(source)
    assert line.id == "D0010"
    assert source[slice(*line.ranges[0])] == "回来了。"
    assert [issue.code for issue in validate_screenplay_coverage(units[1:], source)] == ["screenplay_source_coverage"]
    assert [issue.code for issue in validate_screenplay_coverage(
        [units[0], {"source_text": speech, "text": "她站住。"}], source,
    )] == ["screenplay_dialogue_coverage"]


@pytest.mark.parametrize("prefix", [
    "制作参考\n雪地。\n第1集",                       # No screenplay body marker.
    "制作参考\n雪地。\n五、剧本正文",                 # No episode heading.
    "制作参考\n雪地。\n剧本正文\n未标记的开场。\n第1集",
    "制作参考\n第1集\n▲雪地。\n剧本正文\n第2集",    # Cannot swallow an earlier episode/scene.
    "制作参考\n1-1 大殿 夜 内\n雪地。\n剧本正文\n第1集",
    "制作参考\n▲雪地。\n剧本正文\n第1集",
    "雪地。\n制作参考\n剧本正文\n第1集",              # Not a leading reference title.
    "甲：请看制作参考。\n雪地。\n剧本正文\n第1集",   # A mention is not a title.
])
def test_production_reference_fails_closed_without_a_bounded_prefix(prefix):
    action = "▲她推开木门。"
    issues = validate_screenplay_coverage([{"source_text": action, "text": action}], prefix + "\n" + action)
    assert any(issue.code == "screenplay_source_coverage" and "雪地" in str(issue) for issue in issues)


def test_reference_text_repeated_in_body_is_covered_only_by_body_anchors():
    repeated = "雪落在门前。"
    source = f"制作参考\n{repeated}\n剧本正文\n第1集\n{repeated}"
    unit = {"source_text": repeated, "text": repeated}
    assert validate_screenplay_coverage([unit], source) == []
    start = source.index(repeated)
    only_reference = {**unit, "source_binding": {"start": start, "end": start + len(repeated)}}
    assert [v.code for v in validate_screenplay_coverage([only_reference], source)] == ["screenplay_source_coverage"]


def test_preserves_the_original_dialogue_typo_instead_of_silently_correcting_it():
    units = [
        {
            "source_text": SCRIPT,
            "text": "@[吉迪恩]{Tonight, you shall never escape me for as long as you live.}",
        }
    ]

    assert "screenplay_dialogue_coverage" in {issue.code for issue in validate_screenplay_coverage(units, SCRIPT)}


def test_rejects_mapped_dialogue_attributed_to_the_wrong_character():
    source = "吉迪恩：过来。"
    units = [{"source_text": source, "text": "@[埃利亚斯]{过来。}"}]

    issues = validate_screenplay_coverage(
        units,
        source,
        speaker_aliases={"吉迪恩": "吉迪恩"},
    )

    assert "screenplay_speaker_attribution" in {issue.code for issue in issues}


def test_maps_screenplay_cue_to_character_alias_before_attribution_check():
    source = "GIDEON: Come here."
    units = [{"source_text": source, "text": "@[吉迪恩]{Come here.}"}]

    assert validate_screenplay_coverage(units, source, speaker_aliases={"gideon": "吉迪恩"}) == []


@pytest.mark.parametrize("existing", [False, True])
def test_new_character_aliases_follow_registration_and_auto_merge_without_mutating_project(existing):
    project = {"characters": {"吉迪恩": {"description": "原外观"}} if existing else {}}
    assets = [{"type": "character", "name": "吉迪恩", "decision": "register",
               "reason": "原文称呼", "description": "宽肩男性", "aliases": ["GIDEON"]}]
    aliases = screenplay_speaker_aliases(project, assets)

    assert validate_screenplay_coverage(
        [{"source_text": "GIDEON: Come here.", "text": "@[吉迪恩]{Come here.}"}],
        "GIDEON: Come here.", speaker_aliases=aliases,
    ) == []
    assert project == {"characters": {"吉迪恩": {"description": "原外观"}} if existing else {}}


def test_ambiguous_alias_cannot_silently_select_last_character():
    project = {"characters": {name: {"aliases": ["LORD"]} for name in ("吉迪恩", "埃利亚斯")}}
    aliases = screenplay_speaker_aliases(project)
    issues = validate_screenplay_coverage(
        [{"source_text": "LORD: Come here.", "text": "@[埃利亚斯]{Come here.}"}],
        "LORD: Come here.", speaker_aliases=aliases,
    )
    assert {issue.code for issue in issues} == {"screenplay_speaker_unresolved"}


@pytest.mark.parametrize(
    ("text", "expected_codes"),
    [
        ("@[吉迪恩]{Come} @[吉迪恩]{here.}", set()),
        ("@[吉迪恩]{Come} @[埃利亚斯]{here.}", {"screenplay_speaker_attribution"}),
        ("@[GIDEON]{Come here.}", set()),
        ("{Come here.}", {"screenplay_speaker_attribution"}),
    ],
)
def test_split_dialogue_keeps_speaker_for_every_part(text, expected_codes):
    source = "GIDEON: Come here."
    issues = validate_screenplay_coverage(
        [{"source_text": source, "text": text}],
        source,
        speaker_aliases={"gideon": "吉迪恩", "吉迪恩": "吉迪恩", "埃利亚斯": "埃利亚斯"},
    )
    assert {issue.code for issue in issues} == expected_codes


@pytest.mark.parametrize(
    ("text", "expected_codes"),
    [
        ("@[阿离]{别走。} @[裴与]{别走。}", set()),
        ("@[阿离]{别走。 别走。}", {"screenplay_speaker_attribution"}),
        ("@[裴与]{别走。} @[阿离]{别走。}", {"screenplay_speaker_attribution"}),
        ("@[阿离]{别走。}", {"screenplay_dialogue_coverage"}),
    ],
)
def test_repeated_dialogue_is_attributed_by_source_position(text, expected_codes):
    source = "阿离：别走。\n裴与：别走。"
    issues = validate_screenplay_coverage(
        [{"source_text": source, "text": text}],
        source,
        speaker_aliases={"阿离": "阿离", "裴与": "裴与"},
    )
    assert {issue.code for issue in issues} == expected_codes


def test_block_cues_strip_delivery_directions_and_separate_adjacent_speakers():
    source = "GIDEON (cold)\nCome here.\nELIAS\n(to Gideon)\nNo."
    assert screenplay_dialogue(source) == [("GIDEON", "Come here."), ("ELIAS", "No.")]
    issues = validate_screenplay_coverage(
        [{"source_text": source, "text": "@[吉迪恩]{Come here.} @[吉迪恩]{No.}"}],
        source,
        speaker_aliases={"gideon": "吉迪恩", "elias": "埃利亚斯"},
    )
    assert {issue.code for issue in issues} == {"screenplay_speaker_attribution"}


def test_source_dialogue_preserves_speaker_delivery_and_original_line_ranges():
    source = "人物：阿明、守卫\n阿明（心声）：门开了。\n\t先等等。\n△门轴转动。\n旁白：已是深夜。\n守卫\n不准进去。\n路人\n跑过小巷。"
    lines, ambiguous = screenplay_source_dialogue(source)
    assert [(line.id, line.speaker, line.delivery, line.text) for line in lines] == [
        ("D0002", "阿明", "offscreen", "门开了。\n先等等。"),
        ("D0005", "", "narration", "已是深夜。"),
        ("D0006", "守卫", "onscreen", "不准进去。"),
    ]
    assert [[source[start:end] for start, end in line.ranges] for line in lines] == [
        ["门开了。", "先等等。"], ["已是深夜。"], ["不准进去。"],
    ]
    assert ambiguous == [8]


def test_correct_words_in_the_wrong_unit_report_original_line_and_actual_unit():
    source = "阿明：关门。\n三小时后，走廊空无一人。"
    boundary = source.index("三小时")
    units = [
        {"unit_id": "E1U10", "source_text": source[:boundary], "text": "门在画面中。",
         "source_binding": {"start": 0, "end": boundary}},
        {"unit_id": "E1U30", "source_text": source[boundary:], "text": "空镜。@[阿明]{关门。}",
         "source_binding": {"start": boundary, "end": len(source)}},
    ]
    [issue] = validate_screenplay_coverage(units, source, speaker_aliases={"阿明": "阿明"})
    assert (issue.code, issue.item_id, issue.item_index, issue.locations) == (
        "screenplay_dialogue_outside_unit", "E1U30", 1, ({"source_line": 1, "dialogue_id": "D0001"},),
    )


def test_pure_action_cannot_acquire_dialogue():
    source = "△门自行关上。"
    units = [{"unit_id": "E1U01", "source_text": source, "text": "镜头推进。"}]
    assert not validate_screenplay_coverage(units, source)
    units[0]["text"] += "{关门。}"
    [issue] = validate_screenplay_coverage(units, source)
    assert (issue.code, issue.item_id) == ("screenplay_dialogue_coverage", "E1U01")


def test_advisory_reports_every_uncovered_line_but_preserves_dialogue_gate():

    source = "第1集\n▲门开了。\n▲灯熄了。\nALICE: Stop."
    units = [{"source_text": "ALICE: Stop.", "text": "她停下。"}]
    assert [v.code for v in validate_screenplay_coverage(units, source)] == ["screenplay_source_coverage", "screenplay_dialogue_coverage"]
    warnings = []
    assert [v.code for v in validate_screenplay_coverage(units, source, coverage_advisories=warnings)] == ["screenplay_dialogue_coverage"]
    assert warnings == ["2: ▲门开了。", "3: ▲灯熄了。"]
