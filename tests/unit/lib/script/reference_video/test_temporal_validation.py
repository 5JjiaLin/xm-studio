"""Timeline ranges, local speech budgets and effective manual overrides."""

from copy import deepcopy

import pytest

from lib.script.reference_video.prompt_render import render_video_unit_prompt, temporal_unit_issues
from lib.script.reference_video.temporal_validation import has_timeline, validate_timeline
from lib.script.reference_video.voice_settings import VoiceRenderSettings
from lib.script.script_editor import ScriptEditError, patch_field
from lib.script.script_models import ReferenceVideoUnit


def test_legacy_prose_is_not_a_timeline_and_upgrade_cannot_remove_all_titles():
    text = "两秒后，他转身。"
    assert not has_timeline(text)
    assert validate_timeline(text, 8) == ((), ())
    assert [issue.code for issue in validate_timeline(text, 8, required=True)[1]] == ["timeline_missing"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("【1-8s】\n等待。", "timeline_gap_or_overlap"),
        ("【0-4s】\n等待。\n【3-8s】\n转身。", "timeline_gap_or_overlap"),
        ("【0-3s】\n等待。\n【4-8s】\n转身。", "timeline_gap_or_overlap"),
        ("【0-0s】\n等待。", "timeline_gap_or_overlap"),
        ("【0-9s】\n等待。", "timeline_duration_mismatch"),
        ("【0–8s】\n等待。", "timeline_malformed"),
        ("【-1-8s】\n等待。", "timeline_malformed"),
        ("【0-NaNs】\n等待。", "timeline_malformed"),
        ("【0-Ts】\n等待。", "timeline_malformed"),
        ("【0-8s】\n@[甲]{没说完\n【8-9s】\n的话}", "timeline_malformed"),
        ("@[甲]{开场台词}\n【0-8s】\n等待。", "timeline_speech_outside_segment"),
        ("【0-8s】\n等待。\n【组尾交接】\n@[甲]{重复台词}", "timeline_speech_outside_segment"),
    ],
)
def test_invalid_timelines_report_source_lines(text, code):
    _, issues = validate_timeline(text, 8, required=True)
    assert code in {issue.code for issue in issues}
    assert all(issue.parameters()["line"] >= 1 for issue in issues)


def test_exact_decimal_ranges_and_multilingual_dialogue():
    text = "【0-2.5s】\n@[甲]{Hello there.}\n【2.5-8.000s】\n@[乙]{Welcome back.}"
    segments, issues = validate_timeline(text, 8, language="en")
    assert len(segments) == 2
    assert not issues


def test_segment_overload_even_when_whole_unit_has_room():
    text = "【0-1s】\n@[甲]{这些话不可能在短短一秒之内全部说完。}\n【1-8s】\n静默。"
    _, issues = validate_timeline(text, 8, language="zh")
    assert {issue.code for issue in issues} == {"segment_speech_overflow"}
    assert issues[0].line == 0


def test_manual_override_checks_effective_duration_speakers_and_words():
    project = {"characters": {"甲": {"description": "a"}}, "source_language": "zh"}
    unit = {
        "unit_id": "E1U1",
        "duration_seconds": 8,
        "prompt_format_version": "timeline_v1",
        "text": "【0-8s】\n@[甲]转身。@[甲]{你好。}",
    }
    settings = VoiceRenderSettings()
    rendered = render_video_unit_prompt(unit, project, settings)
    assert not temporal_unit_issues(unit, project, settings, request_references=[], request_duration_seconds=8)
    unit["generation_prompt"] = rendered.creative_text
    assert not temporal_unit_issues(unit, project, settings, request_references=[], request_duration_seconds=8)
    for custom, code in [
        ("站着。", "timeline_missing"),
        (rendered.creative_text.replace("你好。", "再见。"), "creative_override_mismatch"),
        (rendered.creative_text.replace("<甲>说", "<乙>说"), "creative_override_mismatch"),
    ]:
        edited = {**unit, "generation_prompt": custom}
        issues = temporal_unit_issues(edited, project, settings, request_references=[], request_duration_seconds=8)
        assert code in {issue.code for issue in issues}
    issues = temporal_unit_issues(unit, project, settings, request_references=[], request_duration_seconds=10)
    assert any(issue.code == "timeline_duration_mismatch" and issue.source == "request" for issue in issues)


def test_format_metadata_roundtrips_but_is_not_llm_output_or_patchable():
    unit = {
        "unit_id": "E1U1",
        "text": "【0-8s】\n等待。",
        "duration_seconds": 8,
        "prompt_format_version": "timeline_v1",
    }
    assert ReferenceVideoUnit.model_validate(unit).model_dump()["prompt_format_version"] == "timeline_v1"
    assert "prompt_format_version" not in ReferenceVideoUnit.model_json_schema()["properties"]
    original = {"video_units": [unit]}
    updated = patch_field(deepcopy(original), "E1U1", "text", "误删标题")
    assert updated["video_units"][0]["prompt_format_version"] == "timeline_v1"
    with pytest.raises(ScriptEditError):
        patch_field(original, "E1U1", "prompt_format_version", None)
