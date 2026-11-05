"""F1～F4 离线样例：真实 builder/校验器/renderer，不调用供应商、不修改项目。

从项目根运行 ``uv run --no-sync python -m scripts.verify_reference_video_framework``。
输出仅写入 docs/production-mvp/evidence/f1-f4；样例为人工摘编，非模型结果。
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import PROJECT_ROOT
from lib.prompts.prompt_builders_reference import build_reference_video_prompt
from lib.script.reference_video.draft_validation import (
    assert_dialogue_preserved,
    validate_dialogue_load,
    validate_unit_text,
)
from lib.script.reference_video.prompt_render import render_unit_prompt
from lib.script.reference_video.text_parser import derive_references_from_text
from lib.script.reference_video.voice_settings import VoiceRenderSettings


def verify(output_dir: Path) -> None:
    sample = json.loads((output_dir / "sample.json").read_text(encoding="utf-8"))
    project, units, texts = sample["project"], sample["units"], sample["authored_texts"]
    caps = sample["capabilities"]
    settings = VoiceRenderSettings(
        voice_consistency=caps["voice_consistency"],
        max_reference_audio=caps["max_reference_audio"],
        audio_ready=set(project["characters"]),
    )
    selected = [units[1], units[3]]
    prompt = build_reference_video_prompt(
        project_overview={},
        style=project["style"],
        style_description=project["style_description"],
        characters=project["characters"],
        scenes=project["scenes"],
        props=project["props"],
        script_plan_units=selected,
        context_units=units,
        max_refs=caps["max_reference_images"],
        episode=1,
    )
    (output_dir / "new-authoring-input.md").write_text(prompt + "\n", encoding="utf-8")
    old_projection = "\n\n".join(
        f"#### unit {i}（时长 {unit['duration_seconds']}s）\n{unit['text']}" for i, unit in enumerate(selected, start=1)
    )
    (output_dir / "old-target-projection.md").write_text(
        "旧输入机制的目标区投影重建，不是历史实发请求：\n\n" + old_projection + "\n", encoding="utf-8"
    )
    rows = []
    rendered_blocks = ["# 人工目标输出的实际渲染结果\n\n非模型实际输出；音频就绪为离线固定输入。"]
    for unit, text in zip(units, texts, strict=True):
        label = unit["unit_id"]
        if unit["duration_seconds"] not in caps["supported_durations"]:
            raise AssertionError(f"{label}: 非法样例时长")
        assert_dialogue_preserved(label, unit["text"], text)
        refs = validate_unit_text(label, text, project, max_refs=caps["max_reference_images"])
        validate_dialogue_load(label, text, unit["duration_seconds"], "en")
        default = render_unit_prompt(text, project, refs, settings)
        manual = render_unit_prompt(
            text, project, refs, settings, creative_text="人工目标：克制近景；风格及限制由人工维护。"
        )
        if default.declarations != manual.declarations or default.audio_speakers != manual.audio_speakers:
            raise AssertionError(f"{label}: 人工覆盖改变附件声明")
        rows.append(
            {
                "unit_id": label,
                "duration_seconds": unit["duration_seconds"],
                "images": [ref.name for ref in refs],
                "audio_speakers": default.audio_speakers,
                "dialogue_preserved": True,
                "manual_declarations_equal": True,
            }
        )
        rendered_blocks.append(
            f"## {label} / {unit['duration_seconds']}s\n\n{default.prompt}\n\n人工覆盖预览：\n\n{manual.prompt}"
        )
    if ["银针" in row["images"] for row in rows] != [False, True, False, False, True]:
        raise AssertionError("不可见银针被错误加入中间组附件")
    offscreen = "@[埃利亚斯]面部近景，固定机位。\n@[吉迪恩]{Come here.}"
    refs, missing = derive_references_from_text(offscreen, project)
    rendered = render_unit_prompt(offscreen, project, refs, settings)
    if missing or [ref.name for ref in refs] != ["埃利亚斯"] or rendered.audio_speakers != ["吉迪恩"]:
        raise AssertionError("纯说话人位改变了图片附件")
    silent = render_unit_prompt(offscreen, project, refs, VoiceRenderSettings(requested_generate_audio=False))
    if silent.audio_speakers or "@音频" in silent.prompt:
        raise AssertionError("无声路径注入音频")
    narration = render_unit_prompt("@[黑石殿]空镜。\n{后期旁白测试}", project, [], settings)
    if "后期旁白测试" in narration.prompt:
        raise AssertionError("裸旁白进入视频请求")
    (output_dir / "rendered-targets.md").write_text("\n\n".join(rendered_blocks) + "\n", encoding="utf-8")
    result = {
        "notice": sample["notice"],
        "units": rows,
        "offscreen_voice_only": True,
        "silent_path": True,
        "narration_removed": True,
    }
    (output_dir / "checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("5 组对白/档位/引用/人工覆盖验证通过；画外发声、无声、后期旁白验证通过。")


if __name__ == "__main__":
    verify(PROJECT_ROOT / "docs/production-mvp/evidence/f1-f4")
