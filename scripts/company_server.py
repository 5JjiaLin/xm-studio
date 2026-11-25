"""Single-process Windows host with ACL-protected file control (no HTTP control API).

Run via scripts/windows-company/*.ps1. The operations controller owns backup,
maintenance markers and the operations lock; this process owns the instance lock.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portalocker
from dotenv import dotenv_values

from scripts.company_windows_loop import run as run_event_loop

CODE_ROOT = Path(__file__).resolve().parents[1]


def secure_ops(path: Path) -> None:
    """Only the runtime identity, SYSTEM and local administrators can control."""
    if os.name != "nt":
        path.chmod(0o700)
        return
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW}
    system32 = Path(os.environ["SYSTEMROOT"]) / "System32"
    row = next(
        csv.reader(
            subprocess.check_output(
                [str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"], text=True, **flags
            ).splitlines()
        )
    )
    subprocess.run(
        [
            str(system32 / "icacls.exe"),
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{row[1]}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
            "/Q",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        **flags,
    )


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_environment(root: Path, env_file: Path) -> None:
    if not env_file.is_file():
        raise RuntimeError(f"Missing deployment environment: {env_file}")
    values = dotenv_values(env_file, interpolate=False)
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    if os.environ.get("AUTH_ENABLED", "").lower() != "true":
        raise RuntimeError("Company deployment requires AUTH_ENABLED=true")
    data = Path(os.environ.get("ARCREEL_DATA_DIR", "")).resolve()
    if data != root / "data":
        raise RuntimeError("ARCREEL_DATA_DIR must be the deployment root's data directory")
    database_url = "sqlite+aiosqlite:///" + (data / "arcreel.db").as_posix()
    if values.get("DATABASE_URL") != database_url:
        raise RuntimeError("DATABASE_URL must explicitly point to this deployment's data/arcreel.db")
    if not values.get("AUTH_PASSWORD") or values["AUTH_PASSWORD"].startswith("REPLACE_"):
        raise RuntimeError("Company deployment requires an explicit AUTH_PASSWORD")
    # A service account's inherited environment and a developer .env must not
    # redirect the isolated deployment to source-tree data or SDK credentials.
    os.environ["PYTHON_DOTENV_DISABLED"] = "true"
    os.environ["XMSTUDIO_COMPANY_HOST"] = "true"
    os.environ["ARCREEL_PROFILE_DIR"] = str(root / "profile" / "agent")
    os.environ["ARCREEL_API_BASE"] = "http://127.0.0.1:1242/api/v1"
    os.environ["CLAUDE_CONFIG_DIR"] = str(root / "profile" / "claude")
    os.environ["USERPROFILE"] = str(root / "profile")
    os.environ["HOME"] = str(root / "profile")


def validate_start(root: Path, owner: str | None) -> bool:
    marker = root / "ops" / "maintenance.json"
    if not marker.exists():
        if owner:
            raise RuntimeError("Maintenance owner supplied without a persistent marker")
        return False
    value = json.loads(marker.read_text(encoding="utf-8"))
    if not owner or not hmac.compare_digest(value.get("owner", ""), owner):
        raise RuntimeError("Maintenance marker prevents unattended restart")
    # Only the controller holding the operations lock can start the paused host.
    try:
        with portalocker.Lock(root / "ops" / "operations.lock", timeout=0):
            raise RuntimeError("Maintenance restart requires the controller's operations lock")
    except portalocker.exceptions.LockException:
        pass
    return True


async def run_host(root: Path, *, owner: str | None, inspection: bool, port: int) -> None:
    import uvicorn

    from lib.infra.server_runtime import runtime
    from server.app import app
    from server.company_runtime import activity_snapshot, disk_status, readiness

    ops = root / "ops"
    inbox, outbox = ops / "requests", ops / "responses"
    await asyncio.to_thread(inbox.mkdir, exist_ok=True)
    await asyncio.to_thread(outbox.mkdir, exist_ok=True)
    token = secrets.token_urlsafe(48)
    await asyncio.to_thread((ops / "control.token").write_text, token, encoding="ascii")
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        workers=1,
        loop="asyncio",
        proxy_headers=False,
        timeout_graceful_shutdown=None,
    )
    server = uvicorn.Server(config)
    identity = {
        "pid": os.getpid(),
        "port": port,
        "release": str(CODE_ROOT),
        "inspection": inspection,
        "started_at": time.time(),
    }
    await asyncio.to_thread(atomic_json, ops / "instance.json", identity)

    async def snapshot() -> dict:
        # A live actor may receive an autonomous turn after reporting idle.
        # Observing status must never disconnect it or cancel work. Wait for
        # the session manager's normal idle eviction and actual actor exit.
        manager = getattr(app.state, "company_session_manager", None)
        value = activity_snapshot(app)
        value["live_agents"] = len(manager.sessions) if manager else 0
        value["active"] += value["live_agents"]
        return value

    async def process(request: dict) -> dict:
        if not hmac.compare_digest(str(request.get("token", "")), token):
            raise RuntimeError("Unauthorized control request")
        command = request.get("command")
        if command == "status":
            return {**await snapshot(), "initialized": runtime.initialized, **identity}
        if command == "ready":
            return await readiness(app)
        marker = json.loads(await asyncio.to_thread((ops / "maintenance.json").read_text, encoding="utf-8"))
        if not hmac.compare_digest(str(request.get("owner", "")), marker.get("owner", "")):
            raise RuntimeError("Maintenance owner does not match")
        # The owner token alone does not authorize a command after the
        # controller crashed and released its lock.
        await asyncio.to_thread(validate_start, root, str(request.get("owner", "")))
        if command == "drain":
            runtime.maintenance = True
            return await snapshot()
        if command == "resume":
            if inspection:
                raise RuntimeError("Inspection instances cannot resume work")
            await readiness(app)
            # Controller first requests this while still holding the lock. The
            # server clears the marker before admitting any new work.
            await asyncio.to_thread((ops / "maintenance.json").unlink)
            runtime.maintenance = False
            worker = getattr(app.state, "generation_worker", None)
            if worker:
                worker.wake()
            return await snapshot()
        if command == "stop":
            state = await snapshot()
            if not runtime.maintenance or state["active"]:
                raise RuntimeError("Refusing to stop: server has not drained")
            server.should_exit = True
            return state
        raise RuntimeError("Unknown control command")

    async def control_loop() -> None:
        next_disk_check = 0.0
        while not server.should_exit:
            if runtime.initialized and time.monotonic() >= next_disk_check:
                disk = await asyncio.to_thread(disk_status, root / "data")
                runtime.storage_critical = disk["critical"]
                next_disk_check = time.monotonic() + 5
            # All file operations execute outside the application event loop.
            paths = await asyncio.to_thread(lambda: list(inbox.glob("*.json")))
            for path in paths:
                if len(path.stem) != 32 or any(c not in "0123456789abcdef" for c in path.stem):
                    continue
                try:
                    raw = await asyncio.to_thread(path.read_bytes)
                    if len(raw) > 4096:
                        raise ValueError("Control request too large")
                    request = json.loads(raw)
                    # Stale requests from a previous process cannot stop this host.
                    if abs(time.time() - float(request.get("created_at", 0))) > 120:
                        raise RuntimeError("Expired control request")
                    result = {"ok": True, "result": await process(request)}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                await asyncio.to_thread(atomic_json, outbox / path.name, result)
                await asyncio.to_thread(path.unlink, missing_ok=True)
            await asyncio.sleep(0.25)

    control_task = asyncio.create_task(control_loop())
    try:
        await server.serve()
    finally:
        control_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await control_task
        await asyncio.to_thread((ops / "instance.json").unlink, missing_ok=True)
        await asyncio.to_thread((ops / "control.token").unlink, missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(r"D:\XMStudio"))
    parser.add_argument("--env", type=Path)
    parser.add_argument("--maintenance-owner")
    parser.add_argument("--inspection", action="store_true")
    parser.add_argument("--port", type=int, default=1242)
    args = parser.parse_args()
    root = args.root.resolve()
    load_environment(root, args.env or root / "config" / "server.env")
    (root / "ops").mkdir(parents=True, exist_ok=True)
    secure_ops(root / "ops")
    if (root / "ops" / "inspection-only.json").exists() and not args.inspection:
        raise RuntimeError("Restored inspection copy cannot run production work")
    paused = validate_start(root, args.maintenance_owner)
    os.environ["XMSTUDIO_START_MAINTENANCE"] = str(paused).lower()
    os.environ["XMSTUDIO_INSPECTION"] = str(args.inspection).lower()
    if args.port != 1242 and not args.inspection:
        raise RuntimeError("Production internal API port must be 1242")
    with portalocker.Lock(root / "ops" / "instance.lock", timeout=0):
        run_event_loop(run_host(root, owner=args.maintenance_owner, inspection=args.inspection, port=args.port))


if __name__ == "__main__":
    main()
