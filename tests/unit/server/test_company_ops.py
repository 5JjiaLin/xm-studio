import os
import shutil
import sqlite3

import portalocker
import pytest

from scripts.company_ops import backup, make_manifest, restore_isolated, verify_snapshot
from scripts.company_server import atomic_json, load_environment, validate_start


def test_database_verification_preserves_wal_snapshot(tmp_path):
    from scripts.company_ops import digest, verify_database

    live = tmp_path / "live.db"
    copied = tmp_path / "copied"
    copied.mkdir()
    with sqlite3.connect(live) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('wal-only-revision')")
        connection.commit()
        for suffix in ("", "-wal", "-shm"):
            shutil.copy2(str(live) + suffix, copied / ("arcreel.db" + suffix))
    before = {p.name: digest(p) for p in copied.iterdir()}
    verify_database(copied / "arcreel.db")
    verify_database(copied / "arcreel.db")
    assert before == {p.name: digest(p) for p in copied.iterdir()}


def test_backup_drain_failure_preserves_maintenance_without_copy_or_restart(snapshot, tmp_path, monkeypatch):
    from scripts import company_ops as ops

    (snapshot / "ops").mkdir()
    destination = tmp_path / "backups"
    destination.mkdir()
    monkeypatch.setattr(ops, "control", lambda *args, **kw: {"inspection": False})

    def fail(*args):
        raise TimeoutError("still generating")

    monkeypatch.setattr(ops, "drain", fail)
    result = ops.backup(snapshot, destination, timeout=0, local_test=True)
    assert not result["backup_ok"]
    assert not result["restart_ok"]
    assert "still generating" in result["backup_error"]
    assert (snapshot / "ops" / "maintenance.json").exists()
    assert not list(destination.iterdir())


def test_backup_copy_failure_attempts_paused_restart_before_resume(snapshot, tmp_path, monkeypatch):
    from scripts import company_ops as ops

    (snapshot / "ops").mkdir()
    destination = tmp_path / "backups"
    destination.mkdir()
    events = []

    def control(root, command, owner=""):
        events.append(command)
        return {"inspection": False}

    monkeypatch.setattr(ops, "control", control)
    monkeypatch.setattr(ops, "drain", lambda *a: events.append("drain"))
    monkeypatch.setattr(ops, "wait_stopped", lambda *a: events.append("exited"))
    monkeypatch.setattr(ops, "start", lambda *a: events.append("start-paused"))

    def copy_failure(*args):
        raise OSError("copy failed")

    monkeypatch.setattr(ops, "copy_components", copy_failure)
    result = ops.backup(snapshot, destination, timeout=0, local_test=True)
    assert not result["backup_ok"]
    assert result["restart_ok"]
    assert events == ["status", "drain", "stop", "exited", "start-paused", "resume"]
    assert not (snapshot / "ops" / "last-successful-backup.json").exists()


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    for name in ("data", "config", "profile", "caddy-data"):
        (root / name).mkdir(parents=True)
    with sqlite3.connect(root / "data" / "arcreel.db") as db:
        db.execute("CREATE TABLE alembic_version (version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('test-revision')")
    (root / "data" / "project.txt").write_text("project contents", encoding="utf-8")
    (root / "data" / "manifest.json").write_text('{"project": true}', encoding="utf-8")
    (root / "config" / "server.env").write_text(
        "AUTH_ENABLED=true\nAUTH_PASSWORD=test-only\nARCREEL_DATA_DIR=D:/old/data\n", encoding="utf-8"
    )
    atomic_json(root / "manifest.json", make_manifest(root))
    return root


def test_manifest_includes_nested_manifests(snapshot):
    manifest = verify_snapshot(snapshot)
    assert "data/manifest.json" in manifest["files"]
    (snapshot / "data" / "manifest.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum"):
        verify_snapshot(snapshot)


def test_verify_rejects_missing_or_extra_files(snapshot):
    (snapshot / "data" / "project.txt").unlink()
    with pytest.raises(RuntimeError, match="file set"):
        verify_snapshot(snapshot)


def test_verify_rejects_path_traversal(snapshot):
    manifest = make_manifest(snapshot)
    manifest["files"]["../escape.txt"] = {"size": 0, "sha256": ""}
    atomic_json(snapshot / "manifest.json", manifest)
    with pytest.raises(RuntimeError, match="escapes"):
        verify_snapshot(snapshot)


def test_restore_is_independent_inert_copy(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    target = tmp_path / "restored"
    result = restore_isolated(snapshot, target)
    assert result["inspection_only"]
    assert (target / "ops" / "inspection-only.json").exists()
    load_environment(target, target / "config" / "server.env")
    assert str(target / "data") in (target / "config" / "server.env").read_text(encoding="utf-8").replace("\\\\", "\\")
    assert (target / "data" / "project.txt").read_bytes() == (snapshot / "data" / "project.txt").read_bytes()
    with pytest.raises(RuntimeError, match="new independent"):
        restore_isolated(snapshot, target)


def test_maintenance_restart_requires_owner_and_live_operations_lock(tmp_path):
    (tmp_path / "ops").mkdir()
    atomic_json(tmp_path / "ops" / "maintenance.json", {"owner": "owner"})
    with pytest.raises(RuntimeError, match="prevents"):
        validate_start(tmp_path, None)
    with pytest.raises(RuntimeError, match="operations lock"):
        validate_start(tmp_path, "owner")
    with portalocker.Lock(tmp_path / "ops" / "operations.lock", timeout=0):
        assert validate_start(tmp_path, "owner")


def test_production_backup_rejects_local_disk_before_changing_service(snapshot, tmp_path):
    with pytest.raises(RuntimeError, match="UNC"):
        backup(snapshot, tmp_path / "another-local-dir", timeout=1)
    assert not (snapshot / "ops" / "maintenance.json").exists()


def test_verify_requires_even_empty_components(snapshot):
    (snapshot / "caddy-data").rmdir()
    with pytest.raises(RuntimeError, match="component missing"):
        verify_snapshot(snapshot)


def test_backup_retains_maintenance_when_proxy_controller_fails_during_restart(snapshot, tmp_path, monkeypatch):
    from scripts import company_ops as ops

    (snapshot / "ops").mkdir()
    (snapshot / "config" / "caddy.json").write_text("{}")
    destination = tmp_path / "backups"
    destination.mkdir()
    events = []
    readiness = iter([True, False])
    monkeypatch.setattr(ops, "control", lambda root, command, *a, **kw: events.append(command) or {"inspection": False})
    monkeypatch.setattr(ops, "proxy_ready", lambda _: next(readiness))
    monkeypatch.setattr(ops, "proxy_stopped", lambda _: True)
    monkeypatch.setattr(ops, "stop_proxy", lambda *a: None)
    monkeypatch.setattr(ops, "start_proxy", lambda *a: None)
    monkeypatch.setattr(ops, "drain", lambda *a: None)
    monkeypatch.setattr(ops, "wait_stopped", lambda *a: None)
    monkeypatch.setattr(ops, "start", lambda *a: None)
    result = backup(snapshot, destination, timeout=0, local_test=True)
    assert result["backup_ok"]
    assert not result["restart_ok"]
    assert "controller" in result["restart_error"]
    assert (snapshot / "ops" / "maintenance.json").exists()
    assert "resume" not in events
