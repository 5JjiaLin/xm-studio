"""Serialized maintenance, verified offline backup and isolated restore tools."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import portalocker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.company_caddy import load_config as load_proxy_config
from scripts.company_caddy import proxy_ready, proxy_stopped, start_proxy, stop_proxy
from scripts.company_server import atomic_json

COMPONENTS = ("data", "config", "profile", "caddy-data")


def control(root: Path, command: str, owner: str = "", timeout: float = 30) -> dict:
    ops = root / "ops"
    request_id = uuid.uuid4().hex
    request = ops / "requests" / f"{request_id}.json"
    response = ops / "responses" / request.name
    token = (ops / "control.token").read_text(encoding="ascii")
    atomic_json(request, {"command": command, "owner": owner, "token": token, "created_at": time.time()})
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if response.exists():
                result = json.loads(response.read_text(encoding="utf-8"))
                if not result.get("ok"):
                    raise RuntimeError(result.get("error", "Control failed"))
                return result["result"]
            time.sleep(0.25)
        raise TimeoutError(f"Local control timed out: {command}")
    finally:
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def stopped(root: Path) -> bool:
    try:
        with portalocker.Lock(root / "ops" / "instance.lock", timeout=0):
            return True
    except portalocker.exceptions.LockException:
        return False


def wait_stopped(root: Path, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stopped(root):
            return
        time.sleep(0.5)
    raise TimeoutError("Server is still running; no files may be copied or restored")


def start(root: Path, owner: str = "", *, inspection: bool = False, port: int = 1242) -> None:
    if not stopped(root):
        raise RuntimeError("The managed instance is already running")
    release = json.loads((root / "config" / "current-release.json").read_text(encoding="utf-8"))
    code = Path(release["path"]).resolve()
    python = code / ".venv" / "Scripts" / "python.exe"
    if os.name != "nt":
        python = Path(sys.executable)
    command = [
        str(python), str(code / "scripts" / "company_service.py"), "app", "--root", str(root), "--port", str(port)
    ]
    if owner:
        command.extend(["--maintenance-owner", owner])
    if inspection:
        command.append("--inspection")
    (root / "ops-logs").mkdir(exist_ok=True)
    with (root / "ops-logs" / "app-bootstrap.log").open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=code,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited with {process.returncode}; inspect ops-logs/server.log")
        try:
            ready = control(root, "ready", timeout=3)
            if ready.get("ready"):
                return
        except (OSError, RuntimeError, TimeoutError, ValueError):
            pass
        time.sleep(0.5)
    # Never terminate a process which may have started paid generation.
    raise TimeoutError("Startup readiness failed; instance left running for diagnosis")


def begin_maintenance(root: Path, owner: str, reason: str) -> None:
    marker = root / "ops" / "maintenance.json"
    if marker.exists():
        raise RuntimeError("Existing maintenance marker needs explicit recovery; not taking ownership")
    atomic_json(marker, {"owner": owner, "reason": reason, "created_at": time.time()})


def drain(root: Path, owner: str, timeout: float) -> None:
    control(root, "drain", owner)
    deadline, quiet_since = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        state = control(root, "status")
        if state["active"] == 0:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= 3:
                return
        else:
            quiet_since = None
        time.sleep(0.5)
    raise TimeoutError("Drain timed out; work continues, maintenance marker retained, no copy attempted")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_files(root: Path):
    if root.is_symlink() or (os.name == "nt" and root.is_junction()):
        raise RuntimeError(f"Backup/restore rejects links and junctions: {root}")
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or (os.name == "nt" and path.is_junction()):
                raise RuntimeError(f"Backup/restore rejects links and junctions: {path}")
        for name in files:
            yield Path(directory) / name


def verify_database(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError("Backup is missing arcreel.db")
    # mode=ro can create WAL/SHM. Check a disposable copy, preserving the
    # signed snapshot and reading all committed WAL content (no immutable=1).
    with tempfile.TemporaryDirectory(prefix="xmstudio-db-check-") as scratch:
        database = Path(scratch) / path.name
        shutil.copy2(path, database)
        for suffix in ("-wal", "-shm"):
            companion = path.with_name(path.name + suffix)
            if companion.is_file():
                shutil.copy2(companion, database.with_name(database.name + suffix))
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("SQLite integrity check failed")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("SQLite foreign key check failed")
            if not db.execute("SELECT version_num FROM alembic_version").fetchone():
                raise RuntimeError("Database has no migration revision")


def make_manifest(root: Path) -> dict:
    return {
        "format": 1,
        "created_at": time.time(),
        "files": {
            path.relative_to(root).as_posix(): {"size": path.stat().st_size, "sha256": digest(path)}
            for path in safe_files(root)
            if path != root / "manifest.json"
        },
    }


def verify_snapshot(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise RuntimeError("Unsupported backup manifest")
    for component in COMPONENTS:
        directory = root / component
        if not directory.is_dir():
            raise RuntimeError(f"Backup component missing: {component}")
    if manifest.get("production"):
        for relative in ("release/source.zip", "release/release-info.json", "release/caddy.exe"):
            if relative not in manifest["files"]:
                raise RuntimeError(f"Production backup lacks release artifact: {relative}")
    for relative in manifest["files"]:
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise RuntimeError("Manifest path escapes snapshot")
    actual = {p.relative_to(root).as_posix() for p in safe_files(root) if p != root / "manifest.json"}
    if actual != set(manifest["files"]):
        raise RuntimeError("Backup file set differs from manifest")
    for relative, expected in manifest["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise RuntimeError("Manifest path escapes snapshot")
        if path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
            raise RuntimeError(f"Backup checksum mismatch: {relative}")
    verify_database(root / "data" / "arcreel.db")
    return manifest


def copy_components(source: Path, target: Path) -> None:
    names = (*COMPONENTS, "release") if (source / "release").is_dir() else COMPONENTS
    for name in names:
        directory = source / name
        if not directory.is_dir():
            raise RuntimeError(f"Backup component missing: {name}")
        list(safe_files(directory))  # Reject links before copytree follows one.
        shutil.copytree(directory, target / name)


def capture_release(root: Path, snapshot: Path) -> None:
    current = json.loads((root / "config" / "current-release.json").read_text(encoding="utf-8"))
    code = Path(current["path"]).resolve()
    if not code.is_relative_to(root / "releases"):
        raise RuntimeError("Production backup requires a release inside releases/")
    receipt = json.loads((code / "release-info.json").read_text(encoding="utf-8"))
    archive = Path(receipt["source_archive"])
    if not receipt.get("build_ok") or digest(archive) != receipt["source_sha256"]:
        raise RuntimeError("Release archive or build receipt is invalid")
    config, _ = load_proxy_config(root)
    artifacts = snapshot / "release"
    artifacts.mkdir()
    shutil.copy2(archive, artifacts / "source.zip")
    shutil.copy2(code / "release-info.json", artifacts / "release-info.json")
    shutil.copy2(config["executable"], artifacts / "caddy.exe")


def prune_backups(destination: Path, deployment_id: str) -> list[str]:
    """Only verified direct children from this deployment are eligible."""
    destination = destination.resolve()
    entries = []
    for path in destination.glob("xmstudio-*"):
        if not path.is_dir() or path.is_symlink() or (os.name == "nt" and path.is_junction()):
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("deployment_id") == deployment_id and manifest.get("production"):
                entries.append((manifest["created_at"], path))
        except (OSError, ValueError, KeyError):
            continue
    entries.sort(reverse=True)
    # A corrupt recent snapshot must not displace the last known good one.
    # Validate the entire candidate set before deleting any older version.
    for _, path in entries:
        verify_snapshot(path)
    retained, days, weeks = set(), set(), set()
    for timestamp, path in entries:
        date = dt.datetime.fromtimestamp(timestamp, dt.timezone(dt.timedelta(hours=8)))
        day, week = date.date(), date.isocalendar()[:2]
        if day not in days and len(days) < 7:
            days.add(day)
            retained.add(path)
        if week not in weeks and len(weeks) < 4:
            weeks.add(week)
            retained.add(path)
    removed = []
    for _, path in entries:
        if path in retained:
            continue
        resolved = path.resolve()
        if resolved.parent != destination or not resolved.is_relative_to(destination):
            raise RuntimeError("Retention path escapes the confirmed backup directory")
        verify_snapshot(resolved)
        shutil.rmtree(resolved)
        removed.append(str(resolved))
    return removed


def backup(root: Path, destination: Path, *, timeout: float, local_test: bool = False) -> dict:
    destination = destination.resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise RuntimeError("Backup target must be separate from deployment root")
    if not local_test and not str(destination).startswith("\\\\"):
        raise RuntimeError("Production backup requires the confirmed UNC target on another device")
    if not destination.is_dir():
        raise RuntimeError("Backup target must already exist and be accessible to the runtime account")
    proxy_configured = (root / "config" / "caddy.json").is_file()
    if not local_test and not proxy_configured:
        raise RuntimeError("Production backup requires a configured, managed Caddy host")
    if not local_test:
        settings = json.loads((root / "config" / "deployment.json").read_text(encoding="utf-8"))
        if not settings.get("independent_backup_confirmed") or not settings.get("deployment_id"):
            raise RuntimeError("Confirm an independent backup device and deployment_id in deployment.json")
        if Path(settings["backup_unc"]).resolve() != destination:
            raise RuntimeError("Backup destination differs from the confirmed deployment configuration")
    owner = uuid.uuid4().hex
    result = {"backup_ok": False, "restart_ok": False, "local_test_only": local_test}
    # Lock stays held across drain, copying, validation and readiness/resume.
    with portalocker.Lock(root / "ops" / "operations.lock", timeout=0):
        state = control(root, "status")
        if state.get("inspection"):
            raise RuntimeError("Inspection copies do not run production backup/restart workflows")
        if proxy_configured and not proxy_ready(root):
            raise RuntimeError("Confirm the managed Caddy host is running before beginning backup")
        begin_maintenance(root, owner, "backup")
        stage = root / "staging" / f"backup-{owner}"
        stage.mkdir(parents=True)
        service_stopped = False
        try:
            drain(root, owner, timeout)
            control(root, "stop", owner)
            wait_stopped(root)
            service_stopped = True
            if proxy_configured:
                stop_proxy(root, owner)
                if not proxy_stopped(root):
                    raise RuntimeError("Caddy is still writing; no copy attempted")
            copy_components(root, stage)
            if not local_test:
                capture_release(root, stage)
            manifest = make_manifest(stage)
            manifest.update(production=not local_test, deployment_id=settings["deployment_id"] if not local_test else None)
            atomic_json(stage / "manifest.json", manifest)
            verify_snapshot(stage)
            remote_partial = destination / f".partial-{owner}"
            shutil.copytree(stage, remote_partial)
            verify_snapshot(remote_partial)
            final = destination / f"xmstudio-{time.strftime('%Y%m%d-%H%M%S')}-{owner[:8]}"
            remote_partial.rename(final)
            result.update(backup_ok=True, path=str(final), completed_at=time.time())
            if not local_test:
                atomic_json(root / "ops" / "last-successful-backup.json", result)
        except Exception as exc:
            result["backup_error"] = str(exc)
        finally:
            if service_stopped:
                try:
                    start(root, owner)
                    if proxy_configured and proxy_stopped(root):
                        start_proxy(root, owner)
                    if proxy_configured and not proxy_ready(root):
                        raise RuntimeError("Caddy controller is not ready; maintenance retained")
                    control(root, "resume", owner)
                    result["restart_ok"] = True
                except Exception as exc:
                    result["restart_error"] = str(exc)
            # A drain failure leaves service running and paused for inspection.
            # No timeout or failed copy ever triggers a forced process kill.
            if result["backup_ok"] and not local_test and settings.get("retention_enabled", False):
                try:
                    result["retired_versions"] = prune_backups(destination, settings["deployment_id"])
                except Exception as exc:
                    result["retention_error"] = str(exc)
            atomic_json(root / "ops" / "last-backup.json", result)
    return result


def restore_isolated(snapshot: Path, target: Path) -> dict:
    snapshot, target = snapshot.resolve(), target.resolve()
    if target.exists() or target.is_relative_to(snapshot) or snapshot.is_relative_to(target):
        raise RuntimeError("Restore target must be a new independent directory")
    manifest = verify_snapshot(snapshot)
    target.mkdir(parents=True)
    copy_components(snapshot, target)
    # Keep the restored copy inert. Its credentials must not point it at the
    # original database, Agent profile, or any shared writable runtime path.
    from dotenv import dotenv_values

    config = dotenv_values(target / "config" / "server.env", interpolate=False)
    config["ARCREEL_DATA_DIR"] = str(target / "data")
    config["DATABASE_URL"] = "sqlite+aiosqlite:///" + (target / "data" / "arcreel.db").as_posix()
    config["CLAUDE_CONFIG_DIR"] = str(target / "profile" / "claude")
    config["XMSTUDIO_INSPECTION"] = "true"
    # JSON string values are valid dotenv quoted values and escape backslashes.
    (target / "config" / "server.env").write_text(
        "\n".join(key + "=" + json.dumps(value or "", ensure_ascii=False) for key, value in config.items()) + "\n",
        encoding="utf-8",
    )
    (target / "ops").mkdir()
    (target / "staging").mkdir()
    atomic_json(target / "ops" / "inspection-only.json", {"snapshot": str(snapshot), "created_at": time.time()})
    verify_database(target / "data" / "arcreel.db")
    return {"restored": True, "inspection_only": True, "target": str(target), "files": len(manifest["files"])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["status", "start", "stop", "resume", "backup", "verify", "restore", "inspect"]
    )
    parser.add_argument("--root", type=Path, default=Path(r"D:\XMStudio"))
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--local-test", action="store_true")
    args = parser.parse_args()
    for field, commands in {
        "destination": {"backup", "restore"},
        "snapshot": {"verify", "restore"},
    }.items():
        if args.command in commands and getattr(args, field) is None:
            parser.error(f"{args.command} requires --{field}")
    root = args.root.resolve()
    if args.command == "status":
        result = control(root, "status")
    elif args.command == "verify":
        result = verify_snapshot(args.snapshot.resolve())
        result = {"verified": True, "files": len(result["files"])}
    elif args.command == "restore":
        result = restore_isolated(args.snapshot, args.destination)
    elif args.command == "backup":
        result = backup(root, args.destination, timeout=args.timeout, local_test=args.local_test)
    else:
        with portalocker.Lock(root / "ops" / "operations.lock", timeout=0):
            if args.command in ("start", "inspect"):
                start(root, inspection=args.command == "inspect", port=1243 if args.command == "inspect" else 1242)
                if args.command == "start" and (root / "config" / "caddy.json").exists() and proxy_stopped(root):
                    start_proxy(root)
            elif args.command == "stop":
                owner = uuid.uuid4().hex
                begin_maintenance(root, owner, "operator stop")
                drain(root, owner, args.timeout)
                control(root, "stop", owner)
                wait_stopped(root)
                if (root / "config" / "caddy.json").exists():
                    stop_proxy(root, owner)
            elif args.command == "resume":
                marker = json.loads((root / "ops" / "maintenance.json").read_text(encoding="utf-8"))
                owner = marker["owner"]
                if stopped(root):
                    start(root, owner)
                if (root / "config" / "caddy.json").exists() and proxy_stopped(root):
                    start_proxy(root, owner)
                if (root / "config" / "caddy.json").exists() and not proxy_ready(root):
                    raise RuntimeError("Caddy is not managed and ready; maintenance retained")
                control(root, "resume", owner)
            result = {"ok": True, "command": args.command}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "backup" and not (result["backup_ok"] and result["restart_ok"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
