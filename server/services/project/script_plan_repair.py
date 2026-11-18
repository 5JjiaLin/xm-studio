"""Planning repair snapshots and evidence, using the original queue and draft locks."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import TextTaskType
from lib.backends.text_generator import TextGenerator
from lib.episode.episode_paths import episode_drafts_dir, episode_script_filename
from lib.infra.async_thread import run_sync_transaction
from lib.infra.content_digest import prefixed_canonical_json_digest, prefixed_sha256_file
from lib.infra.json_io import atomic_write_json, load_json_or_none
from lib.infra.path_safety import try_safe_join
from lib.project.project_manager import ProjectManager, find_episode
from lib.project.task_project_claim import current_task_project_claim, ensure_task_project_claim
from lib.script import script_review
from lib.script.draft_quarantine import (
    QUARANTINE_KIND_SCRIPT_PLAN,
    draft_owner,
    draft_revision,
    quarantine_path,
    read_quarantine,
    violation_entries,
)
from lib.script.reference_video.plan_repair import (
    REPAIR_VERSION,
    PlanRepairError,
    build_repair_prompt,
    build_repair_schema,
    merge_repair,
    parse_repair_response,
    repair_targets,
)

if TYPE_CHECKING:
    from lib.config.resolver import ConfigResolver
    from server.text_generation import TextGenerationRequest


def repair_enabled() -> bool:
    return os.environ.get("XM_SCRIPT_PLAN_REPAIR_ENABLED", "0") == "1"


def assert_repairable_stage(project_path: Path, project: dict[str, Any], episode: int) -> None:
    entry = find_episode(project, episode) or {}
    script_file = entry.get("script_file") or episode_script_filename(episode)
    paths = [try_safe_join(project_path, script_file), try_safe_join(project_path, f"scripts/{script_file}")]
    # A plan may have been replanned after confirmation. Existing downstream content
    # remains protected even if its review fingerprint no longer matches the plan.
    if (
        script_review.formal_script_plan_confirmed(project_path, project, episode)
        or (entry.get("script_plan_review") or {}).get("confirmed_at")
        or any(path is not None and path.exists() for path in paths)
    ):
        raise PlanRepairError("repair_downstream_exists: 本集已有确认或下游内容，请使用原重新规划流程")


def _file_digest(path: Path) -> str | None:
    return prefixed_sha256_file(path) if path.is_file() else None


def capability_snapshot(caps: Any) -> dict[str, Any]:
    values = caps._asdict()
    values["voice"] = asdict(caps.voice)
    values["text_problem"] = asdict(caps.text_problem) if caps.text_problem is not None else None
    return values


def _input_basis(project_path: Path, request: TextGenerationRequest, project: dict[str, Any], caps: Any) -> str:
    from server.text_generation import load_script_plan_source_with_basis

    _source, _inputs, basis = load_script_plan_source_with_basis(
        project_path, request.source, project, request.episode, "reference_video"
    )
    template = Path(__file__).parents[3] / "lib/prompts/prompt_templates/templates/text/reference_video_script_plan.md"
    # Manifest changes include activated references and invalidate old bucket facts.
    return prefixed_canonical_json_digest(
        {
            "version": REPAIR_VERSION,
            "basis": basis.digest,
            "caps": capability_snapshot(caps),
            "template": _file_digest(template),
            "schema": _file_digest(Path(__file__).parents[3] / "lib/script/script_models.py"),
            "validator": _file_digest(Path(__file__).parents[3] / "lib/script/reference_video/draft_validation.py"),
            "manifest": _file_digest(project_path / ".arcreel_artifacts.json"),
            "prompt_policy_version": request.prompt_policy_version,
            "instructions": request.instructions,
            "replan_reason": request.replan_reason,
            "replan_context": request.replan_context,
            "batch_asset_context": request.batch_asset_context,
        }
    )


def generator_identity(generator: TextGenerator) -> dict[str, Any]:
    return {
        "provider_id": generator.provider_id,
        "model": generator.model,
        "configuration": generator.configuration_fingerprint,
        "max_output_tokens": generator.max_output_tokens,
    }


async def prepare_repair_context(
    request: TextGenerationRequest,
    *,
    project_name: str,
    projects: ProjectManager,
    config_resolver: ConfigResolver,
) -> tuple[str | None, str]:
    """Read/revalidate at admission; freeze the same inputs into the queued payload."""
    from dataclasses import replace

    from server.draft_workflow import revalidate_reference_script_plan_draft
    from server.text_generation import fetch_reference_split_caps, load_novel_source

    if not repair_enabled():
        raise PlanRepairError("repair_disabled: 局部修复当前已关闭")
    project_path = await asyncio.to_thread(projects.get_project_path, project_name)
    project = await asyncio.to_thread(projects.load_project, project_name)
    await asyncio.to_thread(assert_repairable_stage, project_path, project, request.episode)
    source = request.source
    draft = None
    targets = None
    violations = None
    if request.repair_draft_revision:
        draft = await asyncio.to_thread(read_quarantine, project_path, request.episode, QUARANTINE_KIND_SCRIPT_PLAN)
        if draft is None or draft_owner(draft) != "user" or draft_revision(draft) != request.repair_draft_revision:
            raise PlanRepairError("repair_revision_conflict: 草稿已变化，请刷新后核对")
        if "source" not in draft.meta:
            raise PlanRepairError("repair_source_missing: 草稿来源依据缺失，请重新规划")
        formal_path = script_review.official_reference_script_plan_path(project_path, request.episode)
        formal_baseline = await asyncio.to_thread(script_review.content_fingerprint, formal_path)
        if "base_fingerprint" in draft.meta and draft.meta["base_fingerprint"] != formal_baseline:
            raise PlanRepairError("repair_revision_conflict: 正式拆解已更新，请核对后再修复")
        source = draft.meta["source"]
        if request.prompt_policy_version != draft.meta.get("prompt_policy_version"):
            raise PlanRepairError("repair_policy_changed: 修复必须保留原草稿的提示词策略")
        if request.source is not None and request.source != source:
            raise PlanRepairError("repair_source_changed: 请求来源与草稿不符")
        validation = await revalidate_reference_script_plan_draft(
            project_path,
            project,
            request.episode,
            draft,
            config_resolver=config_resolver,
        )
        if validation.schema_failed:
            raise PlanRepairError("repair_schema_invalid: 草稿结构不完整，请人工核对或重新规划")
        novel = await asyncio.to_thread(load_novel_source, project_path, source, episode=request.episode)
        targets = await asyncio.to_thread(repair_targets, draft.content, validation.violations, novel)
        violations = violation_entries(validation.violations)
        caps = validation.caps
    else:
        caps = await fetch_reference_split_caps(project, config_resolver=config_resolver)
    request = replace(request, source=source)
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    snapshot = {
        "version": REPAIR_VERSION,
        "project_name": project_name,
        "episode": request.episode,
        "source": source,
        "basis": await asyncio.to_thread(_input_basis, project_path, request, project, caps),
        "generator": generator_identity(generator),
        "draft_file_digest": await asyncio.to_thread(
            _file_digest, quarantine_path(project_path, request.episode, QUARANTINE_KIND_SCRIPT_PLAN)
        ),
        "formal_fingerprint": formal_baseline if draft is not None else await asyncio.to_thread(
            script_review.content_fingerprint,
            script_review.official_reference_script_plan_path(project_path, request.episode),
        ),
        "draft_revision": draft_revision(draft) if draft else None,
        "content": draft.content if draft else None,
        "targets": targets,
        "violations": violations,
        "parent_task_id": draft.meta.get("origin_task_id") if draft else None,
        "request": {key: value for key, value in request.to_payload().items() if key != "repair_context"},
    }
    return source, json.dumps(snapshot, ensure_ascii=False)


def _evidence_path(project_path: Path, episode: int, task_id: str) -> Path:
    return episode_drafts_dir(project_path, episode) / "script_plan_repair" / (
        prefixed_canonical_json_digest(task_id).split(":")[-1] + ".json"
    )


def read_recovery_evidence(project_path: Path, episode: int, task_id: str) -> dict[str, Any]:
    value = load_json_or_none(_evidence_path(project_path, episode, task_id))
    if (
        not isinstance(value, dict) or value.get("task_id") != task_id
        or not isinstance(value.get("snapshot"), dict)
        or value["snapshot"].get("episode") != episode
        or value["snapshot"].get("version") != REPAIR_VERSION
        or not isinstance(value["snapshot"].get("request"), dict)
    ):
        raise PlanRepairError("repair_evidence_missing: 原任务的恢复依据不完整，请核对原任务")
    return value


async def prepare_recheck_request(
    request: TextGenerationRequest, *, project_name: str, projects: ProjectManager,
) -> TextGenerationRequest:
    """Freeze a local recovery reference; this path can never authorize a new model call."""
    from dataclasses import replace

    from server.text_generation import TextGenerationRequest

    if request.repair_draft_revision or request.repair_once or request.source or request.instructions or request.replan_reason:
        raise PlanRepairError("repair_recheck_invalid: 重新检查不能附带生成或改写要求")
    project_path = await asyncio.to_thread(projects.get_project_path, project_name)
    prior = await asyncio.to_thread(read_recovery_evidence, project_path, request.episode, request.repair_recheck_task_id)
    snapshot = prior["snapshot"]
    if snapshot["project_name"] != project_name:
        raise PlanRepairError("repair_evidence_mismatch: 恢复依据与当前项目不符")
    if prior.get("recovery_rejected"):
        raise PlanRepairError("repair_response_rejected: 已保存响应已完成校验且不合法，请核对草稿或显式重新规划")
    if not prior.get("responses") and not prior.get("expected_committed_fingerprint"):
        raise PlanRepairError("repair_response_unknown: 未保存完整响应，仅能核对原任务，不会重新请求模型")
    restored = TextGenerationRequest(**snapshot["request"])
    if restored.episode != request.episode:
        raise PlanRepairError("repair_evidence_mismatch: 恢复依据与当前集不符")
    snapshot = {**snapshot, "recheck_evidence_digest": await asyncio.to_thread(
        _file_digest, _evidence_path(project_path, request.episode, request.repair_recheck_task_id)
    )}
    return replace(
        restored, repair_recheck_task_id=request.repair_recheck_task_id,
        repair_context=json.dumps(snapshot, ensure_ascii=False), dry_run=request.dry_run,
    )


async def repair_inputs_for_preflight(
    project_path: Path, episode: int, revision: str, source: str | None
) -> tuple[str | None, str | None]:
    draft = await asyncio.to_thread(read_quarantine, project_path, episode, QUARANTINE_KIND_SCRIPT_PLAN)
    if draft is None or draft_owner(draft) != "user" or draft_revision(draft) != revision:
        raise PlanRepairError("repair_revision_conflict: 草稿已变化，请刷新后核对")
    draft_source = draft.meta.get("source")
    if source is not None and source != draft_source:
        raise PlanRepairError("repair_source_changed: 请求来源与草稿不符")
    return draft_source, draft.meta.get("prompt_policy_version")


async def check_repair_basis(
    request: TextGenerationRequest,
    snapshot: dict[str, Any],
    generator: TextGenerator,
    *,
    projects: ProjectManager,
    project_name: str,
    config_resolver: ConfigResolver,
) -> None:
    from server.text_generation import fetch_reference_split_caps

    project = await asyncio.to_thread(projects.load_project, project_name)
    project_path = await asyncio.to_thread(projects.get_project_path, project_name)
    await asyncio.to_thread(assert_repairable_stage, project_path, project, request.episode)
    caps = await fetch_reference_split_caps(project, config_resolver=config_resolver)
    digest = await asyncio.to_thread(_input_basis, project_path, request, project, caps)
    current_generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    if (
        snapshot["project_name"] != project_name
        or snapshot["episode"] != request.episode
        or snapshot["basis"] != digest
        or snapshot["generator"] != generator_identity(generator)
        or snapshot["generator"] != generator_identity(current_generator)
    ):
        raise PlanRepairError("repair_basis_changed: 剧本、资产、模型或配置已变化，请核对后重新提交")
    await asyncio.to_thread(assert_snapshot_files, project_path, request.episode, snapshot)


def assert_snapshot_files(project_path: Path, episode: int, snapshot: dict[str, Any]) -> None:
    ensure_task_project_claim(snapshot["project_name"])
    if (
        _file_digest(quarantine_path(project_path, episode, QUARANTINE_KIND_SCRIPT_PLAN))
        != snapshot["draft_file_digest"]
        or script_review.content_fingerprint(script_review.official_reference_script_plan_path(project_path, episode))
        != snapshot["formal_fingerprint"]
    ):
        raise PlanRepairError("repair_revision_conflict: 草稿或正式内容已变化，候选结果不覆盖当前内容")


def repair_model_context(
    snapshot: dict[str, Any],
    *,
    content: dict[str, Any],
    violations: list[Any],
    novel: str,
    caps: Any,
    prompt_inputs: dict[str, Any],
    instructions: str | None,
) -> dict[str, Any]:
    return {
        "source": novel,
        "content": content,
        "targets": repair_targets(content, violations, novel),
        "violations": violation_entries(violations),
        "capabilities": capability_snapshot(caps),
        "project_context": prompt_inputs,
        "instructions": instructions,
        "parent_task_id": snapshot.get("parent_task_id"),
    }


class RepairEvidence:
    """Per-attempt files are evidence, never a scheduler or a reason to replay POST."""

    def __init__(self, project_path: Path, episode: int, snapshot: dict[str, Any]):
        claim = current_task_project_claim()
        task_id = claim.task_id if claim else str(uuid.uuid4())
        self.directory = episode_drafts_dir(project_path, episode) / "script_plan_repair"
        self.path = _evidence_path(project_path, episode, task_id)
        self.latest = self.directory / "latest.json"
        self.data: dict[str, Any] = {
            "task_id": task_id,
            "snapshot": snapshot,
            "phase": "checking",
            "calls_started": 0,
            "responses": [],
        }

    async def begin(self) -> str | None:
        return await run_sync_transaction(self._begin)

    def completed_result(self) -> str | None:
        prior = load_json_or_none(self.path)
        if not isinstance(prior, dict) or prior.get("task_id") != self.data["task_id"]:
            return None
        expected = prior.get("committed_fingerprint") or prior.get("expected_committed_fingerprint")
        episode = self.data["snapshot"]["episode"]
        if expected and prior.get("result_message"):
            current = script_review.content_fingerprint(script_review.official_reference_script_plan_path(self.directory.parents[2], episode))
            if current == expected:
                ensure_task_project_claim(self.data["snapshot"]["project_name"])
                return prior["result_message"]
        return None

    def _begin(self) -> str | None:
        locks = ProjectManager.for_project_dir(self.directory.parents[2])
        with locks.file_lock(self.path):
            prior = load_json_or_none(self.path)
            if self.path.exists() and not isinstance(prior, dict):
                raise PlanRepairError("repair_evidence_corrupted: 原任务证据不可读，不会再次请求模型")
            if prior is not None:
                expected = prior.get("committed_fingerprint") or prior.get("expected_committed_fingerprint")
                episode = prior.get("snapshot", {}).get("episode")
                if expected and type(episode) is int:
                    current = script_review.content_fingerprint(script_review.official_reference_script_plan_path(self.directory.parents[2], episode))
                    if current == expected and prior.get("result_message"):
                        return prior["result_message"]
                raise PlanRepairError("repair_attempt_exists: 原任务已有执行证据，请核对原任务，不会再次调用模型")
            self._write()
        return None

    async def record(self, phase: str, **updates: Any) -> None:
        self.data.update(updates, phase=phase)
        await run_sync_transaction(self._write)

    def _write(self) -> None:
        ensure_task_project_claim(self.data["snapshot"]["project_name"])
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)
        atomic_write_json(self.latest, {key: self.data[key] for key in ("task_id", "phase", "calls_started")})


def read_repair_progress(project_path: Path, episode: int) -> dict[str, Any] | None:
    value = load_json_or_none(episode_drafts_dir(project_path, episode) / "script_plan_repair/latest.json")
    if not isinstance(value, dict) or not isinstance(value.get("task_id"), str):
        return None
    try:
        evidence = read_recovery_evidence(project_path, episode, value["task_id"])
    except ValueError:
        return {**value, "can_recheck": False, "recheck_reason": "原任务恢复证据不可读，请核对原任务；不会重新调用模型"}
    rejected = evidence.get("recovery_rejected", False)
    return {
        **value,
        "can_recheck": bool(evidence.get("responses")) and evidence.get("phase") != "completed" and not rejected,
        "recheck_reason": evidence.get("error") if rejected else None,
    }


async def repair_offer(
    project_path: Path, project: dict[str, Any], episode: int, draft: Any, validation: Any
) -> dict[str, Any]:
    """Purely read-only eligibility for the existing review response."""
    from server.text_generation import load_novel_source

    try:
        if not repair_enabled():
            raise PlanRepairError("局部修复当前已关闭")
        await asyncio.to_thread(assert_repairable_stage, project_path, project, episode)
        if draft_owner(draft) != "user" or validation.content is None or "source" not in draft.meta:
            raise PlanRepairError("草稿结构或来源不完整，请人工核对或重新规划")
        novel = await asyncio.to_thread(load_novel_source, project_path, draft.meta["source"], episode=episode)
        targets = await asyncio.to_thread(repair_targets, draft.content, validation.violations, novel)
        return {"available": True, "target_count": len(targets), "reason": None}
    except ValueError as exc:
        return {"available": False, "target_count": 0, "reason": str(exc).split(": ", 1)[-1]}


async def execute_reference_repair_plan(
    request: TextGenerationRequest,
    *,
    project_name: str,
    projects: ProjectManager,
    config_resolver: ConfigResolver,
    novel: str,
    prompt_inputs: dict[str, Any],
    basis: Any,
    caps: Any,
    initial_prompt: str,
    before_commit: Callable[[], None] | None = None,
) -> Any:
    """At most one initial response and one repair; success uses the original commit."""
    from lib.backends.text_backends.base import DEFAULT_MAX_OUTPUT_TOKENS
    from lib.backends.text_backends.base import TextGenerationRequest as BackendRequest
    from lib.infra.text_utils import strict_json_loads
    from lib.script.script_models import build_reference_units_script_plan_model
    from server.text_generation import (
        TextGenerationError,
        TextGenerationResult,
        _commit_generated_reference_script_plan,
        _quarantine_invalid_script_plan_generation,
        _reference_soft_violation_lines,
        build_reference_units_from_flat,
        collect_reference_flat_violations,
        parse_reference_script_plan_candidate,
        reference_result_text,
    )

    if not request.repair_context:
        raise PlanRepairError("repair_context_missing: 修复任务缺少提交依据，请重新提交")
    snapshot = strict_json_loads(request.repair_context)
    project_path = await asyncio.to_thread(projects.get_project_path, project_name)
    if not request.repair_recheck_task_id and not repair_enabled():
        raise PlanRepairError("repair_disabled: 局部修复当前已关闭")
    evidence = RepairEvidence(project_path, request.episode, snapshot)
    committed_result = await asyncio.to_thread(evidence.completed_result)
    if committed_result and not request.dry_run:
        return TextGenerationResult(committed_result)
    if request.repair_recheck_task_id:
        if await asyncio.to_thread(
            _file_digest, _evidence_path(project_path, request.episode, request.repair_recheck_task_id)
        ) != snapshot.get("recheck_evidence_digest"):
            raise PlanRepairError("repair_evidence_changed: 原任务恢复证据已变化，请刷新后核对")
        prior = await asyncio.to_thread(
            read_recovery_evidence, project_path, request.episode, request.repair_recheck_task_id
        )
        expected = prior.get("committed_fingerprint") or prior.get("expected_committed_fingerprint")
        formal = script_review.official_reference_script_plan_path(project_path, request.episode)
        if expected and prior.get("result_message") and await asyncio.to_thread(
            script_review.content_fingerprint, formal
        ) == expected:
            return TextGenerationResult(prior["result_message"])
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    await check_repair_basis(
        request, snapshot, generator, projects=projects, project_name=project_name, config_resolver=config_resolver
    )
    draft_path = quarantine_path(project_path, request.episode, QUARANTINE_KIND_SCRIPT_PLAN)
    locks = await asyncio.to_thread(ProjectManager.for_project_dir, project_path)
    initial_schema = build_reference_units_script_plan_model(caps.durations, use_source_spans=True)
    repair_schema = build_repair_schema(caps.durations)
    content = snapshot.get("content")
    calls = 0
    responses: list[dict[str, Any]] = []
    begun = False

    async def check() -> None:
        await check_repair_basis(
            request, snapshot, generator, projects=projects, project_name=project_name, config_resolver=config_resolver
        )

    async def generate(prompt: str, schema: Any, phase: str) -> str:
        nonlocal calls
        if request.repair_recheck_task_id:
            raise PlanRepairError("repair_local_only: 本地重检不允许请求模型")
        await check()
        limit = 1 if request.repair_draft_revision else 2
        if calls >= limit:
            raise PlanRepairError("repair_call_limit: 本次修复调用已达上限")
        calls += 1
        # No structured-output fallback may silently buy an extra content response.
        # Provider transport retry remains bounded by the existing rejection policy.
        wire_prompt = (
            prompt
            + "\n只输出符合此 JSON Schema 的完整 JSON：\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        await evidence.record(phase, calls_started=calls, request_prompt=wire_prompt)
        if not repair_enabled():
            raise PlanRepairError("repair_disabled: 局部修复当前已关闭，不会发出新模型请求")
        response = await generator.generate(
            BackendRequest(prompt=wire_prompt, max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS, prefer_stream=True),
            project_name=project_name,
            require_complete=True,
        )
        responses.append({"phase": phase, "text": response.text})
        await evidence.record("validating" if phase == "generating" else "revalidating", responses=responses)
        await check()
        return response.text

    async def validate(value: dict[str, Any]) -> list[Any]:
        project = await asyncio.to_thread(projects.load_project, project_name)
        return await asyncio.to_thread(
            collect_reference_flat_violations,
            value["units"],
            project,
            project_path=project_path,
            episode=request.episode,
            novel_text=novel,
            caps=caps,
            source_language=project.get("source_language"),
            new_assets=value.get("new_assets", []),
            prompt_policy_version=request.prompt_policy_version,
        )

    def assert_commit_inputs() -> None:
        assert_snapshot_files(project_path, request.episode, snapshot)
        if request.repair_recheck_task_id and _file_digest(
            _evidence_path(project_path, request.episode, request.repair_recheck_task_id)
        ) != snapshot["recheck_evidence_digest"]:
            raise PlanRepairError("repair_evidence_changed: 原任务恢复证据已变化，请刷新后核对")
        project = projects.load_project(project_name)
        assert_repairable_stage(project_path, project, request.episode)
        if _input_basis(project_path, request, project, caps) != snapshot["basis"]:
            raise PlanRepairError("repair_basis_changed: 提交前输入已变化，保留候选供核对")
        if before_commit is not None:
            before_commit()

    try:
        if request.dry_run:
            preview = initial_prompt
            if request.repair_draft_revision:
                issues = await validate(content)
                context = await asyncio.to_thread(
                    repair_model_context,
                    snapshot,
                    content=content,
                    violations=issues,
                    novel=novel,
                    caps=caps,
                    prompt_inputs=prompt_inputs,
                    instructions=request.instructions,
                )
                preview = await asyncio.to_thread(build_repair_prompt, context)
            return TextGenerationResult(f"DRY RUN — 以下是将发送给文本模型的 Prompt:\n\n{preview}")
        prior_result = await evidence.begin()
        if prior_result:
            return TextGenerationResult(prior_result)
        begun = True
        if request.repair_recheck_task_id:
            prior = await asyncio.to_thread(
                read_recovery_evidence, project_path, request.episode, request.repair_recheck_task_id
            )
            responses = prior.get("responses") or []
            await evidence.record(
                "revalidating", responses=responses, parent_task_id=request.repair_recheck_task_id,
            )
            if not responses:
                raise PlanRepairError("repair_response_unknown: 未保存完整响应，不会再次请求模型")
            last = responses[-1]
            if last.get("phase") == "repairing":
                parsed = await asyncio.to_thread(parse_repair_response, repair_schema, last["text"])
                targets = await asyncio.to_thread(repair_targets, content, await validate(content), novel)
                content = await asyncio.to_thread(merge_repair, content, targets, parsed)
            elif last.get("phase") == "generating":
                content = await asyncio.to_thread(
                    parse_reference_script_plan_candidate, last["text"], initial_schema, caps.durations, novel
                )
            else:
                raise PlanRepairError("repair_response_unknown: 恢复响应缺少可识别的生成阶段")
            await evidence.record("revalidating", candidate=content)
        elif not request.repair_draft_revision:
            text = await generate(initial_prompt, initial_schema, "generating")
            content = await asyncio.to_thread(
                parse_reference_script_plan_candidate, text, initial_schema, caps.durations, novel
            )
        violations = await validate(content)
        if request.repair_recheck_task_id and violations:
            if responses[-1].get("phase") == "generating" and snapshot.get("draft_file_digest") is None:
                async with locks.async_file_lock(draft_path):
                    await run_sync_transaction(assert_commit_inputs)
                    await run_sync_transaction(
                        _quarantine_invalid_script_plan_generation,
                        project_path,
                        request.episode,
                        QUARANTINE_KIND_SCRIPT_PLAN,
                        content,
                        violations,
                        request.source,
                        snapshot["formal_fingerprint"],
                        prompt_policy_version=request.prompt_policy_version,
                    )
                    draft = await asyncio.to_thread(
                        read_quarantine, project_path, request.episode, QUARANTINE_KIND_SCRIPT_PLAN
                    )
                    snapshot.update(
                        content=content,
                        draft_revision=draft_revision(draft),
                        draft_file_digest=await asyncio.to_thread(
                            _file_digest, draft_path
                        ),
                    )
                    await evidence.record("failed", snapshot=snapshot, violations=violation_entries(violations))
            raise PlanRepairError("repair_local_validation_failed: 完整响应仍有问题，请核对草稿并显式修复；" + "；".join(str(v) for v in violations))
        if violations:
            if not request.repair_draft_revision:
                # The original failed output remains the editable draft even when a
                # later candidate fails protection or full validation.
                async with locks.async_file_lock(draft_path):
                    await run_sync_transaction(assert_commit_inputs)
                    await run_sync_transaction(
                        _quarantine_invalid_script_plan_generation,
                        project_path,
                        request.episode,
                        QUARANTINE_KIND_SCRIPT_PLAN,
                        content,
                        violations,
                        request.source,
                        snapshot["formal_fingerprint"],
                        prompt_policy_version=request.prompt_policy_version,
                    )
                    snapshot["draft_file_digest"] = await asyncio.to_thread(_file_digest, draft_path)
                    draft = await asyncio.to_thread(
                        read_quarantine, project_path, request.episode, QUARANTINE_KIND_SCRIPT_PLAN
                    )
                    snapshot.update(content=content, draft_revision=draft_revision(draft))
            context = await asyncio.to_thread(
                repair_model_context,
                snapshot,
                content=content,
                violations=violations,
                novel=novel,
                caps=caps,
                prompt_inputs=prompt_inputs,
                instructions=request.instructions,
            )
            targets = context["targets"]
            await evidence.record(
                "checking", targets=targets, violations=violation_entries(violations), snapshot=snapshot
            )
            prompt = await asyncio.to_thread(build_repair_prompt, context)
            text = await generate(prompt, repair_schema, "repairing")
            parsed = await asyncio.to_thread(parse_repair_response, repair_schema, text)
            candidate = await asyncio.to_thread(merge_repair, content, targets, parsed)
            await evidence.record("revalidating", candidate=candidate)
            violations = await validate(candidate)
            if violations:
                await evidence.record("failed", violations=violation_entries(violations), recovery_rejected=True)
                raise TextGenerationError(
                    f"AI 修复后仍有 {len(violations)} 处问题，原草稿已保留：" + "；".join(str(v) for v in violations)
                )
            content = candidate
        elif request.repair_draft_revision:
            raise PlanRepairError("repair_no_longer_needed: 草稿已通过校验，请使用重新检查采用，不需要调用模型")

        await check()
        project = await asyncio.to_thread(projects.load_project, project_name)
        ids = await run_sync_transaction(
            projects.reserve_item_ids, project_name, request.episode, "video_units", len(content["units"])
        )
        units = await asyncio.to_thread(
            build_reference_units_from_flat,
            content["units"],
            project,
            episode=request.episode,
            max_refs=caps.max_refs,
            unit_ids=ids,
            new_assets=content.get("new_assets", []),
        )
        soft = _reference_soft_violation_lines(
            [u["text"] for u in content["units"]], project, episode=request.episode, voice=caps.voice,
            unit_ids=ids, new_assets=content.get("new_assets", []),
        )
        formal = script_review.official_reference_script_plan_path(project_path, request.episode)
        result_message = reference_result_text(formal.relative_to(project_path), units, soft, action="修复" if calls > 1 or request.repair_draft_revision or request.repair_recheck_task_id else "拆分")
        if request.prompt_policy_version:
            content = {**content, "prompt_policy_version": request.prompt_policy_version}
        await evidence.record("committing", candidate=content, expected_committed_fingerprint=script_review.content_fingerprint_of_data({**content, "units": units}), result_message=result_message)
        await check()
        async with locks.async_file_lock(draft_path):
            await run_sync_transaction(
                _commit_generated_reference_script_plan,
                project_path,
                request.episode,
                {**content, "units": units},
                snapshot["formal_fingerprint"],
                basis,
                assert_commit_inputs,
            )
        committed_fingerprint = await asyncio.to_thread(script_review.content_fingerprint, formal)
        await evidence.record("completed", committed_fingerprint=committed_fingerprint)
        return TextGenerationResult(result_message)
    except asyncio.CancelledError:
        if begun:
            await evidence.record("interrupted", error="任务已中断；不会自动重新提交模型请求")
        raise
    except Exception as exc:
        if begun:
            updates = {"recovery_rejected": True} if isinstance(exc, ValueError) else {}
            await evidence.record("failed", error=str(exc), **updates)
        raise
