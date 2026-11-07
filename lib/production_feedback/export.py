"""Portable evidence packages; no environment, credentials or whole database exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

from lib.infra.content_digest import sha256_file
from lib.production_feedback.store import FeedbackStore, canonical, utc_now


def _jsonl(rows) -> bytes:
    return ("\n".join(canonical(row) for row in rows) + "\n").encode()


def csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO()
    columns = [
        "event_id",
        "installation_id",
        "tester_id",
        "test_batch_id",
        "test_case_id",
        "project",
        "project_chain_id",
        "local_sequence",
        "created_at",
        "operation_id",
        "episode_id",
        "event_type",
        "source",
        "action",
        "resource",
        "object_chain_id",
        "before_revision",
        "after_revision",
        "changed_fields",
        "reason",
        "reason_source",
        "before",
        "after",
        "template_digest",
        "task_id",
    ]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: canonical(value) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
                if key in columns
            }
        )
    return ("\ufeff" + output.getvalue()).encode()


def _safe_media(directory: Path, relative: str) -> Path | None:
    path = directory / relative
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return None
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != directory.parent):
        return None
    return path if path.is_file() else None


def export_project(store: FeedbackStore, project: str, *, include_media: bool = False) -> bytes:
    store.baseline_project(project)
    events = store.events(project)
    directory = store.layout.projects_dir / project
    current_chain = store.project_chain(project)
    files = {"events.jsonl": _jsonl(events), "events.csv": csv_bytes(events)}
    from lib.production_feedback.metrics import difference_report, summarize

    files["report.md"] = difference_report(events, summarize(events)).encode("utf-8")
    template_digests = {event["template_digest"] for event in events if event.get("template_digest")}
    with store.connection() as db:
        for identifier in sorted(template_digests):
            row = db.execute(
                "SELECT payload FROM production_feedback_snapshots WHERE digest=?", (identifier,)
            ).fetchone()
            if row:
                files[f"templates/{identifier}.json"] = row[0].encode()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        requests = []
        if "api_calls" in tables:
            # Explicit evidence allowlist. Provider credentials and raw provider responses are excluded.
            columns = {row[1] for row in db.execute("PRAGMA table_info(api_calls)")}
            wanted = [
                key
                for key in (
                    "id",
                    "task_id",
                    "episode_id",
                    "generation_target",
                    "call_type",
                    "model",
                    "provider",
                    "prompt",
                    "inputs",
                    "status",
                    "output_path",
                    "duration_seconds",
                    "aspect_ratio",
                    "resolution",
                    "generate_audio",
                    "started_at",
                    "finished_at",
                    "retry_count",
                )
                if key in columns
            ]
            for row in db.execute(
                f"SELECT {','.join(wanted)} FROM api_calls WHERE project_name=? ORDER BY id", (project,)
            ):
                item = dict(row)
                if isinstance(item.get("inputs"), str):
                    item["inputs"] = json.loads(item["inputs"])
                item["installation_id"] = store.settings()["installation_id"]
                item["project"] = project
                requests.append(item)
        files["requests.jsonl"] = _jsonl(requests)
    media: dict[str, dict] = {}
    # Include every historical version observed by the journal, not just today's selected pointer.
    for event in events:
        if event["resource"] != "versions/versions.json":
            continue
        for value in (event.get("before"), event.get("after")):
            if not isinstance(value, dict):
                continue
            for kind, bucket in value.items():
                if not isinstance(bucket, dict):
                    continue
                for name, version_data in bucket.items():
                    for version in version_data.get("versions", []):
                        relative = version.get("file")
                        if not isinstance(relative, str):
                            continue
                        path = _safe_media(directory, relative)
                        chain = event.get("project_chain_id")
                        if chain != current_chain:
                            path = None
                        # Later snapshots may observe damaged bytes; they cannot replace
                        # the first recorded digest of an immutable version file.
                        previous = media.get(f"{chain}/{relative}", {})
                        expected = (
                            previous.get("sha256")
                            or version.get("content_digest")
                            or event.get("media_evidence", {}).get(relative, {}).get("sha256")
                        )
                        actual = sha256_file(path) if path else None
                        corrupted = bool(expected and actual and expected != actual)
                        if corrupted:
                            path = None
                        package_file = f"media/{chain}/{relative}"
                        item = {
                            "project_chain_id": chain,
                            "resource_type": kind,
                            "resource_id": name,
                            "version": version.get("version"),
                            "file": relative,
                            "prompt": version.get("prompt"),
                            "metadata": {
                                key: value for key, value in version.items() if key not in {"file", "version", "prompt"}
                            },
                            "missing": path is None,
                            "corrupted": corrupted,
                            "sha256": expected or actual,
                            "included": include_media and path is not None,
                            "package_file": package_file if include_media and path is not None else None,
                        }
                        if path:
                            item.update(size=path.stat().st_size)
                            if include_media:
                                files[package_file] = path.read_bytes()
                        media[f"{chain}/{relative}"] = item
    files["media.jsonl"] = _jsonl(list(media.values()))
    manifest = {
        "schema_version": 1,
        "package_id": str(uuid4()),
        "created_at": utc_now(),
        **store.settings(),
        "project": project,
        "project_chain_id": current_chain,
        "event_count": len(events),
        "sequence_range": [
            min((e["local_sequence"] for e in events if e.get("local_sequence")), default=None),
            max((e["local_sequence"] for e in events if e.get("local_sequence")), default=None),
        ],
        "template_digests": sorted(template_digests),
        "application_versions": sorted({e["application_version"] for e in events if e.get("application_version")}),
        "include_media": include_media,
        "media_inspection": "available" if include_media else "not_checked",
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }
    files["manifest.json"] = canonical(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return output.getvalue()


def merge_packages(paths: list[Path], destination: Path) -> dict:
    events: dict[str, dict] = {}
    requests: dict[str, dict] = {}
    manifests = []
    templates: dict[str, bytes] = {}
    media: dict[str, dict] = {}
    media_files: dict[str, bytes] = {}
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("schema_version") != 1:
                raise ValueError(f"Unsupported feedback schema: {path.name}")
            for name, checksum in manifest["files"].items():
                from pathlib import PurePosixPath

                parts = PurePosixPath(name)
                if parts.is_absolute() or ".." in parts.parts or "\\" in name:
                    raise ValueError("Unsafe evidence path")
                if hashlib.sha256(archive.read(name)).hexdigest() != checksum:
                    raise ValueError(f"Evidence checksum mismatch: {path.name}/{name}")
            manifests.append(manifest)
            for line in archive.read("events.jsonl").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                identifier = event["event_id"]
                if identifier in events and events[identifier] != event:
                    raise ValueError(f"Conflicting event_id: {identifier}")
                events[identifier] = event
            for line in archive.read("requests.jsonl").splitlines():
                if line.strip():
                    item = json.loads(line)
                    requests[f"{item['installation_id']}:{item['id']}"] = item
            for line in archive.read("media.jsonl").splitlines():
                if line.strip():
                    item = {
                        **json.loads(line),
                        "installation_id": manifest["installation_id"],
                        "project": manifest["project"],
                    }
                    package_file = item.get("package_file")
                    if item.get("included") and package_file:
                        if package_file not in manifest["files"]:
                            raise ValueError("Media is absent from the verified manifest")
                        target = f"media/{item['installation_id']}/{item.get('project_chain_id')}/{item['file']}"
                        from pathlib import PurePosixPath

                        if PurePosixPath(target).is_absolute() or ".." in PurePosixPath(target).parts or "\\" in target:
                            raise ValueError("Unsafe media path")
                        raw = archive.read(package_file)
                        if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
                            raise ValueError("Media digest does not match its evidence")
                        if target in media_files and media_files[target] != raw:
                            raise ValueError(f"Conflicting media bytes: {target}")
                        media_files[target] = raw
                        item["merged_file"] = target
                    identifier = (
                        f"{item['installation_id']}:{item['project']}:{item.get('project_chain_id')}:{item['file']}"
                    )
                    previous = media.get(identifier)
                    if previous:
                        if previous.get("sha256") and item.get("sha256") and previous["sha256"] != item["sha256"]:
                            raise ValueError(f"Conflicting media digest: {identifier}")
                        # Prefer attached, verified evidence regardless of package order.
                        item = max(
                            (previous, item),
                            key=lambda row: (
                                bool(row.get("included")),
                                bool(row.get("sha256")),
                                not row.get("missing"),
                                not row.get("corrupted"),
                                canonical(row),
                            ),
                        )
                    media[identifier] = item
            for name in manifest["files"]:
                if name.startswith("templates/"):
                    templates[name] = archive.read(name)
    rows = sorted(
        events.values(),
        key=lambda event: (event["installation_id"], event.get("local_sequence") or 0, event["event_id"]),
    )
    from lib.production_feedback.metrics import difference_report, summarize

    summary = summarize(rows)
    summary["media_inspection"] = (
        "not_checked"  # Exported media is available evidence; merging does not inspect pixels.
    )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "events.jsonl").write_bytes(_jsonl(rows))
    (destination / "requests.jsonl").write_bytes(_jsonl(list(requests.values())))
    (destination / "media.jsonl").write_bytes(_jsonl([media[key] for key in sorted(media)]))
    (destination / "events.csv").write_bytes(csv_bytes(rows))
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (destination / "report.md").write_text(difference_report(rows, summary), encoding="utf-8")
    for name, raw in templates.items():
        target = destination / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw)
    for name, raw in media_files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return summary
