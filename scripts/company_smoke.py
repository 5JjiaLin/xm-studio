"""Start a fresh, isolated inspection instance and exercise the real IPC/HTTP host.

Never copies production data or credentials and never starts generation workers.
The target must not exist. Keep the evidence directory for review.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portalocker

from scripts.company_caddy import proxy_stopped, start_proxy, stop_proxy
from scripts.company_ops import (
    begin_maintenance,
    control,
    copy_components,
    digest,
    make_manifest,
    restore_isolated,
    start,
    stopped,
    verify_snapshot,
    wait_stopped,
)
from scripts.company_server import CODE_ROOT, atomic_json


def http_status(url: str, method: str = "GET", context: ssl.SSLContext | None = None) -> int:
    # Local checks must not inherit the operator's external HTTP proxy.
    handlers = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(urllib.request.Request(url, method=method), timeout=10) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        with exc:
            exc.read()
            return exc.code


def smoke(root: Path, port: int, caddy: Path | None = None) -> dict:
    root = root.resolve()
    if root.exists():
        raise RuntimeError("Smoke test requires a new directory; never reuse production data")
    for name in ("data", "config", "ops", "staging", "profile/claude", "caddy-data"):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copytree(CODE_ROOT / "agent_runtime_profile", root / "profile" / "agent")
    values = {
        "AUTH_ENABLED": "true",
        "AUTH_USERNAME": "inspection-only",
        "AUTH_PASSWORD": secrets.token_urlsafe(32),
        "ARCREEL_DATA_DIR": (root / "data").as_posix(),
        "DATABASE_URL": "sqlite+aiosqlite:///" + (root / "data" / "arcreel.db").as_posix(),
        "ARCREEL_LOG_FILE_DISABLED": "true",
        "PYTHONUTF8": "1",
    }
    (root / "config" / "server.env").write_text(
        "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()), encoding="utf-8"
    )
    atomic_json(root / "config" / "current-release.json", {"path": str(CODE_ROOT), "smoke_only": True})
    atomic_json(root / "ops" / "inspection-only.json", {"fresh_empty_data": True})
    if caddy:
        shutil.copy2(CODE_ROOT / "deploy" / "windows-company" / "Caddyfile.example", root / "config" / "Caddyfile")
        atomic_json(
            root / "config" / "caddy.json",
            {
                "executable": str(caddy.resolve()),
                "sha256": digest(caddy),
                "site_address": "https://localhost:8443",
                "bind_ip": "127.0.0.1",
                "admin_address": "127.0.0.1:2020",
                "backend_port": port,
            },
        )
    result = {"started_at": time.time(), "root": str(root), "port": port, "production_data_used": False}
    with portalocker.Lock(root / "ops" / "operations.lock", timeout=0):
        try:
            start(root, inspection=True, port=port)
            ready = control(root, "ready")
            state = control(root, "status")
            assert ready["ready"]
            assert ready["inspection"]
            assert state["inspection"]
            assert state["agents"] == 0
            assert state["generation_tasks"] == 0
            base = f"http://127.0.0.1:{port}"
            checks = {
                "health": http_status(base + "/health"),
                "readiness": http_status(base + "/_ops/ready"),
                "api_read_blocked": http_status(base + "/api/v1/projects"),
                "api_write_blocked": http_status(base + "/api/v1/projects", "POST"),
                "mcp_blocked": http_status(base + "/mcp", "POST"),
            }
            assert checks == {
                "health": 200,
                "readiness": 200,
                "api_read_blocked": 503,
                "api_write_blocked": 503,
                "mcp_blocked": 503,
            }, checks
            result.update(ready=ready, state=state, http=checks)
            # A real client reset previously left Proactor Server.wait_closed
            # stuck forever on Windows despite zero active requests.
            with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
                connection.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
            time.sleep(0.3)
            assert http_status(base + "/health") == 200
            result["client_reset_survived"] = True
            if caddy:
                start_proxy(root)
                ca = root / "caddy-data" / "pki" / "authorities" / "local" / "root.crt"
                context = ssl.create_default_context(cafile=ca)
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
                )
                with opener.open("https://localhost:8443/health", timeout=15) as response:
                    response.read()
                    assert response.status == 200
                assert http_status("https://localhost:8443/_ops/ready", context=context) == 404
                result["https"] = {"verified_ca": True, "health": 200, "operations_hidden": True}
        finally:
            owner = uuid.uuid4().hex
            if not stopped(root) or (caddy and not proxy_stopped(root)):
                begin_maintenance(root, owner, "isolated inspection test complete")
            if not stopped(root):
                control(root, "drain", owner)
                control(root, "stop", owner)
                wait_stopped(root)
                result["gracefully_stopped"] = True
            if caddy and not proxy_stopped(root):
                stop_proxy(root, owner)
                result["caddy_gracefully_stopped"] = True
            atomic_json(root / "ops" / "smoke-result.json", result)
    snapshot = root.with_name(root.name + "-snapshot")
    snapshot.mkdir()
    copy_components(root, snapshot)
    atomic_json(snapshot / "manifest.json", make_manifest(snapshot))
    verify_snapshot(snapshot)
    restored = root.with_name(root.name + "-restored")
    result["restore"] = restore_isolated(snapshot, restored)
    with portalocker.Lock(restored / "ops" / "operations.lock", timeout=0):
        try:
            start(restored, inspection=True, port=port)
            assert control(restored, "ready")["inspection"]
            result["restore"]["inspection_start_passed"] = True
        finally:
            if not stopped(restored):
                owner = uuid.uuid4().hex
                begin_maintenance(restored, owner, "restored inspection complete")
                control(restored, "drain", owner)
                control(restored, "stop", owner)
                wait_stopped(restored)
    atomic_json(root / "ops" / "smoke-result.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=1243)
    parser.add_argument("--caddy", type=Path)
    args = parser.parse_args()
    print(json.dumps(smoke(args.root, args.port, args.caddy), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
