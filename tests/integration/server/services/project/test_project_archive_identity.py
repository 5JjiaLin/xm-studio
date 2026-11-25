"""A project archive installs a fresh authorization identity in all conflict modes."""

from contextlib import contextmanager

import pytest

from lib.project.identity import ensure_all_project_identities, validate_project_id
from lib.project.project_manager import ProjectManager
from server.services.project.project_archive import ProjectArchiveService


@contextmanager
def _retire_project(project_name):
    yield f"{project_name}#deleted-20261110T000000Z"


@pytest.mark.parametrize("scope", ["full", "current"])
@pytest.mark.parametrize("conflict_policy", ["prompt", "rename", "overwrite"])
def test_archive_import_uses_new_identity(tmp_path, scope, conflict_policy):
    source = ProjectManager(tmp_path / "source")
    source.create_project("demo")
    original = source.create_project_metadata("demo", "Demo")
    archive_path, _name = ProjectArchiveService(source).export_project("demo", scope=scope)
    target = ProjectManager(tmp_path / "target")
    existing_id = None
    if conflict_policy != "prompt":
        target.create_project("demo")
        existing_id = target.create_project_metadata("demo", "Existing")["project_id"]
    result = ProjectArchiveService(target, retire_project=_retire_project).import_project_archive(
        archive_path, uploaded_filename="demo.zip", conflict_policy=conflict_policy
    )
    imported_id = target.load_project(result.project_name)["project_id"]
    assert validate_project_id(imported_id) == imported_id
    assert imported_id != original["project_id"]
    assert imported_id != existing_id
    assert ensure_all_project_identities(target.projects_dir) == (0, [])
    if conflict_policy == "rename":
        assert target.load_project("demo")["project_id"] == existing_id
