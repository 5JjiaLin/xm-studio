"""Recovery, same-name projects, saved-response replay, and attribution boundaries."""

import json
import multiprocessing
import shutil

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from lib.artifacts.formal_write import formal_write_transaction
from lib.infra.async_thread import run_sync_transaction
from lib.production_feedback.context import operation
from lib.production_feedback.metrics import summarize
from lib.production_feedback.store import FeedbackStore, atomic_write_json, register_root
from server.services.production_feedback import feedback_middleware


def _live_writer(root, ready, finish):
    from pathlib import Path

    store = register_root(Path(root))
    path = store.layout.projects_dir / "demo" / "project.json"
    with operation(source="human"), formal_write_transaction(path):
        atomic_write_json(path, {"props": {"针": {"generation_prompt": "银针"}}})
        ready.set()
        finish.wait(20)


def test_other_process_cannot_finish_a_live_transaction(tmp_path):
    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text('{"props":{}}')
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    child = context.Process(target=_live_writer, args=(str(tmp_path), ready, finish))
    child.start()
    try:
        assert ready.wait(15), "writer did not reach the held transaction"
        assert not [e for e in store.events("demo") if e["event_type"] == "content_saved"]
        child.terminate()
        child.join(10)
        restarted = FeedbackStore(tmp_path)
        assert any(e["event_type"] == "content_saved" and e["after"].get("props") for e in restarted.events("demo"))
    finally:
        if child.is_alive():
            child.terminate()
        child.join(10)


def test_same_name_project_recreation_has_new_baseline_and_chain(tmp_path):
    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    path = directory / "project.json"
    with operation(source="model"):
        atomic_write_json(path, {"props": {"针": {"generation_prompt": "旧针"}}})
    first = store.events("demo")[-1]
    # hard-delete: temp -- this test owns its temporary project directory.
    shutil.rmtree(directory)
    directory.mkdir()
    path.write_text('{"props":{"针":{"generation_prompt":"新针"}}}')
    store.baseline_project("demo")
    second = store.events("demo")[-1]
    assert first["project_chain_id"] != second["project_chain_id"]
    assert first["object_chain_id"] != second["object_chain_id"]
    assert second["baseline"] is True
    assert second["parent_event_id"] is None


def test_http_response_replay_conflict_and_lost_response_do_not_write_twice(tmp_path, monkeypatch):
    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    path = directory / "project.json"
    path.write_text('{"props":{}}')
    monkeypatch.setattr("server.services.production_feedback.register_root", lambda root: store)
    app = FastAPI()
    app.middleware("http")(feedback_middleware)

    @app.post("/api/v1/projects/demo/edit")
    async def edit(request: Request):
        body = await request.json()
        await run_sync_transaction(atomic_write_json, path, body)
        if body.get("lose_response"):
            raise RuntimeError("response lost after saving")
        return {"saved": True}

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Production-Operation-Id": "save-once", "X-Production-Source": "human"}
    body = {"props": {"针": {"generation_prompt": "银针"}}}
    assert client.post("/api/v1/projects/demo/edit", json=body, headers=headers).status_code == 200
    count = len(store.events("demo"))
    assert client.post("/api/v1/projects/demo/edit", json=body, headers=headers).json() == {"saved": True}
    assert len(store.events("demo")) == count
    assert client.post("/api/v1/projects/demo/edit", json={}, headers=headers).status_code == 409
    headers["X-Production-Operation-Id"] = "lost"
    body = {"props": {"针": {"generation_prompt": "修改针"}}, "lose_response": True}
    assert client.post("/api/v1/projects/demo/edit", json=body, headers=headers).status_code == 500
    assert client.post("/api/v1/projects/demo/edit", json=body, headers=headers).status_code == 409
    assert json.loads(path.read_text())["props"]["针"]["generation_prompt"] == "修改针"
    assert len([e for e in store.events("demo") if e["event_type"] == "content_saved"]) == 2


def test_nested_model_save_keeps_frozen_tester_and_ambiguous_edits_are_unknown(tmp_path):
    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    path = directory / "project.json"
    settings = store.save_settings({"tester_id": "A", "test_batch_id": "B1"})
    store.save_settings({"tester_id": "B", "test_batch_id": "B2"})
    with operation(operation_id="task-1", source="model", settings_snapshot=settings), operation(source="model"):
        atomic_write_json(path, {"props": {"针": {"generation_prompt": "银针"}}})
    event = next(e for e in store.events("demo") if e["resource"] == "prop/针")
    assert event["tester_id"] == "A"
    assert event["test_batch_id"] == "B1"
    assert event["operation_id"] == "task-1"
    store.review("demo", "prop/针", revision=event["after_revision"], usable=True, reason=None, layer="text")
    before = json.loads(path.read_text())
    store.prepare("demo", "project.json", before, {"props": {}})
    path.write_text('{"props":{"针":{"generation_prompt":"外部未知修改"}}}')
    summary = summarize(store.events("demo"))
    assert summary["text_zero_edit_adoption_rate"] is None
    assert summary["unknown_events"] == 1
    assert summary["groups"][0]["summary"]["unknown_events"] == 1
    assert summary["groups"][0]["summary"]["text_zero_edit_adoption_rate"] is None


def test_grouped_candidates_include_media_from_a_different_batch(tmp_path):
    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    store.save_settings({"test_batch_id": "text-batch"})
    with operation(source="model"):
        atomic_write_json(directory / "project.json", {"props": {"针": {"generation_prompt": "银针"}}})
    with operation(source="human"):
        atomic_write_json(directory / "project.json", {"props": {"针": {"generation_prompt": "金针"}}})
    store.save_settings({"test_batch_id": "media-batch"})
    versions = directory / "versions/versions.json"
    versions.parent.mkdir()
    with operation(source="model"):
        atomic_write_json(versions, {"props": {"针": {"current_version": 1, "versions": [{"version": 1}]}}})
    summary = summarize(store.events("demo"))
    text_group = next(group["summary"] for group in summary["groups"] if group["test_batch_id"] == "text-batch")
    assert len(text_group["objects"]) == 1
    assert text_group["objects"][0]["media_candidates_after_last_text_edit"] == 1
