"""Deterministic screenplay division and transactional import over the episode ledger."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from itertools import pairwise
from typing import Any

from lib.episode.episode_ids import allocate_episode_ids
from lib.episode.episode_ledger import SOURCE_FINGERPRINTS_KEY, normalize_source_text, source_range_value
from lib.episode.episode_paths import episode_script_relpath, episode_source_path
from lib.episode.episode_source_commands import register_whole_source_file, write_episode_source_file
from lib.episode.episode_sources import SOURCE_ORIGIN_FIELD, SourceOrigin, source_snapshot_path
from lib.episode.script_batch_state import require_batch_idle

_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?第[ \t]*([0-9０-９零〇一二两三四五六七八九十百千]+)[ \t]*集(?=$|[ \t:：、.．—–-])[^\n]*$",
    re.M,
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _number(value: str) -> int:
    value = unicodedata.normalize("NFKC", value)
    if value.isdecimal():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9), strict=True))
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        else:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
    return total + current


def project_basis(project: dict) -> str:
    return digest({key: project.get(key) for key in ("episodes", "whole_source_files", "production_management")})


def verify_import_sources(directory, project: dict, imported: dict) -> None:
    entries = {e["episode"]: e for e in project.get("episodes", [])}
    full_sources = {}
    for episode in imported["episodes"]:
        if episode not in entries:
            raise ValueError("本次导入的部分集已删除，请核对项目，不能重复导入覆盖")
        span = entries[episode].get("source_range", {})
        rel = span.get("source_file", "")
        if not rel.startswith("source/screenplay-") or span.get("end_file", rel) != rel:
            raise ValueError("整部剧本分集范围已变化，请重新导入修订剧本")
        try:
            if rel not in full_sources:
                full = (directory / rel).read_text(encoding="utf-8")
                if digest(full) != imported["fingerprint"]:
                    raise ValueError("整部剧本已被修改，请重新导入修订剧本")
                full_sources[rel] = full
            full = full_sources[rel]
            derived = episode_source_path(directory, episode).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("整部剧本或派生集原文缺失，请核对文件后重试") from exc
        if full[span["start"] : span["end"]] != derived:
            raise ValueError("整部剧本或派生集原文已被修改，请重新导入修订剧本")


def divide_script(text: str, boundaries: list[dict] | None = None) -> dict:
    text = normalize_source_text(text)
    if not text.strip():
        raise ValueError("剧本不能为空")
    matches = list(_HEADING.finditer(text))
    warnings: list[str] = []
    if boundaries is None:
        boundaries = [{"start": m.start(), "title": m.group().strip(), "number": _number(m[1])} for m in matches]
        if not boundaries:
            boundaries = [{"start": 0, "title": "第1集", "number": 1}]
            warnings.append("未识别到分集标题，当前保留全文为一集；请核对或手动添加分界。")
        elif boundaries[0]["start"]:
            boundaries[0]["start"] = 0
            warnings.append("首个集标题之前的文字已归入第一集，请核对。")
    if len(boundaries) > 1000:
        raise ValueError("单次最多导入 1000 集，请分批导入")
    if not boundaries or boundaries[0]["start"] != 0:
        raise ValueError("第一集必须从全文开头开始")
    starts = [b["start"] for b in boundaries]
    if any(type(s) is not int or s < 0 or s >= len(text) or (s and text[s - 1] != "\n") for s in starts):
        raise ValueError("分界必须位于原文行首")
    if starts != sorted(set(starts)):
        raise ValueError("分界必须按原文顺序排列且不能重复")
    errors: list[str] = []
    numbers = [b.get("number") for b in boundaries]
    if any(type(n) is not int or n < 1 for n in numbers):
        errors.append("原集号必须为正整数")
    elif numbers != sorted(set(numbers)):
        errors.append("原集号重复或倒序，请修正原集号或取消误识别的分界")
    elif any(b != a + 1 for a, b in pairwise(numbers)):
        warnings.append("原集号存在缺号，请核对是否遗漏。")
    episodes = []
    for i, boundary in enumerate(boundaries):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        body = text[starts[i] : end]
        if not body.strip() or not _HEADING.sub("", body).strip():
            errors.append(f"第 {i + 1} 个分段没有正文")
        title = boundary["title"].strip()
        if not title or len(title) > 200:
            errors.append(f"第 {i + 1} 个标题不能为空且不超过 200 字")
        episodes.append(
            {
                "start": starts[i],
                "end": end,
                "title": title,
                "number": numbers[i],
                "characters": len(body),
                "head": body[:100],
                "tail": body[-100:],
            }
        )
    return {"text": text, "fingerprint": digest(text), "episodes": episodes, "warnings": warnings, "errors": errors}


def import_script(
    pm,
    name: str,
    text: str,
    boundaries: list[dict],
    fingerprint: str,
    expected_project: str,
    original: tuple[str, bytes] | None = None,
) -> dict:
    preview = divide_script(text, boundaries)
    if preview["fingerprint"] != fingerprint or preview["errors"]:
        raise ValueError("原文或分界校验失败，请重新预览：" + "；".join(preview["errors"]))
    import_id = digest({"text": fingerprint, "boundaries": boundaries})
    project_dir = pm.get_project_path(name)
    with pm.locked_source_registration(name) as (source_dir, project, undo):
        require_batch_idle(project_dir)
        previous = project.get("production_script_imports", {}).get(import_id)
        if previous:
            verify_import_sources(project_dir, project, previous)
            return previous
        if project_basis(project) != expected_project:
            raise ValueError("项目已变化，请刷新分集预览后确认")
        if project.get("production_management", {}).get("archived") or project.get("content_mode") != "drama":
            raise ValueError("请在未归档的剧情项目中导入剧本")
        filename = f"screenplay-{import_id}.txt"
        path = source_dir / filename
        if path.exists():
            raise ValueError("源文件已经存在，请刷新后核对")
        undo.callback(path.unlink, missing_ok=True)  # hard-delete: rollback -- newly imported file only
        path.write_text(preview["text"], encoding="utf-8")
        rel = f"source/{filename}"
        register_whole_source_file(project, rel, source_kind="screenplay")
        snapshot = source_snapshot_path(project_dir, rel)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        undo.callback(snapshot.unlink, missing_ok=True)  # hard-delete: rollback -- new source snapshot
        snapshot.write_text(preview["text"], encoding="utf-8")
        project.setdefault(SOURCE_FINGERPRINTS_KEY, {})[rel] = hashlib.sha256(preview["text"].encode()).hexdigest()
        ids = allocate_episode_ids(project, len(preview["episodes"]))
        for episode, part in zip(ids, preview["episodes"], strict=True):
            write_episode_source_file(
                project_dir, episode, preview["text"][part["start"] : part["end"]], archive_existing=True, undo=undo
            )
            project.setdefault("episodes", []).append(
                {
                    "episode": episode,
                    "title": part["title"],
                    "script_file": episode_script_relpath(episode),
                    SOURCE_ORIGIN_FIELD: SourceOrigin.WHOLE_SOURCE.value,
                    "source_range": source_range_value(rel, part["start"], rel, part["end"]),
                    "ledger_status": "planned",
                }
            )
        if original:
            from pathlib import Path

            suffix = Path(original[0]).suffix.lower()
            raw_dir = source_dir / "raw" / import_id
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / (hashlib.sha256(original[1]).hexdigest() + suffix)
            if not raw_path.exists():
                undo.callback(raw_path.unlink, missing_ok=True)  # hard-delete: rollback -- newly uploaded original only
                raw_path.write_bytes(original[1])
        result = {"import_id": import_id, "episodes": ids, "fingerprint": fingerprint}
        project.setdefault("production_script_imports", {})[import_id] = result
    return result
