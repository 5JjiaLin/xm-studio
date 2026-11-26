"""Internal OpenFilm workflow for a complete episode."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import shutil
import signal
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lib.artifacts.version_manager import VersionManager
from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import ImageInput, TextGenerationRequest, TextTaskType
from lib.backends.text_generator import TextGenerator
from lib.episode.episode_paths import episode_source_path
from lib.final_cut.basis import current_video, output_profile_for_project, video_resource_type_for
from lib.infra.content_digest import prefixed_canonical_json_digest, sha256_file, unprefixed
from lib.infra.json_io import atomic_write_json, load_json
from lib.infra.media_probe import probe_media
from lib.infra.text_utils import strip_json_code_fences
from lib.project.project_manager import ProjectManager
from lib.script.script_editor import resolve_items

logger = logging.getLogger(__name__)
HERE = Path(__file__).parent
ENGINE_VERSION = "0.4.1"
TASK_TYPE = "render_episode_edit"
_LIBRARY_ENV = ("OPENFILM_LIBRARY_CACHE", "PLAYWRIGHT_BROWSERS_PATH")
_DRAFT_MARKER = "##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##"


class EditError(ValueError):
    pass


class EditDecisionError(EditError):
    pass


def engine_path() -> Path:
    value = os.environ.get("XM_OPENFILM_DIR")
    if not value:
        raise EditError("剪辑运行环境尚未配置")
    module = Path(value).resolve()
    if (
        not (module / "src/render.mjs").is_file()
        or not (module / "manual/film/SKILL.md").is_file()
        or not all(shutil.which(command) for command in ("node", "ffmpeg", "ffprobe"))
    ):
        raise EditError("剪辑运行环境尚未配置")
    if not (module / "package.json").is_file() or load_json(module / "package.json").get("version") != ENGINE_VERSION:
        raise EditError("剪辑运行环境版本不匹配")
    return module


def snapshot(projects: ProjectManager, project: str, episode: int, music: bool) -> dict:
    root = projects.get_project_path(project)
    data = projects.load_project(project)
    entry = next((e for e in data["episodes"] if e["episode"] == episode), None)
    if not entry or not entry.get("script_file"):
        raise EditError("本集还没有分镜")
    script = projects.load_script_readonly(project, entry["script_file"].removeprefix("scripts/"))
    items, id_field, kind = resolve_items(script)
    resource = video_resource_type_for(kind)
    versions = VersionManager(root)
    shots = []
    for index, item in enumerate(items, 1):
        unit = item[id_field]
        video = current_video(root, versions, resource, unit, lambda p: sha256_file(root / p))
        if video is None:
            raise EditError(f"片段 {unit} 缺少可用视频")
        key = f"shot-{index:03d}"
        shots.append(
            {
                "key": key,
                "unit": unit,
                "version": video.version,
                "source": video.snapshot.relative_to(root).as_posix(),
                "hash": unprefixed(video.content_digest),
                "file": f"assets/{key}.mp4",
                "story": {
                    k: item[k] for k in ("source_text", "text", "description", "dialogue", "segment_break") if k in item
                },
            }
        )
    if not shots:
        raise EditError("本集还没有可剪辑的视频")
    text = episode_source_path(root, episode)
    profile = output_profile_for_project(data, kind)
    # Reference descriptions clarify who and what the editor sees in the footage.
    references = {
        kind: {
            name: asset.get("description", "")
            for name, asset in data.get(kind, {}).items()
            if isinstance(asset, dict) and any(f"@[{name}" in str(shot["story"]) for shot in shots)
        }
        for kind in ("characters", "scenes", "props")
    }
    return {
        "schema": 1,
        "episode": episode,
        "source_text": text.read_text(encoding="utf-8") if text.is_file() else "",
        "shots": shots,
        "references": references,
        "music": music,
        "target": {"width": profile.width, "height": profile.height, "fps": profile.fps},
    }


def input_basis(inputs: dict) -> str:
    return prefixed_canonical_json_digest(inputs)


async def subprocess_in(directory: Path, args: list[str], env: dict[str, str], *, deadline_seconds: int) -> None:
    log = await asyncio.to_thread((directory / "engine.log").open, "ab")
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=directory,
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=os.name != "nt",
        )
        try:
            code = await asyncio.wait_for(process.wait(), timeout=deadline_seconds)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                try:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        if code:
            raise EditError("剪辑子任务执行失败")
    finally:
        await asyncio.to_thread(log.close)


def runtime_env(directory: Path) -> dict[str, str]:
    env = {
        key: val
        for key, val in os.environ.items()
        if key
        in (
            "PATH",
            "HOME",
            "TMPDIR",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "LANG",
            *_LIBRARY_ENV,
        )
    }
    env.update(OPENFILM_HOME=str(directory / ".film-home"), OPENFILM_NO_BROWSER="1", OPENFILM_NO_UPDATE_CHECK="1")
    return env


async def native(directory: Path, action: str) -> None:
    await subprocess_in(
        directory,
        ["node", str(HERE / "bridge.mjs"), str(await asyncio.to_thread(engine_path)), action],
        runtime_env(directory),
        deadline_seconds=2400,
    )


async def score(directory: Path, data: dict, observations: list[dict]) -> bool:
    destination = directory / "assets" / "score.wav"
    if await asyncio.to_thread(destination.is_file):
        return True
    command = os.environ.get("XM_EDIT_MUSIC_PYTHON")
    checkpoints = os.environ.get("XM_EDIT_MUSIC_CHECKPOINTS")
    if not command or not checkpoints or not data["music_prompt"]:
        return False
    instruction = directory / "score-request.json"
    await asyncio.to_thread(
        atomic_write_json,
        instruction,
        {
            "prompt": data["music_prompt"],
            "duration": max(12, min(180, sum(o["duration"] for o in observations))),
        },
    )
    try:
        await subprocess_in(
            directory,
            [command, str(HERE / "score.py"), checkpoints, str(instruction), str(destination)],
            runtime_env(directory),
            deadline_seconds=1500,
        )
    except (OSError, EditError, TimeoutError):
        logger.warning("Optional episode score failed", exc_info=True)
        return False
    return await asyncio.to_thread(destination.is_file)


class ShotDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(max_length=500)
    review: str = Field(max_length=500)


class EditDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shots: list[ShotDecision]
    music_prompt: str = Field(max_length=800)


def inspect_results(directory: Path, inputs: dict) -> list[dict]:
    return [load_json(directory / "inspection" / f"{shot['key']}.json") for shot in inputs["shots"]]


def load_skills(directory: Path) -> tuple[str, str]:
    skill_path = directory / "editing-skills.md"
    if not skill_path.is_file():
        skill_path.write_text(
            (engine_path() / "manual/film/SKILL.md").read_text(encoding="utf-8")
            + "\n\n"
            + (HERE / "drama-editing.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    return skill_path.read_text(encoding="utf-8"), sha256_file(skill_path)


async def cut_episode(directory: Path, inputs: dict, observations: list[dict], project: str, user: str) -> dict:
    generator = None
    skill, skill_digest = await asyncio.to_thread(load_skills, directory)
    story = [{"key": s["key"], "story": s["story"]} for s in inputs["shots"]]
    output: list[dict] = []
    music_prompt = ""
    for start in range(0, len(story), 4):
        end = min(start + 4, len(story))
        checkpoint = directory / f"decisions-{start}.json"
        received = directory / f"model-response-{start}.json"
        submitted = directory / f"model-submitted-{start}.json"
        checkpointed, responded, sent_request = await asyncio.to_thread(
            lambda *paths: tuple(path.is_file() for path in paths), checkpoint, received, submitted
        )
        if checkpointed:
            decision = EditDecision.model_validate(await asyncio.to_thread(load_json, checkpoint))
        elif responded:
            decision = parse_decision((await asyncio.to_thread(load_json, received))["text"])
        else:
            if sent_request:
                raise EditError("剪辑模型的请求结果待核对")
            sent = list(range(max(start - 1, 0), min(end + 1, len(story))))
            evidence = [
                {**story[i], "duration": observations[i]["duration"], "speech": observations[i]["speech"]} for i in sent
            ]
            prompt = json.dumps(
                {
                    "script": inputs["source_text"],
                    "storyboard": story,
                    "references": inputs["references"],
                    "evidence_in_image_order": evidence,
                    "decide_only": [s["key"] for s in story[start:end]],
                    "generate_music": inputs["music"] and start == 0,
                },
                ensure_ascii=False,
            )
            prompt = (
                "You are editing an episode with the OpenFilm workflow. Read the contact sheets and timed spoken words. "
                "Return each requested shot once, unchanged order, with source seconds start/end. Protect intelligible speech, "
                "narrative sound and action. Keep a full shot when evidence is uncertain. Report visible continuity problems "
                "in review, an empty string if none. Write the reasons and review in Simplified Chinese. If requested, "
                "write an instrumental music prompt for the whole episode in English.\n" + prompt
            )
            if generator is None:
                generator = await TextGenerator.create(
                    TextTaskType.EPISODE_EDIT,
                    project,
                    purpose=CallPurpose.GENERATION_TASK,
                    user_id=user,
                )
            await asyncio.to_thread(
                atomic_write_json,
                submitted,
                {
                    "model": generator.model,
                    "provider": generator.provider_id,
                    "skill_sha256": skill_digest,
                },
            )
            result = await generator.generate(
                TextGenerationRequest(
                    prompt=prompt,
                    system_prompt=skill,
                    response_schema=EditDecision,
                    preserve_raw_response=True,
                    images=[ImageInput(path=directory / observations[i]["frames"][0]) for i in sent],
                    max_output_tokens=4500,
                ),
                project,
                require_complete=True,
            )
            await asyncio.to_thread(atomic_write_json, received, {"text": result.text})
            decision = parse_decision(result.text)
        if [s.key for s in decision.shots] != [s["key"] for s in story[start:end]]:
            raise EditDecisionError("剪辑判断返回了不匹配的镜头")
        if not checkpointed:
            await asyncio.to_thread(atomic_write_json, checkpoint, decision.model_dump())
        if start == 0:
            music_prompt = decision.music_prompt
        output.extend(s.model_dump() for s in decision.shots)
    return {"shots": output, "music_prompt": music_prompt}


def parse_decision(raw: str) -> EditDecision:
    try:
        return EditDecision.model_validate_json(strip_json_code_fences(raw))
    except ValidationError as exc:
        raise EditDecisionError("剪辑判断结果的格式不正确") from exc


def recovery(directory: Path) -> str:
    """Continue only from known results, never resend an unacknowledged request."""
    for item in directory.glob("model-submitted-*.json"):
        response = directory / item.name.replace("model-submitted-", "model-response-")
        decision = directory / item.name.replace("model-submitted-", "decisions-")
        if not response.is_file():
            return "unconfirmed"
        if not decision.is_file():
            return "invalid"
    return "resume"


def caption_groups(words: list[dict], language: str) -> list[tuple[float, float, str]]:
    groups: list[list] = []
    compact = language in {"zh", "ja", "yue"}
    for word in words:
        token = str(word["token"])
        a, b = word["start"], word["end"]
        if not any(c.isalnum() for c in token) and not groups:
            continue
        if (
            not groups
            or a - groups[-1][1] > 0.55
            or (
                any(c.isalnum() for c in token)
                and (len(groups[-1][2]) >= (17 if compact else 37) or b - groups[-1][0] > 4.2)
            )
        ):
            groups.append([a, b, token.strip()])
        else:
            groups[-1][1] = max(groups[-1][1], b)
            groups[-1][2] += ("" if compact or not any(c.isalnum() for c in token) else " ") + token.strip()
    return [(a, b, text) for a, b, text in groups if b > a and any(c.isalnum() for c in text)]


def vtt_time(ms: int) -> str:
    minutes, rest = divmod(ms, 60_000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{rest // 1000:02d}.{rest % 1000:03d}"


def duck_score(directory: Path, duration: float, cues: list[tuple[int, int, str]]) -> float:
    """Bake only the ducking into a music copy; original audio and NLE music gain stay independent."""
    with wave.open(str(directory / "assets/score.wav"), "rb") as source:
        rate, channels = source.getframerate(), source.getnchannels()
        length = min(source.getnframes(), round(duration * rate))
        if source.getsampwidth() != 2 or length == 0:
            raise ValueError("Invalid score audio")
        pcm = np.frombuffer(source.readframes(length), dtype="<i2").reshape(-1, channels)
    span = len(pcm) / rate
    offset = duration - span
    envelope = np.ones(len(pcm), dtype=np.float32)
    attack, release, depth = 0.10, 0.35, 10 ** (-12 / 20)
    for start, end, _ in cues:
        a, b = start / 1000 - offset, end / 1000 - offset
        lo, hi = max(0, int((a - attack) * rate)), min(len(pcm), math.ceil((b + release) * rate))
        if lo >= hi:
            continue
        clock = np.arange(lo, hi) / rate
        ramp = np.clip(np.minimum((clock - a + attack) / attack, (b + release - clock) / release), 0, 1)
        gain = 1 - (1 - depth) * ramp**2 * (3 - 2 * ramp)
        np.minimum(envelope[lo:hi], gain, out=envelope[lo:hi])
    pending = directory / "assets/score-ducked.pending"
    with wave.open(str(pending), "wb") as track:
        track.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        track.writeframes(np.rint(pcm * envelope[:, None]).astype("<i2").tobytes())
    pending.replace(directory / "assets/score-ducked.wav")
    return span


def write_film(directory: Path, inputs: dict, data: dict, observations: list[dict], music: bool) -> dict:
    target = inputs["target"]
    fps = target["fps"]
    frame = 0
    video_tags = []
    cues: list[tuple[int, int, str]] = []
    reviews = []
    for shot, decision, observed in zip(inputs["shots"], data["shots"], observations, strict=True):
        duration = observed["duration"]
        speech = observed["speech"] or {}
        words = speech.get("words", [])
        spoken = [w for w in words if any(c.isalnum() for c in str(w["token"]))]
        begin, finish = float(decision["start"]), min(duration, float(decision["end"]))
        if (
            begin >= finish
            or finish - begin < 1 / fps
            or any(
                (w["start"] - 0.14 < begin and begin > 0) or (w["end"] + 0.12 > finish and finish < duration)
                for w in spoken
            )
        ):
            begin, finish = 0.0, duration
        start_frame = math.floor(Fraction(str(begin)) * fps)
        count = max(1, round(Fraction(str(finish)) * fps) - start_frame)
        at = frame / fps
        begin = start_frame / fps
        # Start and length use the output grid so the native NLE exporter and renderer place the same cuts.
        finish = (start_frame + count) / fps
        video_tags.append(
            f'<video id="{shot["key"]}" src="{shot["file"]}#t={begin:.9f},{finish:.9f}" '
            f'at="{at:.9f}" volume="1" style="object-fit:cover"></video>'
        )
        if decision["review"]:
            reviews.append({"at": at, "unit": shot["unit"], "text": decision["review"]})
        for low, high, text in caption_groups(words, speech.get("language", "")):
            if high <= begin or low >= finish:
                continue
            ms = max(round((at + max(begin, low) - begin) * 1000), cues[-1][1] if cues else 0)
            end_ms = round((at + min(finish, high) - begin) * 1000)
            if end_ms > ms:
                cues.append((ms, end_ms, text))
        frame += count
    vtt = "WEBVTT\n\nSTYLE\n::cue { font-size: 5.7vh; }\n\n"
    for index, (a, b, text) in enumerate(cues, 1):
        vtt += f"{index}\n{vtt_time(a)} --> {vtt_time(b)}\n{html.escape(text)}\n\n"
    (directory / "subtitles.vtt").write_text(vtt, encoding="utf-8")
    captions = (
        '<section id="subtitles"><track kind="captions" src="subtitles.vtt" label="对白"></section>' if cues else ""
    )
    bgm = ""
    if music:
        try:
            use = duck_score(directory, frame / fps, cues)
        except (OSError, EOFError, ValueError, wave.Error):
            logger.warning("Unable to prepare optional episode score", exc_info=True)
        else:
            at = frame / fps - use
            fade = min(1, use / 2)
            attributes = html.escape(json.dumps([{"fade": [fade, fade]}]), quote=True)
            bgm = (
                f'<section id="music"><audio src="assets/score-ducked.wav#t=0,{use:.9f}" at="{at:.9f}" '
                f'volume="0.12" overrides="{attributes}"></audio></section>'
            )
    target_width, target_height = target["width"], target["height"]
    film = f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width={target_width}, height={target_height}"></head><body>{captions}<section id="picture">{"".join(video_tags)}</section>{bgm}</body></html>'
    temporary = directory / "film.pending"
    temporary.write_text(film, encoding="utf-8")
    temporary.replace(directory / "film.html")
    return {"review": reviews, "music_failed": inputs["music"] and not bgm}


def prepare_sources(directory: Path, root: Path, inputs: dict) -> None:
    (directory / "assets").mkdir(exist_ok=True)
    for shot in inputs["shots"]:
        dest = directory / shot["file"]
        if dest.is_file() and sha256_file(dest) == shot["hash"]:
            continue
        source = (root / shot["source"]).resolve()
        if not source.is_relative_to(root.resolve()) or sha256_file(source) != shot["hash"]:
            raise EditError(f"片段 {shot['unit']} 的素材版本已变化")
        shutil.copyfile(source, dest)
        if sha256_file(dest) != shot["hash"]:
            raise EditError(f"片段 {shot['unit']} 的素材复制失败")


def native_draft(directory: Path) -> Path:
    drafts = sorted((directory / "draft").glob("*/draft_info.json"))
    if len(drafts) != 1:
        raise EditError("剪映草稿文件未完整输出")
    draft = drafts[0].parent
    info = load_json(drafts[0])
    meta = load_json(draft / "draft_meta_info.json")
    if info["id"] != meta["draft_id"] or info["duration"] <= 0 or not info.get("tracks"):
        raise EditError("剪映草稿结构无效")
    for kind in ("videos", "audios"):
        for item in info["materials"].get(kind, []):
            path = item["path"]
            prefix = f"{_DRAFT_MARKER}/media/"
            if not path.startswith(prefix):
                raise EditError("剪映草稿包含外部素材引用")
            name = path.removeprefix(prefix)
            if name != Path(name).name or not (draft / "media" / name).is_file():
                raise EditError("剪映草稿缺少素材")
    return draft


async def ensure_inspection(directory: Path, inputs: dict) -> list[dict]:
    if not await asyncio.to_thread((directory / "inspection.done").is_file):
        await native(directory, "inspect")
        await asyncio.to_thread((directory / "inspection.done").touch)
    return await asyncio.to_thread(inspect_results, directory, inputs)


async def execute(projects: ProjectManager, project: str, task: dict, directory: Path) -> dict:
    inputs = await asyncio.to_thread(load_json, directory / "input.json")
    root = await asyncio.to_thread(projects.get_project_path, project)
    await asyncio.to_thread(prepare_sources, directory, root, inputs)
    decision_path = directory / "edit.json"
    if await asyncio.to_thread(decision_path.is_file):
        data = await asyncio.to_thread(load_json, decision_path)
    else:
        observations = await ensure_inspection(directory, inputs)
        data = await cut_episode(directory, inputs, observations, project, task["user_id"])
        await asyncio.to_thread(atomic_write_json, decision_path, data)
    if not await asyncio.to_thread((directory / "review.json").is_file):
        observations = await ensure_inspection(directory, inputs)
        has_music = inputs["music"] and await score(directory, data, observations)
        review = await asyncio.to_thread(write_film, directory, inputs, data, observations, has_music)
        await asyncio.to_thread(atomic_write_json, directory / "review.json", review)
    if not await asyncio.to_thread((directory / "delivery.done").is_file):
        await native(directory, "deliver")
    draft = await asyncio.to_thread(native_draft, directory)
    mp4 = directory / "final.mp4"
    probe = await probe_media(mp4)
    if not probe.duration_seconds or not any(s.kind == "video" and s.packet_count for s in probe.streams):
        raise EditError("输出视频无法播放")
    draft_duration = (await asyncio.to_thread(load_json, draft / "draft_info.json"))["duration"] / 1_000_000
    if abs(draft_duration - probe.duration_seconds) > 2 / inputs["target"]["fps"]:
        raise EditError("成片与剪映草稿时长不一致")
    await asyncio.to_thread((directory / "delivery.done").touch)
    return {
        "sha256": await asyncio.to_thread(sha256_file, mp4),
        "draft": draft.relative_to(directory).as_posix(),
        **await asyncio.to_thread(load_json, directory / "review.json"),
    }


def publish_video(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if sha256_file(target) != sha256_file(source):
            raise EditError("已发布成片的内容不一致")
        return
    pending = target.with_suffix(".pending")
    shutil.copyfile(source, pending)
    pending.replace(target)
