"""Project/script/media aggregates projected into independently reviewable objects."""

from __future__ import annotations

from typing import Any


def objects(resource: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if resource == "project.json":
        return {
            f"{kind}/{name}": {
                key: item for key, item in data.items() if key in {"description", "generation_prompt", "aliases"}
            }
            for bucket, kind in (
                ("characters", "character"),
                ("scenes", "scene"),
                ("props", "prop"),
                ("products", "product"),
            )
            for name, data in (value.get(bucket) or {}).items()
        }
    if resource == "versions/versions.json":
        return {
            f"media/{kind}/{name}": data
            for kind, bucket in value.items()
            if isinstance(bucket, dict)
            for name, data in bucket.items()
        }
    items = value.get("video_units", value.get("units", []))
    result = {
        f"unit/{resource}/{unit.get('unit_id', index)}": {
            "sequence_index": index,
            **{
                key: item
                for key, item in unit.items()
                if key not in {"generated_assets", "generation_status", "speech_admission", "updated_at"}
            },
        }
        for index, unit in enumerate(items)
        if isinstance(unit, dict)
    }
    result.update(
        {
            f"draft_asset/{resource}/{asset.get('type')}/{asset.get('name')}": asset
            for asset in value.get("new_assets", [])
            if isinstance(asset, dict)
        }
    )
    return result


def changes(before: Any, after: Any) -> list[str]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["created" if before is None else "deleted" if after is None else "value"]
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
