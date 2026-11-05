"""参考生视频草稿的最终提示词，使用当前请求投影的实发参考图与能力。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from lib.artifacts.video_visual_provenance import resolve_video_aspect_ratio
from lib.generation.batch_admission import DURATION_CONFIRMATION_CODE
from lib.i18n import render_message
from lib.i18n.display_names import DisplayNames
from lib.script.reference_video.prompt_render import render_video_unit_prompt, resolve_reference_audio_paths
from lib.script.reference_video.request_projection import (
    ReferenceUnitRequestProjection,
    resolve_reference_assets,
    unit_reference_declarations,
)
from lib.script.reference_video.text_parser import speech_speaker_references
from lib.script.reference_video.voice_settings import VoiceRenderSettings


def render_reference_prompt_preview(
    *,
    project: dict,
    unit: dict,
    project_path: Path,
    projection: ReferenceUnitRequestProjection,
    translate: Callable[..., str],
    names: DisplayNames,
) -> dict[str, Any]:
    """只读渲染；时长待确认不妨碍查看文本，其他投影阻断项显式显示为不可用。

    与分镜预览同一口径：``text`` 与 ``unavailable`` 恰有一个非 ``None``，``warnings`` 只随文本出现。
    """
    result: dict[str, Any] = {
        "text": None,
        "unavailable": None,
        "is_text_form": True,
        "references": [],
        "warnings": [],
    }
    logical = unit_reference_declarations(project, unit)
    candidates = resolve_reference_assets(project, project_path, unit)
    result["used_assets"] = [
        {
            "type": ref.type, "name": ref.name,
            "paths": [a.path.relative_to(project_path).as_posix() for a in candidates if a.reference == ref],
            "request_indices": [i + 1 for i, a in enumerate(projection.request_assets) if a.reference == ref],
            "status": "sent" if any(a.reference == ref for a in projection.request_assets)
                else "excluded" if any(a.reference == ref for a in candidates) else "missing",
        }
        for ref in logical
    ]
    visual_names = {ref.name for ref in logical}
    result["voice_only"] = [name for name in speech_speaker_references(str(unit.get("text") or "")) if name not in visual_names]
    blockers = [problem for problem in projection.blocking_problems if problem.code != DURATION_CONFIRMATION_CODE]
    if blockers:
        result["unavailable"] = render_message(blockers[0].code, blockers[0].parameters(), translate, names)
        return result
    request_facts = projection.request_facts
    if request_facts is None:
        result["unavailable"] = translate(
            "reference_capability_unavailable", capability=projection.hydrated_generation_type
        )
        return result
    if not str(unit.get("text") or "").strip():
        result["unavailable"] = translate("reference_prompt_preview_missing")
        return result

    rendered = render_video_unit_prompt(
        unit,
        project,
        VoiceRenderSettings.from_request_facts(
            request_facts, audio_ready=resolve_reference_audio_paths(project, project_path)
        ),
        request_references=[asset.reference for asset in projection.request_assets],
    )
    result["text"] = rendered.prompt
    result["declarations"] = rendered.declarations
    result["creative_text"] = rendered.creative_text
    result["references"] = [
        {
            "type": asset.reference.type,
            "name": asset.reference.name,
            "path": asset.path.relative_to(project_path).as_posix(),
        }
        for asset in projection.request_assets
    ]
    audio_ready = resolve_reference_audio_paths(project, project_path)
    result["audio_references"] = [
        {"name": speaker, "path": audio_ready[speaker].relative_to(project_path).as_posix()}
        for speaker in rendered.audio_speakers
    ]
    result["parameters"] = {
        "provider": request_facts.provider_id,
        "model": request_facts.model_id,
        "resolution": request_facts.resolution,
        "aspect_ratio": resolve_video_aspect_ratio(project),
        "duration_seconds": projection.request_duration.seconds if projection.request_duration else None,
        "generate_audio": request_facts.requested_generate_audio,
    }
    result["system_message"] = None
    result["warnings"] = [
        *(render_message(problem.code, problem.parameters(), translate, names) for problem in projection.problems),
        *(render_message(warning["key"], warning["params"], translate, names) for warning in rendered.warnings),
    ]
    return result
