"""Queued tasks retain the tester who submitted them even after local settings change."""

from lib.generation.generation_queue import GenerationQueue
from lib.infra.data_root_layout import DataRootLayout
from lib.production_feedback.store import register_root


async def test_deduped_queue_task_keeps_submission_identity(tmp_path, monkeypatch, session_factory):
    store = register_root(tmp_path / "feedback")
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text("{}")
    monkeypatch.setattr(DataRootLayout, "current", classmethod(lambda cls: store.layout))
    store.save_settings({"tester_id": "A", "test_batch_id": "B1"})
    queue = GenerationQueue(session_factory=session_factory)
    first = await queue.enqueue_task(project_name="demo", task_type="character", media_type="image", resource_id="甲")
    store.save_settings({"tester_id": "B", "test_batch_id": "B2"})
    second = await queue.enqueue_task(project_name="demo", task_type="character", media_type="image", resource_id="甲")
    assert second["task_id"] == first["task_id"]
    task = await queue.get_task(first["task_id"])
    assert task is not None
    assert task["payload"]["_production_feedback"]["tester_id"] == "A"
    assert task["payload"]["_production_feedback"]["test_batch_id"] == "B1"
