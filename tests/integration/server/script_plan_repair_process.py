"""Isolated process driver for planning repair restart tests; never uses a live provider."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import TextCapability, TextGenerationResult
from lib.backends.text_generator import TextGenerator
from lib.billing.ledger import Ledger
from lib.db import async_engine, async_session_factory
from lib.db.base import DEFAULT_USER_ID
from lib.generation.generation_queue import GenerationQueue
from lib.generation.generation_queue_client import wait_for_task
from lib.generation.generation_worker import CapacityTable, GenerationWorker
from lib.project.project_manager import ProjectManager
from server.tool_runtime import (
    CallerContext,
    GenerateScriptPlanRequest,
    ProjectScope,
    Services,
    ToolRequest,
    execute_queued_text_task,
    generate_script_plan,
)
from tests.factories import make_video_request_facts
from tests.fakes import FakeConfigResolver, observe_json_replace, refuse_resume_execution, use_text_generator

CRASH_EXIT_CODE = 73


class LocalBackend:
    """Deterministic backend; the real generator and ledger still execute."""

    name = "openai"
    model = "gpt-5.4-mini"
    configuration_fingerprint = "restart-fixture-no-secret"

    def __init__(self, control: Path, crash_phase: str, action: str):
        self.control = control
        self.crash_phase = crash_phase
        self.action = action
        self.capabilities = {TextCapability.TEXT_GENERATION}

    async def generate(self, _request):
        if self.action != "start":
            raise AssertionError("restart or local recovery attempted a provider request")

        def respond():
            calls_path = self.control / "calls.json"
            count = json.loads(calls_path.read_text()) if calls_path.exists() else 0
            count += 1
            calls_path.write_text(json.dumps(count))
            if self.crash_phase == "acceptance_unknown":
                os._exit(CRASH_EXIT_CODE)
            responses = json.loads((self.control / "responses.json").read_text())
            return responses[count - 1]

        text = await asyncio.to_thread(respond)
        return TextGenerationResult(text=text, provider="openai", model=self.model, input_tokens=10, output_tokens=10)


async def run(control: Path, action: str, phase: str) -> None:
    projects = ProjectManager(os.environ["ARCREEL_DATA_DIR"])
    project_name = "restart-demo"
    queue = GenerationQueue(session_factory=async_session_factory, project_manager=projects)
    services = Services(
        projects=projects, workflow_planner=object(), capabilities=FakeConfigResolver(), queue=queue,
    )
    backend = LocalBackend(control, phase, action)

    async def create_generator(*_args, **_kwargs):
        return TextGenerator(
            backend, Ledger(session_factory=async_session_factory), "openai",
            purpose=CallPurpose.SCRIPT_GENERATION,
        )

    async def video_facts(*_args, **_kwargs):
        return make_video_request_facts(
            route="reference_video", generation_type="i2v",
            supported_durations=(4, 6, 8, 12), allowed_durations=(4, 6, 8, 12),
        )

    async def execute(task, *, claimed_provider_id=None):
        return await execute_queued_text_task(task, services=services)

    async def provider(_task):
        return "text"

    worker = GenerationWorker(
        queue=queue,
        capacity=CapacityTable(_limits={}, _defaults={"text": 1}),
        provider_projection=provider,
        executor=execute,
        resume_executor=refuse_resume_execution,
        lanes=("text",),
    )
    worker.poll_interval = 0.01
    worker.heartbeat_interval = 0.01
    worker.lease_ttl = 1.0
    def crash_after_replace(src, dst):
        target = Path(dst)
        if (
            action == "start"
            and target.name == "latest.json"
            and target.parent.name == "script_plan_repair"
            and json.loads(target.read_text(encoding="utf-8"))["phase"] == phase
        ):
            os._exit(CRASH_EXIT_CODE)

    # These fixtures replace the external model/config boundaries. Queue, ledger,
    # project locks, source registration, validation and restart recovery are real.
    with pytest.MonkeyPatch.context() as monkeypatch:
        use_text_generator(monkeypatch, create_generator)
        monkeypatch.setattr("lib.script.reference_video.request_projection.evaluate_video_request_facts", video_facts)
        observe_json_replace(monkeypatch, crash_after_replace)
        try:
            if action == "start":
                assert await queue.acquire_or_renew_worker_lease(
                    name=worker.lease_name, owner_id=worker.owner_id, ttl_seconds=worker.lease_ttl,
                )
                outcome = await generate_script_plan(
                    ToolRequest(GenerateScriptPlanRequest(episode_id=1, repair_once=True)),
                    ProjectScope(project_name=project_name, data_root=projects.data_root),
                    CallerContext(user_id=DEFAULT_USER_ID, source="mcp"), services,
                )
                assert outcome.problem is None, outcome.problem
                task_id = outcome.value.members[0].task_id
                (control / "task.json").write_text(json.dumps(task_id))
                await worker.start()
                terminal = await wait_for_task(task_id, 0.01, queue=queue, timeout_seconds=15)
                raise AssertionError(f"expected injected process exit; got {terminal}")

            task_id = json.loads((control / "task.json").read_text())
            await worker.start()
            terminal = await wait_for_task(task_id, 0.01, queue=queue, timeout_seconds=15)
            assert terminal["status"] == "failed", terminal
            assert "restart_lost_text" in terminal["error_message"], terminal
            if action == "observe":
                (control / "observed.json").write_text(json.dumps(terminal))
                return
            for episode, user_id in ((1, "other-user"), (2, DEFAULT_USER_ID)):
                refused = await generate_script_plan(
                    ToolRequest(GenerateScriptPlanRequest(episode_id=episode, repair_recheck_task_id=task_id)),
                    ProjectScope(project_name=project_name, data_root=projects.data_root),
                    CallerContext(user_id=user_id, source="mcp"), services,
                )
                assert refused.problem is not None, refused
                assert refused.problem.code == "generation_refused", refused
            outcome = await generate_script_plan(
                ToolRequest(GenerateScriptPlanRequest(episode_id=1, repair_recheck_task_id=task_id)),
                ProjectScope(project_name=project_name, data_root=projects.data_root),
                CallerContext(user_id=DEFAULT_USER_ID, source="mcp"), services,
            )
            if phase == "acceptance_unknown":
                assert outcome.problem is not None
                assert "repair_response_unknown" in outcome.problem.detail, outcome.problem
                (control / "recovery.json").write_text(json.dumps({"refused": True}))
                return
            assert outcome.problem is None, outcome.problem
            recovered_id = outcome.value.members[0].task_id
            worker.wake()
            recovered = await wait_for_task(recovered_id, 0.01, queue=queue, timeout_seconds=15)
            assert recovered["status"] == "succeeded", recovered
            (control / "recovery.json").write_text(json.dumps(recovered))
        finally:
            await worker.stop()
            await async_engine.dispose()


if __name__ == "__main__":
    with asyncio.Runner() as runner:
        runner.run(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
