"""Deployment boundaries which must hold before any company cutover."""

import datetime as dt
import json
import os
import shutil
from contextlib import closing

import pytest

from scripts import company_caddy, company_health, company_release
from scripts.company_ops import make_manifest, prune_backups
from scripts.company_server import atomic_json


def test_source_archive_keeps_agent_templates_and_excludes_runtime_credentials(tmp_path):
    for relative in (
        "plan.md", "agent_runtime_profile/.claude/settings.json",
        "agent_runtime_profile/.claude/settings.local.json", "frontend/.env.example", "frontend/.env.production",
        "frontend/node_modules/dependency/index.js", "frontend/dist/index.html", "server/app.py",
        "deploy/production/pgdata/secret", "deploy/claude_data/secret", "projects/arcreel.db",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    files = {p.relative_to(tmp_path).as_posix() for p in company_release.source_files(tmp_path)}
    assert files == {
        "plan.md", "agent_runtime_profile/.claude/settings.json", "frontend/.env.example", "server/app.py",
    }


def test_source_archive_rejects_linked_top_level_directory(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "server"
    if os.name == "nt":
        import subprocess

        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(RuntimeError, match="linked entries"):
            list(company_release.source_files(tmp_path))
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove only the test junction, never its target.
        else:
            link.unlink()
    assert target.exists()


def test_caddy_orphan_child_is_running_but_not_ready(tmp_path, monkeypatch):
    (tmp_path / "ops").mkdir()
    identity = {"pid": 123, "created": 456}
    atomic_json(tmp_path / "ops" / "caddy-instance.json", {"child": identity})
    monkeypatch.setattr(company_caddy, "process_identity", lambda pid: identity)
    assert not company_caddy.proxy_stopped(tmp_path)
    assert not company_caddy.proxy_ready(tmp_path)
    monkeypatch.setattr(company_caddy, "process_identity", lambda pid: {"pid": 123, "created": 789})
    assert company_caddy.proxy_stopped(tmp_path)


def test_health_preserves_unknown_state_and_records_corrupt_receipt(tmp_path, monkeypatch):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "last-successful-backup.json").write_text("{broken", encoding="utf-8")

    def inaccessible(_):
        raise PermissionError("Process identity inaccessible")

    monkeypatch.setattr(company_health, "proxy_stopped", inaccessible)
    monkeypatch.setattr(company_health, "stopped", lambda _: True)
    result = company_health.check(tmp_path)
    assert result["stopped"] == {"app": True, "caddy": None}
    assert result["backup_age_hours"] is None
    assert not result["ok"]
    assert any("corrupt" in issue.lower() or "independent backup" in issue.lower() for issue in result["issues"])
    assert json.loads((tmp_path / "ops" / "last-health.json").read_text())["stopped"]["caddy"] is None


def make_production_snapshot(base, name, timestamp, deployment="test"):
    import sqlite3

    root = base / name
    for component in ("data", "config", "profile", "caddy-data", "release"):
        (root / component).mkdir(parents=True)
    with closing(sqlite3.connect(root / "data" / "arcreel.db")) as db:
        db.execute("CREATE TABLE alembic_version (version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('test')")
        db.commit()
    for name in ("source.zip", "release-info.json", "caddy.exe"):
        (root / "release" / name).write_bytes(b"fixture")
    manifest = make_manifest(root)
    manifest.update(created_at=timestamp, deployment_id=deployment, production=True)
    atomic_json(root / "manifest.json", manifest)
    return root


def test_retention_preserves_seven_days_four_weeks_and_other_deployment(tmp_path):
    latest = dt.datetime(2026, 11, 9, 2, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    for index in range(28):
        make_production_snapshot(tmp_path, f"xmstudio-{index:02d}", (latest - dt.timedelta(days=index)).timestamp())
    other = make_production_snapshot(tmp_path, "xmstudio-other", latest.timestamp(), deployment="other")
    partial = tmp_path / ".partial-fixture"
    partial.mkdir()
    removed = prune_backups(tmp_path, "test")
    assert len(removed) == 19
    assert {p.name for p in tmp_path.glob("xmstudio-*")} == {
        "xmstudio-00", "xmstudio-01", "xmstudio-02", "xmstudio-03", "xmstudio-04", "xmstudio-05",
        "xmstudio-06", "xmstudio-08", "xmstudio-15", other.name,
    }
    assert partial.exists()


def test_retention_aborts_before_any_delete_when_new_snapshot_is_corrupt(tmp_path, monkeypatch):
    for index in range(10):
        snapshot = make_production_snapshot(tmp_path, f"xmstudio-{index}", 1794000000 + index)
    (snapshot / "release" / "source.zip").write_bytes(b"corrupt")
    monkeypatch.setattr(shutil, "rmtree", lambda _: pytest.fail("No snapshot may be deleted"))
    with pytest.raises(RuntimeError, match="checksum"):
        prune_backups(tmp_path, "test")
