"""Manual clip edits: validity and clip-relative subtitle placement, shared by UI and render."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from lib.edit_timeline.model import EditClip
from lib.edit_timeline.sources import UnitMedia


def edit_issues(clips: Sequence[EditClip], media: Mapping[str, UnitMedia]) -> list[dict]:
    issues = []
    for clip in clips:
        if not clip.included:
            continue
        current = media.get(clip.unit_id)
        version = current.video_version if current else None
        duration = current.video_duration_us if current else None
        if clip.trim and (clip.trim.basis_version != version or duration is None or clip.trim.out_us > duration):
            issues.append(
                {
                    "clip_id": clip.id,
                    "field": "trim",
                    "message": "裁剪所依据的视频版本或时长已变化，请重新确认入点和出点",
                }
            )
        low = clip.trim.in_us if clip.trim else 0
        high = clip.trim.out_us if clip.trim else duration
        issues.extend(
            {
                "clip_id": clip.id,
                "subtitle_id": cue.id,
                "field": "subtitles",
                "message": "字幕超出裁剪范围或视频版本已变化，请调整时间并确认",
            }
            for cue in clip.subtitles or ()
            if cue.basis_version != version or high is None or cue.start_us < low or cue.end_us > high
        )
    return issues
