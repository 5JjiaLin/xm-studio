"""Read-time timeline contract. No shots, references or continuity are persisted here."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from lib.script.reference_video.text_parser import SpeechMark, split_speech_line
from lib.speech.speech_rate import estimate_spoken_seconds

TIMELINE_V1 = "timeline_v1"
TIMELINE_CODES = frozenset(
    {
        "timeline_missing",
        "timeline_malformed",
        "timeline_gap_or_overlap",
        "timeline_duration_mismatch",
        "segment_speech_overflow",
        "creative_override_mismatch",
        "timeline_speech_outside_segment",
    }
)
_HEADING = re.compile(r"^\s*【([0-9]+(?:\.[0-9]{1,3})?)-([0-9]+(?:\.[0-9]{1,3})?)s】\s*$")
# Recognize broken numeric/time titles as intentional timelines, never as legacy prose.
_TIME_TITLE = re.compile(r"^\s*[【\[]\s*(?:[-+]?\d|NaN|inf|[Tab](?=\s*[-–—~～])).*(?:[-–—~～]|s|秒).*", re.I)
_RENDERED_SPEAKER = re.compile(r"<([^<>\n]+)>说\s*(?=\{)")
_SECTION = re.compile(r"^\s*【[^】]+】\s*$")


@dataclass(frozen=True)
class TemporalIssue:
    code: str
    line: int = 0  # zero-based, same coordinate as draft violations
    source: str = "text"
    expected: str = ""
    actual: str = ""

    def parameters(self) -> dict[str, object]:
        return {
            "line": self.line + 1,
            "source": self.source,
            "expected": self.expected,
            "actual": self.actual,
            "example": "【0.0-2.5s】",
        }


@dataclass(frozen=True)
class TemporalSegment:
    start: Decimal
    end: Decimal
    line: int
    text: str


def has_timeline(text: str) -> bool:
    return any(_HEADING.fullmatch(line) or _TIME_TITLE.fullmatch(line) for line in text.splitlines())


def speech_marks(text: str, *, rendered: bool = False) -> list[SpeechMark]:
    if rendered:
        text = _RENDERED_SPEAKER.sub(lambda match: f"@[{match[1]}]", text)
    return [part for line in text.splitlines() for part in split_speech_line(line) if isinstance(part, SpeechMark)]


def validate_timeline(
    text: str,
    duration_seconds: int,
    *,
    required: bool = False,
    source: str = "text",
    language: object = None,
    speech_rate_override: float | None = None,
    rendered: bool = False,
) -> tuple[tuple[TemporalSegment, ...], tuple[TemporalIssue, ...]]:
    """Validate continuous ranges and segment speech, leaving original text untouched."""
    # Shared tolerance with whole-unit speech validation; local import avoids an import cycle.
    from lib.script.reference_video.draft_validation import SPEECH_OVERFLOW_TOLERANCE

    if not required and not has_timeline(text):
        return (), ()
    lines = text.splitlines()
    segments: list[TemporalSegment] = []
    issues: list[TemporalIssue] = []
    active: tuple[Decimal, Decimal, int] | None = None
    body: list[str] = []

    def finish() -> None:
        nonlocal active, body
        if active is not None:
            segments.append(TemporalSegment(*active, "\n".join(body)))
        active, body = None, []

    for index, line in enumerate(lines):
        match = _HEADING.fullmatch(line)
        if match:
            finish()
            active = (Decimal(match[1]), Decimal(match[2]), index)
        elif _SECTION.fullmatch(line) or _TIME_TITLE.fullmatch(line):
            finish()
            if _TIME_TITLE.fullmatch(line):
                issues.append(TemporalIssue("timeline_malformed", index, source, "【0.0-2.5s】", line.strip()))
        elif active is not None:
            body.append(line)
        elif speech_marks(line, rendered=rendered):
            issues.append(TemporalIssue("timeline_speech_outside_segment", index, source))
    finish()
    if not segments:
        issues.append(TemporalIssue("timeline_missing", source=source))
        return (), tuple(issues)
    previous = Decimal(0)
    for segment in segments:
        if segment.start != previous or segment.end <= segment.start:
            issues.append(
                TemporalIssue(
                    "timeline_gap_or_overlap", segment.line, source, str(previous), f"{segment.start}-{segment.end}"
                )
            )
        previous = segment.end
        if not segment.text.strip():
            issues.append(TemporalIssue("timeline_malformed", segment.line, source, "segment content", "empty"))
        marks = speech_marks(segment.text, rendered=rendered)
        spoken = sum(
            estimate_spoken_seconds(mark.text, language if isinstance(language, str) else None, speech_rate_override)
            for mark in marks
        )
        budget = float(segment.end - segment.start) * (1 + SPEECH_OVERFLOW_TOLERANCE)
        if spoken > max(budget, 0):
            issues.append(
                TemporalIssue("segment_speech_overflow", segment.line, source, f"{max(budget, 0):.2f}", f"{spoken:.2f}")
            )
        # An unclosed speech mark must not straddle a time boundary.
        if segment.text.count("{") != segment.text.count("}"):
            issues.append(
                TemporalIssue(
                    "timeline_malformed",
                    segment.line,
                    source,
                    "complete speech mark within one segment",
                    "unbalanced braces",
                )
            )
    if previous != Decimal(duration_seconds):
        issues.append(
            TemporalIssue("timeline_duration_mismatch", segments[-1].line, source, str(duration_seconds), str(previous))
        )
    return tuple(segments), tuple(issues)


def validate_effective_timeline(
    *,
    unit: dict,
    project: dict,
    default_creative: str,
    effective_creative: str,
    request_duration_seconds: int | None,
) -> tuple[TemporalIssue, ...]:
    """Check source and the actual creative text, including manual overrides."""
    from lib.speech.speech_rate import project_speech_rate_override

    text = str(unit.get("text") or "")
    custom = str(unit.get("generation_prompt") or "")
    required = unit.get("prompt_format_version") == TIMELINE_V1 or has_timeline(text) or has_timeline(custom)
    if not required:
        return ()
    duration = int(unit.get("duration_seconds") or 0)
    args = {
        "required": True,
        "language": project.get("source_language"),
        "speech_rate_override": project_speech_rate_override(project),
    }
    _, source_issues = validate_timeline(text, duration, **args)
    segments, effective_issues = validate_timeline(
        effective_creative,
        duration,
        source="generation_prompt" if custom else "rendered",
        rendered=True,
        **args,
    )
    issues = [*source_issues, *effective_issues]
    if custom:
        expected = [(mark.speaker_reference, mark.text) for mark in speech_marks(default_creative, rendered=True)]
        actual = [(mark.speaker_reference, mark.text) for mark in speech_marks(effective_creative, rendered=True)]
        # Exact speaker/word/order parity; a bare brace cannot impersonate a speaker.
        if expected != actual:
            issues.append(TemporalIssue("creative_override_mismatch", source="generation_prompt"))
        known = set(re.findall(r"<([^<>\n]+)>", default_creative))
        actual_subjects = set(re.findall(r"<([^<>\n]+)>", effective_creative))
        if actual_subjects != known or re.search(r"@(?:图片|音频)\d+|@\[", custom):
            issues.append(TemporalIssue("creative_override_mismatch", source="generation_prompt"))
    if request_duration_seconds is not None and segments and segments[-1].end != Decimal(request_duration_seconds):
        issues.append(
            TemporalIssue(
                "timeline_duration_mismatch",
                segments[-1].line,
                "request",
                str(request_duration_seconds),
                str(segments[-1].end),
            )
        )
    return tuple(dict.fromkeys(issues))


def retain_timeline_contract(before: dict | None, after: dict) -> None:
    """Keep an opted-in unit opted in across formal saves/import-like replacements."""
    previous = {
        item.get("unit_id"): item for item in ((before or {}).get("video_units") or []) if isinstance(item, dict)
    }
    units = after.get("video_units")
    if not isinstance(units, list):
        return
    for item in units:
        if not isinstance(item, dict):
            continue
        old = previous.get(item.get("unit_id"), {})
        if (
            old.get("prompt_format_version") == TIMELINE_V1
            or has_timeline(str(old.get("text") or ""))
            or has_timeline(str(item.get("text") or ""))
            or has_timeline(str(item.get("generation_prompt") or ""))
        ):
            item["prompt_format_version"] = TIMELINE_V1
