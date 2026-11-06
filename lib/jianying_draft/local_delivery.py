"""Local Jianying handoff. Only explicit commands write; delivered drafts are never rewritten."""

from __future__ import annotations

import csv
import io
import json
import os
import platform
import plistlib
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal

import portalocker
from pydantic import BaseModel, ConfigDict

from lib.infra.json_io import atomic_write_bytes, atomic_write_json, load_json
from lib.jianying_draft.archive import file_sha256, portable_name


class DeliveryError(ValueError):
    pass


class LocalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    app_path: str = ""
    draft_root: str = ""


def discover() -> LocalSettings:
    home = Path.home()
    if platform.system() == "Darwin":
        return LocalSettings(
            app_path="/Applications/VideoFusion-macOS.app",
            draft_root=str(home / "Movies/JianyingPro/User Data/Projects/com.lveditor.draft"),
        )
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local"))) / "JianyingPro"
        apps = sorted((base / "Apps").glob("*/JianyingPro.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        return LocalSettings(
            app_path=str(apps[0]) if apps else str(base / "Apps/JianyingPro.exe"),
            draft_root=str(base / "User Data/Projects/com.lveditor.draft"),
        )
    return LocalSettings()


def process_state() -> Literal["running", "stopped", "unknown"]:
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True, timeout=5, check=True)
            if not result.stdout.strip():
                return "unknown"
            return (
                "running"
                if any(
                    ("VideoFusion-macOS" in row or "JianyingPro" in row)
                    and Path(row.strip()).name != "VideoFusion-macOSTrayHelper"
                    for row in result.stdout.splitlines()
                )
                else "stopped"
            )
        if platform.system() == "Windows":
            result = subprocess.run(
                [str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/tasklist.exe"), "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            rows = list(csv.reader(io.StringIO(result.stdout)))
            if not rows or any(len(row) < 2 for row in rows):
                return "unknown"
            return (
                "running"
                if any(
                    row[0].lower() in {"jianyingpro.exe", "videofusion.exe", "videofusion-macos.exe"} for row in rows
                )
                else "stopped"
            )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return "unknown"


def compatibility(settings: LocalSettings) -> dict:
    system = platform.system()
    version = build = ""
    app = Path(settings.app_path)
    if system == "Darwin" and (app / "Contents/Info.plist").is_file():
        with (app / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        version, build = info.get("CFBundleShortVersionString", ""), info.get("CFBundleVersion", "")
    index = Path(settings.draft_root) / "root_meta_info.json"
    supported = system in {"Darwin", "Windows"}
    ready = supported and app.exists() and index.is_file()
    return {
        "system": system,
        "architecture": platform.machine(),
        "version": version,
        "build": build,
        "ready": ready,
        "process": process_state(),
        "verification": "mac_11_7_sample"
        if system == "Darwin" and version == "11.7.0" and platform.machine() == "arm64"
        else "unverified",
    }


class LocalDeliveryStore:
    def __init__(self, data_root: Path):
        self.directory = data_root / "jianying_delivery"

    def _init(self):
        self.directory.mkdir(parents=True, exist_ok=True)

    def lock(self):
        self._init()
        return portalocker.Lock(str(self.directory / "delivery.lock"), timeout=15)

    def settings(self) -> LocalSettings:
        path = self.directory / "settings.json"
        return LocalSettings.model_validate(load_json(path)) if path.is_file() else discover()

    def save_settings(self, settings: LocalSettings):
        if settings.enabled and not compatibility(settings)["ready"]:
            raise DeliveryError("找不到剪映应用或草稿索引，请先打开剪映建立一个空草稿，再检查路径")
        with self.lock():
            atomic_write_json(self.directory / "settings.json", settings.model_dump())

    def record_path(self, delivery_id: str) -> Path:
        if str(uuid.UUID(delivery_id)) != delivery_id:
            raise DeliveryError("交付标识无效")
        return self.directory / f"{delivery_id}.json"

    def read(self, delivery_id: str) -> dict:
        return load_json(self.record_path(delivery_id))

    def write(self, record: dict):
        atomic_write_json(self.record_path(record["delivery_id"]), record)

    def latest(self, project: str, episode: int) -> dict | None:
        if not self.directory.exists():
            return None
        records = [load_json(p) for p in self.directory.glob("*-*-*-*-*.json")]
        matches = [r for r in records if r["project"] == project and r["episode"] == episode]
        return max(matches, key=lambda r: r["created_ns"]) if matches else None

    def reserve(
        self,
        *,
        project: str,
        episode: int,
        timeline_id: str,
        revision: int,
        narration: str,
        basis: str,
        name: str,
        new_version: bool,
        previous_id: str | None,
        local: bool,
    ) -> dict:
        with self.lock():
            previous = self.latest(project, episode)
            if previous:
                if not new_version:
                    return previous
                # CAS: a repeated new-version request reuses the version it already created.
                if previous_id != previous["delivery_id"]:
                    return previous
                if previous["state"] == "preparing":
                    raise DeliveryError("当前交接尚未完成，请先继续或重试")
            settings = self.settings()
            if local and (not settings.enabled or not compatibility(settings)["ready"]):
                raise DeliveryError("请先启用本机剪映交接并确认路径")
            identity = str(uuid.uuid4())
            folder = portable_name(name)[:100] + "_XM_" + identity[:8]
            record = {
                "delivery_id": identity,
                "draft_id": str(uuid.uuid4()).upper(),
                "project": project,
                "episode": episode,
                "timeline_id": timeline_id,
                "revision": revision,
                "narration": narration,
                "basis": basis,
                "folder": folder,
                "draft_root": settings.draft_root if local else "",
                "local": local,
                "state": "preparing",
                "created_ns": time.time_ns(),
                "error": None,
            }
            self.write(record)
            return record

    def package_path(self, delivery_id: str) -> Path:
        self.record_path(delivery_id)
        return self.directory / f"{delivery_id}.zip"

    def patch(self, delivery_id: str, **values) -> dict:
        with self.lock():
            record = self.read(delivery_id)
            record.update(values)
            self.write(record)
            return record

    def observed(self, record: dict) -> dict:
        """Read the index only; never inspect the editor's private saved draft content."""
        result = dict(record)
        if record["state"] != "delivered" or not record["local"]:
            return result
        try:
            root = Path(record["draft_root"])
            entries = load_json(root / "root_meta_info.json")["all_draft_store"]
            if not isinstance(entries, list):
                raise ValueError("unknown index")
            present = any(e.get("draft_id") == record["draft_id"] and not e.get("tm_draft_removed", 0) for e in entries)
            if not present or not (root / record["folder"]).is_dir():
                result.update(state="removed", error=None)
        except (OSError, ValueError, KeyError, TypeError):
            result["error"] = "暂时无法读取剪映索引，请刷新状态"
        return result

    def _validate_stage(self, target: Path, record: dict):
        marker = load_json(target / "xm_delivery.json")
        if marker != {"delivery_id": record["delivery_id"], "draft_id": record["draft_id"], "basis": record["basis"]}:
            raise DeliveryError("草稿目录已有其他内容，未覆盖")
        if file_sha256(target / "draft_info.json") != record["content_sha256"]:
            raise DeliveryError("草稿已在外部修改，未覆盖；请建立新版本")
        for name, digest in load_json(target / "xm_materials.json")["assets"].items():
            if portable_name(name) != name or file_sha256(target / "assets" / name) != digest:
                raise DeliveryError("草稿素材校验失败，请重新准备")

    def register(self, delivery_id: str) -> dict:
        with self.lock():
            record = self.read(delivery_id)
            if record["state"] == "delivered" or not record["local"]:
                return record
            if not self.settings().enabled:
                raise DeliveryError("本机交接已停用")
            root = Path(record["draft_root"])
            if not root.is_absolute() or not (root / "root_meta_info.json").is_file():
                raise DeliveryError("草稿索引不存在，请检查剪映草稿位置")
            with portalocker.Lock(str(root / ".xm-delivery.lock"), timeout=15):
                state = process_state()
                if state != "stopped":
                    record.update(state="waiting_for_exit" if state == "running" else "process_unknown", error=None)
                    self.write(record)
                    return record
                index_path = root / "root_meta_info.json"
                original = index_path.read_bytes()
                index = json.loads(original)
                entries = index.get("all_draft_store")
                if not isinstance(entries, list):
                    raise DeliveryError("剪映索引格式无法识别，未修改")
                target = root / record["folder"]
                matches = [e for e in entries if e.get("draft_id") == record["draft_id"]]
                if matches:
                    entry = matches[0]
                    if (
                        len(matches) != 1
                        or entry.get("tm_draft_removed", 0)
                        or Path(entry.get("draft_fold_path", "")) != target
                        or not target.is_dir()
                    ):
                        raise DeliveryError("剪映中的草稿已移动或删除，请建立新版本")
                    # Index committed but response lost: never inspect or overwrite the editor's saved content.
                    marker = load_json(target / "xm_delivery.json")
                    if marker.get("delivery_id") != delivery_id:
                        raise DeliveryError("草稿身份冲突")
                    record.update(state="delivered", error=None)
                    self.write(record)
                    return record
                if target.is_symlink():
                    raise DeliveryError("草稿目录不能是符号链接")
                if target.exists():
                    self._validate_stage(target, record)
                else:
                    stage = root / f".xm-stage-{delivery_id}"
                    if stage.is_symlink():
                        raise DeliveryError("暂存目录不能是符号链接")
                    if stage.exists():
                        shutil.rmtree(stage)  # hard-delete: temp -- owned incomplete staging directory
                    stage.mkdir()
                    try:
                        with zipfile.ZipFile(self.package_path(delivery_id)) as archive:
                            for item in archive.infolist():
                                relative = Path(item.filename)
                                if (
                                    relative.is_absolute()
                                    or ".." in relative.parts
                                    or relative.parts[0] != record["folder"]
                                    or "\\" in item.filename
                                ):
                                    raise DeliveryError("草稿包路径无效")
                            archive.extractall(stage)
                        staged = stage / record["folder"]
                        self._validate_stage(staged, record)
                        staged.rename(target)
                    finally:
                        shutil.rmtree(stage, ignore_errors=True)  # hard-delete: temp -- owned staging directory
                stamp = int(time.time() * 1_000_000)
                entry = {
                    "draft_id": record["draft_id"],
                    "draft_name": record["folder"],
                    "draft_fold_path": str(target),
                    "draft_json_file": str(target / "draft_info.json"),
                    "draft_cover": str(target / "draft_cover.jpg"),
                    "draft_root_path": str(root),
                    "draft_new_version": "164.0.0",
                    "draft_timeline_materials_size": sum(p.stat().st_size for p in (target / "assets").iterdir()),
                    "streaming_edit_draft_ready": True,
                    "tm_draft_create": stamp,
                    "tm_draft_modified": stamp,
                    "tm_draft_removed": 0,
                    "tm_duration": record["duration_us"],
                }
                # Keep exact original bytes for manual recovery. Never restore this over external changes.
                atomic_write_bytes(root / f".xm-index-{delivery_id}.bak", original)
                if process_state() != "stopped" or index_path.read_bytes() != original:
                    raise DeliveryError("剪映状态或索引已变化，未覆盖；退出剪映后重试")
                entries.insert(0, entry)
                if isinstance(index.get("draft_ids"), int):
                    index["draft_ids"] += 1
                atomic_write_json(index_path, index)
                record.update(state="delivered", error=None)
                self.write(record)
                return record

    def open_target(self, delivery_id: str, target: Literal["app", "folder"]):
        record = self.read(delivery_id)
        if not record["local"]:
            raise DeliveryError("远端草稿不能打开浏览器电脑上的剪映")
        path = Path(self.settings().app_path) if target == "app" else Path(record["draft_root"]) / record["folder"]
        if not path.exists():
            raise DeliveryError("目标不存在")
        if platform.system() == "Darwin":
            subprocess.run(["/usr/bin/open", str(path)], check=True, timeout=10)
        elif platform.system() == "Windows":
            os.startfile(str(path))
        else:
            raise DeliveryError("当前系统不支持打开剪映")
