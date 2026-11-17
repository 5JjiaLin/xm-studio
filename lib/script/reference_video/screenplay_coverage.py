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
_INLINE_SPEECH = re.compile(
    r"^(?P<speaker>[A-Z][A-Z \d_.-]{0,60}|[\u4e00-\u9fff·]{1,16})"
    r"(?:\s*(?:[Oo][.]?[Ss][.]?|[Vv][.]?[Oo][.]?|旁白|画外音|内心独白))?"
    r"\s*(?:[（(][^()（）]*[）)]\s*)?[:：]\s*(?P<text>.+)$"
)
_NON_SPEAKERS = frozenset({"人物", "出场人物", "角色", "场景", "时间", "地点", "镜头", "动作", "画面"})
_BLOCK_CUE = re.compile(r"(?P<speaker>[A-Z][A-Z \d_.-]{0,60}?)(?:\s*[（(][^()（）]*[）)])?")


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def screenplay_dialogue(source: str) -> list[tuple[str, str]]:
    """Extract colon cues and standard uppercase cue blocks without rewriting words."""
    lines = source.splitlines()
    result: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip().lstrip("# ")
        match = _INLINE_SPEECH.fullmatch(line)
        if match and match["speaker"] not in _NON_SPEAKERS:
            result.append((match["speaker"].strip(), match["text"].strip()))
        elif line and (cue := _BLOCK_CUE.fullmatch(line)) and not _METADATA.fullmatch(line):
            # In screenplay layout, a cue is immediately followed by its speech,
            # optionally with a parenthesized delivery direction on its own line.
            end = index + 1
            speech: list[str] = []
            while end < len(lines) and lines[end].strip():
                following = lines[end].strip()
                if (
                    following.startswith(("△", "【", "#"))
                    or _INLINE_SPEECH.fullmatch(following)
                    or (speech and _BLOCK_CUE.fullmatch(following))
                ):
                    break
                if not re.fullmatch(r"[（(].*[）)]", following):
                    speech.append(following)
                end += 1
            if speech:
                result.append((cue["speaker"].strip(), " ".join(speech)))
                index = end - 1
        index += 1
    return result


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

    expected = screenplay_dialogue(source)
    actual = [
        part
        for unit in units
        for line in str(unit.get("text") or "").splitlines()
        for part in split_speech_line(line)
        if isinstance(part, SpeechMark)
    ]
    # Speech may be split across units to fit a model's duration tiers. Ignore only
    # boundary whitespace; punctuation, spelling, repetitions and order stay exact.
    pattern = " ?".join(f"({re.escape(_normal(mark.text))})" for mark in actual)
    original = " ".join(_normal(text) for _speaker, text in expected)
    matched = re.fullmatch(pattern, original) if actual else None
    dialogue_mismatch = bool(expected and matched is None)
    aliases = speaker_aliases or {}
    speaker_mismatch = False
    unresolved: list[str] = []
    if expected and matched is not None:
        # Attribute every intersecting span, so splitting a speech across units or
        # merging adjacent speeches cannot bypass the speaker check.
        offset = 0
        for source_speaker, text in expected:
            end = offset + len(_normal(text))
            expected_asset = aliases.get(asset_name_comparison_key(source_speaker).casefold())
            if speaker_aliases is not None and expected_asset is None:
                unresolved.append(source_speaker)
            if expected_asset is not None:
                for index, mark in enumerate(actual, start=1):
                    start_actual, end_actual = matched.span(index)
                    if start_actual >= end or end_actual <= offset:
                        continue
                    actual_key = asset_name_comparison_key(mark.speaker).casefold()
                    actual_asset = aliases.get(actual_key, mark.speaker)
                    if (
                        asset_name_comparison_key(actual_asset).casefold()
                        != asset_name_comparison_key(expected_asset).casefold()
                    ):
                        speaker_mismatch = True
                        break
            if speaker_mismatch:
                break
            offset = end + 1
    if unresolved:
        issues.append(DraftViolation(
            f"源剧本说话人 {'、'.join(dict.fromkeys(unresolved))} 无法对应已登记或建议人物；请在人物 aliases 补充原文称呼后重验",
            code="screenplay_speaker_unresolved",
        ))
    if dialogue_mismatch or speaker_mismatch:
        detail = "说话人归属不匹配" if speaker_mismatch and not dialogue_mismatch else "发声记号未按序逐字保留"
        issues.append(
            DraftViolation(
                f"源剧本可识别出 {len(expected)} 段对白，但{detail}；"
                "请补齐遗漏对白，保留原文拼写和标点，不得只把台词放进 source_text 或画面描述。",
                code="screenplay_speaker_attribution"
                if speaker_mismatch and not dialogue_mismatch
                else "screenplay_dialogue_coverage",
            )
        )
    return issues
