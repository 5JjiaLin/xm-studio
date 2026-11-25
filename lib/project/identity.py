"""Stable project identities stored in project metadata."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from lib.artifacts.formal_write import project_metadata_lock
from lib.infra.data_root_layout import list_project_dirs
from lib.infra.json_io import atomic_write_json


def validate_project_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("project_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("project_id must be a canonical UUID") from exc
    if value != str(parsed):
        raise ValueError("project_id must be a canonical UUID")
    return value


def ensure_project_identity(project_dir: Path) -> tuple[str, bool]:
    """Assign missing IDs under the metadata lock; refuse corrupt persisted IDs."""
    project_dir = Path(project_dir)
    project_file = project_dir / "project.json"
    with project_metadata_lock(project_dir):
        project = json.loads(project_file.read_text(encoding="utf-8"))
        if not isinstance(project, dict):
            raise ValueError(f"project metadata must be an object: {project_file}")
        if "project_id" in project:
            return validate_project_id(project["project_id"]), False
        project_id = str(uuid4())
        project["project_id"] = project_id
        atomic_write_json(project_file, project)
        return project_id, True


def ensure_all_project_identities(projects_dir: Path) -> tuple[int, list[str]]:
    """Idempotently assign stable IDs to existing project directories."""
    migrated = 0
    failed: list[str] = []
    seen: dict[str, str] = {}
    for project_dir in list_project_dirs(Path(projects_dir)):
        try:
            project_id, changed = ensure_project_identity(project_dir)
            migrated += int(changed)
            if project_id in seen:
                raise ValueError(f"duplicate project_id shared with {seen[project_id]}")
            seen[project_id] = project_dir.name
        except Exception as exc:
            failed.append(f"{project_dir.name}: {exc}")
    return migrated, failed


def read_unique_project_identity(project_dir: Path) -> str:
    """Read without migration and reject copied identities added after startup.

    This checks the current on-disk projects rather than caching startup results.
    A malformed sibling also fails closed until its identity can be inspected.
    """
    project_dir = Path(project_dir)
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    if not isinstance(project, dict):
        raise ValueError("project metadata must be an object")
    project_id = validate_project_id(project.get("project_id"))
    for sibling in list_project_dirs(project_dir.parent):
        if sibling.name == project_dir.name:
            continue
        other = json.loads((sibling / "project.json").read_text(encoding="utf-8"))
        if not isinstance(other, dict):
            raise ValueError("project metadata must be an object")
        if validate_project_id(other.get("project_id")) == project_id:
            raise ValueError("duplicate project_id")
    return project_id


__all__ = [
    "ensure_all_project_identities",
    "ensure_project_identity",
    "read_unique_project_identity",
    "validate_project_id",
]
