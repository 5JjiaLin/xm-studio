"""Verify the hand-authored Test1 target through production validators/renderers.

Run from the repository root: .venv/bin/python -m scripts.verify_workflow_stability
No providers, production projects or user configuration are accessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZipFile

from lib import PROJECT_ROOT
from lib.artifacts.artifact_manifest import InMemoryArtifactManifestAdapter
from lib.artifacts.generation_input import ManifestInputObservation, asset_sheet_input
from lib.script.reference_video.draft_validation import (
    out_of_order_source_anchor_indices,
    validate_dialogue_load,
    validate_source_text_anchor,
    validate_unit_text,
)
from lib.script.reference_video.prompt_policy_validation import (
    compile_policy_handoffs,
    validate_policy_asset_descriptions,
    validate_policy_handoffs,
    validate_policy_text,
)
from lib.script.reference_video.prompt_render import render_unit_prompt
from lib.script.reference_video.screenplay_coverage import screenplay_dialogue, validate_screenplay_coverage
from lib.script.reference_video.temporal_validation import validate_timeline
from lib.script.reference_video.voice_settings import VoiceRenderSettings


def require_clean(issues: object) -> None:
    if issues:
        raise AssertionError(str(issues))


def verify(source_docx: Path | None = None) -> None:
    fixture = json.loads(
        (PROJECT_ROOT / "docs/production-mvp/fixtures/workflow-stability-test1-golden.json").read_text()
    )
    source = fixture["source_text"]
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    assert source_hash == fixture["source_sha256"]
    if source_docx:
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with ZipFile(source_docx) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
        paragraphs = [
            "".join(t.text or "" for t in p.findall(".//w:t", ns)) for p in document.findall(".//w:body/w:p", ns)
        ]
        assert "\n".join(p for p in paragraphs if p.strip()) == source

    project = {
        "style": "真人电视剧风格",
        "style_description": "精品短剧画风，大师级构图",
        "content_mode": "drama",
        "generation_mode": "reference_video",
        "characters": {},
        "scenes": {},
        "props": {},
    }
    buckets = {"character": "characters", "scene": "scenes", "prop": "props"}
    for asset in fixture["new_assets"]:
        project[buckets[asset["type"]]][asset["name"]] = {
            "description": asset["description"],
            "aliases": asset["aliases"],
        }
    require_clean(validate_policy_asset_descriptions(fixture["new_assets"]))
    units = compile_policy_handoffs(fixture["units"], fixture["units"])
    aliases = {key.casefold(): value for key, value in fixture["speaker_aliases"].items()}
    require_clean(validate_screenplay_coverage(units, source, speaker_aliases=aliases))
    require_clean(out_of_order_source_anchor_indices([u["source_text"] for u in units], source))
    require_clean(validate_policy_handoffs(units, units))
    output = PROJECT_ROOT / "docs/production-mvp/evidence/workflow-stability"
    output.mkdir(parents=True, exist_ok=True)
    notice = fixture["kind"] + "。本文件由正式校验器和渲染器离线生成，不是供应商实发请求。"
    assets_md = ["# 资产预期设计的渲染结果", notice]
    asset_checks = []
    with TemporaryDirectory(prefix="xm-stability-target-") as temp:
        observation = ManifestInputObservation(Path(temp), InMemoryArtifactManifestAdapter())
        for asset in fixture["new_assets"]:
            item = asset_sheet_input(project, asset_type=asset["type"], name=asset["name"], observation=observation)
            preview = item.render(max_reference_images=1, model="offline-target")
            frozen = item.freeze(max_reference_images=1, model="offline-target", bind_claims=lambda claims, digests: ())
            assert not item.references  # Text-only target: no fabricated image claims.
            assert preview.prompt == frozen.prompt
            if asset["type"] == "character":
                assert "四个" in preview.prompt
                assert "90°" in preview.prompt
            assets_md.append(f"## {asset['name']}\n\n{preview.prompt}\n\n设计依据：{asset['reason']}")
            asset_checks.append({"name": asset["name"], "preview_equals_frozen": True})
    video_md = ["# 视频预期正文及实际渲染结果", notice]
    unit_checks = []
    for unit, gold in zip(units, fixture["event_gold"], strict=True):
        uid, text, duration = unit["unit_id"], unit["text"], unit["duration_seconds"]
        validate_source_text_anchor(uid, unit["source_text"], source)
        require_clean(validate_policy_text(uid, text))
        references = validate_unit_text(uid, text, project, max_refs=5)
        assert {r.name for r in references} == set(gold["expected_assets"]), uid
        required = " ".join(f"@[{name}]" for name in gold["expected_assets"])
        require_clean(validate_policy_text(uid, text, required_source_text=required, scene_names=["黑色石殿"]))
        validate_dialogue_load(uid, text, duration, "en")
        _segments, issues = validate_timeline(text, duration, required=True, language="en")
        require_clean(issues)
        rendered = render_unit_prompt(
            text, project, references, VoiceRenderSettings(), prompt_policy_version="drama_detail_v1"
        )
        assert rendered.prompt.count("Style:") == rendered.prompt.count("Avoid:") == 1
        unit_checks.append(
            {
                "unit_id": uid,
                "duration_seconds": duration,
                "assets": [r.name for r in references],
                "events_for_review": gold["events"],
            }
        )
        video_md.append(
            f"## {uid} / {duration}s\n\n待核对事件：{'；'.join(gold['events'])}\n\n编辑正文：\n\n{text}\n\n渲染结果：\n\n{rendered.prompt}"
        )
    result = {
        "notice": notice,
        "source_sha256": source_hash,
        "docx_checked": source_docx is not None,
        "source_anchors_and_order": True,
        "screenplay_dialogue_and_speakers": screenplay_dialogue(source),
        "continuous_handoffs": True,
        "asset_checks": asset_checks,
        "units": unit_checks,
        "real_model_calls": 0,
        "semantic_and_media_quality": "人工目标供审阅；未调用真实模型或生成媒体",
    }
    (output / "assets-rendered.md").write_text("\n\n".join(assets_md) + "\n")
    (output / "videos-rendered.md").write_text("\n\n".join(video_md) + "\n")
    (output / "checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        f"离线目标通过：{len(asset_checks)}项资产、{len(units)}组、{len(screenplay_dialogue(source))}段对白；真实模型调用0次。"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-docx", type=Path)
    verify(parser.parse_args().source_docx)
