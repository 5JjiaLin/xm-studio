"""Music uses the existing audio queue and ledger; recovery never repeats a POST."""

import asyncio
import json
import os
from uuid import UUID

from lib.backends.http_status_errors import ProviderRejectedError
from lib.backends.music import (
    DEFAULT_MUSIC_MODEL,
    MUSIC_PROVIDER,
    MiniMaxMusicBackend,
    MusicRejectedError,
    MusicRequest,
)
from lib.backends.providers import CallPurpose
from lib.bgm.service import BgmLibraryService
from lib.billing.ledger import Ledger
from lib.config.resolver import ConfigResolver
from lib.db import async_session_factory
from lib.db.base import DEFAULT_USER_ID
from lib.infra.async_thread import run_noninterruptible_sync
from lib.infra.json_io import atomic_write_json, load_json_or_none
from lib.project.project_manager import get_project_manager


async def execute_music_task(project_name: str, resource_id: str, payload: dict, *,
                             user_id: str = DEFAULT_USER_ID, task_id: str | None = None,
                             claimed_provider_id: str | None = None) -> dict:
    candidate = str(UUID(resource_id))
    if claimed_provider_id not in (None, MUSIC_PROVIDER):
        raise ValueError("Music provider changed")
    pm = get_project_manager()
    library = BgmLibraryService(pm)
    tracks = await library.list(project_name)
    for track in tracks:
        if track.generation_id == candidate:
            return {"bgm_id": track.id, "file_path": track.file}
    root = await asyncio.to_thread(pm.get_project_path, project_name)
    spool = root / "bgm" / ".music-candidates"
    await asyncio.to_thread(spool.mkdir, parents=True, exist_ok=True)
    output = spool / f"{candidate}.mp3"
    marker = spool / f"{candidate}.json"
    if payload.get("recover_only") and not await asyncio.to_thread(output.is_file):
        raise RuntimeError("No local audio to recover; this provider has no polling API")
    if not await asyncio.to_thread(output.is_file):
        if await asyncio.to_thread(marker.exists):
            raise RuntimeError("[create_ambiguous] Music submission recorded without audio; verify with provider. No automatic resubmission.")
        config = await ConfigResolver(async_session_factory).provider_config(MUSIC_PROVIDER)
        backend = MiniMaxMusicBackend(api_key=config.get("api_key", ""), base_url=config.get("base_url"))
        prompt = str(payload["prompt"])
        # Durable submission fence precedes the one paid call, including across crashes.
        def fence() -> None:
            try:
                with marker.open("x", encoding="utf-8") as handle:
                    json.dump({"task_id": task_id, "prompt": prompt, "model": DEFAULT_MUSIC_MODEL,
                               "summary": payload.get("summary"), "mood": payload.get("mood"),
                               "target_seconds": payload.get("target_seconds")}, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as exc:
                raise RuntimeError("[create_ambiguous] Music already submitted; no automatic resubmission") from exc
        await run_noninterruptible_sync(fence)
        async with Ledger().record(project_name=project_name, call_type="audio", model=DEFAULT_MUSIC_MODEL,
                                   provider=MUSIC_PROVIDER, prompt=prompt, user_id=user_id, task_id=task_id,
                                   purpose=CallPurpose.GENERATION_TASK,
                                   inputs={"is_instrumental": True, "target_seconds": payload.get("target_seconds")}) as call:
            try:
                result = await backend.generate(MusicRequest(prompt, output))
            except (MusicRejectedError, ProviderRejectedError):
                def rejected() -> None:
                    facts = load_json_or_none(marker) or {}
                    atomic_write_json(marker, {**facts, "outcome": "rejected"})
                await run_noninterruptible_sync(rejected)
                raise
            call.success(result)
    # A restart/registration error can recover these exact bytes without billing again.
    content = await asyncio.to_thread(output.read_bytes)
    track = await library.upload(project_name, filename=f"Music-{candidate[:8]}.mp3", content=content,
                                 generation_id=candidate)
    return {"bgm_id": track.id, "file_path": track.file, "duration_seconds": track.duration}
