"""Validation helpers for versioned reference-video prompt policies."""

from __future__ import annotations

import re
from collections.abc import Sequence

from lib.script.prompt_policy import prompt_content_validation_enabled
from lib.script.reference_video.draft_validation import DraftViolation
from lib.script.reference_video.text_parser import extract_mentions

POLICY_SECTIONS = ("本组设定", "开场承接", "分段时间轴", "组尾交接", "必要限制")
_SPEECH_MARK = re.compile(r"\{[^{}]*\}")
_MEASURED_SIZE = re.compile(
    r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百半]+)\s*(?:毫米|厘米|公分|米|英寸|英尺|寸|尺|mm|cm|m)",
    re.I,
)
_NARRATIVE_RELATIONS = frozenset({"首组", "连续", "换场", "跳时", "非连续"})
INHERIT_HANDOFF = "（由系统填入上一组尾态）"


def _screen_sides(text: str) -> dict[str, str]:
    """Only explicit screen positions; facing right is not being on the right."""
    result: dict[str, str] = {}
    pattern = r"(?:@\[([^\]]+)\]|<([^<>]+)>)\s*(?:仍|依然)?\s*(?:位于|站在|处于|在|居)\s*(?:画面)?\s*(左|右)(?:侧|边|方)?"
    for match in re.finditer(pattern, text):
        result[(match[1] or match[2]).split("/视角-")[0]] = match[3]
    return result


def _first_segment(text: str) -> str:
    timeline = text.partition("【分段时间轴】")[2].partition("【组尾交接】")[0]
    parts = re.split(r"【\d+(?:\.\d+)?-\d+(?:\.\d+)?s】", timeline)
    return parts[1] if len(parts) > 1 else ""


def compile_policy_handoffs(authored: Sequence[dict], context: Sequence[dict]) -> list[dict]:
    """Fill only model-owned inheritance slots, in episode order.

    Persisted/manual bodies are never rewritten. An explicit contradictory starting
    screen position stays a repairable violation rather than being covered by a copy.
    """
    selected = {str(item.get("unit_id")): dict(item) for item in authored}
    effective: list[dict] = []
    for original in context:
        uid = str(original.get("unit_id"))
        item = selected.get(uid, original)
        if uid in selected and item.get("prompt_policy_version") == "drama_detail_v1" and effective:
            text = str(item.get("text") or "")
            if narrative_relation(text) == "连续":
                previous = effective[-1]
                tail = section_body(str(previous.get("generation_prompt") or previous.get("text") or ""), "组尾交接")
                opening = section_body(text, "开场承接")
                if tail and previous.get("prompt_policy_version") == "drama_detail_v1":
                    expected = _screen_sides(tail)
                    actual = _screen_sides(opening + "\n" + _first_segment(text))
                    if prompt_content_validation_enabled() and any(
                        name in actual and actual[name] != side for name, side in expected.items()
                    ):
                        raise DraftViolation(
                            f"unit {uid}: 开场或首段人物左右位置与前组交接不一致；请修订实际镜头动作",
                            code="prompt_policy_handoff_mismatch", label=f"unit {uid}",
                        )
                    # Only the explicit model-owned slot is writable. Matching
                    # sides alone cannot prove matching contact or held objects.
                    if opening == INHERIT_HANDOFF and not item.get("generation_prompt"):
                        marker = "【开场承接】"
                        start = text.index(marker) + len(marker)
                        end = text.find("【", start)
                        if end >= 0:
                            item["text"] = text[:start] + "\n" + tail + "\n" + text[end:]
            selected[uid] = item
        effective.append(item)
    return [selected[str(item.get("unit_id"))] for item in authored]


def section_body(text: str, name: str) -> str:
    lines = text.splitlines()
    marker = f"【{name}】"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == marker) + 1
    except StopIteration:
        return ""
    end = next((i for i in range(start, len(lines)) if lines[i].strip().startswith("【")), len(lines))
    return "\n".join(lines[start:end]).strip()


def narrative_relation(text: str) -> str | None:
    """Read the single explicit relation declaration from the fixed setup section."""
    declarations = [
        line.partition("：")[2].strip().removesuffix("。").removesuffix(".").strip()
        for line in section_body(text, "本组设定").splitlines()
        if line.startswith("叙事关系：")
    ]
    if len(declarations) != 1 or declarations[0] not in _NARRATIVE_RELATIONS:
        return None
    return declarations[0]


def validate_policy_text(
    unit_id: str, text: str, *, required_source_text: str | None = None,
    scene_names: Sequence[str] = (),
) -> list[DraftViolation]:
    if not prompt_content_validation_enabled():
        return []
    headings = [line.strip()[1:-1] for line in text.splitlines() if line.strip().startswith("【") and line.strip().endswith("】")]
    missing = [name for name in POLICY_SECTIONS if headings.count(name) != 1]
    issues: list[DraftViolation] = []
    if missing:
        issues.append(
            DraftViolation(
                f"unit {unit_id}: 短剧提示词分区缺失或重复：{'、'.join(missing)}",
                code="prompt_policy_structure",
                label=f"unit {unit_id}",
            )
        )
    # Existing v1 bodies predate this declaration. Validate it when supplied,
    # without making old saved prompts invalid solely for omitting the line.
    if any(line.startswith("叙事关系：") for line in section_body(text, "本组设定").splitlines()) and narrative_relation(text) is None:
        issues.append(
            DraftViolation(
                f"unit {unit_id}: 本组设定必须且只能声明一项叙事关系：首组、连续、换场、跳时或非连续",
                code="prompt_policy_narrative_relation",
                label=f"unit {unit_id}",
            )
        )
    for name in ("开场承接", "组尾交接"):
        body = section_body(text, name)
        if extract_mentions(body) or _SPEECH_MARK.search(body):
            issues.append(
                DraftViolation(
                    f"unit {unit_id}: {name} 只能写纯状态文字，不得包含图片引用或发声记号",
                    code="prompt_policy_state_binding",
                    label=f"unit {unit_id}",
                )
            )
    if "Style:" in text or "Avoid:" in text:
        issues.append(
            DraftViolation(
                f"unit {unit_id}: 正文不得重复生成 Style/Avoid，固定文本由系统渲染",
                code="prompt_policy_style_duplicate",
                label=f"unit {unit_id}",
            )
        )
    if INHERIT_HANDOFF in text:
        issues.append(DraftViolation(
            f"unit {unit_id}: 未取得上一有效单元尾态，不能保留交接占位符",
            code="prompt_policy_handoff_mismatch", label=f"unit {unit_id}",
        ))
    if required_source_text is not None:
        # A setup-only list is not evidence that an asset participates in the shot.
        # The scene establishes the setting; other assets belong in actual timed content.
        timeline = text.partition("【分段时间轴】")[2].partition("【组尾交接】")[0]
        from lib.references.reference_catalog import split_derivative_reference

        def identity(name: str) -> str:
            base, derivative = split_derivative_reference(name)
            return base if derivative.startswith("视角-") else name

        present = {identity(name) for name in extract_mentions(timeline)}
        present.update(set(extract_mentions(section_body(text, "本组设定"))) & set(scene_names))
        missing = list(dict.fromkeys(name for name in extract_mentions(required_source_text) if identity(name) not in present))
        if missing:
            issues.append(
                DraftViolation(
                    f"unit {unit_id}: 视频正文遗漏已确认资产引用 {', '.join(f'@[{name}]' for name in missing)}",
                    code="prompt_policy_asset_reference_missing",
                    label=f"unit {unit_id}",
                )
            )
    return issues


def validate_policy_asset_descriptions(raw_assets: object) -> list[DraftViolation]:
    """Reject unfinished designs, only for newly opted-in registered assets.

    This is a completion check, not an aesthetic score. Broad visual vocabulary
    catches skeletal character descriptions without prescribing one face or costume.
    """
    if not prompt_content_validation_enabled() or not isinstance(raw_assets, list):
        return []
    issues: list[DraftViolation] = []
    for index, asset in enumerate(raw_assets):
        if not isinstance(asset, dict) or asset.get("decision") != "register":
            continue
        description = str(asset.get("description") or "")
        reason = str(asset.get("reason") or "")
        label = f"new_assets[{index}]"
        if re.search(r"待确认|未交代|未说明|待补充|待设计|未知|TBD", description, re.I):
            issues.append(DraftViolation(
                f"{label}（{asset.get('name')}）外观设计仍有占位内容；请完成设计，在 reason 标明设计补全范围",
                code="prompt_policy_asset_design_incomplete", label=label,
            ))
        if asset.get("type") == "character":
            # A narrative character need not have human anatomy. Check visible
            # design facets (including nonhuman equivalents), not species names
            # or provenance words in reason. This remains a coarse completion
            # check; detailed art direction belongs in the generation prompt.
            facets = (
                r"岁|少年|青年|中年|老年|幼年|成年|年迈|轮廓|球状|" + _MEASURED_SIZE.pattern,
                r"身|肩|腰|体态|体型|躯干|比例|修长|纤细|健硕|壮实|四足|四肢|盘成|内部|层|环绕",
                r"脸|面容|眉|眼|鼻|唇|颌|颧|五官|头|吻|喙|獠牙|传感器|核心",
                r"肤|皮|肌理|鳞|毛|羽|金属|陶瓷|黏液|透明|薄雾|外壳",
                r"发|髮|秃|光头|毛|冠|触角|耳|尾|翼|羽|鳍|花纹|纹路|触须|边缘",
                r"衣|袍|衫|裙|裤|服|甲|裳|裸|围|披|装束|黑|白|灰|红|蓝|绿|青|紫|银|金色|渐变|磨砂",
            )
            if sum(bool(re.search(f, description, re.I)) for f in facets) < 5:
                issues.append(DraftViolation(
                    f"{label}（{asset.get('name')}）角色设计未展开；请按实际形态补足尺度或年龄、体态结构、头部或核心特征、表面材质、附属结构与配色等适用设计；非人角色不强求人类发型或服装",
                    code="prompt_policy_character_design_incomplete", label=label,
                ))
        if asset.get("type") != "prop":
            continue
        # Nonphysical effects have no stable measurable body; a pending decision
        # about an ordinary solid prop is not an exemption.
        if re.search(r"无固定形体|非实体光效|非实体能量", reason):
            continue
        measures = len(_MEASURED_SIZE.findall(description))
        shape = str(asset.get("name") or "")
        required = 3 if re.search(r"箱|柜|盒", shape) else 2 if re.search(r"针|杆|棒|瓶|罐|杯", shape) else 1
        if re.search(r"\d+(?:\.\d+)?\s*[×xX*]\s*\d+(?:\.\d+)?\s*[×xX*]\s*\d+(?:\.\d+)?\s*(?:厘米|毫米|公分|cm|mm)", description):
            measures = max(measures, 3)
        if measures < required:
            issues.append(
                DraftViolation(
                    f"{label}（{asset.get('name', '未命名道具')}）缺少可执行尺寸；"
                    f"请给出适合形制的{required}项数值和单位（箱体长宽高；针杆总长与主体粗细），人体参照只作辅助",
                    code="prompt_policy_prop_scale_missing",
                    label=label,
                )
            )
    return issues


def validate_policy_handoffs(authored: Sequence[dict], context: Sequence[dict]) -> list[DraftViolation]:
    """Compare targeted openings with their immediate opted-in predecessor tail."""
    if not prompt_content_validation_enabled():
        return []
    positions = {str(item.get("unit_id")): i for i, item in enumerate(context) if isinstance(item, dict)}
    authored_by_id = {str(item.get("unit_id")): item for item in authored}
    effective = [
        authored_by_id.get(str(item.get("unit_id")), item)
        for item in context
        if isinstance(item, dict)
    ]
    violations: list[DraftViolation] = []
    targets = set(authored_by_id)
    for item in effective:
        uid = str(item.get("unit_id"))
        index = positions.get(uid)
        if uid not in targets and not (
            index and str(effective[index - 1].get("unit_id")) in targets and not item.get("pending_authoring")
        ):
            continue
        if index is None or item.get("prompt_policy_version") != "drama_detail_v1":
            continue
        text = str(item.get("generation_prompt") or item.get("text") or "")
        relation = narrative_relation(text)
        if index == 0:
            if relation is not None and relation != "首组":
                violations.append(
                    DraftViolation(
                        f"unit {item.get('unit_id')}: 首个单元必须声明叙事关系为首组",
                        code="prompt_policy_narrative_relation",
                        label=f"unit {item.get('unit_id')}",
                    )
                )
            continue
        if relation == "首组":
            violations.append(
                DraftViolation(
                    f"unit {item.get('unit_id')}: 非首个单元不能声明叙事关系为首组",
                    code="prompt_policy_narrative_relation",
                    label=f"unit {item.get('unit_id')}",
                )
            )
            continue
        if relation in {"换场", "跳时", "非连续"}:
            continue
        predecessor = effective[index - 1]
        if predecessor.get("prompt_policy_version") != "drama_detail_v1":
            continue
        opening = section_body(text, "开场承接")
        handoff = section_body(str(predecessor.get("generation_prompt") or predecessor.get("text") or ""), "组尾交接")
        normalized_opening = " ".join(opening.split())
        normalized_handoff = " ".join(handoff.split())
        expected_sides = _screen_sides(handoff)
        actual_sides = _screen_sides(_first_segment(text))
        contradicts = any(name in actual_sides and actual_sides[name] != side for name, side in expected_sides.items())
        if not handoff or normalized_opening != normalized_handoff or contradicts:
            violations.append(
                DraftViolation(
                    f"unit {item.get('unit_id')}: 开场承接与前一单元 {predecessor.get('unit_id')} 的组尾交接不一致",
                    code="prompt_policy_handoff_mismatch",
                    label=f"unit {item.get('unit_id')}",
                )
            )
    return violations
