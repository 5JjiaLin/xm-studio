"""Production handoff orchestration using the existing render lane and artifact basis."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import zipfile
from pathlib import Path

from lib.infra.json_io import atomic_write_json, load_json
from lib.jianying_draft.archive import file_sha256, package_jianying_draft
from lib.jianying_draft.local_delivery import DeliveryError, LocalDeliveryStore
from lib.project.project_manager import ProjectManager
from server.services.presentation.timeline_jianying_draft import TimelineJianyingDraftService


def delivery_store(projects: ProjectManager) -> LocalDeliveryStore:
    return LocalDeliveryStore(projects.projects_dir.parent)


def _package(store: LocalDeliveryStore, record: dict, project_dir: Path, artifact: str):
    # All assets are copied and checked before a package becomes available for registration.
    with tempfile.TemporaryDirectory(prefix="xm-jianying-") as directory:
        workspace = Path(directory)
        package = workspace / "package.zip"
        package_jianying_draft(
            project_dir / artifact,
            project_dir=project_dir,
            draft_root=record["draft_root"] or "XM_DRAFT_ROOT",
            draft_name=record["folder"],
            jianying_version="6",
            output=package,
        )
        with zipfile.ZipFile(package) as archive:
            archive.extractall(workspace / "unpacked")
        folder = workspace / "unpacked" / record["folder"]
        content_path = folder / "draft_info.json"
        content = load_json(content_path)
        content["id"] = record["draft_id"]
        content["name"] = record["folder"]
        atomic_write_json(content_path, content)
        meta_path = folder / "draft_meta_info.json"
        meta = load_json(meta_path)
        meta.update(
            draft_id=record["draft_id"],
            draft_name=record["folder"],
            draft_fold_path=f"{record['draft_root']}/{record['folder']}",
            draft_root_path=record["draft_root"],
        )
        atomic_write_json(meta_path, meta)
        marker = {key: record[key] for key in ("delivery_id", "draft_id", "basis")}
        atomic_write_json(folder / "xm_delivery.json", marker)
        for name, digest in load_json(folder / "xm_materials.json")["assets"].items():
            if file_sha256(folder / "assets" / name) != digest:
                raise DeliveryError("复制后的素材摘要不一致")
        digest = file_sha256(content_path)
        final = workspace / "delivery.zip"
        if not record["local"]:
            # Rebase on the destination computer; no executable or host-specific absolute paths.
            (folder / "迁移说明.txt").write_text(
                "请将此文件夹复制到目标电脑的剪映草稿根目录，再将 draft_info.json 内的 XM_DRAFT_ROOT 替换为该根目录（Windows 路径使用 /）。此包未自动登记到目标电脑的草稿索引。",
                encoding="utf-8",
            )
        with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED) as archive:
            for path in folder.rglob("*"):
                if path.is_file():
                    archive.write(path, f"{record['folder']}/{path.relative_to(folder).as_posix()}")
        target = store.package_path(record["delivery_id"])
        pending = target.with_suffix(".tmp")
        shutil.copyfile(final, pending)
        # Commit recovery metadata before publishing the package; a crash after rename is resumable.
        store.patch(record["delivery_id"], content_sha256=digest, duration_us=content["duration"])
        pending.replace(target)
        store.patch(
            record["delivery_id"],
            state="ready" if record["local"] else "package_ready",
            error=None,
        )


async def execute_delivery(projects: ProjectManager, delivery_id: str) -> dict:
    store = delivery_store(projects)
    record = await asyncio.to_thread(store.read, delivery_id)
    try:
        if not await asyncio.to_thread(store.package_path(delivery_id).is_file):
            service = TimelineJianyingDraftService(projects)
            job = await service.prepare(
                record["project"], record["timeline_id"], narration=record["narration"], revision=record["revision"]
            )
            if job.basis.digest != record["basis"]:
                raise DeliveryError("素材或时间线已变化，请建立新版本")
            result = await service.render_prepared(job)
            await asyncio.to_thread(_package, store, record, job.project_dir, result.artifact_path)
        return await asyncio.to_thread(store.register, delivery_id)
    except Exception as exc:
        await asyncio.to_thread(store.patch, delivery_id, state="failed", error=str(exc))
        raise
