"""Music, story and picture share the existing absolute edit timeline."""

import asyncio
import hashlib
import json
from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import asdict
from itertools import pairwise

from lib.artifacts.version_manager import VersionManager
from lib.bgm.beats import ANALYSIS_VERSION, BeatAnalysis, analyze_audio
from lib.bgm.library import read_bgm_library
from lib.edit_timeline.model import EditTimelineDocument, RevisionAuthor
from lib.edit_timeline.model import seconds_to_microseconds as us
from lib.edit_timeline.operations import MIN_TRIM_MICROSECONDS, TimelineOperationAdapter
from lib.edit_timeline.readout import effective_source_range_us, project_readout
from lib.edit_timeline.service import EditTimelineService
from lib.edit_timeline.sources import (
    EpisodeSources,
    _episode_script_file,
    load_episode_script_units,
    load_episode_sources,
    script_units_from_snapshot,
)
from lib.edit_timeline.store import EditTimelineStore
from lib.infra.async_thread import run_sync_transaction
from lib.infra.content_digest import sha256_file
from lib.infra.json_io import atomic_write_json, load_json_or_none
from lib.infra.path_safety import safe_join
from lib.project.project_manager import ProjectManager
from lib.speech.speech_composition import SpeechMode

MAX_SAFE_SHIFT_US = 120_000


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def propose(document: EditTimelineDocument, sources: EpisodeSources,
            music: dict[str, BeatAnalysis], fingerprints: dict[str, str]) -> dict:
    """Conservative proposals: protect complete speech/caption clips without word timing."""
    content = document.latest.content
    view = project_readout(document, document.latest, sources)
    clips = [clip for clip in content.clips if clip.included and sources.unit(clip.unit_id)]
    positions = {clip.id: clip for clip in view.clips}
    beat_confidence: dict[int, float] = {}
    tracks = []
    for bgm in view.bgm:
        analysis = music.get(bgm.bgm_id)
        if analysis is None or bgm.volume <= 0:
            continue
        start, end, source_in = us(bgm.start), us(bgm.end), us(bgm.source_in)
        mapped = [start + value - source_in for value in analysis.beats_us if source_in <= value < source_in + end - start]
        for point in mapped:
            beat_confidence[point] = min(beat_confidence.get(point, 1.0), analysis.confidence)
        tracks.append({"clip_id": bgm.id, "bgm_id": bgm.bgm_id, "start_us": start, "end_us": end,
                       "source_in_us": source_in, "bpm": analysis.bpm, "candidates": analysis.candidates,
                       "confidence": analysis.confidence, "waveform": analysis.waveform,
                       "duration_us": sources.bgm[bgm.bgm_id].duration_us if bgm.bgm_id in sources.bgm else 0,
                       "beats_us": mapped})
    beats = sorted(beat_confidence)
    suggestions = []
    events = []
    for clip in clips:
        unit = sources.unit(clip.unit_id)
        pos = positions[clip.id]
        events.append({"clip_id": clip.id, "unit_id": clip.unit_id, "start_us": us(pos.start),
                       "end_us": us(pos.start + pos.duration), "text": unit.subtitle_text if unit else "",
                       "speech": str(unit.speech_mode) if unit else None})
    for left, right in pairwise(clips):
        cut = us(positions[right.id].start)
        index = bisect_left(beats, cut)
        neighbors = beats[max(0, index - 1):index + 1]
        if not neighbors:
            continue
        target = min(neighbors, key=lambda point: abs(point - cut))
        delta = target - cut
        confidence = beat_confidence[target]
        reason = None
        for clip in (left, right):
            unit = sources.unit(clip.unit_id)
            media = sources.media.get(clip.unit_id)
            if unit is None or unit.speech_mode != SpeechMode.SILENT or unit.subtitle_text.strip() or clip.subtitles or (media and media.narration_duration_us):
                reason = "speech_or_subtitles"
            if media is None or media.video_version is None or media.video_duration_us is None:
                reason = "media_missing"
            elif clip.trim and clip.trim.basis_version != media.video_version:
                reason = "media_changed"
        if left.hold_us or right.hold_us or left.transition_to_next or right.transition_to_next:
            reason = "hold_or_transition"
        status = "conflict" if reason else "unconfirmed"
        operations = []
        if not reason and abs(delta) <= 20_000:
            status = "aligned"
        elif abs(delta) > MAX_SAFE_SHIFT_US:
            reason = reason or "too_far"
        elif confidence < 0.35:
            reason = reason or "low_confidence"
        elif not reason:
            lm, rm = sources.media[left.unit_id], sources.media[right.unit_id]
            li, lo = effective_source_range_us(left, lm.video_version, lm.video_duration_us or 0)
            ri, ro = effective_source_range_us(right, rm.video_version, rm.video_duration_us or 0)
            if (li + MIN_TRIM_MICROSECONDS <= lo + delta <= (lm.video_duration_us or 0)
                    and 0 <= ri + delta <= ro - MIN_TRIM_MICROSECONDS):
                status = "near"
                operations = [
                    {"op": "set_trim", "clip": left.id, "trim": {"source_in": li / 1e6, "source_out": (lo + delta) / 1e6}},
                    {"op": "set_trim", "clip": right.id, "trim": {"source_in": (ri + delta) / 1e6, "source_out": ro / 1e6}},
                ]
            else:
                reason = "no_source_handle"
        suggestions.append({"id": f"{left.id}/{right.id}", "left": left.id, "right": right.id,
                            "cut_us": cut, "beat_us": target, "offset_us": delta,
                            "status": status, "reason": reason, "operations": operations})
    basis = digest({"algorithm": ANALYSIS_VERSION, "timeline": document.id, "revision": document.latest.number,
                    "fingerprints": fingerprints, "view": view.model_dump(mode="json"),
                    "script": asdict(sources.script), "media": {k: asdict(v) for k, v in sources.media.items()}})
    return {"basis": basis, "timeline_id": document.id, "revision": document.latest.number,
            "duration_us": us(view.duration), "tracks": tracks, "events": events, "suggestions": suggestions}


class BeatSyncService:
    def __init__(self, projects: ProjectManager) -> None:
        self.projects = projects

    async def analyze(self, project: str, timeline: str) -> dict:
        report, *_ = await self._analyze(project, timeline)
        return report

    async def _analyze(self, project: str, timeline: str):
        document = await asyncio.to_thread(lambda: EditTimelineStore(self.projects, project).find(timeline))
        script = await asyncio.to_thread(load_episode_script_units, self.projects, project, document.episode)
        sources = await load_episode_sources(self.projects, project, script, {c.unit_id for c in document.latest.content.clips})
        data = await asyncio.to_thread(self.projects.load_project, project)
        root = await asyncio.to_thread(self.projects.get_project_path, project)
        library = read_bgm_library(data)
        music, fingerprints = {}, {}
        for bgm_id in {b.bgm_id for b in document.latest.content.bgm}:
            track = library.get(bgm_id)
            if track is None:
                raise ValueError("Music missing; choose a project BGM again")
            path = safe_join(root, track.file)
            fingerprint = await asyncio.to_thread(sha256_file, path)
            if fingerprint != track.content_digest:
                raise ValueError("Music content changed; upload it again")
            fingerprints[bgm_id] = fingerprint
            cache = root / "bgm" / f".beats-v{ANALYSIS_VERSION}-{fingerprint}.json"
            cached = await asyncio.to_thread(load_json_or_none, cache)
            try:
                analysis = BeatAnalysis(**cached) if isinstance(cached, dict) else None
                if analysis is not None and (not 0 <= analysis.confidence <= 1 or
                        any(not isinstance(t, int) or t < 0 or t > track.duration_us for t in analysis.beats_us)):
                    analysis = None
            except (TypeError, ValueError):
                analysis = None
            if analysis is None:
                analysis = await analyze_audio(path, duration_us=track.duration_us)
                await run_sync_transaction(atomic_write_json, cache, analysis.to_dict())
            music[bgm_id] = analysis
        report = await asyncio.to_thread(propose, document, sources, music, fingerprints)
        return report, document, sources, fingerprints

    async def apply(self, project: str, timeline: str, *, basis: str, ids: list[str], author: RevisionAuthor) -> None:
        current, document, sources, fingerprints = await self._analyze(project, timeline)
        if current["basis"] != basis:
            raise ValueError("Analysis expired; analyze the current timeline again")
        selected = {s["id"]: s for s in current["suggestions"]}
        merged: dict[str, dict] = {}
        touched = set()
        for key in dict.fromkeys(ids):
            suggestion = selected.get(key)
            if not suggestion or suggestion["status"] != "near" or not suggestion["operations"]:
                raise ValueError("Only safe suggestions can be applied; review conflicts in the editor")
            touched.add(key)
            for index, operation in enumerate(suggestion["operations"]):
                clip = operation["clip"]
                if clip not in merged:
                    merged[clip] = {**operation, "trim": dict(operation["trim"])}
                # A left neighbor changes this clip's in point; a right neighbor changes its out point.
                field = "source_out" if index == 0 else "source_in"
                merged[clip]["trim"][field] = operation["trim"][field]
        if not touched:
            raise ValueError("Choose at least one safe suggestion")
        if any((op["trim"]["source_out"] - op["trim"]["source_in"]) * 1e6 < MIN_TRIM_MICROSECONDS for op in merged.values()):
            raise ValueError("Combined beat moves would remove a clip; apply individually")
        await EditTimelineService(self.projects).edit(project, timeline, base_revision=current["revision"],
            summary=f"Music beat alignment: {len(touched)} cuts; duration preserved", author=author, strict_revision=True,
            operations=[TimelineOperationAdapter.validate_python(op) for op in merged.values()],
            source_guard=lambda: self.commit_guard(project, document, sources, fingerprints))


    @contextmanager
    def commit_guard(self, project: str, document: EditTimelineDocument,
                     sources: EpisodeSources, fingerprints: dict[str, str]):
        """Keep script, project library and current selections stable through revision commit.

        Called inside the episode timeline lock; source locks follow the existing
        script -> project -> version order. No awaits or script rereads under locks.
        """
        root = self.projects.get_project_path(project)
        script_file = _episode_script_file(self.projects.load_project(project), document.episode)
        with self.projects.locked_project_script_snapshot(project, script_file) as (metadata, raw):
            if (_episode_script_file(metadata, document.episode) != script_file
                    or script_units_from_snapshot(raw, document.episode) != sources.script):
                raise ValueError("Story changed; analyze again before moving cuts")
            versions = VersionManager(root)
            # The manager's shared lock serializes every resource selection.
            if not sources.media:
                raise ValueError("Media missing; analyze again before moving cuts")
            with versions.locked_version_snapshot(sources.script.video_resource_type, next(iter(sources.media))):
                for unit, expected in sources.media.items():
                    video = versions.get_current_version(sources.script.video_resource_type, unit) or None
                    audio = versions.get_current_version("audio", unit) or None
                    if video != expected.video_version or audio != expected.narration_version:
                        raise ValueError("Media changed; analyze again before moving cuts")
                library = read_bgm_library(metadata)
                for bgm_id, fingerprint in fingerprints.items():
                    track = library.get(bgm_id)
                    if track is None or track.content_digest != fingerprint or sha256_file(safe_join(root, track.file)) != fingerprint:
                        raise ValueError("Music changed; analyze again before moving cuts")
                yield
