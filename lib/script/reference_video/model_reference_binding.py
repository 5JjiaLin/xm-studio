"""Bind exact confirmed visual names in model actions, before normal validation.

This is syntax completion, not identity resolution: no aliases, pronouns, new
assets or inferred derivatives. Saved/manual text does not opt in implicitly.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from lib.references.reference_catalog import split_derivative_reference
from lib.script.reference_video.text_parser import extract_mentions, find_malformed_mention


def repair_model_mention_closers(text: str, *, registered_names: Sequence[str]) -> tuple[str, list[tuple[int, str]]]:
    """Repair only unambiguous model visual references ending in ``>``.

    Speech contents and speaker markers are left untouched.
    Never rewrite handoff markers, valid references or unregistered names.
    """
    counts = Counter(registered_names)
    tokens = re.compile(
        r"\{[^{}\n]*\}|<[^<>\n]*>|@\[[^\[\]<>@\n]*\]|"
        r"(?<![A-Za-z0-9_])@\[(?P<name>[^\[\]<>@{}\n]+)>"
    )
    repairs: list[tuple[int, str]] = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if (
            any(ch in line for ch in "｛｝")
            or line.count("{") != line.count("}")
            or re.search(r"\{[^}]*\{", line)
            or re.search(r"@\[[^\]>\n]*@\[", line)
            or "<" in line
        ):
            continue

        def replace(match: re.Match[str], line_index: int = index, source: str = line) -> str:
            name = match.group("name")
            if name is None or counts[name] != 1 or re.match(r"\s*[:：]?\s*\{", source[match.end():]):
                return match.group(0)
            repairs.append((line_index, name))
            return f"@[{name}]"

        lines[index] = tokens.sub(replace, line)
    return "".join(lines), repairs


def normalize_model_visual_references(
    text: str, *, required_source_text: str, registered_names: Sequence[str],
) -> str:
    """Complete missing @[] only inside an explicitly timed action section."""
    if text.count("【分段时间轴】") != 1 or text.count("【组尾交接】") != 1:
        return text
    start = text.index("【分段时间轴】") + len("【分段时间轴】")
    end = text.index("【组尾交接】")
    if end <= start:
        return text
    timeline = text[start:end]
    if any(find_malformed_mention(line) for line in timeline.splitlines()):
        return text

    def identity(name: str) -> str:
        base, derivative = split_derivative_reference(name)
        return base if derivative.startswith("视角-") else name

    present = {identity(name) for name in extract_mentions(timeline)}
    registered = set(registered_names)
    missing = {
        name for name in extract_mentions(required_source_text)
        if name in registered and len(name) >= 2 and identity(name) not in present
    }
    if not missing:
        return text
    # Longer catalog names consume their whole token; a required base must never
    # be substituted inside another registered identity or an unregistered form.
    names = "|".join(re.escape(name) for name in sorted(registered, key=lambda n: (-len(n), n)))
    tokens = re.compile(r"@\[[^\]\n]*\]|\{[^{}]*\}|<[^<>\n]*>|(?P<name>" + names + r")")
    offscreen = re.compile(r"画外|镜头外|不入画|不出镜|未出镜|不可见|不在画面|回忆|想起|off[- ]?screen|out of frame", re.I)

    def bind_clause(clause: str) -> str:
        if offscreen.search(clause):
            return clause

        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            if name not in missing:
                return match[0]
            before = clause[match.start() - 1:match.start()] if match.start() else ""
            after = clause[match.end():]
            if before in {"/", "@", "[", "<"} or after.startswith("/"):
                return name
            if (before and re.fullmatch(r"[A-Za-z0-9_]", before)) or re.match(r"[A-Za-z0-9_]", after):
                return name
            # A bare name beside speech is not permission to change its speaker.
            if re.match(r"\s*[:：]?\s*\{", after):
                return name
            return f"@[{name}]"

        return tokens.sub(replace, clause)

    # Do not split inside speech/mentions: even punctuation in dialogue is opaque.
    # Keep whole sentences for offscreen detection, while tokens protect quoted data.
    clauses = re.split(r"((?:@\[[^\]\n]*\]|\{[^{}]*\}|<[^<>\n]*>|[^。！？!?；;\n])+[。！？!?；;\n]?)", timeline)
    return text[:start] + "".join(bind_clause(clause) for clause in clauses) + text[end:]
