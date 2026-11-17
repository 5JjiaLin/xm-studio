import pytest

from lib.script.reference_video.screenplay_coverage import (
    screenplay_dialogue,
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
