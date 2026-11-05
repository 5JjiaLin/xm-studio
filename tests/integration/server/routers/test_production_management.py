"""Archives preserve episode identity and files and never cancel writers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.episode.episode_management import create_episode, move_episode
from lib.generation.generation_admission import generation_admission_lock
from lib.generation.generation_queue import GenerationQueue
from lib.infra.api_errors import ConflictError
from lib.infra.async_thread import run_sync_transaction
from lib.infra.json_io import atomic_write_json
from lib.project.production_management import MANAGEMENT_RESOURCE, set_archive
from lib.project.project_manager import ProjectManager
from server.error_handlers import register_error_handlers
from server.routers import production
from server.services.project.production import ProductionService


@pytest.fixture
def management(tmp_path, monkeypatch):
    pm = ProjectManager(tmp_path / "data")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Test", "Anime", "drama")
    first = create_episode(pm, "demo", title="First", source_text="第一集对白", source_kind="screenplay")
    second = create_episode(pm, "demo", title="Second", source_text="第二集对白", source_kind="screenplay")
    queue = SimpleNamespace(list_tasks=AsyncMock(return_value={"items": []}))
    monkeypatch.setattr(production, "get_generation_queue", lambda: queue)
    app = FastAPI()
    app.state.production_service = ProductionService(pm)
    register_error_handlers(app)
    # Archive routes are protected by the app-level authentication boundary in server.app.
    app.include_router(production.router, prefix="/api/v1")
    app.include_router(production.source_router, prefix="/api/v1")
    return TestClient(app), pm, queue, first, second


def test_archive_restore_preserves_scripts_media_identity_and_order(management):
    client, pm, _, first, second = management
    move_episode(pm, "demo", second, after=None)
    path = pm.get_project_path("demo")
    media = path / "renders" / "kept.mp4"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b"existing final cut")
    before = pm.load_project("demo")
    source = (path / "source" / f"episode_{first}.txt").read_bytes()
    with client:
        for target in (f"episodes/{first}/production/archive", "production/archive"):
            url = f"/api/v1/projects/demo/{target}"
            assert client.post(url, json={"archived": True}).status_code == 200
            assert client.post(url, json={"archived": False}).status_code == 200
    after = pm.load_project("demo")
    assert after["episodes"] == before["episodes"]
    assert after["episode_id_high_water"] == before["episode_id_high_water"]
    assert media.read_bytes() == b"existing final cut"
    assert (path / "source" / f"episode_{first}.txt").read_bytes() == source


@pytest.mark.parametrize("status", ["queued", "running"])
def test_active_project_writers_prevent_archiving_without_cancellation(management, status):
    client, pm, queue, first, _ = management
    queue.list_tasks.side_effect = lambda **kwargs: {
        "items": [{"task_id": "keep"}] if kwargs["status"] == status else []
    }
    with client:
        response = client.post(f"/api/v1/projects/demo/episodes/{first}/production/archive", json={"archived": True})
    assert response.status_code == 409
    assert not pm.load_project("demo").get("production_management")


@pytest.mark.parametrize("status", ["active", "submitting", "unknown"])
def test_continuation_between_task_stages_prevents_archive(management, status):
    client, pm, _, first, _ = management
    journal = pm.get_project_path("demo") / "production" / f"episode_{first}.json"
    journal.parent.mkdir()
    atomic_write_json(journal, {"episode": first, "status": status})
    with client:
        response = client.post("/api/v1/projects/demo/production/archive", json={"archived": True})
    assert response.status_code == 409
    assert not pm.load_project("demo").get("production_management")


def test_unknown_episode_does_not_create_an_archive_marker(management):
    client, pm, _, _, _ = management
    with client:
        response = client.post("/api/v1/projects/demo/episodes/999/production/archive", json={"archived": True})
    assert response.status_code == 404
    assert not pm.load_project("demo").get("production_management")


async def test_admission_rechecks_archive_after_wait_and_other_episode_still_works(management, db_factory):
    _, pm, _, first, second = management
    queue = GenerationQueue(session_factory=db_factory, project_manager=pm)
    async with generation_admission_lock(project_name="demo", script_file="", resource_id=MANAGEMENT_RESOURCE):
        enqueue = asyncio.create_task(
            queue.enqueue_task(
                project_name="demo",
                task_type="text_script_plan",
                media_type="text",
                resource_id=f"episode_{first}",
                payload={"episode": first},
                provider_id="text",
            )
        )
        await asyncio.sleep(0.1)
        assert not enqueue.done()
        await run_sync_transaction(set_archive, pm, "demo", True, first)
    with pytest.raises(ConflictError):
        await enqueue
    listing = await queue.list_tasks(project_name="demo")
    assert listing["items"] == []
    admitted = await queue.enqueue_task(
        project_name="demo",
        task_type="text_script_plan",
        media_type="text",
        resource_id=f"episode_{second}",
        payload={"episode": second},
        provider_id="text",
    )
    assert admitted["task_id"]
    await run_sync_transaction(set_archive, pm, "demo", False, first)
    restored = await queue.enqueue_task(
        project_name="demo",
        task_type="text_script_plan",
        media_type="text",
        resource_id=f"episode_{first}",
        payload={"episode": first},
        provider_id="text",
    )
    assert restored["task_id"] != admitted["task_id"]


def test_cost_endpoint_reads_all_episode_ids_without_generation(management, monkeypatch):
    from lib.db.base import DEFAULT_USER_ID
    from server.auth import CurrentUserInfo, get_current_user

    client, _, queue, first, second = management
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(
        id=DEFAULT_USER_ID, sub="local", role="admin"
    )
    aggregate = AsyncMock(return_value={"episodes": [], "unassigned": {"call_count": 0}})
    monkeypatch.setattr(production.UsageRepository, "production_costs", aggregate)

    async def factory():
        yield SimpleNamespace()

    from contextlib import asynccontextmanager

    monkeypatch.setattr(production, "async_session_factory", asynccontextmanager(factory))
    with client:
        response = client.get("/api/v1/projects/demo/production/costs")
    assert response.status_code == 200
    assert set(aggregate.call_args.args[1].values()) == {first, second}
    queue.list_tasks.assert_not_awaited()


# A real DOCX archive: body paragraphs and a table must stay in document order.
def _screenplay_docx():
    import io
    import zipfile

    buffer = io.BytesIO()
    xml = b'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    xml += "<w:p><w:r><w:t>第一场</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>小明：你好。</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>小红：再见。</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>第二场</w:t></w:r></w:p>".encode()
    xml += b"</w:body></w:document>"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_docx_preview_then_edited_save_retains_original_and_episode_identity(management):
    client, pm, _, first, second = management
    original = _screenplay_docx()
    before = pm.load_project("demo")
    url = f"/api/v1/projects/demo/episodes/{second}/production/source"
    with client:
        preview = client.post("/api/v1/production/source-preview", files={"file": ("单集.docx", original)})
        assert preview.status_code == 200
        text = preview.json()["text"]
        assert text.index("第一场") < text.index("小明：你好。") < text.index("小红：再见。") < text.index("第二场")
        assert pm.load_project("demo") == before
        saved = client.post(
            url,
            data={"text": text + "\n人工修改", "expected_text": "第二集对白"},
            files={"file": ("单集.docx", original)},
        )
        assert saved.status_code == 200
        assert saved.json()["applied"]
        # Retry is safe after a response loss and never creates another episode.
        retry = client.post(
            url, data={"text": text, "expected_text": "第二集对白"}, files={"file": ("单集.docx", original)}
        )
        assert retry.status_code == 409
    project_dir = pm.get_project_path("demo")
    assert len(pm.load_project("demo")["episodes"]) == 2
    assert (project_dir / "source" / f"episode_{first}.txt").read_text() == "第一集对白"
    assert (project_dir / "source" / f"episode_{second}.txt").read_text().endswith("人工修改")
    raw = list((project_dir / "source" / "raw" / f"episode_{second}").glob("*.docx"))
    assert len(raw) == 1
    assert raw[0].read_bytes() == original


@pytest.mark.parametrize(
    ("filename", "body"), [("broken.docx", b"invalid"), ("empty.txt", b""), ("wrong.exe", b"text")]
)
def test_failed_source_preview_or_save_keeps_existing_text(management, filename, body):
    client, pm, _, first, _ = management
    path = pm.get_project_path("demo") / "source" / f"episode_{first}.txt"
    with client:
        preview = client.post("/api/v1/production/source-preview", files={"file": (filename, body)})
        saved = client.post(
            f"/api/v1/projects/demo/episodes/{first}/production/source",
            data={"text": "new", "expected_text": "第一集对白"},
            files={"file": (filename, body)},
        )
    assert preview.status_code in {400, 422}
    assert saved.status_code in {400, 422}
    assert path.read_text() == "第一集对白"


def test_source_replacement_requires_confirmation_and_checks_concurrent_edits(management):
    client, pm, _, first, _ = management
    path = pm.get_project_path("demo")
    plan = path / "drafts" / f"episode_{first}" / "script_plan_normalized_script.json"
    plan.parent.mkdir(parents=True)
    plan.write_text("{}")
    url = f"/api/v1/projects/demo/episodes/{first}/production/source"
    data = {"text": "替换后的对白", "expected_text": "第一集对白"}
    with client:
        asked = client.post(url, data=data)
        assert asked.status_code == 200
        assert asked.json()["needs_confirmation"]
        assert (path / "source" / f"episode_{first}.txt").read_text() == "第一集对白"
        saved = client.post(url, data={**data, "confirm": "true"})
        assert saved.status_code == 200
        assert saved.json()["applied"]
        conflict = client.post(url, data={**data, "confirm": "true"})
        assert conflict.status_code == 409


def test_edit_api_revisions_persist_and_reject_cross_episode(timeline_project):
    from lib.db.base import DEFAULT_USER_ID
    from lib.edit_timeline import EditTimelineService
    from lib.edit_timeline.model import RevisionAuthor
    from server.auth import CurrentUserInfo, get_current_user

    pm = timeline_project
    timeline = asyncio.run(
        EditTimelineService(pm).create_from_script(
            "demo", episode=1, name="剪辑", author=RevisionAuthor(kind="creator")
        )
    ).timeline
    second = create_episode(pm, "demo", title="Second", source_text="第二集", source_kind="novel")
    journal = pm.get_project_path("demo") / "production"
    journal.mkdir()
    for episode in (1, second):
        atomic_write_json(
            journal / f"episode_{episode}.json", {"episode": episode, "status": "completed", "timeline_id": timeline.id}
        )
    app = FastAPI()
    app.state.production_service = ProductionService(pm)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id=DEFAULT_USER_ID, sub="local", role="admin")
    register_error_handlers(app)
    app.include_router(production.router, prefix="/api/v1")
    app.include_router(production.source_router, prefix="/api/v1")
    url = "/api/v1/projects/demo/episodes/1/production/edit"
    body = {
        "timeline_id": timeline.id,
        "base_revision": 1,
        "operations": [
            {"op": "set_volume", "clip": "c1", "volume": 0},
            {"op": "set_included", "clip": "c2", "included": False},
            {"op": "set_subtitles", "clip": "c1", "subtitles": []},
        ],
    }
    with TestClient(app) as client:
        initial = client.get(url)
        assert initial.status_code == 200
        assert initial.json()["revision"] == 1
        saved = client.put(url, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 2
        assert saved.json()["clips"][0]["source_volume"] == 0
        assert saved.json()["clips"][0]["subtitles"] == []
        assert saved.json()["clips"][1]["included"] is False
        assert client.get(url).json() == saved.json()
        stale = {**body, "operations": [{"op": "set_volume", "clip": "c1", "volume": 0.5}]}
        assert client.put(url, json=stale).status_code == 409
        other = url.replace("episodes/1", f"episodes/{second}")
        assert client.get(other).status_code == 409
        assert client.put(other, json=body).status_code == 409
        restore = {**body, "base_revision": 2, "operations": [{"op": "set_included", "clip": "c2", "included": True}]}
        assert client.put(url, json=restore).status_code == 200


def test_draft_prompt_matches_registered_override_and_reports_shared_episodes(timeline_project):
    from server.routers._asset_router_factory import build_asset_router

    pm = timeline_project
    pm.add_character("demo", "阿明", "青年")
    project = pm.load_project("demo")
    script = pm.load_script("demo", project["episodes"][0]["script_file"])
    script["video_units"][0]["text"] = "@[阿明]在画面中"
    pm.save_script("demo", script, "episode_1.json")
    app = FastAPI()
    app.state.production_service = ProductionService(pm)
    register_error_handlers(app)
    app.include_router(production.router, prefix="/api/v1")
    app.include_router(build_asset_router(asset_type="character", pm_getter=lambda: pm), prefix="/api/v1")
    with TestClient(app) as client:
        url = "/api/v1/projects/demo/production/asset-prompt"
        preview = client.post(url, json={"kind": "character", "name": "阿明", "description": "青年"})
        assert preview.status_code == 200, preview.text
        assert preview.json()["affected_episodes"] == [1]
        custom = "完整自定义：画风、外观、三视图和负向词。"
        saved = client.patch("/api/v1/projects/demo/characters/阿明", json={"generation_prompt": custom})
        assert saved.status_code == 200
        from lib.artifacts.generation_input import asset_sheet_input, project_input_observation

        effective = asset_sheet_input(
            pm.load_project("demo"),
            asset_type="character",
            name="阿明",
            observation=project_input_observation(pm.get_project_path("demo")),
        )
        assert effective.render(max_reference_images=0, model="").prompt == custom
        assert (
            client.post(
                url, json={"kind": "character", "name": "阿明", "description": "青年", "generation_prompt": custom}
            ).json()["text"]
            == custom
        )


@pytest.mark.parametrize("with_file", [False, True])
def test_first_source_save_accepts_empty_previous_text_and_keeps_other_episodes(management, with_file):
    client, pm, _, first, second = management
    episode = create_episode(pm, "demo", title="Blank")
    project_dir = pm.get_project_path("demo")
    path = project_dir / "source" / f"episode_{episode}.txt"
    assert not path.exists()
    data = {"text": "新单集：小明说你好。", "expected_text": ""}
    original = _screenplay_docx()
    upload = {"file": ("单集.docx", original)} if with_file else None
    with client:
        saved = client.post(
            f"/api/v1/projects/demo/episodes/{episode}/production/source", data=data, files=upload
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["applied"]
        # An omitted/empty baseline must still refuse overwriting an existing source.
        conflict = client.post(
            f"/api/v1/projects/demo/episodes/{episode}/production/source",
            data={"text": "不能覆盖的新内容", "expected_text": ""},
        )
        assert conflict.status_code == 409
    assert path.read_text() == data["text"]
    assert (project_dir / "source" / f"episode_{first}.txt").read_text() == "第一集对白"
    assert (project_dir / "source" / f"episode_{second}.txt").read_text() == "第二集对白"
    assert pm.load_project("demo")["episodes"][-1]["source_origin"] == "own"
    if with_file:
        assert next((project_dir / "source" / "raw" / f"episode_{episode}").glob("*.docx")).read_bytes() == original
