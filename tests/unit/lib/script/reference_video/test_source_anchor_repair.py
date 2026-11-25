import copy

import pytest

from lib.script.reference_video.draft_validation import (
    is_verbatim_source_anchor,
    out_of_order_source_anchor_indices,
)
from lib.script.reference_video.screenplay_coverage import validate_screenplay_coverage
from lib.script.reference_video.source_anchor_repair import (
    preserve_source_bindings,
    restore_source_anchor_layout,
)


def test_extra_chinese_line_break_restores_original_excerpt_and_clears_coverage():
    source = "▲手机响。\n张三（喊）：你在哪？赶快回来！\n▲张三跑开。"
    content = {"units": [
        {"source_text": "▲手机响。\n张三（喊）：你在哪？\n赶快回来！\n",
         "text": "手机响。@[张三]{你在哪？赶快回来！}", "duration_seconds": 8},
        {"source_text": "▲张三跑开。", "text": "@[张三]跑开。", "duration_seconds": 4},
    ], "new_assets": [{"name": "张三", "description": "原外观"}]}
    before = copy.deepcopy(content)
    assert {issue.code for issue in validate_screenplay_coverage(content["units"], source)} == {
        "screenplay_source_coverage",
    }

    repaired = restore_source_anchor_layout(content, source)

    assert repaired["units"][0]["source_text"] == "▲手机响。\n张三（喊）：你在哪？赶快回来！"
    assert is_verbatim_source_anchor(repaired["units"][0]["source_text"], source)
    assert validate_screenplay_coverage(repaired["units"], source) == []
    expected = copy.deepcopy(content)
    expected["units"][0]["source_text"] = repaired["units"][0]["source_text"]
    assert repaired == expected
    assert content == before
    assert restore_source_anchor_layout(repaired, source) is repaired


def test_trailing_boundary_whitespace_is_removed_from_a_bound_source_excerpt():
    source = "▲沈渊攥紧项链，往路边跑。\n苏晚（喊）：沈渊，你注定属于我！"
    binding = {
        "source_revision": "sha256-v1:source",
        "start": 0,
        "end": len(source),
        "span_start": "S0001",
        "span_end": "S0002",
    }
    content = {"units": [{"source_text": source + "\n", "source_binding": binding, "text": "原文"}]}

    repaired = restore_source_anchor_layout(content, source)

    assert repaired["units"][0]["source_text"] == source
    assert repaired["units"][0]["source_binding"] == binding
    assert content["units"][0]["source_text"].endswith("\n")


def test_ai_repair_cannot_rewrite_server_owned_source_bindings():
    binding = {"source_revision": "sha256-v1:source", "start": 0, "end": 3, "span_start": "S1", "span_end": "S1"}
    baseline = {"units": [{"source_text": "原文", "source_binding": binding, "text": "原"}]}
    candidate = {"units": [{"source_text": "原文", "source_binding": {"start": 0}, "text": "修复"}]}

    repaired = preserve_source_bindings(candidate, baseline)

    assert repaired["units"][0]["source_binding"] == binding
    assert candidate["units"][0]["source_binding"] == {"start": 0}


@pytest.mark.parametrize(("anchor", "source"), [
    ("你好，\n再见。", "你好，别走。"),  # Real replacement.
    ("你好，\n再见。", "你好，请等一下，再见。"),  # Omission / stitching.
    ("你好！\n再见。", "你好，再见。"),  # Punctuation.
    ("去NewYork。", "去New York。"),  # Latin word boundary.
    ("去 New\nYork。", "去 NewYork。"),
    ("张三：你\n好。", "张三：你好。\n张三：你 好。"),  # Ambiguous source location.
    ("哈\n哈", "哈哈哈"),  # Overlapping ambiguous matches.
    ("", "你好。"),
    (" \n ", "你好。"),
])
def test_ambiguous_or_substantive_changes_are_never_guessed(anchor, source):
    content = {"units": [{"source_text": anchor, "text": "不修改"}]}
    assert restore_source_anchor_layout(content, source) is content


@pytest.mark.parametrize("content", [{}, {"units": None}, {"units": [None, {}, {"source_text": 3}]}])
def test_malformed_content_is_left_for_schema_validation(content):
    assert restore_source_anchor_layout(content, "原文") is content


def test_layout_repair_cannot_hide_reversed_units_or_missing_spoken_content():
    source = "张三：第一句。\n张三：第二句。"
    content = {"units": [
        {"source_text": "张三：第\n二句。", "text": "@[张三]{第二句。}"},
        {"source_text": "张三：第\n一句。", "text": "@[张三]沉默。"},
    ]}
    repaired = restore_source_anchor_layout(content, source)
    assert out_of_order_source_anchor_indices([u["source_text"] for u in repaired["units"]], source) == [1]
    assert "screenplay_dialogue_coverage" in {
        issue.code for issue in validate_screenplay_coverage(repaired["units"], source)
    }
