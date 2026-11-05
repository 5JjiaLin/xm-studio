"""Actual version files, attached exports, and media evidence corruption checks."""

import hashlib
import io
import json
import zipfile

import pytest

from lib.artifacts.version_manager import VersionManager
from lib.production_feedback.context import identity_link, operation
from lib.production_feedback.export import export_project, merge_packages
from lib.production_feedback.metrics import summarize
from lib.production_feedback.store import atomic_write_json, register_root


def test_media_digests_attachments_and_incremental_merge_preserve_version_bytes(tmp_path):
    store = register_root(tmp_path / "data")
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    atomic_write_json(directory / "project.json", {"props": {"针": {"generation_prompt": "银针"}}})
    versions = VersionManager(directory)
    source = tmp_path / "image.png"
    source.write_bytes(b"original version bytes")
    with operation(source="model"):
        versions.add_version("props", "针", "silver needle", source_file=source)
    package = tmp_path / "first.zip"
    package.write_bytes(export_project(store, "demo", include_media=True))
    merge_packages([package, package], tmp_path / "merged")
    media = [json.loads(line) for line in (tmp_path / "merged/media.jsonl").read_text().splitlines()]
    assert len(media) == 1
    assert (tmp_path / "merged" / media[0]["merged_file"]).read_bytes() == source.read_bytes()
    snapshot = directory / media[0]["file"]
    snapshot.write_bytes(b"unexpected later replacement")
    with zipfile.ZipFile(io.BytesIO(export_project(store, "demo", include_media=True))) as archive:
        corrupted = json.loads(archive.read("media.jsonl").splitlines()[0])
        assert corrupted["corrupted"] is True
        assert corrupted["included"] is False
    with operation(source="model"):
        versions.add_version("props", "针", "gold needle", source_file=source)
    with zipfile.ZipFile(io.BytesIO(export_project(store, "demo", include_media=True))) as archive:
        rows = [json.loads(line) for line in archive.read("media.jsonl").splitlines()]
        original = next(row for row in rows if row["version"] == 1)
        assert original["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert original["corrupted"] is True
        assert original["included"] is False
        assert next(row for row in rows if row["version"] == 2)["included"] is True


def test_mixed_media_exports_merge_independently_of_order_and_reject_digest_conflicts(tmp_path):
    store = register_root(tmp_path / "data")
    directory = store.layout.projects_dir / "demo"
    directory.mkdir(parents=True)
    atomic_write_json(directory / "project.json", {"props": {}})
    source = tmp_path / "image.png"
    source.write_bytes(b"version evidence")
    VersionManager(directory).add_version("props", "针", "needle", source_file=source)
    attached, omitted = tmp_path / "attached.zip", tmp_path / "omitted.zip"
    attached.write_bytes(export_project(store, "demo", include_media=True))
    omitted.write_bytes(export_project(store, "demo"))
    for index, packages in enumerate(([attached, omitted], [omitted, attached])):
        destination = tmp_path / f"merged-{index}"
        merge_packages(packages, destination)
        row = json.loads((destination / "media.jsonl").read_text())
        assert row["included"] is True
        assert (destination / row["merged_file"]).read_bytes() == source.read_bytes()
    assert (tmp_path / "merged-0/media.jsonl").read_bytes() == (tmp_path / "merged-1/media.jsonl").read_bytes()

    with zipfile.ZipFile(omitted) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    row = json.loads(files["media.jsonl"])
    row["sha256"] = "0" * 64
    files["media.jsonl"] = json.dumps(row).encode() + b"\n"
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["media.jsonl"] = hashlib.sha256(files["media.jsonl"]).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    conflict = tmp_path / "conflict.zip"
    with zipfile.ZipFile(conflict, "w") as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    for packages in ([attached, conflict], [conflict, attached]):
        with pytest.raises(ValueError, match="Conflicting media digest"):
            merge_packages(packages, tmp_path / "conflicting-merge")


def test_asset_registration_keeps_model_lineage_and_draft_edits_prevent_zero_edit(tmp_path):
    store = register_root(tmp_path)
    directory = store.layout.projects_dir / "demo"
    (directory / "drafts").mkdir(parents=True)
    draft = directory / "drafts/plan.json"
    asset = {"type": "prop", "name": "针", "decision": "register", "description": "银针"}
    with operation(source="model"):
        atomic_write_json(draft, {"new_assets": [asset]})
    with operation(source="human"):
        atomic_write_json(draft, {"new_assets": [{**asset, "generation_prompt": "修改为金针"}]})
        atomic_write_json(directory / "project.json", {"props": {"针": {"generation_prompt": "修改为金针"}}})
    current = [e for e in store.events("demo") if e["resource"] == "prop/针"][-1]
    assert current["registered_from"]
    store.review("demo", "prop/针", revision=current["after_revision"], usable=True, reason=None, layer="text")
    summary = summarize(store.events("demo"))
    assert summary["text"]["usable"] == 1
    assert summary["text_zero_edit_adoption_rate"] == 0

    formal = directory / "scripts/episode_1.json"
    formal.parent.mkdir()
    with operation(source="model"):
        atomic_write_json(draft, {"units": [{"unit_id": "D1", "text": "模型正文"}]})
    with (
        operation(source="human"),
        identity_link("unit", "unit/drafts/plan.json/D1", "unit/scripts/episode_1.json/E1U2", "materialize"),
    ):
        atomic_write_json(formal, {"video_units": [{"unit_id": "E1U2", "text": "模型正文"}]})
    unit = [e for e in store.events("demo") if e["resource"].endswith("/E1U2")][-1]
    assert unit["registered_from"]
    store.review("demo", unit["resource"], revision=unit["after_revision"], usable=True, reason=None, layer="text")
    assert summarize(store.events("demo"))["text"]["zero_edit_adopted"] == 1
