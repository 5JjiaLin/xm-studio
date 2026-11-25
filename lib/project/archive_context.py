"""Read-only generation context for projects with archived episodes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lib.infra.path_safety import safe_join
from lib.project.asset_types import ASSET_SPECS, DERIVATIVES_FIELD, asset_name_comparison_key

# Generation snapshot only, never persisted to project.json.
ARCHIVED_ASSET_NAMES = "_archived_asset_names"


def generation_project(project_path: Path, project: Mapping[str, Any], *, episode: int | None = None) -> dict:
    """Exclude archived-only assets without changing files or shared identities.

    Current scripts and plans provide references for legacy archives as well.
    History/trash are deliberately not scanned. Unreadable files fail closed:
    silently ignoring them could hide an asset shared with an active episode.
    """
    archived = set((project.get("production_management") or {}).get("archived_episodes", []))
    if not archived or episode in archived:
        return dict(project)

    from lib.script.script_references import payload_reference_names
    from lib.script.script_review import script_plan_path

    active_names: set[str] = set()
    archived_names: set[str] = set()
    for entry in project.get("episodes", []):
        number = entry["episode"]
        names: set[str] = set()
        script = entry.get("script_file")
        paths = [safe_join(project_path, script)] if script else []
        plan = script_plan_path(project_path, dict(project), number)
        if plan is not None:
            paths.append(plan)
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            refs = payload_reference_names(payload)
            if path == plan and isinstance(payload, dict):
                new_items = payload.get("new_assets") or []
                # A new same-name declaration does not mean intentional sharing
                # with an archived asset. Materialized scripts remain authoritative.
                temporary = {
                    asset_name_comparison_key(item["name"])
                    for item in new_items
                    if isinstance(item, dict) and item.get("name") and item.get("decision", "register") == "register"
                }
                if number in archived:
                    refs.update(temporary)
                    refs.update(
                        asset_name_comparison_key(item["asset_name"])
                        for item in new_items if isinstance(item, dict) and item.get("asset_name")
                    )
                else:
                    refs.difference_update(temporary)
                    refs.difference_update(
                        asset_name_comparison_key(item["asset_name"])
                        for item in new_items
                        if isinstance(item, dict) and item.get("asset_name") and item.get("decision", "register") == "register"
                    )
                    refs.difference_update(
                        asset_name_comparison_key(f"{item['target']}/{item.get('asset_name') or item['name']}")
                        for item in new_items
                        if isinstance(item, dict) and item.get("decision") == "derivative" and item.get("target") and item.get("name")
                    )
            names.update(refs)
        names.update(name.split("/", 1)[0] for name in tuple(names) if "/" in name)
        (archived_names if number in archived else active_names).update(names)

    excluded = archived_names - active_names
    result = dict(project)
    reserved = {key: list(value) for key, value in project.get(ARCHIVED_ASSET_NAMES, {}).items()}
    for spec in ASSET_SPECS.values():
        bucket = project.get(spec.bucket_key)
        if not isinstance(bucket, Mapping):
            continue
        hidden = [name for name in bucket if asset_name_comparison_key(name) in excluded]
        reserved[spec.bucket_key] = sorted(set(reserved.get(spec.bucket_key, [])) | set(hidden))
        result[spec.bucket_key] = {name: asset for name, asset in bucket.items() if name not in hidden}
        if spec.supports_derivatives:
            for name, asset in result[spec.bucket_key].items():
                if not isinstance(asset, Mapping) or not isinstance(asset.get(DERIVATIVES_FIELD), Mapping):
                    continue
                derivatives = asset[DERIVATIVES_FIELD]
                hidden_forms = {
                    form for form in derivatives
                    if asset_name_comparison_key(f"{name}/{form}") in excluded
                }
                if hidden_forms:
                    result[spec.bucket_key][name] = {
                        **asset, DERIVATIVES_FIELD: {form: value for form, value in derivatives.items() if form not in hidden_forms},
                    }
                    reserved[spec.bucket_key] = sorted(set(reserved[spec.bucket_key]) | {f"{name}/{form}" for form in hidden_forms})
    result[ARCHIVED_ASSET_NAMES] = reserved
    return result
