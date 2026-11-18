"""Source positions for screenplay dialogue, independent of content-quality checks."""

from __future__ import annotations

import json
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from lib.project.asset_types import asset_name_comparison_key
from lib.script.draft_violation import DraftViolation
from lib.script.reference_video.screenplay_coverage import SourceDialogue, screenplay_source_dialogue
from lib.script.reference_video.text_parser import OFFSCREEN_SPEECH_CUE, SpeechMark, split_speech_line

_ID = re.compile(r"(D\d{4,}):(\d+)-(\d+)")
_PLACEHOLDER = re.compile(r"\[\[([^\[\]\n]*)\]\]")


@dataclass(frozen=True)
class DialogueFragment:
    dialogue: SourceDialogue
    start: int
    end: int
    text: str

    @property
    def id(self) -> str:
        return f"{self.dialogue.id}:{self.start}-{self.end}"


def dialogue_request_ids(fragments: Sequence[DialogueFragment]) -> dict[str, DialogueFragment]:
    """Use one short token per fragment in the model request, including split lines."""
    return {f"Q{index:04d}": fragment for index, fragment in enumerate(fragments, start=1)}


def render_dialogue_catalogue(fragments: Sequence[DialogueFragment]) -> str:
    return "\n".join(
        f"[[{identifier}]] 第{part.dialogue.line}行 | {part.dialogue.delivery} | "
        f"{part.dialogue.speaker or '无主旁白'} | {json.dumps(part.text, ensure_ascii=False)}"
        for identifier, part in dialogue_request_ids(fragments).items()
    ) or "（无已识别对白）"


def dialogue_fragments(
    source: str, cuts: Sequence[int], *, include_recap: bool = False
) -> list[DialogueFragment]:
    points = sorted(set(cuts))
    dialogue, _ = screenplay_source_dialogue(source, include_recap=include_recap)
    return [
        DialogueFragment(speech, left, right, source[left:right])
        for speech in dialogue
        for start, end in speech.ranges
        for left, right in pairwise([start, *points[bisect_right(points, start) : bisect_left(points, end)], end])
        if source[left:right].strip()
    ]


def resolve_dialogue_fragments(source: str, ids: Sequence[str]) -> dict[str, DialogueFragment]:
    """Resolve persisted IDs directly against the source revision, without current model settings."""
    dialogue, _ = screenplay_source_dialogue(source, include_recap=True)
    by_id = {speech.id: speech for speech in dialogue}
    resolved = {}
    for identifier in ids:
        match = _ID.fullmatch(identifier)
        if not match or len(match[2]) > 10 or len(match[3]) > 10:
            continue
        speech = by_id.get(match[1])
        start, end = int(match[2]), int(match[3])
        if speech and any(left <= start < end <= right for left, right in speech.ranges):
            fragment = DialogueFragment(speech, start, end, source[start:end])
            if fragment.text.strip() and fragment.id == identifier:
                resolved[identifier] = fragment
    return resolved


def _speaker(fragment: DialogueFragment, aliases: dict[str, str]) -> str:
    name = fragment.dialogue.speaker
    return aliases.get(asset_name_comparison_key(name).casefold(), name)


def speech_signature(text: str, aliases: dict[str, str]) -> list[tuple[str, str, str]]:
    result = []
    for line in text.splitlines():
        previous = ""
        for part in split_speech_line(line):
            if isinstance(part, SpeechMark):
                speaker = aliases.get(asset_name_comparison_key(part.speaker).casefold(), part.speaker)
                delivery = (
                    "narration"
                    if not speaker
                    else "offscreen"
                    if previous.rstrip().endswith(OFFSCREEN_SPEECH_CUE)
                    else "onscreen"
                )
                result.append((speaker, delivery, part.text))
                previous = ""
            else:
                previous = part
    return result


def fragment_signature(fragments: Sequence[DialogueFragment], aliases: dict[str, str]) -> list[tuple[str, str, str]]:
    return [
        (
            asset_name_comparison_key(_speaker(fragment, aliases)),
            fragment.dialogue.delivery,
            unicodedata.normalize("NFC", fragment.text.replace("\ufeff", "")),
        )
        for fragment in fragments
    ]


def retain_dialogue_sources(previous: Sequence[dict], candidate: Sequence[dict]) -> None:
    """Manual text edits preserve the server-owned source identity and positions."""
    if not has_dialogue_bindings(previous):
        return
    if len(previous) != len(candidate) or any(not isinstance(unit, dict) for unit in candidate):
        raise ValueError("含对白绑定的单元数量变化，请从剧本重新拆解。")
    for old, new in zip(previous, candidate, strict=True):
        if new.get("source_text") != old.get("source_text") or (
            old.get("unit_id") is not None and old.get("unit_id") != new.get("unit_id")
        ):
            raise ValueError("含对白绑定的单元原文或身份变化，请从剧本重新拆解。")
        new["source_binding"] = dict(old["source_binding"])


def has_dialogue_bindings(units: Sequence[dict]) -> bool:
    return any(
        (unit.get("source_binding") or {}).get("dialogue_ids") is not None for unit in units if isinstance(unit, dict)
    )


def bind_generated_dialogue(
    units: list[dict], fragments: Sequence[DialogueFragment], aliases: dict[str, str]
) -> list[DraftViolation]:
    """Compile model placeholders into the existing editor and video speech syntax."""
    # Stored drafts and earlier repair requests can still use the full source ID.
    by_id = {fragment.id: fragment for fragment in fragments}
    by_id.update(dialogue_request_ids(fragments))
    seen: set[str] = set()
    issues: list[DraftViolation] = []
    last_end = -1
    for index, unit in enumerate(units):
        text = unit["text"]
        binding = unit["source_binding"]
        selected: list[DialogueFragment] = []
        label = f"units[{index}]"
        remainder = _PLACEHOLDER.sub("", text)
        if any(char in remainder for char in "{}｛｝") or "[[" in remainder or "]]" in remainder:
            issues.append(
                DraftViolation(
                    f"{label} 发声只能使用给出的 [[对白片段编号]]，请移除手写台词或修正占位符。",
                    code="dialogue_placeholder_invalid",
                    label=label,
                    item_index=index,
                )
            )
        placeholders = list(_PLACEHOLDER.finditer(text))
        for match in placeholders:
            identifier = match[1]
            fragment = by_id.get(identifier)
            reason = None
            if fragment is None:
                reason = "编号不在本次请求的对白清单中"
            elif fragment.id in seen:
                reason = "编号已使用"
            elif not (binding["start"] <= fragment.start < fragment.end <= binding["end"]):
                reason = "不在该单元选择的原文范围内"
            elif fragment.start < last_end:
                reason = "顺序早于前一个对白片段"
            if reason:
                line = fragment.dialogue.line if fragment else None
                issues.append(
                    DraftViolation(
                        f"{label} 的对白 {identifier}{f'（原文第 {line} 行）' if line else ''}：{reason}。",
                        code="dialogue_binding_invalid",
                        label=label,
                        item_index=index,
                        params={"dialogue_id": identifier, "source_line": line},
                    )
                )
                continue
            selected.append(fragment)
            seen.add(fragment.id)
            last_end = fragment.end
        kinds = {bool(fragment.dialogue.speaker) for fragment in selected}
        if len(kinds) > 1:
            issues.append(
                DraftViolation(
                    f"{label} 同时含具名发声和无主旁白，请按原文顺序拆成独立单元。",
                    code="dialogue_binding_mixed",
                    label=label,
                    item_index=index,
                    params={"dialogue_ids": [fragment.id for fragment in selected]},
                )
            )
        if len(selected) != len(placeholders):
            continue
        if not remainder.strip():
            issues.append(
                DraftViolation(f"{label} 缺少画面描述。", code="blank_description", label=label, item_index=index)
            )

        def render(match: re.Match[str]) -> str:
            fragment = by_id[match[1]]
            speaker = _speaker(fragment, aliases)
            mark = f"@[{speaker}]" if speaker else ""
            if fragment.dialogue.delivery == "offscreen":
                mark = OFFSCREEN_SPEECH_CUE + mark
            # Prevent a preceding visual mention from becoming a narrator's speaker.
            return f"\n{mark}{{{fragment.text}}}\n"

        rendered = _PLACEHOLDER.sub(render, text).strip()
        if speech_signature(rendered, aliases) != fragment_signature(selected, aliases):
            issues.append(
                DraftViolation(
                    f"{label} 回填的对白无法按发声语法读取，请核对原文中的特殊符号和说话人名称。",
                    code="dialogue_render_invalid",
                    label=label,
                    item_index=index,
                    params={"dialogue_ids": [fragment.id for fragment in selected]},
                )
            )
        unit["text"] = rendered
        binding["dialogue_ids"] = [fragment.id for fragment in selected]
    for fragment in fragments:
        if fragment.id in seen:
            continue
        index = next(
            (
                i
                for i, unit in enumerate(units)
                if unit["source_binding"]["start"] <= fragment.start < unit["source_binding"]["end"]
            ),
            None,
        )
        issues.append(
            DraftViolation(
                f"遗漏原文第 {fragment.dialogue.line} 行的对白 {fragment.id}「{fragment.text}」。",
                code="dialogue_binding_missing",
                item_index=index,
                params={"dialogue_id": fragment.id, "source_line": fragment.dialogue.line},
            )
        )
    return issues


def validate_stored_dialogue(
    units: Sequence[dict], source: str, aliases: dict[str, str], *, record_manual_edits: bool = False
) -> list[DraftViolation]:
    bindings = [unit.get("source_binding") or {} for unit in units]
    if not has_dialogue_bindings(units):
        return []
    resolved = resolve_dialogue_fragments(source, [key for b in bindings for key in (b.get("dialogue_ids") or [])])
    issues = []
    last_end = -1
    cuts = []
    selected_ids = []
    for index, (unit, binding) in enumerate(zip(units, bindings, strict=True)):
        ids = binding.get("dialogue_ids")
        item_id = unit.get("unit_id")
        selected = []
        if ids is not None:
            for key in ids:
                fragment = resolved.get(key)
                if (
                    fragment is None
                    or not (binding["start"] <= fragment.start < fragment.end <= binding["end"])
                    or fragment.start < last_end
                ):
                    break
                selected.append(fragment)
                cuts.extend((fragment.start, fragment.end))
                selected_ids.append(key)
                last_end = fragment.end
        if ids is None or len(selected) != len(ids):
            issues.append(
                DraftViolation(
                    f"第 {index + 1} 单元的对白编号缺失、错序或与原文范围不符。",
                    code="dialogue_binding_invalid",
                    item_index=index,
                    item_id=item_id,
                )
            )
            continue
        changed = speech_signature(unit["text"], aliases) != fragment_signature(selected, aliases)
        if record_manual_edits:
            binding["dialogue_edited"] = changed
        elif changed and not binding.get("dialogue_edited"):
            issues.append(
                DraftViolation(
                    f"第 {index + 1} 单元的发声内容与绑定的原文对白不符。",
                    code="dialogue_binding_text_changed",
                    item_index=index,
                    item_id=item_id,
                    params={"dialogue_ids": ids},
                )
            )
    expected_ids = [fragment.id for fragment in dialogue_fragments(source, cuts)]
    if selected_ids != expected_ids:
        # Before recap recognition, a confirmed draft may have bound its lines
        # as speech. Accept its original coverage without rewriting the draft.
        legacy_ids = [fragment.id for fragment in dialogue_fragments(source, cuts, include_recap=True)]
        if selected_ids != legacy_ids:
            issues.append(DraftViolation("拆解中的对白位置未完整覆盖当前原文。", code="dialogue_binding_missing"))
    return issues
