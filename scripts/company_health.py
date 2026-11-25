"""Read local readiness, disk and backup age without importing the application."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.company_caddy import proxy_ready, proxy_stopped
from scripts.company_ops import control, stopped
from scripts.company_server import atomic_json


def check(root: Path) -> dict:
    result = {
        "checked_at": time.time(), "maintenance": (root / "ops" / "maintenance.json").exists(),
        "issues": [], "stopped": {"app": None, "caddy": None}, "backup_age_hours": None,
    }
    for component, probe in (("app", stopped), ("caddy", proxy_stopped)):
        try:
            result["stopped"][component] = probe(root)
        except Exception as exc:
            result["issues"].append(f"Cannot establish {component} process state: {exc}")
    try:
        if not result["maintenance"]:
            if result["stopped"]["app"] is False:
                result["readiness"] = control(root, "ready", timeout=5)
                if not result["readiness"].get("ready"):
                    result["issues"].append("Application is not ready")
            if any(state is True for state in result["stopped"].values()):
                result["issues"].append("A managed process is absent")
            if result["stopped"]["caddy"] is False and not proxy_ready(root):
                result["issues"].append("Caddy controller is not ready")
    except Exception as exc:
        result["issues"].append(str(exc))
    try:
        usage = shutil.disk_usage(root)
        result["disk"] = {"free_bytes": usage.free, "total_bytes": usage.total}
        if usage.free / usage.total < 0.20 or usage.free < 100 * 1024**3:
            result["issues"].append("Disk below warning threshold")
    except OSError as exc:
        result["issues"].append(f"Cannot inspect disk: {exc}")
    receipt = root / "ops" / "last-successful-backup.json"
    try:
        backup = json.loads(receipt.read_text(encoding="utf-8"))
        age = (time.time() - float(backup["completed_at"])) / 3600
        if not math.isfinite(age) or age < 0 or not backup.get("backup_ok") or backup.get("local_test_only"):
            raise ValueError("Invalid independent backup receipt")
        result["backup_age_hours"] = age
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["issues"].append(f"Cannot establish last independent backup: {exc}")
    if result["backup_age_hours"] is None or result["backup_age_hours"] > 24:
        result["issues"].append("Independent verified backup is missing or older than 24 hours")
    result["ok"] = not result["issues"]
    atomic_json(root / "ops" / "last-health.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(r"D:\XMStudio"))
    args = parser.parse_args()
    result = check(args.root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
