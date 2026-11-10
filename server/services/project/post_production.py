"""Explicit local finishing over the production journal and existing render queue.

Video source audio is retained. Music generation and TTS are not prerequisites.
Legacy music helpers remain for existing evidence, but finishing never calls them.
"""

from __future__ import annotations

import asyncio
import math
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from lib.artifacts.artifact_manifest import ArtifactBasis
from lib.bgm.service import BgmLibraryService
from lib.config.resolver import ConfigResolver
from lib.db import async_session_factory
from lib.edit_timeline import EditTimelineService, RevisionAuthor
from lib.edit_timeline.operations import InsertBgm
from lib.edit_timeline.sources import load_episode_script_units
from lib.edit_timeline.store import EditTimelineStore
from lib.final_cut.basis import BURNED_SUBTITLES
from lib.final_cut.service import FinalCutService
from lib.generation.generation_admission import generation_admission_lock
from lib.generation.generation_queue_client import TaskSpec
from lib.infra.async_thread import run_sync_transaction
from lib.jianying_draft.basis import WITHOUT_NARRATION
from lib.jianying_draft.local_delivery import compatibility
from lib.project.production_management import assert_production_open
from server.services.presentation.jianying_delivery import delivery_store
from server.services.presentation.timeline_jianying_draft import TimelineJianyingDraftService, draft_folder_name
from server.services.tasks.render_tasks import jianying_draft_task_request

if TYPE_CHECKING:
    from server.services.project.production import ProductionService


class ProductionError(Exception):
    pass


def music_prompt(units: list[dict], seconds: float, hook: str = "") -> str:
    """Use confirmed text as story context, not as measured media event times."""
    # Keep both the beginning and handoff/ending of each unit within a bounded request.
    selected = units if len(units) <= 10 else [*units[:4], *units[-6:]]
    budget = max(40, 1000 // max(1, len(selected)))

    def excerpt(value):
        text = str(value)
        return text if len(text) <= budget else text[: budget // 2] + " … " + text[-budget // 2 :]

    story = "\n".join(
        f"{unit.get('unit_id', index + 1)}: {excerpt(unit.get('text', ''))}" for index, unit in enumerate(selected)
    )
    return (
        f"Instrumental underscore for a short drama, target {math.ceil(seconds)} seconds. "
        "No vocals, no spoken words. Follow the supplied story; do not invent scenes. "
        "Keep a continuous motif across cuts, leave space for dialogue, build tension with the drama. "
        "The final phrase must remain unresolved: suspenseful ending, no triumphant resolution or long outro.\n"
        f"Confirmed story and handoffs:\n{story}\nEnding context: {hook[:200]}"
    )[:2000]


def score_segments(clips: list[dict], track_us: int, *, silent_units: set[str]) -> list[dict]:
    """Fit a score to real clip placements, repeating short tracks without extending picture.

    Speech protection uses the entire unit when word alignment is unavailable.
    Segment gains and source ranges are shared by the MP4 and editable draft.
    """
    if track_us < 1_000_000:
        raise ProductionError("finish_music_too_short")
    segments: list[dict] = []
    total = max((c["end_us"] for c in clips), default=0)
    boundaries = sorted({0, total, *(c[key] for c in clips for key in ("start_us", "end_us"))})
    groups = []
    for start, end in pairwise(boundaries):
        covering = [c for c in clips if c["start_us"] < end and c["end_us"] > start]
        volume = 0.25 if covering and all(c["unit_id"] in silent_units for c in covering) else 0.12
        if groups and groups[-1][2] == volume:
            groups[-1][1] = end
        else:
            groups.append([start, end, volume])
    # Timeline operations require >= 1 s source ranges. Merge short gain regions
    # conservatively, rather than overlapping music or raising gain under speech.
    index = 0
    while len(groups) > 1 and index < len(groups):
        if groups[index][1] - groups[index][0] < 1_000_000:
            neighbor = index - 1 if index else 1
            first, last = sorted((index, neighbor))
            groups[first : last + 1] = [[groups[first][0], groups[last][1], min(groups[first][2], groups[last][2])]]
            index = max(0, first - 1)
        else:
            index += 1
    # A gain region must be expressible as source slices of at least one second.
    # If it cannot, use a single conservative gain across the whole film.
    if any(end - start < math.ceil((end - start) / track_us) * 1_000_000 for start, end, _ in groups):
        groups = [[0, total, min((group[2] for group in groups), default=0.12)]]
    cursor = 0
    for start, end, volume in groups:
        remaining = end - start
        count = math.ceil(remaining / track_us)
        if remaining >= 1_000_000 and remaining < count * 1_000_000:
            raise ProductionError("finish_music_too_short")
        for index in range(count):
            length = remaining // (count - index)
            source_length = max(1_000_000, length)
            if cursor + source_length > track_us:
                cursor = 0
            segments.append({"start_us": start, "in_us": cursor, "out_us": cursor + source_length, "volume": volume})
            start += length
            remaining -= length
            cursor += source_length
            if len(segments) > 1000:
                raise ProductionError("finish_music_too_short")
    for index, segment in enumerate(segments):
        duration = segment["out_us"] - segment["in_us"]
        segment["fade_in_us"] = min(400_000 if index == 0 else 40_000, duration // 4)
        segment["fade_out_us"] = min(150_000 if segment["start_us"] + duration == total else 40_000, duration // 4)
    return segments


class PostProduction:
    def __init__(self, owner: ProductionService):
        self.owner = owner
        self.projects = owner.projects
        self.queue = owner.services.queue

    async def start(self, project: str, episode: int, user_id: str, *, local: bool) -> dict[str, Any]:
        await asyncio.to_thread(assert_production_open, self.projects, project, {episode})
        path = await asyncio.to_thread(self.owner._path, project, episode)
        async with self.projects.async_file_lock(path):
            previous = await self.owner.status(project, episode)
            if previous["status"] in {"active", "submitting", "unknown"}:
                return previous
            basis = await asyncio.to_thread(self.owner._script_hash, project, episode)
            if (
                previous.get("action") == "finish"
                and previous.get("basis") == basis
                and previous.get("error") not in {"finish_inputs_changed", "finish_timeline_changed"}
                and (previous["status"] != "completed" or previous.get("output", {}).get("status") == "current")
            ):
                # A repeated click is neither a retry nor permission to regenerate music.
                return previous
            state = {
                **{k: previous[k] for k in ("timeline_id", "output") if k in previous},
                "episode": episode,
                "action": "finish",
                "status": "active",
                "stage": "prepare",
                "user_id": user_id,
                "basis": basis,
                "local_delivery": local,
                "candidate_id": str(uuid4()),
                "error": None,
            }
            await self.owner._write(project, episode, state)
            return state

    async def resume(self, project: str, episode: int, *, local: bool) -> dict[str, Any]:
        await asyncio.to_thread(assert_production_open, self.projects, project, {episode})
        path = await asyncio.to_thread(self.owner._path, project, episode)
        async with self.projects.async_file_lock(path):
            state = await asyncio.to_thread(self.owner._read, project, episode)
            if state.get("action") != "finish" or state["status"] not in {"failed", "unknown"}:
                return state
            if state.get("local_delivery") and not local:
                raise ProductionError("finish_requires_local")
            try:
                await self._guard(project, episode, state)
            except ProductionError as exc:
                if str(exc) not in {"finish_inputs_changed", "finish_timeline_changed"}:
                    raise
                state.update(status="failed", error=str(exc))
                await self.owner._write(project, episode, state)
                return state
            if state.get("stage") == "draft" and state.get("delivery_id"):
                store = delivery_store(self.projects)
                record = await asyncio.to_thread(store.read, state["delivery_id"])
                if record.get("task_id"):
                    task = await self.queue.get_task(record["task_id"])
                    if task and task["status"] in {"failed", "cancelled"}:
                        await run_sync_transaction(store.patch, record["delivery_id"], task_id=None)
            state.update(status="active", error=None)
            await self.owner._write(project, episode, state)
            return state

    async def _document(self, project, timeline):
        return await asyncio.to_thread(lambda: EditTimelineStore(self.projects, project).find(timeline))

    async def _guard(self, project, episode, state):
        if state["basis"] != await asyncio.to_thread(self.owner._script_hash, project, episode):
            raise ProductionError("finish_inputs_changed")
        if state.get("revision") is not None:
            document = await self._document(project, state["timeline_id"])
            if document.latest.number != state["revision"]:
                # Recover the single BGM revision whose write completed before journal commit.
                latest = document.latest
                parent = document.revision(state["revision"])
                if (
                    state["stage"] == "apply_music"
                    and latest.parent == state["revision"]
                    and latest.summary == self._summary(state)
                    and parent is not None
                    and parent.content.clips == latest.content.clips
                    and latest.content.bgm
                    and all(b.bgm_id == state["bgm_id"] for b in latest.content.bgm)
                ):
                    state["revision"] = latest.number
                else:
                    raise ProductionError("finish_timeline_changed")

        if state.get("media_basis") and await self._media_basis(project, state) != state["media_basis"]:
            raise ProductionError("finish_inputs_changed")

    async def _media_basis(self, project, state):
        job = await TimelineJianyingDraftService(self.projects).prepare(
            project, state["timeline_id"], revision=state["revision"], narration=state["narration"]
        )
        inputs = job.basis.to_evidence_dict()["inputs"]
        if not isinstance(inputs, dict):
            raise ProductionError("finish_inputs_changed")
        # BGM insertion changes the revision, but must not change picture, speech or subtitles.
        return ArtifactBasis.build(
            "production-finish/media",
            kind_version=1,
            inputs={key: inputs[key] for key in ("units", "aspect_ratio", "narration")},
        ).digest

    @staticmethod
    def _summary(state):
        return f"自动后期 {state['candidate_id'][:8]}：保留画面衔接，配乐避让对白，短尾收束"

    async def advance(self, project: str, episode: int, state: dict):
        if state["status"] not in {"active", "unknown"}:
            return
        try:
            await self._guard(project, episode, state)
            stage = state["stage"]
            if stage == "prepare":
                await self._prepare(project, episode, state)
            elif stage in {"music", "apply_music"}:
                # Revalidate a legacy job locally. Keep its candidate/task IDs for
                # auditing, but never submit or require the abandoned music step.
                await self._prepare(project, episode, state)
            elif stage == "render":
                await self._render(project, episode, state)
            elif stage == "draft":
                await self._draft(project, episode, state)
        except Exception as exc:
            # Retry is explicit. Candidate, audio, revision and last usable output survive.
            state.update(status="failed", error=str(exc))
            await self.owner._write(project, episode, state)

    async def _prepare(self, project, episode, state):
        timelines = EditTimelineService(self.projects)
        if not state.get("timeline_id"):
            name = "XM Studio " + state["basis"][:12]
            existing = await timelines.list_timelines(project, episode=episode)
            match = next((t for t in existing if t.name == name), None)
            timeline = (
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
            state["timeline_id"] = timeline
            await self.owner._write(project, episode, state)
        timeline = state["timeline_id"]
        if state["local_delivery"]:
            settings = await asyncio.to_thread(delivery_store(self.projects).settings)
            compatible = await asyncio.to_thread(compatibility, settings)
            if not settings.enabled or not compatible["ready"]:
                raise ProductionError("finish_configure_jianying")
        # Validate picture and subtitle inputs without requiring a TTS rendition.
        # MiniMax 生视频已经携带原声。合并成片只负责按剪辑时间线拼接画面，
        # 不要求 TTS/旁白配置，也不在这里自动生成一首 BGM。
        checked = await FinalCutService(self.projects).check(
            project, timeline, narration=WITHOUT_NARRATION, subtitles=BURNED_SUBTITLES
        )
        await TimelineJianyingDraftService(self.projects).check(
            project, timeline, revision=checked.revision, narration=WITHOUT_NARRATION
        )
        readout = await timelines.read(project, timeline, revision=checked.revision)
        if readout.duration <= 0 or any(c.status != "ready" for c in readout.clips):
            raise ProductionError("finish_video_not_ready")
        state.update(
            revision=checked.revision,
            narration=checked.narration,
            # 配乐仍可由用户在时间线中手工加入；默认合片直接渲染视频原声。
            stage="render",
        )
        state["media_basis"] = await self._media_basis(project, state)
        state["notes"] = [
            "finish_preserve_picture",
            "finish_existing_music" if readout.bgm else "finish_native_video_audio",
        ]
        await self.owner._write(project, episode, state)

    async def _candidate_task(self, project, state):
        if state.get("music_task_id"):
            return await self.queue.get_task(state["music_task_id"])
        page = 1
        while True:
            result = await self.queue.list_tasks(project_name=project, task_type="music", page=page, page_size=100)
            match = next((t for t in result["items"] if t["resource_id"] == state["candidate_id"]), None)
            if match:
                return match
            if page * 100 >= result["total"]:
                return None
            page += 1

    async def _music(self, project, episode, state):
        tracks = await BgmLibraryService(self.projects).list(project)
        track = next((t for t in tracks if t.generation_id == state["candidate_id"]), None)
        if track:
            state.update(bgm_id=track.id, stage="apply_music")
            await self.owner._write(project, episode, state)
            return
        async with generation_admission_lock(
            project_name=project, script_file="", resource_id=f"music:{state['candidate_id']}"
        ):
            task = await self._candidate_task(project, state)
            root = await asyncio.to_thread(self.projects.get_project_path, project)
            audio = root / "bgm" / ".music-candidates" / f"{state['candidate_id']}.mp3"
            marker = audio.with_suffix(".json")
            recover = await asyncio.to_thread(audio.is_file)
            if task and task["status"] in {"queued", "running"}:
                state["music_task_id"] = task["task_id"]
                await self.owner._write(project, episode, state)
                return
            if not recover and (task or await asyncio.to_thread(marker.exists)):
                # A lost result is not proof of rejection, nor permission for a new candidate.
                raise ProductionError("finish_music_result_unavailable")
            await self._guard(project, episode, state)
            if not recover:
                config = await ConfigResolver(async_session_factory).provider_config("minimax")
                if not config.get("api_key"):
                    raise ProductionError("finish_configure_minimax")
            spec = TaskSpec.from_request(
                task_type="music",
                media_type="audio",
                resource_id=state["candidate_id"],
                prompt=state["music_prompt"],
                source="webui",
                extra_payload={
                    "episode": episode,
                    "billing_episode_id": episode,
                    "target_seconds": state["target_seconds"],
                    "recover_only": recover,
                },
            )
            enqueued = await self.queue.enqueue_task(
                project_name=project,
                task_type="music",
                media_type="audio",
                resource_id=state["candidate_id"],
                payload=spec.payload,
                source="webui",
                user_id=state["user_id"],
                provider_id="minimax",
            )
            state["music_task_id"] = enqueued["task_id"]
            await self.owner._write(project, episode, state)

    async def _apply_music(self, project, episode, state):
        document = await self._document(project, state["timeline_id"])
        if document.latest.content.bgm:
            # A completed write can be observed after interruption; never insert twice.
            if not all(b.bgm_id == state["bgm_id"] for b in document.latest.content.bgm):
                raise ProductionError("finish_timeline_changed")
        else:
            script = await asyncio.to_thread(load_episode_script_units, self.projects, project, episode)
            silent = {u.unit_id for u in script.units if u.speech_mode == "silent"}
            tracks = await BgmLibraryService(self.projects).list(project)
            track = next(t for t in tracks if t.id == state["bgm_id"])
            segments = score_segments(state["placements"], track.duration_us, silent_units=silent)
            result = await EditTimelineService(self.projects).edit(
                project,
                state["timeline_id"],
                base_revision=state["revision"],
                strict_revision=True,
                summary=self._summary(state),
                author=RevisionAuthor(kind="creator", user_id=state["user_id"]),
                operations=[
                    InsertBgm(
                        op="insert_bgm",
                        bgm_id=track.id,
                        start=s["start_us"] / 1_000_000,
                        source_in=s["in_us"] / 1_000_000,
                        source_out=s["out_us"] / 1_000_000,
                        volume=s["volume"],
                        fade_in=s["fade_in_us"] / 1_000_000,
                        fade_out=s["fade_out_us"] / 1_000_000,
                    )
                    for s in segments
                ],
            )
            state["revision"] = result.revision
        state.update(stage="render", batch_id=None)
        await self.owner._write(project, episode, state)

    async def _render(self, project, episode, state):
        if not state.get("batch_id"):
            await self.owner._submit(project, episode, state, "render")
            return
        batch = await self.queue.get_generation_batch(
            project_name=project, batch_id=state["batch_id"], user_id=state["user_id"]
        )
        if not batch.done:
            return
        if batch.counts.failed or batch.counts.cancelled or batch.counts.blocked:
            # Local rendering can be retried without music submission.
            state["batch_id"] = None
            raise ProductionError("finish_render_failed")
        output = await FinalCutService(self.projects).status(
            project, state["timeline_id"], narration=state["narration"], subtitles=BURNED_SUBTITLES
        )
        if output.status != "current" or output.version is None:
            raise ProductionError("finish_output_not_current")
        state.update(output=output.model_dump(mode="json"), stage="draft", batch_id=None)
        await self.owner._write(project, episode, state)

    async def _draft(self, project, episode, state):
        store = delivery_store(self.projects)
        if not state.get("delivery_id"):
            drafts = TimelineJianyingDraftService(self.projects)
            job = await drafts.prepare(
                project, state["timeline_id"], revision=state["revision"], narration=state["narration"]
            )
            previous = await asyncio.to_thread(store.latest, project, episode)
            data = await asyncio.to_thread(self.projects.load_project, project)
            readout = await EditTimelineService(self.projects).read(project, state["timeline_id"])
            same = bool(
                previous and previous["basis"] == job.basis.digest and previous["local"] == state["local_delivery"]
            )
            record = await run_sync_transaction(
                store.reserve,
                project=project,
                episode=episode,
                timeline_id=state["timeline_id"],
                revision=job.revision,
                narration=job.narration,
                basis=job.basis.digest,
                name=draft_folder_name(
                    project, data, episode=episode, timeline_name=readout.timeline.name, narration=job.narration
                ),
                new_version=bool(previous and not same),
                previous_id=previous["delivery_id"] if previous else None,
                local=state["local_delivery"],
            )
            if record["basis"] != job.basis.digest or record["local"] != state["local_delivery"]:
                raise ProductionError("finish_draft_conflict")
            state["delivery_id"] = record["delivery_id"]
            await self.owner._write(project, episode, state)
        record = await asyncio.to_thread(store.read, state["delivery_id"])
        if record["state"] in {"delivered", "package_ready"}:
            state.update(status="completed", error=None)
            await self.owner._write(project, episode, state)
            return
        # Packaged audio/video survives a running editor or failed registration.
        if await asyncio.to_thread(store.package_path(record["delivery_id"]).is_file):
            registered = await run_sync_transaction(store.register, record["delivery_id"])
            if registered["state"] not in {"delivered", "package_ready"}:
                raise ProductionError("finish_close_jianying")
            return
        task = await self.queue.get_task(record["task_id"]) if record.get("task_id") else None
        if task and task["status"] in {"queued", "running"}:
            return
        if task:
            raise ProductionError("finish_draft_failed")
        task_request = jianying_draft_task_request(
            episode=episode,
            timeline_id=record["timeline_id"],
            revision=record["revision"],
            narration=record["narration"],
        )
        task_request.payload["delivery_id"] = record["delivery_id"]
        enqueued = await self.queue.enqueue_task(
            project_name=project, **task_request.enqueue_fields(), source="webui", user_id=state["user_id"]
        )
        await run_sync_transaction(
            store.patch, record["delivery_id"], task_id=enqueued["task_id"], state="preparing", error=None
        )
