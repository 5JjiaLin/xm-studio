"""Saved edits, recoverable journal, and portable local-testing evidence."""

import hashlib
import io
import json
import zipfile

import pytest

from lib.artifacts.formal_write import formal_write_transaction
from lib.infra.json_io import atomic_write_json as plain_write
from lib.production_feedback.context import identity_link, operation
from lib.production_feedback.export import export_project, merge_packages
from lib.production_feedback.store import FeedbackStore, atomic_write_json, canonical, register_root


@pytest.fixture
def workspace(tmp_path):
    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    path = directory / "project.json"
    plain_write(path, {"characters": {"甲": {"description": "青年", "generation_prompt": ""}}, "props": {}})
    return store, directory, path


def test_full_before_after_noop_and_default_source_switch(workspace):
    store, _, path = workspace
    before = json.loads(path.read_text())
    after = {**before, "characters": {"甲": {"description": "青年", "generation_prompt": "完整全文"}}}
    with operation(operation_id="save1", source="human"), formal_write_transaction(path):
        atomic_write_json(path, after)
    events = store.events("demo")
    assert [e["source"] for e in events if e["resource"] == "character/甲"] == ["import", "human"]
    saved = [e for e in events if e["event_type"] == "content_saved"]
    assert saved[0]["before"] == before
    assert saved[0]["after"] == after
    assert saved[0]["template_digest"]
    count = len(events)
    atomic_write_json(path, after)
    assert len(store.events("demo")) == count
    with operation(source="human"):
        atomic_write_json(path, before)
    changed = [e for e in store.events("demo") if e["resource"] == "character/甲"][-1]
    assert changed["changed_fields"] == ["generation_prompt"]
    assert changed["parent_event_id"]


def test_outer_rollback_does_not_record_success(workspace):
    store, _, path = workspace
    before = json.loads(path.read_text())

    def reject_commit():
        with formal_write_transaction(path):
            atomic_write_json(path, {"characters": {"乙": {"description": "替换"}}})
            assert not [e for e in store.events("demo") if e["event_type"] == "content_saved"]
            raise ValueError("reject")

    with pytest.raises(ValueError, match="reject"):
        reject_commit()
    assert json.loads(path.read_text()) == before
    assert not [e for e in store.events("demo") if e["event_type"] == "content_saved"]


def test_recovery_after_commit_and_ambiguous_later_content(workspace):
    store, _, path = workspace
    before = json.loads(path.read_text())
    after = {"characters": {"甲": {"generation_prompt": "第一次修改"}}}
    store.prepare("demo", "project.json", before, after)
    plain_write(path, after)
    restarted = FeedbackStore(store.layout.root)
    restarted.recover()
    assert any(e["after"] == after and e["event_type"] == "content_saved" for e in restarted.events("demo"))
    restarted.prepare("demo", "project.json", after, before)
    unrelated = {"characters": {"甲": {"generation_prompt": "后续内容"}}}
    plain_write(path, unrelated)
    restarted.recover()
    assert any(e["event_type"] == "unknown" for e in restarted.events("demo"))
    assert json.loads(path.read_text()) == unrelated


def test_rename_merge_rebuild_and_same_name_recreation(workspace):
    store, directory, path = workspace
    store.baseline_project("demo")
    original = next(e for e in store.events("demo") if e["resource"] == "character/甲")
    with identity_link("character", "甲", "乙", "rename"):
        atomic_write_json(path, {"characters": {"乙": {"description": "青年", "generation_prompt": ""}}})
    renamed = [e for e in store.events("demo") if e["resource"] == "character/乙"][-1]
    assert renamed["object_chain_id"] == original["object_chain_id"]
    assert renamed["renamed_from"] == "character/甲"
    atomic_write_json(path, {"characters": {}})
    atomic_write_json(path, {"characters": {"乙": {"description": "另一个人"}}})
    recreated = [e for e in store.events("demo") if e["resource"] == "character/乙"][-1]
    assert recreated["object_chain_id"] != renamed["object_chain_id"]
    script = directory / "scripts" / "episode_1.json"
    script.parent.mkdir()
    atomic_write_json(script, {"video_units": [{"unit_id": "E1U1", "text": "旧正文"}]})
    atomic_write_json(script, {"video_units": [{"unit_id": "E1U2", "text": "重拆正文"}]})
    unit = [e for e in store.events("demo") if e["resource"].endswith("/E1U2")][-1]
    assert unit["replaces"] == ["unit/scripts/episode_1.json/E1U1"]


def test_two_installations_incremental_duplicate_exports_and_missing_media(workspace, tmp_path):
    first, directory, path = workspace
    first.save_settings({"tester_id": "A", "test_batch_id": "B1", "test_case_id": "案例一"})
    first.baseline_project("demo")
    with operation(source="human"):
        atomic_write_json(path, {"characters": {"甲": {"generation_prompt": "改后"}}})
    versions = directory / "versions" / "versions.json"
    versions.parent.mkdir()
    atomic_write_json(
        versions,
        {
            "characters": {
                "甲": {"current_version": 1, "versions": [{"version": 1, "file": "versions/characters/missing.png"}]}
            }
        },
    )
    package1 = tmp_path / "first.zip"
    package1.write_bytes(export_project(first, "demo"))
    with operation(source="human"):
        atomic_write_json(path, {"characters": {"甲": {"generation_prompt": "再改"}}})
    incremental = tmp_path / "incremental.zip"
    incremental.write_bytes(export_project(first, "demo"))
    second = register_root(tmp_path / "second")
    other = second.layout.projects_dir / "demo"
    other.mkdir(parents=True)
    plain_write(other / "project.json", {"characters": {"甲": {"generation_prompt": "同名另一个实验"}}})
    package2 = tmp_path / "second.zip"
    package2.write_bytes(export_project(second, "demo"))
    report = merge_packages([package1, package1, incremental, package2], tmp_path / "merged")
    assert report["installations"] == 2
    assert report["events"] == len(first.events("demo")) + len(second.events("demo"))
    assert report["text_zero_edit_adoption_rate"] is None
    assert report["media_inspection"] == "not_checked"
    with zipfile.ZipFile(package1) as archive:
        media = [json.loads(line) for line in archive.read("media.jsonl").splitlines()]
        assert media[0]["missing"] is True
        assert not any(name.endswith((".db", ".env")) for name in archive.namelist())
    assert "再改" in (tmp_path / "merged" / "report.md").read_text()


def test_same_event_id_with_changed_payload_is_rejected(workspace, tmp_path):
    store, _, _ = workspace
    raw = export_project(store, "demo")
    first = tmp_path / "first.zip"
    second = tmp_path / "conflict.zip"
    first.write_bytes(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    rows = [json.loads(line) for line in files["events.jsonl"].splitlines()]
    rows[0]["tester_id"] = "conflicting"
    files["events.jsonl"] = ("\n".join(canonical(row) for row in rows) + "\n").encode()
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["events.jsonl"] = hashlib.sha256(files["events.jsonl"]).hexdigest()
    files["manifest.json"] = canonical(manifest).encode()
    with zipfile.ZipFile(second, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="Conflicting event_id"):
        merge_packages([first, second], tmp_path / "result")


def test_only_explicit_model_adoption_counts_as_zero_edit(workspace):
    store, directory, _ = workspace
    script = directory / "scripts" / "episode_1.json"
    script.parent.mkdir()
    with operation(source="model"):
        atomic_write_json(script, {"video_units": [{"unit_id": "E1U1", "text": "初始模型正文"}]})
    latest = [e for e in store.events("demo") if e["resource"].endswith("/E1U1")][-1]
    with operation(source="human"):
        store.review(
            "demo", latest["resource"], revision=latest["after_revision"], usable=True, reason=None, layer="text"
        )
    from lib.production_feedback.metrics import summarize

    summary = summarize(store.events("demo"))
    assert summary["text"]["zero_edit_adopted"] == 1
    assert summary["text_zero_edit_adoption_rate"] == 1
    atomic_write_json(
        script,
        {"video_units": [{"unit_id": "E1U1", "text": "初始模型正文", "generated_assets": {"video_clip": "a.mp4"}}]},
    )
    assert (
        len(
            [
                e
                for e in store.events("demo")
                if e["resource"].endswith("/E1U1") and e["event_type"] == "object_revision"
            ]
        )
        == 1
    )


def test_paid_version_survives_feedback_database_failure(workspace, monkeypatch):
    import sqlite3

    from lib.project.task_project_claim import claim_task_project

    store, directory, _ = workspace
    versions = directory / "versions" / "versions.json"
    versions.parent.mkdir()
    after = {
        "characters": {
            "甲": {
                "current_version": 1,
                "versions": [{"version": 1, "file": "versions/characters/a.png", "task_id": "paid-task"}],
            }
        }
    }

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is full")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", unavailable)
        with claim_task_project("paid-task", "demo"):
            atomic_write_json(versions, after)
    assert json.loads(versions.read_text()) == after
    store.recover()
    event = next(e for e in store.events("demo") if e["resource"] == "media/characters/甲")
    assert event["task_id"] == "paid-task"
    assert event["after"]["current_version"] == 1
    assert event["recovered_paid_output"] is True


@pytest.mark.parametrize(("old", "new"), [("甲", "乙"), ("乙", "甲")])
def test_renamed_object_remains_current_and_reviewable(tmp_path, old, new):
    from lib.production_feedback.metrics import summarize

    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    path = directory / "project.json"
    value = {"generation_prompt": "青年"}
    with operation(source="model"):
        atomic_write_json(path, {"characters": {old: value}})
    with operation(source="human"), identity_link("character", old, new, "rename"):
        atomic_write_json(path, {"characters": {new: value}})
    latest = [e for e in store.events("demo") if e["event_type"] == "object_revision"][-1]
    assert latest["resource"] == f"character/{new}"
    store.review("demo", latest["resource"], revision=latest["after_revision"], usable=True, reason=None, layer="text")
    summary = summarize(store.events("demo"))
    assert summary["text"]["usable"] == 1
    assert summary["objects"][0]["resource"] == f"character/{new}"


@pytest.mark.parametrize("rollback", [False, True])
def test_paid_fallback_waits_for_outer_transaction(workspace, monkeypatch, rollback):
    import sqlite3
    from contextlib import nullcontext

    from lib.project.task_project_claim import claim_task_project

    store, _, path = workspace
    before = json.loads(path.read_text())
    after = {"props": {"针": {"generation_prompt": "付费产物"}}}

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is full")

    with (
        pytest.raises(ValueError, match="rollback") if rollback else nullcontext(),
        formal_write_transaction(path),
        claim_task_project("paid-task", "demo"),
    ):
        with formal_write_transaction(path), monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", unavailable)
            atomic_write_json(path, after)
        assert not [e for e in store.events("demo") if e["event_type"] == "content_saved"]
        if rollback:
            raise ValueError("rollback")
    saved = [e for e in store.events("demo") if e["event_type"] == "content_saved"]
    assert json.loads(path.read_text()) == (before if rollback else after)
    assert len(saved) == (0 if rollback else 1)
    assert not list(store.layout.production_feedback_pending_dir.glob("*.json"))
