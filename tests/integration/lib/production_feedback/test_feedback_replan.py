"""Correction input survives queue failures and exports with the resulting revisions."""

import asyncio
import csv
import io
import json
import zipfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lib.db.base import Base
from lib.db.models import register_models
from lib.generation.generation_queue import ActiveTaskRequestConflict, GenerationQueue, text_task_request_facts
from lib.generation.generation_queue_client import wait_for_task
from lib.generation.generation_worker import CapacityTable, GenerationWorker
from lib.infra.data_root_layout import DataRootLayout
from lib.production_feedback.context import operation
from lib.production_feedback.export import export_project, merge_packages
from lib.production_feedback.store import atomic_write_json, register_root
from tests.fakes import refuse_resume_execution


@pytest.fixture
async def workspace(tmp_path, monkeypatch):
    store = register_root(tmp_path / "data")
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text("{}")
    monkeypatch.setattr(DataRootLayout, "current", classmethod(lambda cls: store.layout))
    engine = create_async_engine(f"sqlite+aiosqlite:///{store.db}")
    register_models()
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    try:
        yield store, directory, GenerationQueue(session_factory=async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def request_payload():
    return {
        "episode": 2,
        "instructions": "保持对白原文",
        "replan_reason": "将手链、脚链合为人物束缚形态，不再独立登记铁链。",
        "replan_context": json.dumps({"status": "current_plan", "content": {"units": [{"text": "旧铁链资产"}]}}),
    }


async def submit(queue, payload):
    return await queue.enqueue_task(
        project_name="demo",
        task_type="text_reference_script_plan",
        media_type="text",
        resource_id="episode-2",
        provider_id="text",
        payload=payload,
    )


async def test_replan_input_freezes_identity_dedupes_and_rejects_changed_input(workspace):
    store, _, queue = workspace
    store.save_settings({"tester_id": "A"})
    payload = request_payload()
    with operation(operation_id="replan-request", source="human"):
        first = await submit(queue, payload)
    store.save_settings({"tester_id": "B"})
    second = await submit(queue, payload)
    assert second["task_id"] == first["task_id"]
    with pytest.raises(ActiveTaskRequestConflict):
        await submit(queue, {**payload, "replan_reason": "不同的原因"})
    events = store.events("demo")
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "replan_submitted"
    assert event["tester_id"] == "A"
    assert event["episode_id"] == 2
    assert event["reason"] == payload["replan_reason"]
    assert event["after"] == {"source": None, **payload}
    assert event["operation_id"] == "replan-request"
    assert event["task_id"] == first["task_id"]
    assert text_task_request_facts((await queue.get_task(first["task_id"]))["payload"]) == payload


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
async def test_accepted_task_recovers_input_after_journal_failure_and_exports_once(
    workspace, monkeypatch, tmp_path, terminal
):
    store, _, queue = workspace
    original = store.record_replan_submission
    with monkeypatch.context() as patch:

        def fail(*args):
            raise OSError("journal interrupted after enqueue")

        patch.setattr(store, "record_replan_submission", fail)
        accepted = await submit(queue, request_payload())
    assert store.record_replan_submission == original
    if terminal == "failed":
        claimed = await queue.claim_next_task("text")
        assert claimed["task_id"] == accepted["task_id"]
        assert await queue.mark_task_failed(accepted["task_id"], "model refused") == 1
    else:
        await queue.cancel_task(accepted["task_id"])
    package = tmp_path / "feedback.zip"
    package.write_bytes(export_project(store, "demo"))
    with zipfile.ZipFile(package) as archive:
        rows = [json.loads(line) for line in archive.read("events.jsonl").splitlines()]
        submissions = [e for e in rows if e["event_type"] == "replan_submitted"]
        assert len(submissions) == 1
        assert submissions[0]["reason"] == request_payload()["replan_reason"]
        assert [e["after"]["status"] for e in rows if e["event_type"] == "replan_finished"] == [terminal]
        report = archive.read("report.md").decode()
        assert f"状态：{terminal}" in report
        assert "旧铁链资产" in report
        assert "保持对白原文" in report
        assert "将手链、脚链" in report
    merged = merge_packages([package, package], tmp_path / "merged")
    assert merged["replan_requests"] == 1
    assert merged["replan_outcomes"] == {terminal: 1}
    assert merged["human_operations"] == 0
    assert len(store.events("demo")) == len(rows)


async def test_worker_model_revisions_keep_reason_and_export_with_submission(workspace):
    store, directory, queue = workspace
    draft = directory / "drafts/episode_2/script_plan_reference_units.json"
    draft.parent.mkdir(parents=True)
    old = "@[张三] 戴着 @[铁链]。"
    new = "@[张三/束缚形态] 双手戴手链、双脚戴脚链。"
    atomic_write_json(draft, {"units": [{"unit_id": "U1", "text": old}]})
    accepted = await submit(queue, request_payload())

    async def execute(task, *, claimed_provider_id):
        def save_model():
            # ScriptGenerator nests its own model operation around its final save.
            with operation(source="model"):
                atomic_write_json(draft, {"units": [{"unit_id": "U1", "text": new}]})

        await asyncio.to_thread(save_model)
        return {}

    async def provider(task):
        return "text"

    worker = GenerationWorker(
        queue=queue,
        capacity=CapacityTable(_limits={}, _defaults={"text": 1}),
        provider_projection=provider,
        executor=execute,
        lanes=("text",),
        resume_executor=refuse_resume_execution,
    )
    worker.poll_interval = 0.01
    worker.heartbeat_interval = 0.01
    await worker.start()
    try:
        task = await asyncio.wait_for(wait_for_task(accepted["task_id"], 0.01, queue=queue), timeout=10)
        assert task["status"] == "succeeded", task
    finally:
        await worker.stop()
    saved = next(e for e in store.events("demo") if e["source"] == "model" and e["event_type"] == "object_revision")
    assert saved["task_id"] == accepted["task_id"]
    assert saved["reason"] == request_payload()["replan_reason"]
    assert saved["episode_id"] == 2
    with zipfile.ZipFile(io.BytesIO(export_project(store, "demo"))) as archive:
        report = archive.read("report.md").decode()
        assert f"-{old}" in report
        assert f"+{new}" in report
        assert "状态：succeeded" in report


@pytest.mark.parametrize("payload", [{}, {"replan_reason": "   "}, {"replan_reason": "预览", "dry_run": True}])
async def test_no_effective_submission_without_reason_or_on_preview(workspace, payload):
    store, _, queue = workspace
    await submit(queue, {"episode": 2, **payload})
    assert store.events("demo") == []


def test_asset_and_video_prompt_edits_export_full_before_after_and_default_reset(tmp_path):
    store = register_root(tmp_path / "data")
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    project, script = directory / "project.json", directory / "scripts/episode_1.json"
    script.parent.mkdir()
    before, after = "【本组设定】\n时长 9 秒\n0–9秒：静止", "【本组设定】\n时长 9 秒\n0–3秒：推近\n3–9秒：回头"
    with operation(source="model"):
        atomic_write_json(project, {"characters": {"张三": {"generation_prompt": "旧人物提示词"}}})
        atomic_write_json(script, {"video_units": [{"unit_id": "U1", "text": before, "generation_prompt": ""}]})
    with operation(operation_id="manual-save", source="human"):
        atomic_write_json(project, {"characters": {"张三": {"generation_prompt": "新人物提示词"}}})
        atomic_write_json(script, {"video_units": [{"unit_id": "U1", "text": after, "generation_prompt": after}]})
    count = len(store.events("demo"))
    atomic_write_json(script, json.loads(script.read_text()))
    assert len(store.events("demo")) == count
    with operation(source="human"):
        atomic_write_json(script, {"video_units": [{"unit_id": "U1", "text": after, "generation_prompt": ""}]})
    with zipfile.ZipFile(io.BytesIO(export_project(store, "demo"))) as archive:
        csv_rows = list(csv.DictReader(io.StringIO(archive.read("events.csv").decode("utf-8-sig"))))
        saved = next(
            e
            for e in csv_rows
            if e["event_type"] == "object_revision"
            and e["operation_id"] == "manual-save"
            and e["resource"].startswith("unit/")
        )
        assert json.loads(saved["before"])["text"] == before
        assert json.loads(saved["after"])["generation_prompt"] == after
        reset = [e for e in csv_rows if e["resource"].startswith("unit/")][-1]
        assert json.loads(reset["before"])["generation_prompt"] == after
        assert json.loads(reset["after"])["generation_prompt"] == ""
        report = archive.read("report.md").decode()
        assert "-0–9秒：静止\n+0–3秒：推近\n+3–9秒：回头" in report
        assert "-旧人物提示词\n+新人物提示词" in report
