"""Recover unambiguous unit envelopes without inventing or rewriting content."""

from __future__ import annotations

import re

from lib.script.reference_video.prompt_policy_validation import POLICY_SECTIONS
from lib.script.script_models import ReferencePromptAuthoringFlatScript, ReferencePromptAuthoringFlatUnit

_SECTION = re.compile(r"(?m)^[ \t]*【(" + "|".join(POLICY_SECTIONS) + r")】[ \t]*\r?$")


def unpack_authoring_units(
    flat: ReferencePromptAuthoringFlatScript, expected_count: int
) -> ReferencePromptAuthoringFlatScript:
    """Split only complete, ordered five-section documents packed into fewer items.

    No partial recovery: ambiguous headings, prefixes, or a wrong total leave the
    original response intact. Callers must still validate every unit's dialogue,
    references, duration, and handoff against its original content contract.
    """
    if len(flat.units) >= expected_count:
        return flat
    expanded: list[ReferencePromptAuthoringFlatUnit] = []
    for unit in flat.units:
        matches = list(_SECTION.finditer(unit.text))
        names = [match[1] for match in matches]
        if not matches or unit.text[: matches[0].start()].strip():
            return flat
        if len(names) % len(POLICY_SECTIONS) or names != list(POLICY_SECTIONS) * (len(names) // len(POLICY_SECTIONS)):
            return flat
        starts = [match.start() for match in matches if match[1] == POLICY_SECTIONS[0]]
        for start, end in zip(starts, [*starts[1:], len(unit.text)], strict=True):
            expanded.append(ReferencePromptAuthoringFlatUnit(text=unit.text[start:end].strip()))
    return flat.model_copy(update={"units": expanded}) if len(expanded) == expected_count else flat
