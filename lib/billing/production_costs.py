"""Read-only aggregation of stored provider cost snapshots for production."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any


def task_attribution(task: Mapping[str, Any], scripts: Mapping[str, int] | None = None) -> tuple[int | None, str]:
    payload = task.get("payload_json")
    try:
        payload = json.loads(payload) if isinstance(payload, str) else payload
    except (ValueError, TypeError):
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    episode = next(
        (
            payload.get(k)
            for k in ("billing_episode_id", "episode_id", "episode")
            if type(payload.get(k)) is int and payload[k] > 0
        ),
        None,
    )
    script = task.get("script_file") or payload.get("script_file")
    script = script.removeprefix("scripts/") if isinstance(script, str) else None
    if episode is None and scripts is not None:
        episode = scripts.get(script)
    target = json.dumps(
        [task.get("task_type"), task.get("resource_type"), script, task.get("resource_id")], ensure_ascii=False
    )
    return episode, target


def _empty() -> dict[str, Any]:
    return {
        "call_count": 0,
        "known_call_count": 0,
        "amounts": {},
        "unpriced_count": 0,
        "pending_count": 0,
        "reroll_count": 0,
        "reroll_amounts": {},
        "unidentified_target_count": 0,
    }


def aggregate_costs(rows: Iterable[Mapping[str, Any]], scripts: Mapping[str, int]) -> dict[str, Any]:
    episodes = {ep: {**_empty(), "categories": {}} for ep in scripts.values()}
    unassigned = {**_empty(), "categories": {}}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        kind = row["call_type"]
        if kind not in {"text", "image", "video", "audio"} or row["purpose"] in {"assistant_session", "endpoint_trial"}:
            continue
        episode, target = row["episode_id"], row["generation_target"]
        if target is None and row.get("task_type"):
            episode, target = task_attribution(row, scripts)
        bucket = episodes.get(episode, unassigned)
        category = bucket["categories"].setdefault(kind, _empty())
        key = (kind, target)
        reroll = bool(target) and key in seen
        if target:
            seen.add(key)
        for entry in (bucket, category):
            entry["call_count"] += 1
            entry["reroll_count"] += int(reroll)
            entry["unidentified_target_count"] += int(not target)
            if row["status"] == "pending":
                entry["pending_count"] += 1
            elif row["cost_amount"] is None:
                entry["unpriced_count"] += 1
            else:
                entry["known_call_count"] += 1
                currency = row["currency"]
                amount = Decimal(str(row["cost_amount"]))
                entry["amounts"][currency] = entry["amounts"].get(currency, Decimal(0)) + amount
                if reroll:
                    entry["reroll_amounts"][currency] = entry["reroll_amounts"].get(currency, Decimal(0)) + amount

    def finalize(bucket: dict[str, Any]) -> dict[str, Any]:
        for entry in (bucket, *bucket["categories"].values()):
            for field in ("amounts", "reroll_amounts"):
                entry[field] = {c: float(a) for c, a in sorted(entry[field].items())}
        return bucket

    return {
        "episodes": [{"episode": ep, **finalize(bucket)} for ep, bucket in episodes.items()],
        "unassigned": finalize(unassigned),
    }
