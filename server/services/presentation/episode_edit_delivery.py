"""Repackage a native draft for the existing local registration store."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

from lib.infra.content_digest import sha256_file
from lib.infra.json_io import atomic_write_json, load_json
from lib.jianying_draft.archive import portable_name
from lib.jianying_draft.local_delivery import DeliveryError, LocalDeliveryStore
from lib.openfilm.runtime import native_draft


def package_edit_draft(store: LocalDeliveryStore, record: dict, work: Path) -> None:
    source = native_draft(work)
    target = store.package_path(record["delivery_id"])
    if target.is_file():
        return
    with tempfile.TemporaryDirectory(dir=store.directory, prefix=".edit-draft-") as temp:
        staging = Path(temp) / record["folder"]
        staging.mkdir()
        for filename in ("draft_meta_info.json", "draft_info.json", "draft_content.json"):
            shutil.copyfile(source / filename, staging / filename)
        # Keep the engine's media layout. Its placeholder resolves against the draft folder on both platforms.
        media = staging / "media"
        media.mkdir()
        for asset in (source / "media").iterdir():
            if not asset.is_file() or asset.is_symlink() or portable_name(asset.name) != asset.name:
                raise DeliveryError("草稿素材文件无效")
            shutil.copyfile(asset, media / asset.name)
        cover = work / "draft_cover.jpg"
        if cover.is_file():
            shutil.copyfile(cover, staging / cover.name)

        info = load_json(source / "draft_info.json")
        info.update(id=record["draft_id"], name=record["folder"])
        for filename in ("draft_info.json", "draft_content.json"):
            atomic_write_json(staging / filename, info)
        meta = load_json(source / "draft_meta_info.json")
        meta.update(
            draft_id=record["draft_id"],
            draft_name=record["folder"],
            draft_root_path=record["draft_root"],
            draft_fold_path=f"{record['draft_root']}/{record['folder']}",
            draft_cover="draft_cover.jpg" if cover.is_file() else "",
        )
        atomic_write_json(staging / "draft_meta_info.json", meta)
        atomic_write_json(
            staging / "xm_delivery.json", {key: record[key] for key in ("delivery_id", "draft_id", "basis")}
        )
        atomic_write_json(
            staging / "xm_materials.json",
            {
                "assets": {asset.name: sha256_file(asset) for asset in media.iterdir()},
            },
        )
        if not record["local"]:
            (staging / "迁移说明.txt").write_text(
                "请解压并保留完整草稿文件夹（包括 media）。此包未在目标电脑自动登记。"
                "完整本机交接请在安装剪映的电脑上运行 XM，并使用成片页的剪映草稿交接。",
                encoding="utf-8",
            )
        bundle = staging.parent / "draft.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as archive:
            for file in staging.rglob("*"):
                if file.is_file():
                    archive.write(file, file.relative_to(staging.parent).as_posix())
        store.patch(
            record["delivery_id"],
            content_sha256=sha256_file(staging / "draft_info.json"),
            duration_us=info["duration"],
            draft_new_version=info.get("new_version", "164.0.0"),
        )
        bundle.replace(target)
        store.patch(record["delivery_id"], state="ready" if record["local"] else "package_ready", error=None)
