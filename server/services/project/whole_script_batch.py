"""Sequential screenplay breakdown using existing durable generation tasks only."""

from __future__ import annotations

import asyncio
import json
import uuid

from lib.artifacts.artifact_provenance import project_script_plan_prompt_inputs
from lib.episode.episode_paths import episode_source_path
from lib.episode.script_batch_state import ACTIVE_STATUSES, batch_path, read_batch
from lib.episode.whole_script import digest, verify_import_sources
from lib.generation.generation_result import problem_from_task_failure
from lib.infra.async_thread import run_sync_transaction
from lib.infra.json_io import atomic_write_json
from lib.script import script_review
from server.tool_runtime import (
    CallerContext,
    GenerateScriptPlanRequest,
    ProjectScope,
    Services,
    ToolRequest,
    generate_script_plan,
)


class WholeScriptBatchService:
    def __init__(self, projects, services: Services):
        self.projects = projects
        self.services = services

    def _dir(self, name):
        return self.projects.get_project_path(name)

    def _read(self, name):
        return read_batch(self._dir(name))

    async def _write(self, name, state):
        await run_sync_transaction(lambda: atomic_write_json(batch_path(self._dir(name)), state))

    def _inputs(self, name, ids):
        from lib.project.archive_context import generation_project

        project = generation_project(self._dir(name), self.projects.load_project(name))
        directory = self._dir(name)
        existing = {e["episode"]: e for e in project.get("episodes", [])}
        if project.get("production_management", {}).get("archived") or set(ids).intersection(
            project.get("production_management", {}).get("archived_episodes", [])
        ):
            raise ValueError("请先恢复已归档的项目或单集")
        if not set(ids).issubset(existing):
            raise ValueError("待拆解的集已被删除")
        for imported in project.get("production_script_imports", {}).values():
            if set(ids).intersection(imported["episodes"]):
                verify_import_sources(directory, project, imported)
        return digest(
            {
                "order": [e["episode"] for e in project.get("episodes", [])],
                "source": self._sources(name, ids),
                "prompt_inputs": {str(ep): project_script_plan_prompt_inputs(ep, project=project) for ep in ids},
                "whole_sources": {
                    rel: (directory / rel).read_text(encoding="utf-8")
                    for rel in {
                        e["source_range"]["source_file"]
                        for e in existing.values()
                        if e["episode"] in ids and e.get("source_range")
                    }
                },
                "settings": {
                    k: project.get(k)
                    for k in (
                        "overview",
                        "style",
                        "style_description",
                        "generation_mode",
                        "content_mode",
                        "text_backend_script",
                        "video_backend",
                        "default_duration",
                        "model_settings",
                        "aspect_ratio",
                        "source_language",
                        "characters",
                        "scenes",
                        "props",
                    )
                },
            }
        )

    def _sources(self, name, ids):
        return {str(e): digest(episode_source_path(self._dir(name), e).read_text(encoding="utf-8")) for e in ids}

    def _context(self, name, items):
        project = self.projects.load_project(name)
        context, revisions = [], {}
        for item in items:
            if item["status"] != "completed":
                continue
            episode = item["episode"]
            path = script_review.script_plan_path(self._dir(name), project, episode)
            if path is None or not path.is_file():
                raise ValueError(f"第 {episode} 集的拆解结果缺失，请核对")
            data = json.loads(path.read_text(encoding="utf-8"))
            if script_review.script_plan_quarantined(self._dir(name), project, episode):
                raise ValueError(f"第 {episode} 集有待修复草稿，请先进入该集处理")
            revisions[str(episode)] = digest(data)
            # Hints only. Do not put unconfirmed entries in the registered asset catalog.
            context.extend(
                {"episode": episode, **asset} for asset in data.get("new_assets", []) if isinstance(asset, dict)
            )
        unique = {(a.get("type"), a.get("name")): a for a in context}
        return json.dumps(list(unique.values()), ensure_ascii=False), revisions

    def _record_failure(self, item, problem):
        if problem.code == "draft_quarantined":
            item["requires_draft_repair"] = True
            return "生成结果已保存为待修复草稿，请进入该集修复后继续"
        if problem.params.get("may_have_been_accepted") or problem.code == "text_generation_timeout":
            item["requires_provider_check"] = True
            return "供应商可能已受理本次请求，请先核对供应商记录，再决定是否重新提交"
        return problem.detail

    def _accept_repaired_drafts(self, name, state):
        project = self.projects.load_project(name)
        for item in state["items"]:
            if item.get("requires_draft_repair"):
                path = script_review.script_plan_path(self._dir(name), project, item["episode"])
                if script_review.script_plan_quarantined(self._dir(name), project, item["episode"]) or not (
                    path and path.is_file()
                ):
                    raise ValueError(f"第 {item['episode']} 集的草稿仍待修复，请先处理并采用草稿")
                item["status"] = "completed"
                item.pop("requires_draft_repair")

    async def status(self, name):
        state = await asyncio.to_thread(self._read, name)
        if state["status"] != "idle":
            try:
                current = await asyncio.to_thread(self._inputs, name, [i["episode"] for i in state["items"]])
                state["input_changed"] = current != state["basis"]
                state["input_revision"] = current
            except (ValueError, OSError) as exc:
                state["input_changed"] = True
                state["input_error"] = str(exc)
        project = await asyncio.to_thread(self.projects.load_project, name)
        labels = {e["episode"]: (index + 1, e.get("title", "")) for index, e in enumerate(project.get("episodes", []))}
        for item in state["items"]:
            item["position"], item["title"] = labels.get(item["episode"], (None, ""))
        state["imports"] = sorted(
            project.get("production_script_imports", {}).values(), key=lambda i: max(i["episodes"], default=0)
        )
        return state

    async def start(self, name, import_id, user_id):
        directory = await asyncio.to_thread(self._dir, name)
        async with self.projects.async_file_lock(batch_path(directory)):
            state = await asyncio.to_thread(self._read, name)
            if state["status"] in ACTIVE_STATUSES:
                if state.get("import_id") == import_id and state.get("user_id") == user_id:
                    return state
                raise ValueError("本项目已有批量拆解，请先完成或暂停并等待当前集结束")
            project = await asyncio.to_thread(self.projects.load_project, name)
            imported = project.get("production_script_imports", {}).get(import_id)
            if not imported:
                raise ValueError("请先确认整部剧本的分集")
            if state.get("import_id") == import_id:
                return state  # retrying Start must never replay a completed/failed batch
            ids = imported["episodes"]

            def preflight():
                for episode in ids:
                    path = script_review.script_plan_path(directory, project, episode)
                    if (path and path.exists()) or script_review.script_plan_quarantined(directory, project, episode):
                        raise ValueError("本次导入已有拆解结果，请进入对应集审查；不会批量覆盖已有编辑")
                return self._inputs(name, ids)

            basis = await asyncio.to_thread(preflight)
            # Do not join someone else's in-flight single-episode request.
            for status in ("queued", "running"):
                tasks = await self.services.queue.list_tasks(project_name=name, status=status, page=1, page_size=1)
                if tasks.get("items"):
                    raise ValueError("项目仍有生成任务，请等待完成后开始批量拆解")

            def check_continuations():
                for journal in (directory / "production").glob("episode_*.json"):
                    if json.loads(journal.read_text(encoding="utf-8")).get("status") in {
                        "active",
                        "submitting",
                        "unknown",
                    }:
                        raise ValueError("项目仍有单集生产流程，请等待完成后开始批量拆解")

            await asyncio.to_thread(check_continuations)
            previous = state
            state = {
                "id": uuid.uuid4().hex,
                "import_id": import_id,
                "user_id": user_id,
                "status": "active",
                "basis": basis,
                "source_revisions": await asyncio.to_thread(self._sources, name, ids),
                "items": [{"episode": ep, "status": "pending"} for ep in ids],
            }

            def activate():
                # Structural writers use the same project lock and recheck the
                # journal under it, closing the preflight/activation race.
                with self.projects.locked_source_mutation(name):
                    if self._inputs(name, ids) != basis:
                        raise ValueError("项目已变化，请刷新后重试")
                    if previous.get("id"):
                        history = directory / "production" / "script_batch_history"
                        history.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(history / f"{previous['id']}.json", previous)
                    atomic_write_json(batch_path(directory), state)

            await run_sync_transaction(activate)
            return state

    async def control(self, name, action, user_id, input_revision=None, confirm_not_submitted=False):
        directory = await asyncio.to_thread(self._dir, name)
        async with self.projects.async_file_lock(batch_path(directory)):
            state = await asyncio.to_thread(self._read, name)
            if state.get("user_id") != user_id:
                raise ValueError("找不到当前用户的批量拆解")
            if action == "stop":
                if state["status"] in {"active", "submitting", "unknown"}:
                    state["stop_requested"] = True
                    state["status"] = (
                        "unknown"
                        if any(i["status"] == "unknown" for i in state["items"])
                        else "stopping"
                        if any(i["status"] in {"active", "submitting"} for i in state["items"])
                        else "stopped"
                    )
            elif action == "resolve_unknown":
                item = next((entry for entry in state["items"] if entry["status"] == "unknown"), None)
                if item is None:
                    # A failed lookup of a known handle is retried against that same
                    # handle. Never clear it based on a provider confirmation.
                    if state["status"] == "unknown" and any(i["status"] == "active" for i in state["items"]):
                        state["status"] = "stopping" if state.get("stop_requested") else "active"
                        await self._write(name, state)
                        return state
                    raise ValueError("没有待核对的拆解提交")
                if item.get("requires_provider_check"):
                    # The local task is already terminal. Rebinding it cannot
                    # resolve whether the upstream request was accepted.
                    if not confirm_not_submitted:
                        raise ValueError("供应商可能已受理请求，请先核对供应商记录并显式确认，才能重新提交")
                else:
                    match_count = await self._recover_submission(name, state, item)
                    if item["status"] == "active":
                        await self._write(name, state)
                        return state
                    if match_count:
                        await self._write(name, state)
                        raise ValueError("找到匹配任务但无法唯一绑定，请人工核对任务记录；不会重新提交")
                    if not confirm_not_submitted:
                        await self._write(name, state)
                        raise ValueError("队列中未找到该任务。请先在供应商控制台确认没有受理，再确认重新提交")
                item.update(status="pending")
                for key in ("batch_id", "task_id", "attempt", "members", "requires_provider_check"):
                    item.pop(key, None)
                state.update(status="stopped", stop_requested=False, error=None)
            elif action == "continue":
                if any(i["status"] in {"unknown", "submitting"} for i in state["items"]):
                    if any(i.get("requires_provider_check") for i in state["items"]):
                        raise ValueError("请先核对供应商记录，并通过解除等待确认；系统不会重复调用模型")
                    raise ValueError("提交结果未知，请先核对原任务；系统不会重复调用模型")
                if state["status"] not in {"stopped", "failed"}:
                    return state
                if any(i.get("requires_provider_check") for i in state["items"]) and not confirm_not_submitted:
                    raise ValueError("供应商可能已受理请求，请先核对供应商记录并显式确认，才能重新提交")
                basis = await asyncio.to_thread(self._inputs, name, [i["episode"] for i in state["items"]])
                if basis != state["basis"]:
                    if input_revision != basis:
                        raise ValueError("输入已变化，请核对各集原文后确认当前输入并继续")
                    sources = await asyncio.to_thread(self._sources, name, [i["episode"] for i in state["items"]])
                    if any(
                        sources[str(i["episode"])] != state["source_revisions"][str(i["episode"])]
                        for i in state["items"]
                        if i["status"] == "completed"
                    ):
                        raise ValueError("已完成集原文发生变化，请先单独处理该集及受影响的后集")
                    state["basis"] = basis
                    state["source_revisions"] = sources
                # A preceding draft may have been manually corrected while stopped.
                # Record the acknowledged context; later changes pause again.
                await asyncio.to_thread(self._accept_repaired_drafts, name, state)
                _, state["context_revisions"] = await asyncio.to_thread(self._context, name, state["items"])
                for item in state["items"]:
                    if item["status"] == "failed":
                        item.update(status="pending")
                        for key in ("batch_id", "task_id", "attempt", "members"):
                            item.pop(key, None)
                        item.pop("requires_provider_check", None)
                state.update(status="active", stop_requested=False, error=None)
            else:
                raise ValueError("未知操作")
            await self._write(name, state)
            return state

    async def _recover_submission(self, name, state, item):
        # Search accepted tasks by a persisted batch token, never infer from source text.
        matches = []
        page = 1
        while True:
            result = await self.services.queue.list_tasks(project_name=name, page=page, page_size=100)
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise RuntimeError("任务查询响应无效，保留原提交等待核对")
            rows = result["items"]
            matches.extend(
                t
                for t in rows
                if (t.get("payload") or {}).get("production_batch_id") == state["id"]
                and (t.get("payload") or {}).get("production_attempt") == item.get("attempt")
                and (t.get("payload") or {}).get("episode") == item["episode"]
                and t.get("user_id") == state["user_id"]
            )
            if len(rows) < 100:
                break
            page += 1
        if len(matches) == 1:
            task = matches[0]
            item["task_id"] = task["task_id"]
            item["status"] = "active"
            state["status"] = "stopping" if state.get("stop_requested") else "active"
            state["error"] = None
        elif not matches:
            item["status"] = state["status"] = "unknown"
            state["error"] = "提交结果未知，请核对任务记录；不会自动重新提交"
        else:
            item["status"] = state["status"] = "unknown"
            state["error"] = "找到多个匹配任务，请人工核对；不会自动重新提交"
        await self._write(name, state)
        return len(matches)

    async def advance(self, name):
        directory = await asyncio.to_thread(self._dir, name)
        async with self.projects.async_file_lock(batch_path(directory)):
            state = await asyncio.to_thread(self._read, name)
            if state["status"] not in ACTIVE_STATUSES:
                return
            item = next((i for i in state["items"] if i["status"] != "completed"), None)
            if item is None:
                state.update(status="completed", error=None)
                await self._write(name, state)
                return
            try:
                if item["status"] in {"submitting", "unknown"}:
                    if item.get("requires_provider_check"):
                        return
                    await self._recover_submission(name, state, item)
                    if item["status"] == "unknown":
                        return
                if item["status"] == "active":
                    if item.get("task_id"):
                        try:
                            task = await self.services.queue.get_task(item["task_id"])
                        except Exception as exc:
                            raise RuntimeError("原任务查询失败，保留原任务等待核对") from exc
                        if not task:
                            raise RuntimeError("原任务暂不可查询，保留原任务等待核对")
                        if task["status"] in {"queued", "running"}:
                            return
                        if task["status"] != "succeeded":
                            problem = problem_from_task_failure(task.get("error_message"))
                            raise ValueError(self._record_failure(item, problem))
                    else:
                        try:
                            batch = await self.services.queue.get_generation_batch(
                                project_name=name, batch_id=item["batch_id"], user_id=state["user_id"]
                            )
                        except Exception as exc:
                            raise RuntimeError("原批次查询失败，保留原批次等待核对") from exc
                        if not batch.done:
                            return
                        if batch.counts.failed or batch.counts.cancelled or batch.counts.blocked:
                            errors = [m.model_dump(mode="json") for m in batch.members]
                            item["members"] = errors
                            for member in batch.members:
                                if member.problem is not None:
                                    detail = self._record_failure(item, member.problem)
                                    if item.get("requires_draft_repair") or item.get("requires_provider_check"):
                                        raise ValueError(detail)
                            raise ValueError("本集拆解失败，请进入该集查看错误并修正后继续")
                        if batch.counts.total != 1 or batch.counts.succeeded != 1:
                            raise RuntimeError("原批次没有唯一成功任务，请核对；不会自动重新提交")
                    prior_items = [i for i in state["items"] if i is not item]
                    # Success is final even if its output or preceding context needs
                    # repair. Continue may inspect it, but must never pay to replay it.
                    item["status"] = "completed"
                    state.update(status="stopping" if state.get("stop_requested") else "active", error=None)
                    _, prior_revisions = await asyncio.to_thread(self._context, name, prior_items)
                    if state.get("context_revisions", {}) != prior_revisions:
                        # The accepted task is complete, never regenerate it on Continue.
                        item["status"] = "completed"
                        state.update(status="failed", error="前集拆解已修改，请核对后集结果后继续")
                        await self._write(name, state)
                        return
                    item["status"] = "completed"
                    _, revisions = await asyncio.to_thread(self._context, name, state["items"])
                    state["context_revisions"] = revisions
                    await self._write(name, state)
                    return
                if state.get("stop_requested"):
                    state["status"] = "stopped"
                    await self._write(name, state)
                    return
                basis = await asyncio.to_thread(self._inputs, name, [i["episode"] for i in state["items"]])
                if basis != state["basis"]:
                    raise ValueError("剧本或项目设定已变化，请核对后继续")
                context, revisions = await asyncio.to_thread(self._context, name, state["items"])
                if state.get("context_revisions", {}) != revisions:
                    raise ValueError("前集拆解已修改，可能影响后集资产，请核对后继续")
                # Persist before submission. Process loss after this write is reconciled,
                # never retried as a fresh provider request.
                item["status"] = "submitting"
                item["attempt"] = uuid.uuid4().hex
                await self._write(name, state)
                result = await generate_script_plan(
                    ToolRequest(GenerateScriptPlanRequest(episode_id=item["episode"])),
                    ProjectScope(project_name=name, data_root=self.projects.data_root),
                    CallerContext(user_id=state["user_id"], source="webui"),
                    self.services,
                    batch_context=(state["id"], item["attempt"], context),
                )
                if result.problem:
                    if result.problem.code == "internal_error":
                        raise RuntimeError(result.problem.detail)
                    raise ValueError(result.problem.detail)
                item.update(status="active", batch_id=result.value.batch_id)
                await self._write(name, state)
            except ValueError as exc:
                failure_status = "unknown" if item.get("requires_provider_check") else "failed"
                if item["status"] != "completed":
                    item.update(status=failure_status)
                state.update(status=failure_status, error=str(exc))
                await self._write(name, state)
            except Exception as exc:
                # A queue/database failure may have happened after acceptance.
                # Keep the original handle when known, otherwise search by token.
                state.update(status="unknown", error=str(exc))
                if item["status"] == "submitting":
                    item["status"] = "unknown"
                await self._write(name, state)
