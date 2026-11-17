"""Versioned prompt policies for reference-video script authoring."""

from __future__ import annotations

import os

DRAMA_DETAIL_V1 = "drama_detail_v1"
SUPPORTED_PROMPT_POLICIES = frozenset({DRAMA_DETAIL_V1})


def prompt_content_validation_enabled() -> bool:
    """Runtime switch for content gates; templates and execution contracts stay active."""
    return os.getenv("ARCREEL_PROMPT_CONTENT_VALIDATION", "true").strip().lower() not in {
        "false", "0", "off", "no",
    }


def policy_for_new_task(project: dict) -> str | None:
    """Select the current policy only for new drama/reference-video tasks."""
    if project.get("content_mode") == "drama" and project.get("generation_mode") == "reference_video":
        return DRAMA_DETAIL_V1
    return None
