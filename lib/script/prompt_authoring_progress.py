"""Validated prompt batches, scoped to an exact authoring input snapshot.

This is a result cache in the existing episode drafts, not a submission queue:
reading it never starts a model request and unknown responses are never cached.
"""

from __future__ import annotations

from pathlib import Path

from lib.episode.episode_paths import episode_drafts_dir
from lib.infra.json_io import atomic_write_json, load_json_or_none
from lib.project.project_manager import ProjectManager

# Bound the output of each call while retaining the complete read-only episode context.
PROMPT_AUTHORING_BATCH_SIZE = 3


class PromptAuthoringProgress:
    def __init__(self, project_path: Path, episode: int, basis: str, manager: ProjectManager):
        self.path = episode_drafts_dir(project_path, episode) / "prompt_authoring_progress.json"
        self.basis = basis
        self.manager = manager

    def _batches(self) -> dict[str, str]:
        saved = load_json_or_none(self.path)
        if not isinstance(saved, dict) or saved.get("version") != 1 or saved.get("basis") != self.basis:
            return {}
        batches = saved.get("batches")
        if not isinstance(batches, dict):
            return {}
        return {key: text for key, text in batches.items() if isinstance(key, str) and isinstance(text, str)}

    def read(self, key: str) -> str | None:
        with self.manager.file_lock(self.path):
            return self._batches().get(key)

    def save(self, key: str, text: str) -> None:
        with self.manager.file_lock(self.path):
            batches = self._batches()
            batches[key] = text
            atomic_write_json(self.path, {"version": 1, "basis": self.basis, "batches": batches})
