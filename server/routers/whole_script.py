"""Whole screenplay import and explicit sequential text-only breakdown."""

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from lib.episode.whole_script import divide_script, import_script, project_basis
from lib.infra.async_thread import run_sync_transaction
from lib.project.project_change_hints import project_change_source
from server.auth import CurrentUser
from server.i18n import Translator
from server.routers._source_uploads import extract_uploaded_source_text

router = APIRouter()


class Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_line: int = Field(ge=1, strict=True)
    title: str = Field(max_length=200)
    number: int = Field(ge=1, strict=True)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=5_000_000)
    boundaries: list[Boundary] | None = Field(default=None, min_length=1, max_length=1000)


class StartRequest(BaseModel):
    import_id: str = Field(min_length=1, max_length=64)


class ControlRequest(BaseModel):
    action: Literal["stop", "continue"]
    input_revision: str | None = None


def _preview(projects, name, body):
    # Normalize before converting line numbers; never mix browser UTF-16 offsets
    # with the Python Unicode coordinates persisted by the source ledger.
    from lib.episode.episode_ledger import normalize_source_text

    text = normalize_source_text(body.text)
    boundaries = None
    if body.boundaries is not None:
        lines = text.splitlines(keepends=True)
        offsets, offset = [], 0
        for line in lines:
            offsets.append(offset)
            offset += len(line)
        if any(b.start_line > len(offsets) for b in body.boundaries):
            raise ValueError("分界行号超出全文，请核对")
        boundaries = [
            {"start": offsets[b.start_line - 1], "title": b.title, "number": b.number} for b in body.boundaries
        ]
    project = projects.load_project(name)
    result = divide_script(text, boundaries)
    for part in result["episodes"]:
        part["start_line"] = text.count("\n", 0, part["start"]) + 1
    result["project_basis"] = project_basis(project)
    return result


@router.post("/projects/{name}/production/whole-script/preview")
async def preview(name: str, body: PreviewRequest, request: Request, user: CurrentUser):
    try:
        return await asyncio.to_thread(_preview, request.app.state.production_service.projects, name, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/projects/{name}/production/whole-script/import")
async def confirm(
    name: str,
    request: Request,
    user: CurrentUser,
    _t: Translator,
    text: str = Form(...),
    boundaries: str = Form(...),
    fingerprint: str = Form(...),
    expected_project: str = Form(...),
    file: UploadFile | None = File(None),
):
    if len(text) > 5_000_000:
        raise HTTPException(status_code=413, detail="剧本过长，请分批导入")
    if file is not None:
        if (file.filename or "").lower().rsplit(".", 1)[-1] not in {"docx", "txt", "md"}:
            raise HTTPException(status_code=422, detail="仅支持 DOCX、TXT、MD")
        await asyncio.to_thread(extract_uploaded_source_text, file, _t)

    def save():
        parts = json.loads(boundaries)
        # Re-run the same validated line-number conversion as the preview.
        body = PreviewRequest.model_validate({"text": text, "boundaries": parts})
        projects = request.app.state.production_service.projects
        result = _preview(projects, name, body)
        canonical = [{k: p[k] for k in ("start", "title", "number")} for p in result["episodes"]]
        original = None
        if file is not None:
            file.file.seek(0)
            original = (file.filename or "source.txt", file.file.read())
        with project_change_source("webui"):
            return import_script(projects, name, text, canonical, fingerprint, expected_project, original)

    try:
        return await run_sync_transaction(save)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/projects/{name}/production/script-batch")
async def status(name: str, request: Request, user: CurrentUser):
    return await request.app.state.production_service.script_batch.status(name)


@router.post("/projects/{name}/production/script-batch")
async def start(name: str, body: StartRequest, request: Request, user: CurrentUser):
    try:
        return await request.app.state.production_service.script_batch.start(name, body.import_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/projects/{name}/production/script-batch/control")
async def control(name: str, body: ControlRequest, request: Request, user: CurrentUser):
    try:
        return await request.app.state.production_service.script_batch.control(
            name, body.action, user.id, body.input_revision
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
