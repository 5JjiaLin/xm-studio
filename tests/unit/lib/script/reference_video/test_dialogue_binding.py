"""Replays screenplay output failures and preserves dialogue across local repair."""

import json
import re

import pytest

from lib.prompts.prompt_builders_reference import build_reference_units_split_prompt, reference_screenplay_inputs
from lib.script.draft_violation import DraftViolation
from lib.script.reference_video.dialogue_binding import dialogue_fragments, speech_signature, validate_stored_dialogue
from lib.script.reference_video.draft_validation import assert_dialogue_preserved, validate_reference_source_bindings
from lib.script.reference_video.plan_repair import merge_repair, repair_targets
from lib.script.script_models import build_reference_units_script_plan_model
from server.text_generation import ScriptPlanParseError, parse_reference_script_plan_candidate


def _run(source, units, fragments):
    schema = build_reference_units_script_plan_model([4, 8], bind_dialogue=True)
    project = {"characters": {"第一人": {"aliases": ["ALICE"]}, "第二人": {"aliases": ["BOB"]}}}
    return parse_reference_script_plan_candidate(
        json.dumps({"units": units}),
        schema,
        [4, 8],
        source,
        dialogue=fragments,
        project=project,
    )


def _unit(start, text, end=None):
    return {"source_span_start": start, "source_span_end": end or start, "text": text, "duration_seconds": 8}


@pytest.mark.parametrize("failure", ["duplicate", "reversed", "wrong_range", "mixed", "raw", "legacy_shape"])
def test_new_output_never_accepts_invalid_dialogue_when_quality_checks_are_off(monkeypatch, failure):
    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "false")
    source = "ALICE: Go!\nBOB: Go!\nNARRATOR: The door closes."
    _, fragments = reference_screenplay_inputs(source, "en", None, [8])
    a, b, c = [f"[[{part.id}]]" for part in fragments]
    units = [_unit("S0001", f"门口。{a}"), _unit("S0002", f"台阶。{b}"), _unit("S0003", f"远景。{c}")]
    code = "dialogue_binding_invalid"
    if failure == "duplicate":
        units[0]["text"] += a
    elif failure == "reversed":
        units = [_unit("S0001", f"门口。{b}{a}", "S0002"), units[2]]
    elif failure == "wrong_range":
        units[0]["text"] = f"门口。{b}"
    elif failure == "mixed":
        units = [units[0], _unit("S0002", f"台阶。{b}{c}", "S0003")]
        code = "dialogue_binding_mixed"
    elif failure == "raw":
        units[0]["text"] += "@[第二人]{我改写了。}"
        code = "dialogue_placeholder_invalid"
    else:
        units = [{"source_text": source, "text": "门口。", "duration_seconds": 8}]
        code = "schema_invalid"
    with pytest.raises(ScriptPlanParseError) as error:
        _run(source, units, fragments)
    assert code in {violation.code for violation in error.value.violations}


def test_long_dialogue_preserves_exact_source_and_splits_only_at_offered_positions():
    source = "ALICE (V.O.): " + "walk to the bridge and wait " * 5 + "there."
    lines, fragments = reference_screenplay_inputs(source, "en", None, [4, 8])
    spans = [re.match(r"\[(.*?)\]", line)[1] for line in lines]
    assert len(spans) == len(fragments) > 1
    actual = _run(
        source,
        [_unit(span, f"空镜。[[{fragment.id}]]") for span, fragment in zip(spans, fragments, strict=True)],
        fragments,
    )
    speech = [item for unit in actual["units"] for item in speech_signature(unit["text"], {})]
    assert {item[:2] for item in speech} == {("第一人", "offscreen")}
    assert "".join(item[2] for item in speech) == source.partition(": ")[2]
    assert validate_stored_dialogue(actual["units"], source, {"alice": "第一人"}) == []
    assert validate_reference_source_bindings(actual["units"], source) == []
    assert validate_reference_source_bindings(actual["units"], source + "\n△门开了。")


def test_ordinary_english_parenthetical_is_not_an_offscreen_cue():
    source = "ALICE (almost whispering): Wait.\nBOB (O.S.): Wait."
    _, fragments = reference_screenplay_inputs(source, "en", None, [8])
    assert [fragment.dialogue.delivery for fragment in fragments] == ["onscreen", "offscreen"]


def test_local_repair_uses_dialogue_ids_and_preserves_non_target_text():
    source = "ALICE: Go!\nBOB: Go!"
    _, fragments = reference_screenplay_inputs(source, "en", None, [8])
    a, b = [f"[[{part.id}]]" for part in fragments]
    original = _run(source, [_unit("S0001", f"门口。{a}"), _unit("S0002", f"台阶。{b}")], fragments)
    first, second = original["units"]
    targets = repair_targets(original, [DraftViolation("时长", code="duration_off_tier", item_index=1)], source)
    target = targets[0]
    assert [entry["id"] for entry in target["dialogue_fragments"]] == [fragments[1].id]
    repair = {
        "item_index": 1,
        "content_digest": target["content_digest"],
        "units": [
            {"source_text": second["source_text"], "text": f"台阶。{b}", "duration_seconds": 4},
        ],
    }
    aliases = {"alice": "第一人", "bob": "第二人"}
    updated = merge_repair(original, targets, {"replacements": [repair]}, source=source, speaker_aliases=aliases)
    assert updated["units"][0] == first
    assert updated["units"][1]["text"] == second["text"]
    assert updated["units"][1]["duration_seconds"] == 4
    assert validate_reference_source_bindings(updated["units"], source) == []
    assert validate_stored_dialogue(updated["units"], source, aliases) == []
    repair["units"][0]["text"] = f"台阶。{a}"
    with pytest.raises(ValueError, match="repair_dialogue_invalid"):
        merge_repair(original, targets, {"replacements": [repair]}, source=source, speaker_aliases=aliases)


def test_action_only_unit_and_later_authoring_cannot_sneak_in_speech(monkeypatch):
    monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "false")
    source = "△门开了。"
    _, fragments = reference_screenplay_inputs(source, "zh", None, [8])
    candidate = _run(source, [_unit("S0001", "门开了。")], fragments)
    unit = candidate["units"][0]
    assert unit["source_binding"]["dialogue_ids"] == []
    with pytest.raises(DraftViolation, match="台词条数"):
        assert_dialogue_preserved("E1U01", unit["text"], "门开了。{谁在那里？}", required=True)
    with pytest.raises(DraftViolation, match="具名画外发声"):
        assert_dialogue_preserved("E1U01", "（画外发声）@[第一人]{等等。}", "@[第一人]{等等。}", required=True)


def test_model_gets_short_distinct_ids_for_fragments_on_the_same_line():
    source = "ALICE: Run! Hide!"
    lines, fragments = reference_screenplay_inputs(source, "en", None, [4, 8])
    assert len(fragments) == 2
    assert fragments[0].dialogue.line == fragments[1].dialogue.line
    prompt = build_reference_units_split_prompt(
        episode=1, novel_text=source, project_overview={}, characters={}, scenes={}, props={},
        supported_durations=[4, 8], max_duration=8, max_reference_images=None, default_duration=None,
        source_kind="screenplay", screenplay_inputs=(lines, fragments),
    )
    assert "[[Q0001]] 第1行" in prompt
    assert "[[Q0002]] 第1行" in prompt
    assert all(f"[[{part.id}]]" not in prompt for part in fragments)
    with pytest.raises(ScriptPlanParseError) as error:
        _run(source, [_unit("S0001", "门开了。[[D0001]][[D0001]]", "S0002")], fragments)
    assert [issue.code for issue in error.value.violations].count("dialogue_binding_invalid") == 2
    units = _run(source, [_unit("S0001", "门开了。[[Q0001]][[Q0002]]", "S0002")], fragments)["units"]
    assert units[0]["source_binding"]["dialogue_ids"] == [part.id for part in fragments]
    assert speech_signature(units[0]["text"], {}) == [("第一人", "onscreen", "Run!"), ("第一人", "onscreen", " Hide!")]


def test_explicit_recap_is_context_and_legacy_dialogue_bindings_are_readable():
    source = "前情提要\n姜家：人员介绍。\n第1集\nALICE: Run!"
    _, fragments = reference_screenplay_inputs(source, "en", None, [4, 8])
    assert [(part.dialogue.line, part.text) for part in fragments] == [(4, "Run!")]
    units = _run(source, [_unit("S0001", "门开了。[[Q0001]]", "S0004")], fragments)["units"]
    assert units[0]["source_text"] == source
    assert validate_stored_dialogue(units, source, {"alice": "第一人"}) == []
    legacy = dialogue_fragments(source, [], include_recap=True)
    old_units = _run(source, [_unit("S0001", "门开了。[[Q0001]][[Q0002]]", "S0004")], legacy)["units"]
    assert validate_stored_dialogue(old_units, source, {"alice": "第一人"}) == []
    assert speech_signature(old_units[0]["text"], {})[0][:2] == ("姜家", "onscreen")
    assert [part.dialogue.speaker for part in dialogue_fragments("前情提要\n姜家：人员介绍。", [])] == ["姜家"]
