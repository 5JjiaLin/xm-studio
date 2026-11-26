"""Submit episode edits to the existing queue and record the adopted delivery."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lib.generation.generation_queue import GenerationBatchNotFound
from lib.infra.async_thread import run_sync_transaction
from lib.infra.json_io import atomic_write_json, load_json
from lib.openfilm import runtime
from lib.project.production_management import assert_production_open
from lib.project.task_project_claim import ensure_task_project_claim
from server.media_tools.render_submission import RenderSubmission, submit_render_task
from server.services.tasks.render_tasks import RenderTaskRequest
from server.tool_runtime import CallerContext, ProjectScope

if TYPE_CHECKING:
    from server.services.project.production import ProductionService

logger = logging.getLogger(__name__)
BUSY = {"active", "submitting", "unknown"}


def directory(root: Path, episode: int, job: str) -> Path:
    try:
        if str(UUID(hex=job)).replace("-", "") != job:
            raise ValueError
    except ValueError as exc:
        raise runtime.EditError("剪辑版本不存在") from exc
    return root / "production" / "edits" / str(episode) / job


def published_relative(episode: int, job: str) -> str:
    return f"renders/episode_{episode}/edit-{job}/final.mp4"


def delivery(root: Path, episode: int, job: str) -> dict:
    try:
        return load_json(directory(root, episode, job) / "delivery.json")
    except FileNotFoundError as exc:
        raise runtime.EditError("剪辑尚未完成") from exc


async def execute_edit(task: dict, projects) -> dict:
    project = task["project_name"]
    episode = task["payload"]["episode"]
    job = task["payload"]["job"]
    root = await asyncio.to_thread(projects.get_project_path, project)
    work = directory(root, episode, job)
    result = await runtime.execute(projects, project, task, work)
    inputs = await asyncio.to_thread(load_json, work / "input.json")
    target = root / published_relative(episode, job)
    ensure_task_project_claim(project)
    await run_sync_transaction(runtime.publish_video, work / "final.mp4", target)
    record = {"id": job, "basis": runtime.input_basis(inputs), "music": inputs["music"], **result}
    await run_sync_transaction(atomic_write_json, work / "delivery.json", record)
    return {"file_path": published_relative(episode, job), "edit_id": job}


class EpisodeEdit:
    def __init__(self, owner: ProductionService):
        self.owner = owner
        self.projects = owner.projects
        self.queue = owner.services.queue

    async def start(
        self, project: str, episode: int, user: str, music: bool, *, resume: bool = False, retry: bool = False
    ) -> None:
        await asyncio.to_thread(assert_production_open, self.projects, project, {episode})
        await asyncio.to_thread(runtime.engine_path)
        path = await asyncio.to_thread(self.owner._path, project, episode)
        async with self.projects.async_file_lock(path):
            state = await asyncio.to_thread(self.owner._read, project, episode)
            if state["status"] in BUSY:
                return
            root = await asyncio.to_thread(self.projects.get_project_path, project)
            if resume or retry:
                if state.get("action") != "edit" or state["status"] != "failed":
                    raise runtime.EditError("当前没有可继续的剪辑")
                job = state["edit_id"]
                work = directory(root, episode, job)
                recovery = await asyncio.to_thread(runtime.recovery, work)
                if resume and recovery != "resume":
                    raise runtime.EditError("本次剪辑不能直接继续，请重新剪辑")
                if retry and recovery == "resume":
                    raise runtime.EditError("已有可继续的剪辑，请使用继续剪辑")
                if retry:
                    music = (await asyncio.to_thread(load_json, work / "input.json"))["music"]
            if not resume:
                inputs = await asyncio.to_thread(runtime.snapshot, self.projects, project, episode, music)
                basis = runtime.input_basis(inputs)
                if not retry and state.get("action") == "edit" and state.get("edit_basis") == basis:
                    if state["status"] == "completed":
                        return
                    if state["status"] == "failed":
                        return  # Open the existing failure and its specific recovery action.
                job = uuid4().hex
                work = directory(root, episode, job)
                await asyncio.to_thread(work.mkdir, parents=True)
                await run_sync_transaction(atomic_write_json, work / "input.json", inputs)
                state = {
                    **{k: state[k] for k in ("timeline_id", "output", "basis", "narration") if k in state},
                    "episode": episode,
                    "action": "edit",
                    "stage": "edit",
                    "edit_id": job,
                    "edit_basis": basis,
                }
            operation = uuid4().hex
            state.update(
                status="submitting", batch_id=None, operation_id=operation, error=None, user_id=user, edit_recovery=None
            )
            await self.owner._write(project, episode, state)
            try:
                submission = await submit_render_task(
                    RenderTaskRequest(
                        runtime.TASK_TYPE,
                        f"episode-edit-{job}",
                        f"episode-edit/{job}",
                        published_relative(episode, job),
                        {"episode": episode, "job": job},
                    ),
                    operation=runtime.TASK_TYPE,
                    scope=ProjectScope(project, self.projects.data_root),
                    caller=CallerContext(user_id=user, source="mcp", production_batch_id=operation),
                    services=self.owner.services,
                )
            except Exception:
                logger.exception("Episode edit submission interrupted project=%s episode=%s", project, episode)
                return  # The journal points to the original batch, queried by advance.
            if isinstance(submission, RenderSubmission):
                state.update(status="active", batch_id=submission.batch.batch_id)
            else:
                state.update(status="failed", error="剪辑任务提交失败，请继续剪辑", edit_recovery="resume")
            await self.owner._write(project, episode, state)

    async def advance(self, project: str, episode: int, state: dict) -> None:
        if state["status"] not in BUSY:
            return
        try:
            batch = await self.queue.get_generation_batch(
                project_name=project,
                batch_id=state.get("batch_id") or state["operation_id"],
                user_id=state["user_id"],
            )
        except GenerationBatchNotFound:
            state.update(status="failed", error="剪辑任务未启动，请继续剪辑", edit_recovery="resume")
        else:
            if not batch.done:
                if state["status"] != "active":
                    state.update(status="active", batch_id=batch.batch_id)
                    await self.owner._write(project, episode, state)
                return
            root = await asyncio.to_thread(self.projects.get_project_path, project)
            try:
                await asyncio.to_thread(delivery, root, episode, state["edit_id"])
            except runtime.EditError:
                work = directory(root, episode, state["edit_id"])
                recovery = await asyncio.to_thread(runtime.recovery, work)
                state.update(
                    status="failed",
                    edit_recovery=recovery,
                    error={
                        "unconfirmed": "上次剪辑请求结果未知，重新剪辑会发起新的模型请求。",
                        "invalid": "剪辑返回的结果无法使用，请重新剪辑。",
                        "resume": "剪辑未完成，请继续剪辑。",
                    }[recovery],
                )
            else:
                state.update(status="completed", error=None)
        await self.owner._write(project, episode, state)

    def adopted(self, project: str, episode: int) -> dict | None:
        try:
            return load_json(
                self.projects.get_project_path(project) / "production" / "edits" / str(episode) / "adopted.json"
            )
        except FileNotFoundError:
            return None

    def _current(self, project: str, episode: int, record: dict) -> bool:
        try:
            return (
                runtime.input_basis(runtime.snapshot(self.projects, project, episode, record["music"]))
                == record["basis"]
            )
        except (runtime.EditError, FileNotFoundError):
            return False

    def status(self, project: str, episode: int, state: dict) -> dict | None:
        root = self.projects.get_project_path(project)
        adopted = self.adopted(project, episode)
        job = state.get("edit_id") if state.get("action") == "edit" and state["status"] == "completed" else None
        job = job or (adopted["id"] if adopted else None)
        if not job:
            return None
        record = delivery(root, episode, job)
        return {
            "id": job,
            "sha256": record["sha256"],
            "artifact_path": published_relative(episode, job),
            "current": self._current(project, episode, record),
            "adopted": bool(adopted and adopted["id"] == job),
            "review": record["review"],
            "music_failed": record.get("music_failed", False),
        }

    async def confirm(self, project: str, episode: int, job: str, digest: str) -> None:
        await asyncio.to_thread(assert_production_open, self.projects, project, {episode})
        path = await asyncio.to_thread(self.owner._path, project, episode)
        async with self.projects.async_file_lock(path):
            root = await asyncio.to_thread(self.projects.get_project_path, project)
            record = await asyncio.to_thread(delivery, root, episode, job)
            adopted = await asyncio.to_thread(self.adopted, project, episode)
            if adopted == {"id": job, "sha256": digest}:
                return
            state = await asyncio.to_thread(self.owner._read, project, episode)
            if state.get("edit_id") != job or state["status"] != "completed" or record["sha256"] != digest:
                raise runtime.EditError("请刷新后确认当前成片")
            if not await asyncio.to_thread(self._current, project, episode, record):
                raise runtime.EditError("素材已更新，请重新剪辑")
            try:
                actual = await asyncio.to_thread(runtime.sha256_file, root / published_relative(episode, job))
            except FileNotFoundError as exc:
                raise runtime.EditError("成片文件不存在") from exc
            if actual != digest:
                raise runtime.EditError("成片文件校验失败")
            marker = directory(root, episode, job).parent / "adopted.json"
            await run_sync_transaction(atomic_write_json, marker, {"id": job, "sha256": digest})
