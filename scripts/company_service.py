"""Foreground child host with bounded UTF-8 logs and real exit propagation."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portalocker


def host(root: Path, component: str, arguments: list[str]) -> int:
    code = Path(__file__).resolve().parents[1]
    script = code / "scripts" / ("company_server.py" if component == "app" else "company_caddy.py")
    (root / "ops-logs").mkdir(exist_ok=True)
    log_name = "server" if component == "app" else "caddy-host"
    logger = logging.getLogger(f"company.{component}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        root / "ops-logs" / f"{log_name}.log", maxBytes=20 * 1024**2, backupCount=10, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        with portalocker.Lock(root / "ops" / f"{component}-host.lock", timeout=0):
            child = subprocess.Popen(
                [sys.executable, "-u", str(script), "--root", str(root), *arguments],
                cwd=code,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            assert child.stdout is not None
            with child.stdout:
                while chunk := child.stdout.readline(64 * 1024):
                    logger.info(chunk.decode("utf-8", errors="replace").rstrip("\r\n"))
            exit_code = child.wait()
            logger.info("Managed %s child exited with code %d", component, exit_code)
            return exit_code
    finally:
        logger.removeHandler(handler)
        handler.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=["app", "caddy"])
    parser.add_argument("--root", type=Path, required=True)
    args, arguments = parser.parse_known_args()
    raise SystemExit(host(args.root.resolve(), args.component, arguments))


if __name__ == "__main__":
    main()
