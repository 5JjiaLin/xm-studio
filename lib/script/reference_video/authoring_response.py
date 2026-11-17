"""One response contract for new reference prompt authoring and AI repairs.

Manual drafts never opt into model normalization. Provider text remains separate
from the candidate that was checked, so a failed batch cannot resurrect errors.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from lib.infra.text_utils import DuplicateJSONKeyError, strict_json_loads, strip_json_code_fences
from lib.references.reference_catalog import build_reference_catalog
from lib.script.draft_violation import DraftViolation, DraftViolations
from lib.script.reference_video.draft_validation import normative_lines
from lib.script.reference_video.model_reference_binding import (
    normalize_model_visual_references,
    repair_model_mention_closers,
)
from lib.script.reference_video.prompt_policy_validation import POLICY_SECTIONS
from lib.script.reference_video.temporal_validation import normalize_model_timeline_newlines
from lib.script.reference_video.text_parser import extract_mentions
from lib.script.script_models import ReferencePromptAuthoringFlatUnit

AUTHORING_RESPONSE_VERSION = 2


@lru_cache(maxsize=32)
def authoring_response_model(count: int, *, with_title: bool = True) -> type[BaseModel]:
    """Closed wire objects, exact batch size; no generated IDs or durations."""
    if count < 1:
        raise ValueError("authoring response needs at least one unit")
    fields: dict[str, Any] = {
        "units": (list[ReferencePromptAuthoringFlatUnit], Field(min_length=count, max_length=count)),
    }
    if with_title:
        fields["title"] = (str, Field(description="剧集标题"))
    return create_model(
        f"ReferenceAuthoringBatch{count}{'WithTitle' if with_title else ''}",
        __config__=ConfigDict(extra="forbid"), **fields,
    )


def parse_authoring_json(text: str) -> Any:
    """Keep every distinct duplicate value visible; only identical text is harmless."""
    text = strip_json_code_fences(text)
    try:
        return strict_json_loads(text)
    except DuplicateJSONKeyError:
        def identical_text_only(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result and not (key == "text" and isinstance(value, str) and result[key] == value):
                    raise DuplicateJSONKeyError(f"JSON 对象含重复字段: {key}")
                result[key] = value
            return result

        return json.loads(text, object_pairs_hook=identical_text_only)


def recover_repeated_unit_fields(text: str, sources: Sequence[dict]) -> str | None:
    """Recover a misplaced array boundary only with complete, matched documents.

    No general JSON repair, no last-key-wins and no reordering. Duplicate titles,
    unknown fields, missing documents or dialogue/reference mismatches stay errors.
    """
    class Pairs(list):
        pass

    try:
        root = json.loads(strip_json_code_fences(text), object_pairs_hook=Pairs)
    except ValueError:
        return None
    if not isinstance(root, Pairs) or len(root) != 2 or {key for key, _ in root} != {"title", "units"}:
        return None
    outer = dict(root)
    if not isinstance(outer["title"], str) or type(outer["units"]) is not list:
        return None
    texts = []
    duplicate = False
    for item in outer["units"]:
        if not isinstance(item, Pairs) or not item or any(key != "text" or not isinstance(value, str) for key, value in item):
            return None
        duplicate |= len(item) > 1
        texts.extend(value for _, value in item)
    if not duplicate or len(texts) != len(sources):
        return None
    for candidate, source in zip(texts, sources, strict=True):
        normalized = normalize_model_timeline_newlines(candidate)
        headings = [line.strip() for line in normalized.splitlines() if line.strip() in {f"【{s}】" for s in POLICY_SECTIONS}]
        if headings != [f"【{s}】" for s in POLICY_SECTIONS]:
            return None
        original = str(source.get("text") or "")
        if normative_lines(normalized) != normative_lines(original):
            return None
        if not set(extract_mentions(original)).issubset(extract_mentions(normalized)):
            return None
    return json.dumps({"title": outer["title"], "units": [{"text": value} for value in texts]}, ensure_ascii=False)


def normalize_authoring_text(text: str, project: dict, source_text: str) -> str:
    """Only new model text: same syntax treatment at authoring and repair entrypoints."""
    catalog = build_reference_catalog(project)
    names = tuple(name for kind in ("character", "scene", "prop") for name in catalog.reference_names(kind))
    text = normalize_model_timeline_newlines(text)
    text, _repairs = repair_model_mention_closers(text, registered_names=names)
    return normalize_model_visual_references(text, required_source_text=source_text, registered_names=names)


class AuthoringCandidateViolation(DraftViolations):
    """Errors and the exact candidate they describe travel together to quarantine."""

    def __init__(self, items: Sequence[DraftViolation], title: str, units: Sequence[dict]):
        super().__init__(items)
        self.content = {"title": title, "units": [{"text": str(unit["text"])} for unit in units]}
