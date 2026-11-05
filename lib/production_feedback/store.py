"""SQLite evidence journal with prepared JSON writes and immutable revisions."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

import portalocker

from lib.infra.content_digest import sha256_file
from lib.infra.data_root_layout import DataRootLayout
from lib.infra.json_io import atomic_write_json as plain_write_json
from lib.production_feedback.context import FeedbackContext, current
from lib.production_feedback.objects import changes, objects
from lib.project.task_project_claim import current_task_project_claim

logger = logging.getLogger(__name__)
_APPLICATION_VERSION = f"XM_Studio/{version('arcreel')}"
_roots: dict[Path, FeedbackStore] = {}
_roots_lock = threading.RLock()
_active_preparations: set[str] = set()
_active_lock = threading.RLock()
_owner_locks: dict[Path, tuple[str, Any]] = {}
_transaction: ContextVar[list[tuple[FeedbackStore, str]] | None] = ContextVar("feedback_file_transaction", default=None)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_feedback_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS production_feedback_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, operation_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS production_feedback_prepared (id TEXT PRIMARY KEY, resource TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_feedback_pending ON production_feedback_prepared(resource,state);
CREATE TABLE IF NOT EXISTS production_feedback_snapshots (digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS production_feedback_objects (project TEXT NOT NULL, object_key TEXT NOT NULL, chain_id TEXT NOT NULL, active INTEGER NOT NULL, PRIMARY KEY(project,object_key));
CREATE TABLE IF NOT EXISTS production_feedback_requests (operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, status INTEGER, response TEXT);
"""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def register_root(root: Path) -> FeedbackStore:
    root = Path(root).resolve()
    with _roots_lock:
        if root not in _roots:
            _roots[root] = FeedbackStore(root)
        return _roots[root]


def _locate(path: Path) -> tuple[FeedbackStore, str, str] | None:
    resolved = path.absolute()
    for root, store in tuple(_roots.items()):
        try:
            rel = resolved.relative_to(DataRootLayout(root).projects_dir)
        except ValueError:
            continue
        if len(rel.parts) < 2:
            continue
        resource = Path(*rel.parts[1:]).as_posix()
        if (
            resource == "project.json"
            or resource == "versions/versions.json"
            or (resource.endswith(".json") and rel.parts[1] in {"scripts", "drafts"})
        ):
            return store, rel.parts[0], resource
    return None


def read_value(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def content(value: Any, resource: str) -> Any:
    """Discard volatile bookkeeping, preserve all production content and source switches."""
    if resource != "project.json" or not isinstance(value, dict):
        return value
    return {
        key: value.get(key)
        for key in ("characters", "scenes", "props", "products", "episodes", "style", "style_description")
        if key in value
    }


@contextmanager
def transaction() -> Generator[None]:
    if _transaction.get() is not None:
        yield
        return
    pending: list[tuple[FeedbackStore, str]] = []
    token = _transaction.set(pending)
    try:
        yield
    finally:
        _transaction.reset(token)
        for store, preparation in dict.fromkeys(pending):
            try:
                with _active_lock:
                    _active_preparations.discard(preparation)
                store._recover_fallbacks()
                store.reconcile_one(preparation)
            except Exception:
                # The prepared row survives. Media completion must not become another paid request.
                logger.exception("Feedback completion deferred: %s", preparation)


def atomic_write_json(path: Path, data: Any) -> None:
    location = _locate(path)
    if location is None:
        plain_write_json(path, data)
        return
    store, project, resource = location
    before = read_value(path)
    if content(before, resource) == content(data, resource):
        plain_write_json(path, data)
        return
    try:
        preparation = store.prepare(project, resource, before, data, live=True)
    except (OSError, sqlite3.Error):
        if current_task_project_claim() is None:
            raise
        # Paid output already has a recoverable task/checkpoint/version path.
        # Preserve its metadata even when the evidence database is unavailable.
        logger.exception("Paid output feedback deferred to a durable local preparation")
        preparation = store.prepare_fallback(project, resource, before, data)
    with _active_lock:
        _active_preparations.add(preparation)
    try:
        plain_write_json(path, data)
    except BaseException:
        with _active_lock:
            _active_preparations.discard(preparation)
        raise
    pending = _transaction.get()
    if pending is not None:
        pending.append((store, preparation))
    else:
        try:
            with _active_lock:
                _active_preparations.discard(preparation)
            store._recover_fallbacks()
            store.reconcile_one(preparation)
        except Exception:
            logger.exception("Feedback completion deferred: %s", preparation)


class FeedbackStore:
    def __init__(self, root: Path):
        self.layout = DataRootLayout(Path(root))
        self.db = self.layout.sqlite_db_path
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO production_feedback_settings VALUES(1,?)",
                (
                    canonical(
                        {
                            "installation_id": str(uuid4()),
                            "tester_id": "",
                            "test_batch_id": "",
                            "test_case_id": "",
                        }
                    ),
                ),
            )
        self._template_digest: str | None = None
        self._last_settings = self.settings()

    def project_chain(self, project: str) -> str:
        """A feedback-only marker follows trash/restore and separates same-name recreation."""
        path = self.layout.projects_dir / project / ".production-feedback-id"
        with path.open("a+", encoding="utf-8") as stream:
            portalocker.lock(stream, portalocker.LOCK_EX)
            stream.seek(0)
            identifier = stream.read().strip()
            if not identifier:
                identifier = str(uuid4())
                stream.write(identifier)
                stream.flush()
                os.fsync(stream.fileno())
            return identifier

    def _owner(self) -> str:
        with _active_lock:
            root = self.layout.root.resolve()
            if root not in _owner_locks:
                directory = self.layout.production_feedback_pending_dir / "owners"
                directory.mkdir(parents=True, exist_ok=True)
                identifier = str(uuid4())
                handle = (directory / f"{identifier}.lock").open("a+b")
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                _owner_locks[root] = (identifier, handle)
            return _owner_locks[root][0]

    def _owner_alive(self, identifier: str | None) -> bool:
        if not identifier:
            return False
        if identifier == (_owner_locks.get(self.layout.root.resolve()) or (None,))[0]:
            return False
        path = self.layout.production_feedback_pending_dir / "owners" / f"{identifier}.lock"
        try:
            with path.open("a+b") as handle:
                try:
                    portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                except portalocker.LockException:
                    return True
                portalocker.unlock(handle)
        except FileNotFoundError:
            return False
        return False

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self) -> dict:
        with self.connection() as db:
            result = json.loads(db.execute("SELECT payload FROM production_feedback_settings WHERE id=1").fetchone()[0])
            self._last_settings = result
            return result

    def save_settings(self, values: dict) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = json.loads(
                db.execute("SELECT payload FROM production_feedback_settings WHERE id=1").fetchone()[0]
            )
            settings.update(
                {
                    key: str(values[key]).strip()
                    for key in ("tester_id", "test_batch_id", "test_case_id")
                    if key in values
                }
            )
            db.execute("UPDATE production_feedback_settings SET payload=? WHERE id=1", (canonical(settings),))
        self._last_settings = settings
        return settings

    def template_snapshot(self) -> str:
        if self._template_digest is None:
            from lib.prompts.prompt_templates.builtin import builtin_templates

            package = {}
            for metadata in builtin_templates.list_templates():
                body, partials = builtin_templates.read_source(metadata.id)
                package[f"templates/{metadata.id}"] = {"metadata": metadata.model_dump(mode="json"), "body": body}
                for partial in partials:
                    package[f"partials/{partial.name}"] = partial.model_dump(mode="json")
            identifier = digest(package)
            with self.connection() as db:
                db.execute(
                    "INSERT OR IGNORE INTO production_feedback_snapshots VALUES(?,?)",
                    (identifier, canonical(package)),
                )
            self._template_digest = identifier
        return self._template_digest

    def _event(self, project: str, resource: str, before: Any, after: Any, event_type: str) -> dict:
        context = current.get() or FeedbackContext(str(uuid4()))
        claim = current_task_project_claim()
        source = "model" if claim else context.source
        event = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "operation_id": context.operation_id,
            "event_type": event_type,
            "source": source,
            **(context.settings_snapshot or self.settings()),
            "project": project,
            "project_chain_id": self.project_chain(project),
            "resource": resource,
            "created_at": utc_now(),
            "before_revision": digest(before),
            "after_revision": digest(after),
            "before": before,
            "after": after,
            "reason": context.reason,
            "reason_source": "user" if context.reason else None,
            "action": context.action,
            "identity_links": context.identity_links,
            "task_id": claim.task_id if claim else None,
            "template_digest": self.template_snapshot(),
            "application_version": _APPLICATION_VERSION,
        }
        if resource == "versions/versions.json":
            evidence = {}
            directory = self.layout.projects_dir / project
            for bucket in (after or {}).values():
                if not isinstance(bucket, dict):
                    continue
                for item in bucket.values():
                    for version in item.get("versions", []):
                        relative = version.get("file")
                        if not isinstance(relative, str):
                            continue
                        path = directory / relative
                        if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
                            continue
                        if path.is_file():
                            evidence[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
            event["media_evidence"] = evidence
        return event

    def _append(self, db, event: dict, key: str) -> dict:
        old = db.execute("SELECT payload FROM production_feedback_events WHERE operation_key=?", (key,)).fetchone()
        if old:
            previous = json.loads(old[0])
            if (previous["before_revision"], previous["after_revision"], previous["event_type"]) != (
                event["before_revision"],
                event["after_revision"],
                event["event_type"],
            ):
                raise ValueError("operation_id conflicts with a different payload")
            return previous
        parent = db.execute(
            "SELECT event_id FROM production_feedback_events WHERE json_extract(payload,'$.project')=? AND json_extract(payload,'$.project_chain_id')=? AND json_extract(payload,'$.resource')=? ORDER BY seq DESC LIMIT 1",
            (event["project"], event["project_chain_id"], event["resource"]),
        ).fetchone()
        event["parent_event_id"] = parent[0] if parent else None
        cursor = db.execute(
            "INSERT INTO production_feedback_events(event_id,operation_key,payload) VALUES(?,?,?)",
            (event["event_id"], key, canonical(event)),
        )
        event["local_sequence"] = cursor.lastrowid
        db.execute(
            "UPDATE production_feedback_events SET payload=? WHERE event_id=?", (canonical(event), event["event_id"])
        )
        return event

    def prepare(self, project: str, resource: str, before: Any, after: Any, *, live: bool = False) -> str:
        pending = _transaction.get()
        with self.connection() as db:
            if pending is not None:
                for candidate_store, candidate_id in pending:
                    if candidate_store is self:
                        row = db.execute(
                            "SELECT resource,payload FROM production_feedback_prepared WHERE id=? AND state='prepared'",
                            (candidate_id,),
                        ).fetchone()
                        if row and row["resource"] == f"{project}/{resource}":
                            event = json.loads(row["payload"])
                            event["after"], event["after_revision"] = after, digest(after)
                            db.execute(
                                "UPDATE production_feedback_prepared SET after_hash=?,payload=? WHERE id=?",
                                (digest(after), canonical(event), candidate_id),
                            )
                            return candidate_id
        self.recover(project=project, resource=resource)
        event = self._event(project, resource, before, after, "content_saved")
        if live:
            event["preparation_owner"] = self._owner()
        preparation = str(uuid4())
        if live:
            with _active_lock:
                _active_preparations.add(preparation)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT 1 FROM production_feedback_events WHERE json_extract(payload,'$.project')=? AND json_extract(payload,'$.project_chain_id')=? AND json_extract(payload,'$.resource')=? LIMIT 1",
                (project, event["project_chain_id"], resource),
            ).fetchone()
            if not previous and before is not None:
                baseline = {
                    **event,
                    "event_id": str(uuid4()),
                    "event_type": "imported_baseline",
                    "source": "import",
                    "before": None,
                    "after": before,
                    "before_revision": digest(None),
                    "after_revision": digest(before),
                }
                self._append(db, baseline, f"baseline:{event['project_chain_id']}:{project}:{resource}")
                self._object_events(db, baseline)
            db.execute(
                "INSERT INTO production_feedback_prepared VALUES(?,?,?,?,?,?)",
                (preparation, f"{project}/{resource}", digest(before), digest(after), canonical(event), "prepared"),
            )
        return preparation

    def reconcile_one(self, preparation: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM production_feedback_prepared WHERE id=? AND state='prepared'", (preparation,)
            ).fetchone()
            if row is None:
                return
            event = json.loads(row["payload"])
            actual = digest(read_value(self.layout.projects_dir / row["resource"]))
            state = (
                "completed" if actual == row["after_hash"] else "aborted" if actual == row["before_hash"] else "unknown"
            )
            if state == "completed" and content(event["before"], event["resource"]) != content(
                event["after"], event["resource"]
            ):
                aggregate = self._append(
                    db,
                    event,
                    f"{event['operation_id']}:{event['project']}:{event['resource']}:{event['after_revision']}",
                )
                if aggregate["event_id"] == event["event_id"]:
                    self._object_events(db, aggregate)
            db.execute("UPDATE production_feedback_prepared SET state=? WHERE id=?", (state, preparation))

    def _object_events(self, db, event: dict) -> None:
        scope = f"{event['project']}:{event['project_chain_id']}"
        before, after = objects(event["resource"], event["before"]), objects(event["resource"], event["after"])
        removed = set(before) - set(after)
        added = set(after) - set(before)
        # A rename may rewrite image paths; exact content matches are safe identity evidence.
        rename_map = {}
        for kind, old, new, relation in event.get("identity_links", []):
            if relation == "rename":
                rename_map[f"{kind}/{new}"] = f"{kind}/{old}"
                plural = {"character": "characters", "scene": "scenes", "prop": "props", "product": "products"}.get(
                    kind, kind
                )
                rename_map[f"media/{plural}/{new}"] = f"media/{plural}/{old}"
        for old in removed:
            matches = [new for new in added if old.split("/")[0] == new.split("/")[0] and before[old] == after[new]]
            if len(matches) == 1 and matches[0] not in rename_map:
                rename_map[matches[0]] = old
        # Retire old names before publishing their new names on the same chain.
        for key in sorted(set(before) | set(after), key=lambda key: (key in after, key)):
            if before.get(key) == after.get(key):
                continue
            row = db.execute(
                "SELECT chain_id,active FROM production_feedback_objects WHERE project=? AND object_key=?",
                (scope, rename_map.get(key, key)),
            ).fetchone()
            chain = row[0] if row and ((row[1] and key not in added) or key in rename_map) else str(uuid4())
            db.execute(
                "INSERT INTO production_feedback_objects VALUES(?,?,?,?) ON CONFLICT(project,object_key) DO UPDATE SET chain_id=excluded.chain_id, active=excluded.active",
                (scope, key, chain, int(key in after)),
            )
            old_value = before.get(rename_map.get(key, key))
            new_value = after.get(key)
            child = {
                **event,
                "event_id": str(uuid4()),
                "event_type": "object_revision",
                "baseline": event["source"] == "import",
                "resource": key,
                "object_chain_id": chain,
                "aggregate_event_id": event["event_id"],
                "before": old_value,
                "after": new_value,
                "before_revision": digest(old_value),
                "after_revision": digest(new_value),
                "changed_fields": changes(old_value, new_value),
                "renamed_from": rename_map.get(key),
            }
            child["merged_from"] = []
            for kind, old, new, relation in event.get("identity_links", []):
                if kind == "unit" and relation == "materialize" and key == new:
                    prior = db.execute(
                        "SELECT chain_id FROM production_feedback_objects WHERE project=? AND object_key=?",
                        (scope, old),
                    ).fetchone()
                    if prior:
                        child["registered_from"] = prior[0]
                if relation == "merge" and key == f"{kind}/{new}":
                    prior = db.execute(
                        "SELECT chain_id FROM production_feedback_objects WHERE project=? AND object_key=?",
                        (scope, f"{kind}/{old}"),
                    ).fetchone()
                    child["merged_from"].append({"resource": f"{kind}/{old}", "chain_id": prior[0] if prior else None})
            if key in added and key.startswith(("character/", "scene/", "prop/", "product/")):
                kind, name = key.split("/", 1)
                draft_rows = db.execute(
                    "SELECT payload FROM production_feedback_events WHERE json_extract(payload,'$.project')=? AND json_extract(payload,'$.project_chain_id')=? AND json_extract(payload,'$.event_type')='object_revision' AND json_extract(payload,'$.resource') LIKE 'draft_asset/%' ORDER BY seq DESC",
                    (event["project"], event["project_chain_id"]),
                ).fetchall()
                for candidate_row in draft_rows:
                    candidate = json.loads(candidate_row[0])
                    asset = candidate.get("after") or {}
                    if (
                        asset.get("type") == kind
                        and (asset.get("asset_name") or asset.get("name")) == name
                        and asset.get("decision") == "register"
                    ):
                        child["registered_from"] = candidate["object_chain_id"]
                        child["origin_source"] = candidate["source"]
                        break
            if key in added and key.startswith("unit/"):
                child["replaces"] = sorted(removed)
            self._append(db, child, f"object:{event['event_id']}:{key}")

    def baseline_project(self, project: str) -> None:
        from lib.artifacts.version_manager import VersionManager
        from lib.project.project_manager import ProjectManager

        directory = self.layout.projects_dir / project
        manager = ProjectManager(self.layout.root)
        chain = self.project_chain(project)
        paths = [
            directory / "project.json",
            directory / "versions" / "versions.json",
            *sorted((directory / "scripts").glob("*.json")),
            *sorted((directory / "drafts").rglob("*.json")),
        ]
        for path in paths:
            if not path.is_file():
                continue
            resource = path.relative_to(directory).as_posix()
            guard = VersionManager(directory)._lock if resource == "versions/versions.json" else manager.file_lock(path)
            with guard:
                with self.connection() as db:
                    existing = db.execute(
                        "SELECT 1 FROM production_feedback_events WHERE json_extract(payload,'$.project')=? AND json_extract(payload,'$.project_chain_id')=? AND json_extract(payload,'$.resource')=? LIMIT 1",
                        (project, chain, resource),
                    ).fetchone()
                if existing:
                    continue
                value = read_value(path)
                event = self._event(project, resource, None, value, "imported_baseline")
                event["source"] = "import"
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT 1 FROM production_feedback_events WHERE json_extract(payload,'$.project')=? AND json_extract(payload,'$.project_chain_id')=? AND json_extract(payload,'$.resource')=?",
                        (project, chain, resource),
                    ).fetchone()
                    if row:
                        continue
                    self._append(db, event, f"baseline:{chain}:{project}:{resource}")
                    self._object_events(db, event)

    def prepare_fallback(self, project: str, resource: str, before: Any, after: Any) -> str:
        context = current.get() or FeedbackContext(str(uuid4()))
        claim = current_task_project_claim()
        identifier = str(uuid4())
        event = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "operation_id": context.operation_id,
            "event_type": "content_saved",
            "source": "model" if claim else context.source,
            **(context.settings_snapshot or self._last_settings),
            "project": project,
            "project_chain_id": self.project_chain(project),
            "resource": resource,
            "created_at": utc_now(),
            "before": before,
            "after": after,
            "before_revision": digest(before),
            "after_revision": digest(after),
            "task_id": claim.task_id if claim else None,
            "template_digest": self._template_digest,
            "application_version": _APPLICATION_VERSION,
            "identity_links": context.identity_links,
            "reason": None,
            "reason_source": None,
            "action": context.action,
            "recovered_paid_output": True,
            "preparation_owner": self._owner(),
        }
        directory = self.layout.production_feedback_pending_dir
        directory.mkdir(exist_ok=True)
        with _active_lock:
            _active_preparations.add(identifier)
        plain_write_json(directory / f"{identifier}.json", {"id": identifier, "event": event})
        return identifier

    def _recover_fallbacks(self) -> None:
        for path in sorted((self.layout.production_feedback_pending_dir).glob("*.json")):
            value = read_value(path)
            event = value["event"]
            with _active_lock:
                if value["id"] in _active_preparations:
                    continue
            if self._owner_alive(event.get("preparation_owner")):
                continue
            with self.connection() as db:
                db.execute(
                    "INSERT OR IGNORE INTO production_feedback_prepared VALUES(?,?,?,?,?,'prepared')",
                    (
                        value["id"],
                        f"{event['project']}/{event['resource']}",
                        event["before_revision"],
                        event["after_revision"],
                        canonical(event),
                    ),
                )
            path.unlink()  # hard-delete: temp -- durable preparation has moved into SQLite

    def recover(self, *, project: str | None = None, resource: str | None = None) -> None:
        self._recover_fallbacks()
        with self.connection() as db:
            query = "SELECT id,payload FROM production_feedback_prepared WHERE state='prepared'"
            rows = db.execute(
                query + (" AND resource=?" if project and resource else ""),
                (f"{project}/{resource}",) if project and resource else (),
            ).fetchall()
        for row in rows:
            with _active_lock:
                if row[0] in _active_preparations:
                    continue
            if self._owner_alive(json.loads(row[1]).get("preparation_owner")):
                continue
            self.reconcile_one(row[0])

    def events(self, project: str | None = None) -> list[dict]:
        self.recover()
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM production_feedback_events"
                + (" WHERE json_extract(payload,'$.project')=?" if project else "")
                + " ORDER BY seq",
                (project,) if project else (),
            ).fetchall()
            events = [json.loads(row[0]) for row in rows]
            unknown = db.execute("SELECT payload FROM production_feedback_prepared WHERE state='unknown'").fetchall()
            events.extend(
                {**json.loads(row[0]), "event_type": "unknown", "local_sequence": None}
                for row in unknown
                if project is None or json.loads(row[0])["project"] == project
            )
            return events

    def record_request(self, project: str, call_id: int, payload: dict) -> dict:
        event = self._event(project, f"request/{call_id}", None, payload, "request_submitted")
        event["api_call_id"] = call_id
        with self.connection() as db:
            return self._append(db, event, f"request:{project}:{call_id}")

    def review(
        self, project: str, resource: str, *, revision: str, usable: bool, reason: str | None, layer: str
    ) -> dict:
        event = self._event(
            project, resource, None, {"revision": revision, "usable": usable, "layer": layer}, "reviewed"
        )
        event["reason"] = reason
        event["reason_source"] = "user" if reason else None
        with self.connection() as db:
            row = db.execute(
                "SELECT chain_id FROM production_feedback_objects WHERE project=? AND object_key=?",
                (f"{project}:{event['project_chain_id']}", resource),
            ).fetchone()
            event["object_chain_id"] = row[0] if row else None
            return self._append(db, event, f"review:{event['operation_id']}")
