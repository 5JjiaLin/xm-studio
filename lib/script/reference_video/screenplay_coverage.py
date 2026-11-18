"""Whole-screenplay checks for reference-video planning, before prompt authoring.

Source anchors prove source coverage, not semantic fidelity of visual descriptions.
Recognizable dialogue is checked separately against actual speech marks, so widening
an anchor cannot hide missing spoken content. Novel adaptation keeps its own rules.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from lib.project.asset_types import asset_name_comparison_key
from lib.script.draft_violation import DraftViolation
from lib.script.plan_new_assets import NewAssetResolution, resolve_new_assets
from lib.script.reference_video.text_parser import SpeechMark, split_speech_line

_METADATA = re.compile(
    r"(?:Episode\s+\d+|第[\d一二三四五六七八九十百]+[集幕章]|"
    r"(?:出场)?人物\s*[:：][^。！？!?]*|角色\s*[:：][^。！？!?]*|"
    r"(?:\d+[-－.]\d+\s+)?(?:DAY|NIGHT)\s*[—–-]\s*(?:INT|EXT)\..*|"
    r"(?:INT|EXT|INT\./EXT)\..*(?:DAY|NIGHT)|"
    r"\d+[-－.]\d+\s*[^。！？!?]*?(?:日|夜)\s*[·・/／—–-]?\s*(?:内|外)[^。！？!?]*)",
    re.IGNORECASE,
)
# Inline character cues (including performance directions) are unambiguous.
# Unlabeled prose is never guessed to be dialogue.
_OFFSCREEN = r"(?i:O\.?\s*S\.?|V\.?\s*O\.?)|旁白|画外音|心声|内心独白|内心|画外"
_NAME = r"[A-Z][A-Z \d_.-]{0,60}?|[\u4e00-\u9fff·]{1,16}?"
_CUE = re.compile(
    rf"(?P<speaker>{_NAME})(?P<delivery>\s*(?:[（(][^()（）]*[）)]|{_OFFSCREEN}))?\s*"
)
_INLINE_SPEECH = re.compile(r"(?P<cue>[^:：]+)[:：]\s*(?P<text>\S.*)")
_NON_SPEAKERS = frozenset({"人物", "出场人物", "角色", "场景", "时间", "地点", "镜头", "动作", "画面"})
_ACTION = re.compile(r"^(?:[△▲【#]|\s*(?:动作|画面)\s*[:：])")


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


@dataclass(frozen=True)
class SourceDialogue:
    id: str
    line: int
    speaker: str
    delivery: Literal["onscreen", "offscreen", "narration"]
    text: str
    ranges: tuple[tuple[int, int], ...]


def _cue(raw: str) -> tuple[str, Literal["onscreen", "offscreen", "narration"]] | None:
    raw = raw.strip()
    if raw in {"旁白", "画外音", "无主旁白"} or raw.upper() == "NARRATOR":
        return "", "narration"
    cue = _CUE.fullmatch(raw)
    if not cue or cue["speaker"].strip() in _NON_SPEAKERS:
        return None
    delivery = (cue["delivery"] or "").strip(" ()（）")
    return cue["speaker"].strip(), "offscreen" if re.search(_OFFSCREEN, delivery) else "onscreen"


def screenplay_source_dialogue(source: str) -> tuple[list[SourceDialogue], list[int]]:
    """Extract exact speech fragments; bare Chinese cues need cast or inline evidence."""
    lines = source.splitlines(keepends=True)
    inlines = [_INLINE_SPEECH.fullmatch(line.strip().lstrip("# ")) for line in lines]
    known = set()
    for line, match in zip(lines, inlines, strict=True):
        bare = line.strip()
        if cast := re.fullmatch(r"(?:出场人物|人物|角色)\s*[:：]\s*(.*)", bare):
            known.update(name.strip() for name in re.split(r"[、,，/／]", cast[1]))
        elif match and not _METADATA.fullmatch(bare) and (identity := _cue(match["cue"])):
            known.add(identity[0])

    results: list[SourceDialogue] = []
    ambiguous: list[int] = []
    offset = 0
    pending: tuple[int, str, Literal["onscreen", "offscreen", "narration"]] | None = None
    allow_continuation = False
    for line_number, (line, match) in enumerate(zip(lines, inlines, strict=True), start=1):
        bare = line.strip()
        start = offset + len(line) - len(line.lstrip())
        end = start + len(bare)
        offset += len(line)
        if not bare or _ACTION.match(bare) or _METADATA.fullmatch(bare):
            pending = None
        elif match and (identity := _cue(match["cue"])) is not None:
            text = match["text"]
            results.append(SourceDialogue(f"D{line_number:04d}", line_number, *identity, text, ((end - len(text), end),)))
            pending = None
            allow_continuation = True
            continue
        elif pending and re.fullmatch(r"[（(][^()（）]*[）)]", bare):
            if re.search(_OFFSCREEN, bare) and pending[1]:
                pending = (pending[0], pending[1], "offscreen")
            continue
        elif allow_continuation and line.startswith(("\t", "　　")):
            previous = results[-1]
            results[-1] = replace(previous, text=previous.text + "\n" + bare, ranges=(*previous.ranges, (start, end)))
            continue
        elif (identity := _cue(bare.rstrip(":："))) and (
            identity[0] in known or identity[1] != "onscreen" or bare.endswith((":", "："))
            or re.fullmatch(r"[A-Z][A-Z \d_.-]{0,60}", identity[0])
        ):
            pending = (line_number, *identity)
        elif pending:
            cue_line, speaker, delivery = pending
            results.append(SourceDialogue(f"D{cue_line:04d}", cue_line, speaker, delivery, bare, ((start, end),)))
            pending = None
            allow_continuation = True
            continue
        elif re.fullmatch(r"[\u4e00-\u9fff·]{1,8}", bare):
            ambiguous.append(line_number)
        allow_continuation = False
    return results, ambiguous


def screenplay_dialogue(source: str) -> list[tuple[str, str]]:
    return [(line.speaker, line.text) for line in screenplay_source_dialogue(source)[0]]


def screenplay_speaker_aliases(
    project: object, new_assets: object = None, *, resolution: NewAssetResolution | None = None
) -> dict[str, str]:
    """Map cue aliases to final speaker identities without registering draft assets."""
    if not isinstance(project, dict):
        return {}
    # Use the same registration projection as content confirmation. Reading only
    # outcomes misses aliases on new registrations and on automatic merges.
    resolved = resolution if resolution is not None else resolve_new_assets(project, new_assets)
    overlay = copy.deepcopy(project)
    resolved.apply_to_project(overlay)
    candidates: dict[str, set[str]] = {}

    def add(candidate: str, name: str) -> None:
        key = asset_name_comparison_key(candidate).casefold()
        if key:
            candidates.setdefault(key, set()).add(name)

    characters = overlay.get("characters")
    for name, raw in characters.items() if isinstance(characters, dict) else []:
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        aliases = raw.get("aliases")
        for candidate in (name, *(aliases if isinstance(aliases, list) else [])):
            if isinstance(candidate, str):
                add(candidate, name)
    for (asset_type, cue), outcome in resolved.outcomes.items():
        if asset_type == "character":
            # A derivative speaks as its base; an explicit skip keeps its cue.
            for candidate in (cue, outcome.name):
                add(candidate, outcome.name)
    # An ambiguous cue is unresolved, never whichever character appeared last.
    return {cue: next(iter(names)) for cue, names in candidates.items() if len(names) == 1}


def validate_screenplay_coverage(
    units: Sequence[dict], source: str, *, speaker_aliases: dict[str, str] | None = None
) -> list[DraftViolation]:
    """Require narrative anchors and the original ordered dialogue across the episode."""
    from lib.script.prompt_policy import prompt_content_validation_enabled

    if not prompt_content_validation_enabled():
        return []
    lines = [_normal(line) for line in source.splitlines() if _normal(line)]
    normalized = " ".join(lines)
    covered = bytearray(len(normalized))
    previous_start = previous_end = 0
    for unit in units:
        anchor = _normal(str(unit.get("source_text") or ""))
        if not anchor:
            continue
        # Prefer the next occurrence; overlapping anchors may still share a passage.
        start = normalized.find(anchor, previous_end)
        if start < 0:
            start = normalized.find(anchor, previous_start)
        if start < 0:
            continue  # The ordinary anchor/order validator reports this separately.
        end = start + len(anchor)
        covered[start:end] = b"\x01" * len(anchor)
        previous_start, previous_end = start, end

    missing: list[str] = []
    offset = 0
    for line in lines:
        bare = line.strip("# *")
        if not _METADATA.fullmatch(bare) and any(
            char.isalnum() and not covered[offset + index] for index, char in enumerate(line)
        ):
            missing.append(line)
        offset += len(line) + 1
    issues: list[DraftViolation] = []
    if missing:
        excerpts = "；".join(line[:70] for line in missing[:5])
        issues.append(
            DraftViolation(
                f"拆解原文锚遗漏 {len(missing)} 段剧本内容：{excerpts}。"
                "请按原文顺序补齐对应的视频单元及动作、对白；标题、场次标题和人物名单可不入镜。",
                code="screenplay_source_coverage",
            )
        )

    expected, ambiguous = screenplay_source_dialogue(source)
    actual = [
        (index, part)
        for index, unit in enumerate(units)
        for line in str(unit.get("text") or "").splitlines()
        for part in split_speech_line(line)
        if isinstance(part, SpeechMark)
    ]
    # Speech may be split across units to fit a model's duration tiers. Ignore only
    # boundary whitespace; punctuation, spelling, repetitions and order stay exact.
    pattern = " ?".join(f"({re.escape(_normal(mark.text))})" for _index, mark in actual)
    original = " ".join(_normal(line.text) for line in expected)
    matched = re.fullmatch(pattern, original) if actual else None
    dialogue_mismatch = bool(expected and matched is None)
    aliases = speaker_aliases or {}
    if not expected and not ambiguous and actual:
        issues.extend(
            DraftViolation(
                "源剧本没有可发声的对白；此单元新增了台词。",
                code="screenplay_dialogue_coverage", item_index=index, item_id=units[index].get("unit_id"),
            )
            for index in dict.fromkeys(index for index, _ in actual)
        )
    if expected and matched is not None:
        # Attribute every intersecting span, so splitting a speech across units or
        # merging adjacent speeches cannot bypass the speaker check.
        offset = 0
        for speech in expected:
            end = offset + len(_normal(speech.text))
            source_key = asset_name_comparison_key(speech.speaker).casefold()
            expected_asset = aliases.get(source_key, speech.speaker)
            for group, (index, mark) in enumerate(actual, start=1):
                start_actual, end_actual = matched.span(group)
                if start_actual >= end or end_actual <= offset:
                    continue
                unit = units[index]
                unit_id = unit.get("unit_id")
                label = unit_id or f"单元 {index + 1}"
                coordinates = {"source_line": speech.line, "dialogue_id": speech.id}
                actual_key = asset_name_comparison_key(mark.speaker).casefold()
                actual_asset = aliases.get(actual_key, mark.speaker)
                if speaker_aliases is not None and source_key and source_key not in aliases and speech.delivery == "onscreen":
                    issues.append(DraftViolation(
                        f"{label}：原文第 {speech.line} 行说话人 {speech.speaker} 未匹配已登记或建议人物。",
                        code="screenplay_speaker_unresolved", item_index=index, item_id=unit_id, locations=(coordinates,),
                    ))
                elif (speaker_aliases is not None or speech.delivery != "onscreen") and (
                    asset_name_comparison_key(actual_asset).casefold() != asset_name_comparison_key(expected_asset).casefold()
                ):
                    issues.append(DraftViolation(
                        f"{label}：原文第 {speech.line} 行 {speech.id}「{speech.text}」应由"
                        f" {expected_asset or '无主旁白'} 发声，当前为 {actual_asset or '无主旁白'}。",
                        code="screenplay_speaker_attribution", item_index=index, item_id=unit_id, locations=(coordinates,),
                    ))
                if binding := unit.get("source_binding"):
                    prefix_len = 0
                    for range_start, range_end in speech.ranges:
                        fragment = _normal(source[range_start:range_end])
                        fragment_start = offset + prefix_len
                        fragment_end = fragment_start + len(fragment)
                        prefix_len += len(fragment) + 1
                        if start_actual >= fragment_end or end_actual <= fragment_start:
                            continue
                        a = max(range_start, binding["start"])
                        b = min(range_end, binding["end"])
                        if a < b and (
                            len(_normal(source[range_start:a])) <= max(0, start_actual - fragment_start)
                            and len(_normal(source[b:range_end])) <= max(0, fragment_end - end_actual)
                        ):
                            continue
                        issues.append(DraftViolation(
                            f"{label}：台词「{mark.text}」属于原文第 {speech.line} 行 {speech.id}，超出本单元所选原文范围。",
                            code="screenplay_dialogue_outside_unit", item_index=index, item_id=unit_id, locations=(coordinates,),
                        ))
                        break
            offset = end + 1
    if dialogue_mismatch:
        issues.append(
            DraftViolation(
                f"源剧本可识别出 {len(expected)} 段对白，但发声记号未按序逐字保留；"
                "请补齐遗漏对白，保留原文拼写和标点，不得只把台词放进 source_text 或画面描述。",
                code="screenplay_dialogue_coverage",
            )
        )
    return issues
