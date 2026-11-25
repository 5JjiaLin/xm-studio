"""Foreground Caddy host, with a protected graceful stop protocol for backups."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portalocker

from scripts.company_server import atomic_json, secure_ops, validate_start


def process_identity(pid: int) -> dict | None:
    """PID plus creation time prevents mistaking a reused PID for our child."""
    if os.name != "nt":
        raise RuntimeError("The company Caddy host requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: process no longer exists
            return None
        raise OSError(error, "Cannot establish whether the Caddy child has exited")
    try:
        status = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        if status.value != 259:  # STILL_ACTIVE
            return None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"pid": pid, "created": (created.dwHighDateTime << 32) | created.dwLowDateTime}
    finally:
        kernel.CloseHandle(handle)


def proxy_stopped(root: Path) -> bool:
    try:
        with portalocker.Lock(root / "ops" / "caddy.lock", timeout=0):
            pass
    except portalocker.exceptions.LockException:
        return False
    record = root / "ops" / "caddy-instance.json"
    if record.exists():
        child = json.loads(record.read_text(encoding="utf-8"))["child"]
        if process_identity(child["pid"]) == child:
            # The supervisor may have crashed. Do not copy a live CA store.
            return False
    return True


def load_config(root: Path) -> tuple[dict, dict]:
    config = json.loads((root / "config" / "caddy.json").read_text(encoding="utf-8"))
    binary = Path(config["executable"])
    with binary.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if not hmac.compare_digest(actual, config["sha256"].lower()):
        raise RuntimeError("Caddy binary checksum does not match deployment record")
    bind = ipaddress.ip_address(config["bind_ip"])
    admin_host, admin_port = config.get("admin_address", "127.0.0.1:2019").rsplit(":", 1)
    if not ipaddress.ip_address(admin_host).is_loopback or not 1024 <= int(admin_port) <= 65535:
        raise RuntimeError("Caddy admin address must be a loopback high port")
    inspection = (root / "ops" / "inspection-only.json").exists()
    if inspection and not bind.is_loopback:
        raise RuntimeError("Inspection proxy must bind only to loopback")
    backend = int(config.get("backend_port", 1242))
    if backend != 1242 and not inspection:
        raise RuntimeError("Production Caddy backend must use port 1242")
    environment = os.environ.copy()
    environment.update(
        XMSTUDIO_ROOT=root.as_posix(),
        XMSTUDIO_SITE_ADDRESS=config["site_address"],
        XMSTUDIO_BIND_IP=str(bind),
        XMSTUDIO_ADMIN_ADDRESS=f"{admin_host}:{admin_port}",
        XMSTUDIO_BACKEND_PORT=str(backend),
        # Keep autosave and other Caddy configuration out of the desktop profile.
        XDG_CONFIG_HOME=str(root / "config" / "caddy-runtime"),
        XDG_DATA_HOME=str(root / "caddy-data"),
    )
    return config, environment


def run_proxy(root: Path, owner: str | None) -> int:
    (root / "ops").mkdir(exist_ok=True)
    secure_ops(root / "ops")
    with portalocker.Lock(root / "ops" / "caddy.lock", timeout=0):
        validate_start(root, owner)
        config, environment = load_config(root)
        admin_host, admin_port = config.get("admin_address", "127.0.0.1:2019").rsplit(":", 1)
        with socket.socket() as probe:
            probe.bind((admin_host, int(admin_port)))
        (root / "ops-logs" / "caddy").mkdir(parents=True, exist_ok=True)
        command = [
            config["executable"],
            "run",
            "--config",
            str(root / "config" / "Caddyfile"),
            "--adapter",
            "caddyfile",
        ]
        child = subprocess.Popen(
            command, env=environment, stdin=subprocess.DEVNULL,
            stdout=sys.stdout, stderr=sys.stderr, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        identity = process_identity(child.pid)
        if identity is None:
            return child.wait()
        record = root / "ops" / "caddy-instance.json"
        atomic_json(record, {"child": identity, "started_at": time.time()})
        request_file = root / "ops" / "caddy-stop.json"
        while child.poll() is None:
            if request_file.exists():
                request = json.loads(request_file.read_text(encoding="utf-8"))
                response = {"id": request.get("id"), "ok": False}
                try:
                    if abs(time.time() - request["created_at"]) > 120:
                        raise RuntimeError("Expired Caddy stop request")
                    validate_start(root, request.get("owner"))
                    subprocess.run(
                        [config["executable"], "stop", "--address", f"{admin_host}:{admin_port}"],
                        env=environment,
                        check=True,
                        timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    # No termination fallback: the controller confirms child exit.
                    response["ok"] = True
                except Exception as exc:
                    response["error"] = str(exc)
                atomic_json(root / "ops" / "caddy-stop-result.json", response)
                request_file.unlink(missing_ok=True)
            time.sleep(0.25)
        code = child.wait()
        record.unlink(missing_ok=True)
        return code


def start_proxy(root: Path, owner: str = "", timeout: float = 30) -> None:
    if not proxy_stopped(root):
        raise RuntimeError("Caddy is already running, or its previous child has not exited")
    release = json.loads((root / "config" / "current-release.json").read_text(encoding="utf-8"))
    code = Path(release["path"])
    config, _ = load_config(root)
    command = [
        str(code / ".venv" / "Scripts" / "python.exe"),
        str(code / "scripts" / "company_service.py"),
        "caddy",
        "--root",
        str(root),
    ]
    if owner:
        command.extend(["--maintenance-owner", owner])
    (root / "ops-logs").mkdir(exist_ok=True)
    with (root / "ops-logs" / "caddy-bootstrap.log").open("wb") as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW
        )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Caddy host exited with {process.returncode}; inspect caddy-host.log")
        try:
            with opener.open(f"http://{config.get('admin_address', '127.0.0.1:2019')}/config/", timeout=1) as response:
                value = json.load(response)
                storage = value.get("storage", {}) if isinstance(value, dict) else {}
                if storage.get("root") == (root / "caddy-data").as_posix() and not proxy_stopped(root):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise TimeoutError("Caddy readiness timed out; process left for diagnosis")


def proxy_ready(root: Path) -> bool:
    """A live child alone is insufficient: require its controller and storage."""
    try:
        with portalocker.Lock(root / "ops" / "caddy.lock", timeout=0):
            return False
    except portalocker.exceptions.LockException:
        pass
    record = json.loads((root / "ops" / "caddy-instance.json").read_text(encoding="utf-8"))
    if process_identity(record["child"]["pid"]) != record["child"]:
        return False
    config, _ = load_config(root)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{config.get('admin_address', '127.0.0.1:2019')}/config/", timeout=5) as response:
        return json.load(response).get("storage", {}).get("root") == (root / "caddy-data").as_posix()


def stop_proxy(root: Path, owner: str, timeout: float = 30) -> None:
    validate_start(root, owner)
    if proxy_stopped(root):
        return
    request_id = uuid.uuid4().hex
    atomic_json(root / "ops" / "caddy-stop.json", {"id": request_id, "owner": owner, "created_at": time.time()})
    result_file = root / "ops" / "caddy-stop-result.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            if result.get("id") == request_id and not result.get("ok"):
                raise RuntimeError(result.get("error", "Caddy stop failed"))
        if proxy_stopped(root):
            return
        time.sleep(0.25)
    raise TimeoutError("Caddy has not exited; CA storage must not be copied")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(r"D:\XMStudio"))
    parser.add_argument("--maintenance-owner")
    args = parser.parse_args()
    raise SystemExit(run_proxy(args.root.resolve(), args.maintenance_owner))


if __name__ == "__main__":
    main()
