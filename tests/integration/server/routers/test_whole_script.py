"""Whole-script HTTP lifecycle: preview is read-only, import is atomic, start is explicit."""

import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.episode.episode_paths import episode_source_path
from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import production
from server.services.project.production import ProductionService

TEXT = "第一集 开始\n甲说：你好。🙂\n第二集 继续\n甲换上盔甲。"
BASE = "/api/v1/projects/demo/production"


@pytest.fixture
def whole_client(tmp_path, monkeypatch):
    pm = ProjectManager(tmp_path / "data")
    pm.create_project("demo", content_mode="drama")
    pm.create_project_metadata("demo", content_mode="drama")
    queue = SimpleNamespace(list_tasks=AsyncMock(return_value={"items": []}))
    monkeypatch.setattr(production, "get_generation_queue", lambda: queue)
    app = FastAPI()
    app.state.production_service = ProductionService(pm, SimpleNamespace(queue=queue))
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="u1", sub="test")
    register_error_handlers(app)
    app.include_router(production.router, prefix="/api/v1")
    app.include_router(production.source_router, prefix="/api/v1")
    return TestClient(app), pm, queue


def confirm(client, preview, file=None):
    return client.post(
        BASE + "/whole-script/import",
        data={
            "text": preview["text"],
            "fingerprint": preview["fingerprint"],
            "expected_project": preview["project_basis"],
            "boundaries": json.dumps(
                [{k: p[k] for k in ("start_line", "title", "number")} for p in preview["episodes"]]
            ),
        },
        files={"file": file} if file else None,
    )


@pytest.mark.parametrize("extension", ["docx", "txt", "md"])
def test_upload_preview_import_and_explicit_start(whole_client, extension):
    client, pm, queue = whole_client
    raw = TEXT.encode()
    if extension == "docx":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            paragraphs = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in TEXT.splitlines())
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + paragraphs
                + "</w:body></w:document>",
            )
        raw = buffer.getvalue()
    upload = (f"整本.{extension}", raw)
    with client:
        extracted = client.post("/api/v1/production/source-preview", files={"file": upload})
        assert extracted.status_code == 200
        preview = client.post(BASE + "/whole-script/preview", json={"text": extracted.json()["text"]})
        assert preview.status_code == 200
        assert pm.load_project("demo").get("episodes", []) == []
        imported = confirm(client, preview.json(), upload)
        assert imported.status_code == 200, imported.text
        assert confirm(client, preview.json(), upload).json() == imported.json()
        ids = imported.json()["episodes"]
        assert len(ids) == 2
        directory = pm.get_project_path("demo")
        assert "盔甲" in episode_source_path(directory, ids[1]).read_text()
        assert next((directory / "source" / "raw").rglob(f"*.{extension}")).read_bytes() == raw
        state = client.get(BASE + "/script-batch").json()
        assert state["status"] == "idle"
        assert len(state["imports"]) == 1
        queue.list_tasks.assert_not_awaited()
        started = client.post(BASE + "/script-batch", json={"import_id": imported.json()["import_id"]})
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "active"
        assert not list((directory / "scripts").rglob("*.json"))
        assert client.post(BASE + "/archive", json={"archived": True}).status_code == 409
        stopped = client.post(BASE + "/script-batch/control", json={"action": "stop"})
        assert stopped.json()["status"] == "stopped"
        assert client.get(BASE + "/script-batch").json()["status"] == "stopped"


def test_invalid_preview_fingerprint_and_project_conflict_do_not_write(whole_client):
    client, pm, _ = whole_client
    with client:
        invalid = client.post(
            BASE + "/whole-script/preview",
            json={"text": TEXT, "boundaries": [{"start_line": 999, "number": 1, "title": "一"}]},
        )
        assert invalid.status_code == 422
        preview = client.post(BASE + "/whole-script/preview", json={"text": TEXT}).json()
        assert confirm(client, {**preview, "fingerprint": "wrong"}).status_code == 409
        pm.update_project("demo", lambda p: p.update(production_management={"archived": True}))
        assert confirm(client, preview).status_code == 409
    assert pm.load_project("demo").get("episodes", []) == []
    assert not list((pm.get_project_path("demo") / "source").glob("screenplay-*"))
