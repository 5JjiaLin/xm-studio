"""Real application/worker process with deterministic external model boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
import uvicorn

from lib.backends.providers import CallPurpose
from lib.backends.text_generator import TextGenerator
from lib.billing.ledger import Ledger
from tests.factories import make_video_request_facts
from tests.fakes import FakeConfigResolver, observe_json_replace, use_text_generator
from tests.integration.server.script_plan_repair_process import CRASH_EXIT_CODE, LocalBackend


class ControlledBackend(LocalBackend):
    def __init__(self, control, phase, action):
        super().__init__(control, phase, action)
        self.release = asyncio.Event()

    async def generate(self, request):
        if self.action == "start":
            await asyncio.wait_for(self.release.wait(), 15)
        return await super().generate(request)


async def serve(control: Path, action: str, phase: str) -> None:
    from lib.db import async_session_factory
    from server.app import app

    backend = ControlledBackend(control, phase, action)

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

    async def no_external_catalog(*_args, **_kwargs):
        return None

    async def release():
        backend.release.set()
        return {"released": True}

    def crash_after_replace(_src, dst):
        target = Path(dst)
        if (
            action == "start"
            and target.name == "latest.json"
            and target.parent.name == "script_plan_repair"
            and json.loads(target.read_text(encoding="utf-8"))["phase"] == phase
        ):
            os._exit(CRASH_EXIT_CODE)

    # These fixtures fix external model/config inputs and suppress catalog I/O.
    # HTTP auth, lifespan, migrations, queue, worker/executors and ledger are real.
    with pytest.MonkeyPatch.context() as monkeypatch:
        use_text_generator(monkeypatch, create_generator)
        monkeypatch.setattr("server.tool_runtime.ConfigResolver", lambda *_args: FakeConfigResolver())
        monkeypatch.setattr("lib.script.reference_video.request_projection.evaluate_video_request_facts", video_facts)
        monkeypatch.setattr("lib.catalog.official_sync.sync_once", no_external_catalog)
        observe_json_replace(monkeypatch, crash_after_replace)
        app.add_api_route("/__fixture__/release", release, methods=["POST"])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            (control / "port.json").write_text(json.dumps(listener.getsockname()[1]))
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
            await server.serve(sockets=[listener])


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
