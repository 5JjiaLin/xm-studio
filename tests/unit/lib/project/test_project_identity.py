"""Stable identities survive saves and cannot be adopted by imports or payloads."""

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from lib.project.identity import (
    ensure_all_project_identities,
    ensure_project_identity,
    read_unique_project_identity,
    validate_project_id,
)
from lib.project.project_manager import ProjectManager


def _legacy_project(root: Path, name: str = "demo", **fields: object) -> Path:
    project_dir = root / "projects" / name
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(json.dumps({"title": "Demo", **fields}), encoding="utf-8")
    return project_dir


def test_initialization_is_idempotent_and_preserves_other_fields(tmp_path):
    project_dir = _legacy_project(tmp_path)
    project_id, changed = ensure_project_identity(project_dir)
    assert changed
    assert validate_project_id(project_id) == project_id
    before = (project_dir / "project.json").read_bytes()
    assert ensure_project_identity(project_dir) == (project_id, False)
    assert (project_dir / "project.json").read_bytes() == before
    assert json.loads(before)["title"] == "Demo"


def test_concurrent_initialization_publishes_one_identity(tmp_path):
    project_dir = _legacy_project(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(ensure_project_identity, [project_dir] * 16))
    assert len({project_id for project_id, _changed in results}) == 1
    assert sum(changed for _project_id, changed in results) == 1


@pytest.mark.parametrize("value", [None, 12, "", "bad-id", str(uuid4()).upper(), uuid4().hex])
def test_corrupt_identity_is_rejected_without_rewriting(tmp_path, value):
    project_dir = _legacy_project(tmp_path, project_id=value)
    before = (project_dir / "project.json").read_bytes()
    with pytest.raises(ValueError, match="canonical UUID"):
        ensure_project_identity(project_dir)
    assert (project_dir / "project.json").read_bytes() == before


def test_duplicate_identity_is_reported_without_reassigning(tmp_path):
    project_id = str(uuid4())
    first = _legacy_project(tmp_path, "first", project_id=project_id)
    second = _legacy_project(tmp_path, "second", project_id=project_id)
    migrated, failures = ensure_all_project_identities(tmp_path / "projects")
    assert migrated == 0
    assert len(failures) == 1
    assert "duplicate project_id" in failures[0]
    assert ensure_project_identity(first) == ensure_project_identity(second) == (project_id, False)


def test_plain_load_is_read_only(tmp_path):
    project_dir = _legacy_project(tmp_path)
    before = (project_dir / "project.json").read_bytes()
    ProjectManager(tmp_path).load_project("demo")
    assert (project_dir / "project.json").read_bytes() == before
    assert not (project_dir / ".project.json.lock").exists()


@pytest.mark.parametrize("publish", [True, False])
def test_creation_and_metadata_recreation_preserve_identity(tmp_path, publish):
    manager = ProjectManager(tmp_path)
    manager.create_project("demo", publish=publish)
    project = manager.create_project_metadata("demo", "First")
    project_id = validate_project_id(project["project_id"])
    project = manager.create_project_metadata("demo", "Second")
    assert project["project_id"] == project_id
    assert manager.load_project("demo")["project_id"] == project_id


@pytest.mark.parametrize("operation", ["save", "update"])
@pytest.mark.parametrize("replacement", ["different", "missing"])
def test_existing_identity_cannot_be_replaced_or_removed(tmp_path, operation, replacement):
    manager = ProjectManager(tmp_path)
    project_dir = manager.create_project("demo")
    original = manager.load_project("demo")["project_id"]

    def mutate(project):
        if replacement == "missing":
            project.pop("project_id")
        else:
            project["project_id"] = str(uuid4())

    before = (project_dir / "project.json").read_bytes()
    if operation == "save":
        project = manager.load_project("demo")
        mutate(project)
        if replacement == "missing":
            manager.save_project("demo", project)
            assert project["project_id"] == original
        else:
            with pytest.raises(ValueError, match="cannot be changed"):
                manager.save_project("demo", project)
            assert (project_dir / "project.json").read_bytes() == before
    else:
        with pytest.raises(ValueError, match="cannot be changed"):
            manager.update_project("demo", mutate)
        assert (project_dir / "project.json").read_bytes() == before
    assert manager.load_project("demo")["project_id"] == original


@pytest.mark.parametrize("operation", ["save", "update"])
def test_legacy_first_write_assigns_identity_and_ordinary_edits_keep_it(tmp_path, operation):
    _legacy_project(tmp_path)
    manager = ProjectManager(tmp_path)
    if operation == "save":
        project = manager.load_project("demo")
        project["project_id"] = str(uuid4())
        supplied_id = project["project_id"]
        manager.save_project("demo", project)
        assert project["project_id"] != supplied_id
    else:
        project = manager.update_project("demo", lambda project: project.update(title="New"))
    project_id = validate_project_id(project["project_id"])
    manager.update_project("demo", lambda project: project.update(title="Renamed"))
    assert manager.load_project("demo")["project_id"] == project_id


def test_metadata_extras_cannot_supply_identity(tmp_path):
    manager = ProjectManager(tmp_path)
    manager.create_project("demo")
    with pytest.raises(ValueError, match="extras"):
        manager.create_project_metadata("demo", extras={"project_id": str(uuid4())})


def test_whole_data_restore_keeps_identity(tmp_path):
    original_dir = _legacy_project(tmp_path / "original")
    project_id, _changed = ensure_project_identity(original_dir)
    shutil.copytree(tmp_path / "original", tmp_path / "restored")
    assert ensure_all_project_identities(tmp_path / "restored" / "projects") == (0, [])
    assert ensure_project_identity(tmp_path / "restored" / "projects" / "demo") == (project_id, False)


def test_authorization_read_detects_copies_created_after_startup(tmp_path):
    project_dir = _legacy_project(tmp_path)
    project_id, _changed = ensure_project_identity(project_dir)
    assert read_unique_project_identity(project_dir) == project_id
    shutil.copytree(project_dir, project_dir.with_name("copied"))
    with pytest.raises(ValueError, match="duplicate project_id"):
        read_unique_project_identity(project_dir)


def test_authorization_read_never_assigns_missing_identity(tmp_path):
    project_dir = _legacy_project(tmp_path)
    before = (project_dir / "project.json").read_bytes()
    with pytest.raises(ValueError, match="canonical UUID"):
        read_unique_project_identity(project_dir)
    assert (project_dir / "project.json").read_bytes() == before
