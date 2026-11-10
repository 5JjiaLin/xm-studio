"""Single-episode continuations over the existing generation batches and render lane.

The small journal associates the user's explicit action with its current batch. A
submission whose response was lost is never replayed automatically after restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from typing import Any

from lib.config.resolver import ConfigResolver
from lib.db import async_session_factory
from lib.edit_timeline import EditTimelineService, RevisionAuthor
from lib.final_cut.basis import BURNED_SUBTITLES
from lib.final_cut.service import FinalCutService
from lib.generation.generation_batch import GenerationBatchReadModel
from lib.generation.generation_result import GenerationBatchResult
from lib.infra.async_thread import run_sync_transaction
from lib.infra.data_root_layout import list_project_dirs
from lib.infra.json_io import atomic_write_json
from lib.jianying_draft.basis import WITHOUT_NARRATION
from lib.project.project_manager import ProjectManager
from server.media_tools.assets import GenerateAssetsRequest, generate_assets
from server.media_tools.final_cuts import FinalCutToolResult, RenderFinalCutRequest, render_final_cut
from server.media_tools.videos import EpisodeTarget, GenerateVideosRequest, SelectedTarget, generate_videos
from server.services.admission.asset_sheet_batch import AssetSheetScope, load_asset_sheet_batch_plan
from server.services.admission.cost_estimation import estimate_image_batch_cost
from server.services.project.post_production import PostProduction, ProductionError
from server.services.project.script_review import ScriptReviewService
from server.services.tasks.formal_image_commit import get_aspect_ratio
from server.tool_runtime import (
    CallerContext,
    GenerateEpisodeScriptRequest,
    ProjectScope,
    Services,
    ToolRequest,
    generate_episode_script,
)

logger = logging.getLogger(__name__)


class ProductionService:
    def __init__(self, projects: ProjectManager, services: Services | None = None):
        self.projects = projects
        self.services = services or Services.defaults(projects)
        from server.services.project.whole_script_batch import WholeScriptBatchService

        self.script_batch = WholeScriptBatchService(projects, self.services)
        self.post_production = PostProduction(self)
        self._runner: asyncio.Task | None = None

    def _path(self, project: str, episode: int):
        return self.projects.get_project_path(project) / "production" / f"episode_{episode}.json"

    def _read(self, project: str, episode: int) -> dict[str, Any]:
        path = self._path(project, episode)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"episode": episode, "status": "idle", "stage": None}

    async def status(self, project: str, episode: int) -> dict[str, Any]:
        state = await asyncio.to_thread(self._read, project, episode)
        if state.get("batch_id") and state["status"] in {"active", "unknown"}:
            batch = await self.services.queue.get_generation_batch(
                project_name=project, batch_id=state["batch_id"], user_id=state["user_id"]
            )
            state["members"] = [member.model_dump(mode="json") for member in batch.members]
        if state.get("output") and state.get("timeline_id"):
            output = await FinalCutService(self.projects).status(
                project,
                state["timeline_id"],
                narration=state.get("narration", "without_narration"),
                subtitles=BURNED_SUBTITLES,
            )
            state["output"] = output.model_dump(mode="json")
            if state.get("basis") != await asyncio.to_thread(self._script_hash, project, episode):
                state["output"]["status"] = "stale"
        return state

    async def _write(self, project: str, episode: int, state: dict[str, Any]):
        path = await asyncio.to_thread(self._path, project, episode)
        await run_sync_transaction(atomic_write_json, path, state)

    def _script_hash(self, project: str, episode: int) -> str:
        data = self.projects.load_project(project)
        entry = next(e for e in data["episodes"] if e["episode"] == episode)
        script = self.projects.load_script(project, entry["script_file"].removeprefix("scripts/"))
        # Media completion changes generated_assets, but not the confirmed inputs.
        payload = {"episode": episode, "aspect_ratio": data.get("aspect_ratio")}
        payload["video_units"] = [
            {k: v for k, v in u.items() if k != "generated_assets"} for u in script.get("video_units", [])
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def start_assets(self, project: str, episode: int, user_id: str, fingerprint: str) -> dict[str, Any]:
        path = await asyncio.to_thread(self._path, project, episode)
        async with self.projects.async_file_lock(path):
            state = await self.status(project, episode)
            if state["status"] in {"active", "submitting", "unknown"}:
                return state
            review = ScriptReviewService(self.projects)
            current = await review.get_state(project, episode)
            if current["fingerprint"] != fingerprint:
                raise ProductionError("script_plan_changed")
            # The same accepted content cannot launch a second paid continuation.
            if (
                state.get("action") == "assets"
                and state.get("fingerprint") == fingerprint
                and state["status"] == "completed"
            ):
                return state
            state = {
                **{key: state[key] for key in ("timeline_id", "output") if key in state},
                "episode": episode,
                "action": "assets",
                "fingerprint": fingerprint,
                "user_id": user_id,
                "status": "submitting",
                "stage": "confirm",
            }
            await self._write(project, episode, state)
            try:
                if current["status"] != "confirmed":
                    if current.get("script_overwrite"):
                        raise ProductionError("script_overwrite_requires_review")
                    await review.confirm(project, episode)
                # Image generation is an explicit action after prompt review. Video
                # prompt authoring is a separate user action on the video step.
                state["basis"] = await asyncio.to_thread(self._script_hash, project, episode)
                await self._submit(project, episode, state, "assets")
            except Exception as exc:
                await self._fail(project, episode, state, exc)
                raise
            return state

    async def start_videos(
        self,
        project: str,
        episode: int,
        user_id: str,
        durations: dict[str, int] | None = None,
        unit_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        path = await asyncio.to_thread(self._path, project, episode)
        async with self.projects.async_file_lock(path):
            state = await self.status(project, episode)
            if state["status"] in {"active", "submitting", "unknown"}:
                return state
            basis = await asyncio.to_thread(self._script_hash, project, episode)
            if (
                state.get("action") == "videos"
                and state.get("basis") == basis
                and state["status"] == "completed"
                and not unit_ids
            ):
                # Completed video batches wait for explicit review, even before a timeline exists.
                # Rerolls use selected unit_ids; a repeated whole-episode action is idempotent.
                if state.get("stage") == "videos" and not state.get("unit_ids"):
                    return state
                if state.get("stage") != "videos" and state.get("timeline_id"):
                    output = await FinalCutService(self.projects).status(
                        project, state["timeline_id"], narration="without_narration", subtitles=BURNED_SUBTITLES
                    )
                    if output.status == "current":
                        return state
            if durations is None:
                admission = await self.preview_videos(project, episode, user_id, unit_ids)
                if not admission["submittable"]:
                    details = [
                        p["detail"]
                        for unit in admission.get("units", [])
                        for p in unit.get("problems", [])
                        if p.get("detail")
                    ]
                    raise ProductionError("; ".join(details) or "generation_blocked")
                durations = admission["confirmed_request_durations"]
            state = {
                **{key: state[key] for key in ("timeline_id", "output") if key in state},
                "episode": episode,
                "action": "videos",
                "user_id": user_id,
                "status": "submitting",
                "stage": "videos",
                "basis": basis,
                "durations": durations,
                "unit_ids": unit_ids,
            }
            await self._write(project, episode, state)
            try:
                await self._submit(project, episode, state, "videos")
            except Exception as exc:
                await self._fail(project, episode, state, exc)
                raise
            return state

    async def preview_videos(self, project: str, episode: int, user_id: str, unit_ids: list[str] | None = None):
        entry = await asyncio.to_thread(
            lambda: next(e for e in self.projects.load_project(project)["episodes"] if e["episode"] == episode)
        )
        target = (
            SelectedTarget(scope="selected", ids=unit_ids)
            if unit_ids
            else EpisodeTarget(scope="episode", episode_id=episode)
        )
        result = await generate_videos(
            ToolRequest(
                GenerateVideosRequest(
                    script=entry["script_file"].removeprefix("scripts/"),
                    target=target,
                    force=bool(unit_ids),
                    preview=True,
                )
            ),
            ProjectScope(project_name=project, data_root=self.projects.data_root),
            CallerContext(user_id=user_id, source="mcp"),
            self.services,
        )
        if result.problem:
            raise ProductionError(result.problem.detail)
        if not isinstance(result.value, dict):
            raise ProductionError("video_preview_unavailable")
        return result.value["video_quote"]

    async def preview_asset(self, project: str, kind: str, name: str, user_id: str):
        """Use the existing explicit-asset admission and price calculation."""
        data, plan = await load_asset_sheet_batch_plan(
            self.projects,
            self.services.queue,
            project_name=project,
            scope=AssetSheetScope(asset_type=kind, names=(name,)),
            user_id=user_id,
        )
        estimate = await estimate_image_batch_cost(
            data,
            ["i2i" if plan.units[uid].image_to_image else "t2i" for uid in plan.new_task_ids],
            aspect_ratio=get_aspect_ratio(data, "characters"),
            resolver=ConfigResolver(async_session_factory),
            session_factory=async_session_factory,
        )
        problems = [item.problem.detail for item in plan.preflight.items if item.problem]
        return {**estimate.to_payload(), "submittable": bool(plan.new_task_ids) and not problems, "problems": problems}

    async def start_render(self, project: str, episode: int, user_id: str) -> dict[str, Any]:
        """Rebuild output from current clips without submitting provider generation."""
        path = await asyncio.to_thread(self._path, project, episode)
        async with self.projects.async_file_lock(path):
            current = await self.status(project, episode)
            if current["status"] in {"active", "submitting", "unknown"}:
                return current
            basis = await asyncio.to_thread(self._script_hash, project, episode)
            state = {
                **{key: current[key] for key in ("timeline_id", "output") if key in current},
                "episode": episode,
                "action": "videos",
                "user_id": user_id,
                "status": "submitting",
                "stage": "render",
                "narration": WITHOUT_NARRATION,
                "basis": basis,
            }
            await self._write(project, episode, state)
            try:
                await self._submit(project, episode, state, "render")
            except Exception as exc:
                await self._fail(project, episode, state, exc)
                raise
            return state

    async def _submit(self, project: str, episode: int, state: dict[str, Any], stage: str):
        # Persist intent before calling a command; interruption leaves a queryable
        # ambiguous state rather than authorizing a second supplier submission.
        state.update(status="submitting", stage=stage, batch_id=None, error=None)
        await self._write(project, episode, state)
        scope = ProjectScope(project_name=project, data_root=self.projects.data_root)
        # Media tool adapters return their durable batch handle for this host.
        caller = CallerContext(user_id=state["user_id"], source="mcp")
        if stage == "prompts":
            result = await generate_episode_script(
                ToolRequest(GenerateEpisodeScriptRequest(episode_id=episode)), scope, caller, self.services
            )
        elif stage == "assets":
            result = await generate_assets(
                ToolRequest(GenerateAssetsRequest(episode_id=episode)), scope, caller, self.services
            )
        elif stage == "videos":
            entry = await asyncio.to_thread(
                lambda: next(e for e in self.projects.load_project(project)["episodes"] if e["episode"] == episode)
            )
            ids = state.get("unit_ids")
            target = (
                SelectedTarget(scope="selected", ids=ids) if ids else EpisodeTarget(scope="episode", episode_id=episode)
            )
            request = GenerateVideosRequest(
                script=entry["script_file"].removeprefix("scripts/"),
                target=target,
                confirmed_request_durations=state.get("durations"),
                force=bool(ids),
            )
            result = await generate_videos(ToolRequest(request), scope, caller, self.services)
        else:
            timelines = EditTimelineService(self.projects)
            timeline_id = state.get("timeline_id")
            if not timeline_id:
                # The name is unique in the episode. Recover a timeline created
                # before an interrupted journal write without creating another.
                name = "XM Studio " + state["basis"][:12]
                existing = await timelines.list_timelines(project, episode=episode)
                match = next((t for t in existing if t.name == name), None)
                timeline_id = (
                    match.id
                    if match
                    else (
                        await timelines.create_from_script(
                            project,
                            episode=episode,
                            name=name,
                            author=RevisionAuthor(kind="creator", user_id=state["user_id"]),
                        )
                    ).timeline.id
                )
                state["timeline_id"] = timeline_id
                await self._write(project, episode, state)
            state["narration"] = WITHOUT_NARRATION
            # Mechanical creation supplies defaults once. Keep every creator revision on rerender.
            # MCP host semantics return the durable render handle immediately.
            result = await render_final_cut(
                ToolRequest(
                    RenderFinalCutRequest(
                        timeline=timeline_id,
                        **({"revision": state["revision"]} if state.get("revision") is not None else {}),
                        narration=state.get("narration", "without_narration"),
                        burn_subtitles=True,
                    )
                ),
                scope,
                CallerContext(user_id=state["user_id"], source="mcp"),
                self.services,
            )
        if result.problem:
            raise ProductionError(result.problem.detail)
        batch = result.value
        if isinstance(batch, GenerationBatchReadModel):
            state.update(status="active", batch_id=batch.batch_id)
        elif isinstance(batch, FinalCutToolResult):
            # FinalCutToolResult carries the durable render batch beside
            # its immediate projection. Keep the same polling contract as media
            # generation instead of treating the projection as a terminal state.
            state.update(status="active", batch_id=batch.batch_id)
        elif isinstance(batch, dict) and batch.get("generation_result") is not None:
            generation = GenerationBatchResult.model_validate(batch["generation_result"])
            if not generation.ok:
                raise ProductionError(str(batch.get("summary") or "generation_blocked"))
            # An immediate result may still describe an accepted asynchronous batch.
            batch_id = batch.get("batch_id")
            if not batch_id:
                raise ProductionError("generation_batch_handle_missing")
            state.update(status="active", batch_id=batch_id)
        else:
            raise ProductionError(str(batch))
        await self._write(project, episode, state)

    async def _fail(self, project: str, episode: int, state: dict[str, Any], exc: Exception):
        state.update(status="failed" if isinstance(exc, ProductionError) else "unknown", error=str(exc))
        await self._write(project, episode, state)

    async def advance(self, project: str, episode: int):
        path = await asyncio.to_thread(self._path, project, episode)
        async with self.projects.async_file_lock(path):
            state = await asyncio.to_thread(self._read, project, episode)
            if state.get("action") == "finish":
                if state["status"] == "submitting":
                    # The local render submission may have been accepted before
                    # its handle was saved. Do not silently submit it again.
                    state.update(status="failed", error="finish_render_failed")
                    await self._write(project, episode, state)
                    return
                await self.post_production.advance(project, episode, state)
                return
            if state["status"] == "submitting":
                # The submission holds this same lock until its handle is saved.
                # Reaching it here means that process was interrupted. Do not
                # infer a batch or replay a possibly accepted provider request.
                state.update(status="unknown", error="submission_result_unknown")
                await self._write(project, episode, state)
                return
            if state["status"] not in {"active", "unknown"} or not state.get("batch_id"):
                return
            try:
                batch_id = state.get("batch_id")
                batch: GenerationBatchReadModel | None = None
                if batch_id:
                    batch = await self.services.queue.get_generation_batch(
                        project_name=project, batch_id=batch_id, user_id=state["user_id"]
                    )
                    if not batch.done:
                        if state["status"] == "unknown":
                            state.update(status="active", error=None)
                            await self._write(project, episode, state)
                        return
                    if batch.counts.failed or batch.counts.cancelled or batch.counts.blocked:
                        state.update(
                            status="failed",
                            error="generation_failed",
                            members=[m.model_dump(mode="json") for m in batch.members],
                        )
                        await self._write(project, episode, state)
                        return
                stage = state["stage"]
                if stage == "prompts":
                    state["basis"] = await asyncio.to_thread(self._script_hash, project, episode)
                    state.update(status="completed", error=None)
                    await self._write(project, episode, state)
                elif stage == "videos":
                    if state["basis"] != await asyncio.to_thread(self._script_hash, project, episode):
                        raise ProductionError("script_changed_before_render")
                    state.update(status="completed", error=None)
                    if batch is not None:
                        state["members"] = [m.model_dump(mode="json") for m in batch.members]
                    await self._write(project, episode, state)
                else:
                    if state.get("basis") != await asyncio.to_thread(self._script_hash, project, episode):
                        raise ProductionError("script_changed_before_completion")
                    if stage == "render":
                        output = await FinalCutService(self.projects).status(
                            project, state["timeline_id"], narration="without_narration", subtitles=BURNED_SUBTITLES
                        )
                        if output.status != "current" or output.version is None:
                            raise ProductionError("final_cut_not_current")
                        state["output"] = output.model_dump(mode="json")
                    state.update(status="completed", error=None)
                    await self._write(project, episode, state)
            except Exception as exc:
                logger.exception("Production continuation failed project=%s episode=%s", project, episode)
                await self._fail(project, episode, state, exc)

    def _journals(self):
        return [
            (p.parent.parent.name, int(p.stem.removeprefix("episode_")))
            for d in list_project_dirs(self.projects.projects_dir)
            for p in (d / "production").glob("episode_*.json")
        ]

    async def _run(self):
        while True:
            batch_projects = await asyncio.to_thread(
                lambda: [
                    d.name
                    for d in list_project_dirs(self.projects.projects_dir)
                    if (d / "production" / "script_batch.json").is_file()
                ]
            )
            for batch_project in batch_projects:
                try:
                    await self.script_batch.advance(batch_project)
                except Exception:
                    logger.exception("Cannot advance screenplay batch project=%s", batch_project)
            for project, episode in await asyncio.to_thread(self._journals):
                try:
                    await self.advance(project, episode)
                except Exception:
                    logger.exception("Cannot read production journal project=%s episode=%s", project, episode)
            await asyncio.sleep(2)

    def start(self):
        self._runner = asyncio.create_task(self._run())

    async def stop(self):
        if self._runner:
            self._runner.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner
