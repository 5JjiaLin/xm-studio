"""Conservative adoption metrics; missing evidence never counts as success."""

from __future__ import annotations

import difflib
import json
from collections import Counter, defaultdict
from contextlib import suppress

from lib.production_feedback.objects import objects


def summarize(events: list[dict], *, grouped: bool = True, evidence_events: list[dict] | None = None) -> dict:
    evidence = events if evidence_events is None else evidence_events
    revisions = defaultdict(list)
    reviews = defaultdict(list)
    reasons = Counter()
    fields = Counter()
    replacements = Counter()
    props = Counter()
    proposed_props, skipped_props, added_props = set(), set(), set()
    unknown_objects = set()
    unknown_events = []
    for event in evidence:
        if event["event_type"] != "unknown":
            continue
        scope = (event["installation_id"], event["project"], event.get("project_chain_id"))
        affected = {
            (*scope, resource)
            for value in (event.get("before"), event.get("after"))
            for resource in objects(event["resource"], value)
        }
        unknown_objects.update(affected)
        unknown_events.append((event, affected))
    for event in events:
        scope = (event["installation_id"], event["project"], event.get("project_chain_id"))
        key = (*scope, event.get("object_chain_id") or event.get("resource"))
        if event["event_type"] == "replan_submitted" and event.get("reason"):
            reasons[event["reason"]] += 1
        if event["event_type"] == "object_revision":
            revisions[key].append(event)
            if event["source"] == "human" and event.get("before") is not None:
                fields.update(event.get("changed_fields", []))
            if event["resource"].startswith("draft_asset/"):
                before, after = event.get("before") or {}, event.get("after") or {}
                if (
                    event["source"] == "model"
                    and not before
                    and after.get("type") == "prop"
                    and after.get("decision") == "register"
                ):
                    proposed_props.add(key)
                if (
                    event["source"] == "human"
                    and before.get("type") == "prop"
                    and before.get("decision") == "register"
                    and (not after or after.get("decision") == "skip")
                ):
                    skipped_props.add(key)
                if (
                    event["source"] == "human"
                    and not before
                    and after.get("type") == "prop"
                    and after.get("decision") == "register"
                ):
                    added_props.add(key)
            if event["resource"].startswith("media/"):
                before, after = event.get("before") or {}, event.get("after") or {}
                old, new = before.get("versions", []), after.get("versions", [])
                if len(new) > len(old):
                    candidate = new[-1]
                    metadata = candidate.get("metadata", candidate)
                    if "crop_origin" in metadata:
                        replacements["crop"] += 1
                    elif metadata.get("source") in {"upload", "uploaded", "manual_upload"} or metadata.get("uploaded"):
                        replacements["upload"] += 1
                    elif "crop" in json.dumps(metadata).lower():
                        replacements["crop"] += 1
                    elif old:
                        previous = next((v for v in old if v.get("version") == before.get("current_version")), old[-1])
                        replacements[
                            "same_prompt_redraw"
                            if previous.get("prompt") == candidate.get("prompt")
                            else "changed_prompt_regenerate"
                        ] += 1
                elif before.get("current_version") != after.get("current_version"):
                    replacements["version_switch"] += 1
        if event["event_type"] == "reviewed":
            reviews[key].append(event)
            if event.get("reason"):
                reasons[event["reason"]] += 1
    text = Counter()
    media = Counter()
    for key, rows in revisions.items():
        lineage = revisions.get((*key[:3], rows[0].get("registered_from")), [])
        history = [*lineage, *rows]
        initial = history[0]
        judgments = reviews.get(key, [])
        by_layer = {r["after"]["layer"]: r for r in judgments}
        layer = "media" if initial["resource"].startswith("media/") else "text"
        review = by_layer.get(layer)
        stats = media if layer == "media" else text
        if (*key[:3], rows[-1]["resource"]) in unknown_objects:
            stats["unknown_or_baseline"] += 1
            continue
        if review is None or review["after"]["revision"] != rows[-1]["after_revision"]:
            stats["unreviewed"] += 1
            continue
        approved_revision = review["after"]["revision"]
        candidates = [r for r in rows if r["after_revision"] == approved_revision]
        if not candidates or initial.get("baseline") or initial["source"] == "import":
            stats["unknown_or_baseline"] += 1
            continue
        stats["reviewed"] += 1
        usable = review["after"]["usable"]
        stats["usable" if usable else "needs_changes"] += 1
        position = history.index(candidates[-1])
        human_edits = [r for r in history[: position + 1] if r["source"] == "human" and r.get("before") is not None]
        if layer == "text" and usable and initial["source"] == "model" and not human_edits:
            stats["zero_edit_adopted"] += 1
        if layer == "media" and usable:
            selected = candidates[-1]["after"] or {}
            if selected.get("current_version") == 1 and len(selected.get("versions", [])) == 1:
                stats["first_version_adopted"] += 1
    props.update(
        model_proposed=len(proposed_props),
        removed_or_skip=len(skipped_props & proposed_props),
        manual_added=len(added_props),
    )
    result = {
        "events": len(events),
        "installations": len({e["installation_id"] for e in events}),
        "text": dict(text),
        "media": dict(media),
        "human_object_saves": sum(
            e["event_type"] == "object_revision" and e["source"] == "human" and e.get("before") is not None
            for e in events
        ),
        "human_operations": len(
            {
                (e["installation_id"], e["operation_id"])
                for e in events
                if e["event_type"] == "object_revision" and e["source"] == "human"
            }
        ),
        "changed_fields": dict(fields),
        "replacements": dict(replacements),
        "prop_changes": {
            **dict(props),
            "redundancy_rate": props["removed_or_skip"] / props["model_proposed"] if props["model_proposed"] else None,
            "manual_addition_rate": props["manual_added"] / (props["model_proposed"] + props["manual_added"])
            if props["model_proposed"] + props["manual_added"]
            else None,
        },
        "reasons": dict(reasons),
        "replan_requests": sum(e["event_type"] == "replan_submitted" for e in events),
        "replan_outcomes": dict(Counter(e["after"]["status"] for e in events if e["event_type"] == "replan_finished")),
        "unknown_events": sum(
            evidence_events is None
            or any((*key[:3], row["resource"]) in affected for key, rows in revisions.items() for row in rows)
            for _, affected in unknown_events
        ),
        "text_zero_edit_adoption_rate": text["zero_edit_adopted"] / text["reviewed"] if text["reviewed"] else None,
        "media_first_version_adoption_rate": media["first_version_adopted"] / media["reviewed"]
        if media["reviewed"]
        else None,
    }
    result["objects"] = [
        {
            "installation_id": key[0],
            "project": key[1],
            "project_chain_id": key[2],
            "object_chain_id": key[3],
            "resource": rows[-1]["resource"],
            "human_rounds": sum(row["source"] == "human" and row.get("before") is not None for row in rows),
            "latest_revision": rows[-1]["after_revision"],
            "latest_review": reviews.get(key, [])[-1]["after"] if reviews.get(key) else None,
            "media_candidates_after_last_text_edit": _candidates_after_edit(evidence, rows),
        }
        for key, rows in revisions.items()
    ]
    if grouped:
        groups = defaultdict(list)
        model_by_task = {
            (e["installation_id"], e["after"].get("task_id")): e["after"].get("model")
            for e in events
            if e["event_type"] == "request_submitted"
        }
        for key, rows in revisions.items():
            lineage = revisions.get((*key[:3], rows[0].get("registered_from")), [])
            event = (lineage or rows)[0]
            group_key = (
                event.get("test_batch_id"),
                event.get("test_case_id"),
                event.get("template_digest"),
                model_by_task.get((event["installation_id"], event.get("task_id"))),
            )
            groups[group_key].extend([*lineage, *rows, *reviews.get(key, [])])
        result["groups"] = [
            {
                "test_batch_id": key[0],
                "test_case_id": key[1],
                "template_digest": key[2],
                "model": key[3],
                "summary": summarize(
                    list({e["event_id"]: e for e in sorted(rows, key=lambda e: e.get("local_sequence") or 0)}.values()),
                    grouped=False,
                    evidence_events=evidence,
                ),
            }
            for key, rows in groups.items()
        ]
    return result


def _candidates_after_edit(events: list[dict], rows: list[dict]) -> int | None:
    last = rows[-1]
    resource = last["resource"]
    edits = [r for r in rows if r["source"] == "human" and r.get("before") is not None]
    if resource.startswith("media/") or not edits:
        return None
    if resource.startswith("unit/"):
        media_resource = f"media/reference_videos/{resource.rsplit('/', 1)[-1]}"
    elif resource.startswith(("character/", "scene/", "prop/")):
        kind, name = resource.split("/", 1)
        plural = {"character": "characters", "scene": "scenes", "prop": "props"}[kind]
        media_resource = f"media/{plural}/{name}"
    else:
        return None
    result = 0
    for event in events:
        if (event["installation_id"], event["project"], event.get("project_chain_id"), event["resource"]) != (
            last["installation_id"],
            last["project"],
            last.get("project_chain_id"),
            media_resource,
        ):
            continue
        if event["event_type"] != "object_revision" or (event.get("local_sequence") or 0) <= (
            edits[-1].get("local_sequence") or 0
        ):
            continue
        result += max(
            0,
            len((event.get("after") or {}).get("versions", [])) - len((event.get("before") or {}).get("versions", [])),
        )
    return result


def difference_report(events: list[dict], summary: dict) -> str:
    result = [
        "# 制作测试汇总",
        "",
        "按安装标识与本机序号保留顺序；不同电脑的时钟不用于判定先后。媒体画面未检查。",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    replans = {e["task_id"] for e in events if e["event_type"] == "replan_submitted"}
    outcomes = {e["task_id"]: e["after"]["status"] for e in events if e["event_type"] == "replan_finished"}
    for event in events:
        if event["event_type"] == "replan_submitted":
            inputs = event["after"]
            result.extend(
                [
                    f"## 重新拆解 · {event['project']} · 第 {event.get('episode_id', '?')} 集",
                    "",
                    f"时间：{event['created_at']}；任务：{event['task_id']}；状态：{outcomes.get(event['task_id'], '已提交，尚无终态记录')}",
                    "",
                    "### 重新拆解输入",
                    "",
                    *_fenced(event.get("reason") or "", "text"),
                    "### 本次附加指令",
                    "",
                    *_fenced(inputs.get("instructions") or "无", "text"),
                    "### 重拆前的结果快照",
                    "",
                ]
            )
            snapshot = inputs.get("replan_context")
            with suppress(ValueError):
                snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
            result.extend(_fenced(json.dumps(snapshot, ensure_ascii=False, indent=2), "json"))
            continue
        if event["event_type"] != "object_revision" or not (
            event["source"] == "human" or (event["source"] == "model" and event.get("task_id") in replans)
        ):
            continue
        result.extend(
            [
                f"## {event['tester_id']} · {event['project']} · {event['resource']}",
                "",
                f"批次：{event['test_batch_id']}；样例：{event['test_case_id']}；模板：{event['template_digest']}",
                "",
                f"时间：{event['created_at']}；来源：{event['source']}；任务：{event.get('task_id') or '无'}；操作：{event['operation_id']}",
                "",
            ]
        )
        if event.get("reason"):
            result.extend(["修改原因：", "", *_fenced(event["reason"], "text")])
        before, after = event.get("before"), event.get("after")
        if isinstance(before, dict) or isinstance(after, dict):
            before, after = before or {}, after or {}
            for field in sorted(set(before) | set(after)):
                if before.get(field) == after.get(field):
                    continue
                result.extend([f"### {field}", ""])
                result.extend(_value_diff(before.get(field), after.get(field)))
        else:
            result.extend(_value_diff(before, after))
    return "\n".join(result)


def _fenced(value: str, language: str) -> list[str]:
    # User text may contain Markdown fences; keep the whole input inside one block.
    fence = "```"
    while fence in value:
        fence += "`"
    return [f"{fence}{language}", value, fence, ""]


def _value_diff(before, after) -> list[str]:
    def lines(value):
        return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)).splitlines()

    diff = "\n".join(difflib.unified_diff(lines(before), lines(after), fromfile="before", tofile="after", lineterm=""))
    return _fenced(diff, "diff")
