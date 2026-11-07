"""The project-level sequential text batch journal; no queue or media state lives here."""

import json
from pathlib import Path

ACTIVE_STATUSES = {"active", "submitting", "stopping", "unknown"}


def batch_path(project_dir: Path) -> Path:
    return project_dir / "production" / "script_batch.json"


def read_batch(project_dir: Path) -> dict:
    try:
        return json.loads(batch_path(project_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "idle", "items": []}


def require_batch_idle(project_dir: Path, batch_id: str | None = None) -> None:
    state = read_batch(project_dir)
    if state["status"] in ACTIVE_STATUSES and (not batch_id or state.get("id") != batch_id):
        raise ValueError("项目正在逐集拆解，请先停止批量拆解并等待当前集结束")
