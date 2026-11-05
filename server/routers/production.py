"""Five-step production page: explicit actions and read-only continuation status."""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from lib.db import async_session_factory
from lib.db.repositories.usage_repo import UsageRepository
from lib.episode.episode_source_commands import EpisodeSourceError, set_episode_source_text
from lib.generation.generation_admission import generation_admission_lock
from lib.generation.generation_queue import get_generation_queue
from lib.infra.api_errors import ConflictError
from lib.infra.async_thread import run_sync_transaction
from lib.project.production_management import MANAGEMENT_RESOURCE, set_archive
from lib.project.project_change_hints import project_change_source
from server.auth import CurrentUser
from server.i18n import Translator
from server.routers._episode_source_errors import episode_source_http_error
from server.routers._source_uploads import extract_uploaded_source_text
from server.services.project.production import ProductionError, ProductionService

router = APIRouter()
source_router = APIRouter()


def service(request: Request) -> ProductionService:
    return request.app.state.production_service


class AssetsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(min_length=1)


class VideosRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_request_durations: dict[str, Annotated[int, Field(ge=1, strict=True)]] | None = None
    unit_ids: list[str] | None = Field(default=None, min_length=1)


class AssetDraftPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["character", "scene", "prop"]
    name: str = Field(min_length=1)
    description: str = ""
    generation_prompt: str = ""


class AssetPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["character", "scene", "prop"]
    name: str = Field(min_length=1)


@router.get("/projects/{project_name}/production/costs")
async def production_costs(project_name: str, request: Request, user: CurrentUser):
    project = await asyncio.to_thread(service(request).projects.load_project, project_name)
    scripts = {
        e["script_file"].removeprefix("scripts/"): e["episode"]
        for e in project.get("episodes", [])
        if e.get("script_file")
    }
    # New episodes without scripts still have an empty cost view.
    for e in project.get("episodes", []):
        scripts.setdefault(f"__episode_{e['episode']}", e["episode"])
    async with async_session_factory() as session:
        return await UsageRepository(session).production_costs(project_name, scripts, user_id=user.id)


@router.post("/projects/{project_name}/production/assets/preview")
async def production_asset_preview(project_name: str, body: AssetPreviewRequest, request: Request, user: CurrentUser):
    return await service(request).preview_asset(project_name, body.kind, body.name, user.id)


@router.post("/projects/{project_name}/episodes/{episode}/production/render")
async def production_render(project_name: str, episode: int, request: Request, user: CurrentUser):
    try:
        return await service(request).start_render(project_name, episode, user.id)
    except ProductionError as exc:
        raise ConflictError("script_plan_refused", reason=str(exc)) from exc


@router.post("/projects/{project_name}/episodes/{episode}/production/videos/preview")
async def production_video_preview(
    project_name: str, episode: int, body: VideosRequest, request: Request, user: CurrentUser
):
    try:
        return await service(request).preview_videos(project_name, episode, user.id, body.unit_ids)
    except ProductionError as exc:
        raise ConflictError("script_plan_refused", reason=str(exc)) from exc


@router.get("/projects/{project_name}/episodes/{episode}/production")
async def production_status(project_name: str, episode: int, request: Request):
    return await service(request).status(project_name, episode)


@router.post("/projects/{project_name}/episodes/{episode}/production/assets")
async def production_assets(project_name: str, episode: int, body: AssetsRequest, request: Request, user: CurrentUser):
    try:
        return await service(request).start_assets(project_name, episode, user.id, body.fingerprint)
    except ProductionError as exc:
        raise ConflictError("script_plan_refused", reason=str(exc)) from exc


@router.post("/projects/{project_name}/episodes/{episode}/production/videos")
async def production_videos(project_name: str, episode: int, body: VideosRequest, request: Request, user: CurrentUser):
    try:
        return await service(request).start_videos(
            project_name, episode, user.id, body.confirmed_request_durations, body.unit_ids
        )
    except ProductionError as exc:
        raise ConflictError("script_plan_refused", reason=str(exc)) from exc


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


async def _archive(project: str, episode: int | None, archived: bool, request: Request):
    manager = service(request).projects
    # Admissions and archive checks share a cross-process lock. No task is cancelled.
    async with generation_admission_lock(project_name=project, script_file="", resource_id=MANAGEMENT_RESOURCE):
        if archived:
            queue = get_generation_queue()
            # Shared image tasks may update assets used by any episode: wait for all
            # project writers, rather than guessing ownership of old task payloads.
            for status in ("queued", "running"):
                tasks = await queue.list_tasks(project_name=project, status=status, page=1, page_size=1)
                if tasks.get("items"):
                    raise ConflictError("script_plan_refused", reason="项目仍有生成任务，请等待任务完成后归档")

            def has_continuation() -> bool:
                directory = manager.get_project_path(project) / "production"
                for path in directory.glob("episode_*.json"):
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if state.get("status") in {"active", "submitting", "unknown"}:
                        return True
                return False

            if await asyncio.to_thread(has_continuation):
                raise ConflictError("script_plan_refused", reason="制作任务仍在进行或等待核对，请完成后归档")
        await run_sync_transaction(set_archive, manager, project, archived, episode)
    return {"success": True}


@router.post("/projects/{project_name}/production/archive")
async def archive_project(project_name: str, body: ArchiveRequest, request: Request):
    return await _archive(project_name, None, body.archived, request)


@router.post("/projects/{project_name}/episodes/{episode}/production/archive")
async def archive_episode(project_name: str, episode: int, body: ArchiveRequest, request: Request):
    return await _archive(project_name, episode, body.archived, request)


@source_router.post("/production/source-preview")
async def preview_source(_t: Translator, file: UploadFile = File(...)):
    """Extract only: no project, episode or model call is created by preview."""
    if (file.filename or "").lower().rsplit(".", 1)[-1] not in {"docx", "txt", "md"}:
        raise HTTPException(status_code=400, detail=_t("source_unsupported_format", ext=file.filename or ""))
    text = await asyncio.to_thread(extract_uploaded_source_text, file, _t)
    if not text.strip():
        raise HTTPException(status_code=422, detail=_t("episode_source_empty"))
    return {"text": text, "filename": file.filename}


@router.post("/projects/{project_name}/episodes/{episode}/production/source")
async def save_production_source(
    project_name: str,
    episode: int,
    request: Request,
    _t: Translator,
    text: str = Form(...),
    expected_text: str = Form(""),
    confirm: bool = Form(False),
    file: UploadFile | None = File(None),
):
    # Validate original again before any write; the edited preview is the effective source.
    if file is not None:
        await preview_source(_t, file)

    def save():
        original = None
        if file is not None:
            file.file.seek(0)
            original = (file.filename or "source.docx", file.file.read())
        with project_change_source("webui"):
            written = set_episode_source_text(
                service(request).projects,
                project_name,
                episode,
                text,
                source_kind="screenplay",
                confirm=confirm,
                expected_text=expected_text,
                original=original,
                protect_content=True,
            )
        return {
            "success": True,
            "applied": written.applied,
            "needs_confirmation": not written.applied,
            "affected_episodes": written.affected_episodes,
        }

    try:
        return await run_sync_transaction(save)
    except EpisodeSourceError as exc:
        if exc.code == "episode_source_conflict":
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise episode_source_http_error(exc, _t, episode=episode) from exc


@router.post("/projects/{project_name}/production/asset-prompt")
async def production_draft_prompt(project_name: str, body: AssetDraftPromptRequest, request: Request):
    from lib.artifacts.generation_input import asset_sheet_input, project_input_observation
    from lib.project.asset_types import ASSET_SPECS
    from lib.project.episode_asset_references import ReferencedAsset, episode_referenced_assets
    from server.services.admission.asset_sheet_batch import load_episode_script

    def render():
        pm = service(request).projects
        project = pm.load_project(project_name)
        bucket = ASSET_SPECS[body.kind].bucket_key
        draft = {
            **project,
            bucket: {
                **project.get(bucket, {}),
                body.name: {
                    **project.get(bucket, {}).get(body.name, {}),
                    "description": body.description,
                    "generation_prompt": body.generation_prompt,
                },
            },
        }
        generation_input = asset_sheet_input(
            draft,
            asset_type=body.kind,
            name=body.name,
            observation=project_input_observation(pm.get_project_path(project_name)),
        )
        if generation_input.refusal:
            raise HTTPException(status_code=422, detail="资产参考图不可用，请先修复参考图")
        affected = [
            entry["episode"]
            for entry in project.get("episodes", [])
            if ReferencedAsset(body.kind, body.name)
            in episode_referenced_assets(project, load_episode_script(pm, project_name, entry["episode"]))
        ]
        return generation_input, affected

    generation_input, affected = await asyncio.to_thread(render)
    if generation_input.references:
        from server.services.admission.prompt_preview import reference_image_limit

        project = await asyncio.to_thread(service(request).projects.load_project, project_name)
        path = await asyncio.to_thread(service(request).projects.get_project_path, project_name)
        limit, model = await reference_image_limit(project_name, project, path)
    else:
        limit, model = 0, ""
    rendered = await asyncio.to_thread(generation_input.render, max_reference_images=limit, model=model)
    return {
        "text": rendered.prompt,
        "aspect_ratio": generation_input.canvas_ratio,
        "references": [ref.visual.path.name for ref in generation_input.references[: rendered.sent_count]],
        "system_message": None,
        "affected_episodes": affected,
    }


class ProductionEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeline_id: str
    base_revision: int = Field(ge=1)
    operations: list[dict] = Field(min_length=1, max_length=1000)


async def _production_timeline(project: str, episode: int, request: Request):
    from lib.edit_timeline.service import EditTimelineService

    state = await service(request).status(project, episode)
    timeline_id = state.get("timeline_id")
    if not timeline_id:
        raise HTTPException(status_code=409, detail="请先合成初稿，再编辑成片")
    from lib.edit_timeline.store import EditTimelineStore

    pm = service(request).projects
    document = await asyncio.to_thread(lambda: EditTimelineStore(pm, project).find(timeline_id))
    if document.episode != episode:
        raise HTTPException(status_code=409, detail="剪辑不属于当前集")
    return EditTimelineService(pm), timeline_id


@router.get("/projects/{project_name}/episodes/{episode}/production/edit")
async def production_edit_read(project_name: str, episode: int, request: Request):
    from lib.edit_timeline.manual_subtitles import edit_issues
    from lib.edit_timeline.sources import load_episode_script_units, load_episode_sources
    from lib.edit_timeline.store import EditTimelineStore

    timelines, timeline_id = await _production_timeline(project_name, episode, request)
    pm = service(request).projects
    document = await asyncio.to_thread(lambda: EditTimelineStore(pm, project_name).find(timeline_id))
    if document.episode != episode:
        raise HTTPException(status_code=409, detail="剪辑不属于当前集")
    script = await asyncio.to_thread(load_episode_script_units, pm, project_name, episode)
    sources = await load_episode_sources(
        pm, project_name, script, {clip.unit_id for clip in document.latest.content.clips}
    )
    readout = await timelines.read(project_name, timeline_id)
    return {
        "timeline_id": timeline_id,
        "revision": document.latest.number,
        "clips": [clip.model_dump(mode="json") for clip in document.latest.content.clips],
        "media": {
            key: {"version": value.video_version, "duration_us": value.video_duration_us}
            for key, value in sources.media.items()
        },
        "subtitle_drafts": {unit.unit_id: unit.subtitle_text for unit in script.units},
        "duration": readout.duration,
        "issues": edit_issues(document.latest.content.clips, sources.media),
    }


@router.put("/projects/{project_name}/episodes/{episode}/production/edit")
async def production_edit_save(
    project_name: str, episode: int, body: ProductionEditRequest, request: Request, user: CurrentUser
):
    from pydantic import ValidationError

    from lib.edit_timeline.errors import EditTimelineError
    from lib.edit_timeline.model import RevisionAuthor
    from lib.edit_timeline.operations import TimelineOperationAdapter
    from server.routers.edit_timelines import edit_timeline_api_error

    timelines, timeline_id = await _production_timeline(project_name, episode, request)
    if timeline_id != body.timeline_id:
        raise HTTPException(status_code=409, detail="剪辑目标已变化，请刷新后重试")
    try:
        operations = [TimelineOperationAdapter.validate_python(op) for op in body.operations]
        await timelines.edit(
            project_name,
            timeline_id,
            base_revision=body.base_revision,
            summary="人工编辑成片",
            operations=operations,
            author=RevisionAuthor(kind="creator", user_id=user.id),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EditTimelineError as exc:
        if exc.code == "revision_conflict":
            raise HTTPException(status_code=409, detail="剪辑已在其他窗口修改，请刷新后重试") from exc
        if exc.code in {"operation_invalid", "revision_summary_invalid"}:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise edit_timeline_api_error(exc) from exc
    return await production_edit_read(project_name, episode, request)


class FeedbackSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tester_id: str = Field(default="", max_length=120)
    test_batch_id: str = Field(default="", max_length=120)
    test_case_id: str = Field(default="", max_length=120)


def _feedback_store(request: Request):
    from lib.production_feedback.store import register_root

    return register_root(service(request).projects.data_root)


@router.get("/production/feedback/settings")
async def feedback_settings(request: Request, user: CurrentUser):
    return await asyncio.to_thread(lambda: _feedback_store(request).settings())


@router.put("/production/feedback/settings")
async def save_feedback_settings(body: FeedbackSettingsRequest, request: Request, user: CurrentUser):
    return await run_sync_transaction(lambda: _feedback_store(request).save_settings(body.model_dump()))


@router.get("/projects/{project_name}/production/feedback/export")
async def export_feedback(project_name: str, request: Request, user: CurrentUser, include_media: bool = False):
    from starlette.responses import Response

    from lib.production_feedback.export import export_project

    def build():
        service(request).projects.load_project(project_name)
        return export_project(_feedback_store(request), project_name, include_media=include_media)

    raw = await run_sync_transaction(build)
    return Response(
        raw,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="feedback-{project_name}.zip"'},
    )


@router.get("/projects/{project_name}/production/feedback/objects")
async def feedback_objects(project_name: str, request: Request, user: CurrentUser):
    def read():
        service(request).projects.load_project(project_name)
        store = _feedback_store(request)
        store.baseline_project(project_name)
        latest = {}
        reviews = {}
        chain = store.project_chain(project_name)
        for event in store.events(project_name):
            if event.get("project_chain_id") != chain:
                continue
            if event["event_type"] == "object_revision":
                latest[event["resource"]] = {
                    "revision": event["after_revision"],
                    "chain_id": event["object_chain_id"],
                    "value": event["after"],
                }
            elif event["event_type"] == "reviewed":
                reviews[f"{event['resource']}:{event['after']['layer']}"] = event["after"]
        return {"objects": latest, "reviews": reviews}

    return await run_sync_transaction(read)


class FeedbackReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: str = Field(min_length=1, max_length=500)
    revision: str = Field(min_length=64, max_length=64)
    usable: bool
    layer: Literal["text", "media"]
    reason: str | None = Field(default=None, max_length=500)


@router.post("/projects/{project_name}/production/feedback/review")
async def review_feedback(project_name: str, body: FeedbackReviewRequest, request: Request, user: CurrentUser):
    def write():
        service(request).projects.load_project(project_name)
        store = _feedback_store(request)
        store.baseline_project(project_name)
        candidates = [
            event
            for event in store.events(project_name)
            if event["event_type"] == "object_revision"
            and event["resource"] == body.resource
            and event.get("project_chain_id") == store.project_chain(project_name)
        ]
        if not candidates or candidates[-1]["after_revision"] != body.revision:
            raise ConflictError("script_plan_refused", reason="Content changed; refresh before reviewing")
        if body.layer == "media" and not (candidates[-1]["after"] or {}).get("current_version"):
            raise ConflictError("script_plan_refused", reason="No selected media version")
        if (body.layer == "media") != body.resource.startswith("media/"):
            raise ConflictError("script_plan_refused", reason="Review layer does not match the object")
        return store.review(
            project_name,
            body.resource,
            revision=body.revision,
            usable=body.usable,
            reason=body.reason,
            layer=body.layer,
        )

    return await run_sync_transaction(write)
