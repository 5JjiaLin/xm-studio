import hashlib
import json
import zipfile

import pytest

from lib.infra.json_io import atomic_write_json, load_json
from lib.jianying_draft import local_delivery as local
from lib.jianying_draft.archive import portable_name


def prepared(tmp_path, monkeypatch):
    root = tmp_path / "剪映 草稿"
    root.mkdir()
    index = root / "root_meta_info.json"
    atomic_write_json(
        index,
        {
            "all_draft_store": [{"draft_id": "existing", "unknown": [1, 2]}],
            "draft_ids": 7,
            "future": {"preserve": True},
        },
    )
    store = local.LocalDeliveryStore(tmp_path / "data")
    monkeypatch.setattr(local, "compatibility", lambda _: {"ready": True})
    monkeypatch.setattr(local, "process_state", lambda: "stopped")
    store.save_settings(local.LocalSettings(enabled=True, app_path=str(tmp_path), draft_root=str(root)))
    args = {
        "project": "demo",
        "episode": 1,
        "timeline_id": "tl-one",
        "revision": 2,
        "narration": "without_narration",
        "basis": "hash",
        "name": "中文 单集",
        "new_version": False,
        "previous_id": None,
        "local": True,
    }
    record = store.reserve(**args)
    data = b"test video"
    content = b'{"duration":1000000}'
    with zipfile.ZipFile(store.package_path(record["delivery_id"]), "w") as archive:
        for name, body in {
            "draft_info.json": content,
            "assets/video.mp4": data,
            "xm_materials.json": json.dumps({"assets": {"video.mp4": hashlib.sha256(data).hexdigest()}}),
            "xm_delivery.json": json.dumps({key: record[key] for key in ("delivery_id", "draft_id", "basis")}),
        }.items():
            archive.writestr(f"{record['folder']}/{name}", body)
    store.patch(
        record["delivery_id"], state="ready", content_sha256=hashlib.sha256(content).hexdigest(), duration_us=1000000
    )
    return store, record, args, root, index


def test_registration_preserves_unknown_fields_and_retries_keep_manual_edits(tmp_path, monkeypatch):
    store, record, args, root, index = prepared(tmp_path, monkeypatch)
    result = store.register(record["delivery_id"])
    assert result["state"] == "delivered"
    current = load_json(index)
    assert current["future"] == {"preserve": True}
    assert current["all_draft_store"][1] == {"draft_id": "existing", "unknown": [1, 2]}
    assert current["draft_ids"] == 8
    target = root / record["folder"] / "draft_info.json"
    target.write_bytes(b"encrypted manual editor output")
    assert store.reserve(**args)["delivery_id"] == record["delivery_id"]
    assert store.register(record["delivery_id"])["state"] == "delivered"
    assert target.read_bytes() == b"encrypted manual editor output"
    assert len(load_json(index)["all_draft_store"]) == 2
    new = store.reserve(**{**args, "new_version": True, "previous_id": record["delivery_id"]})
    assert new["draft_id"] != record["draft_id"]
    assert store.reserve(**{**args, "new_version": True, "previous_id": record["delivery_id"]}) == new


@pytest.mark.parametrize(("state", "expected"), [("running", "waiting_for_exit"), ("unknown", "process_unknown")])
def test_does_not_write_when_process_is_running_or_unknown(tmp_path, monkeypatch, state, expected):
    store, record, _, root, index = prepared(tmp_path, monkeypatch)
    before = index.read_bytes()
    monkeypatch.setattr(local, "process_state", lambda: state)
    assert store.register(record["delivery_id"])["state"] == expected
    assert index.read_bytes() == before
    assert not (root / record["folder"]).exists()


@pytest.mark.parametrize("after_index", [False, True])
def test_recovers_after_directory_or_index_commit_and_restart(tmp_path, monkeypatch, after_index):
    store, record, _, root, index = prepared(tmp_path, monkeypatch)
    write = local.atomic_write_json

    def crash(path, data):
        if path == index:
            if after_index:
                write(path, data)
            raise OSError("simulated interruption")
        write(path, data)

    monkeypatch.setattr(local, "atomic_write_json", crash)
    with pytest.raises(OSError, match="simulated interruption"):
        store.register(record["delivery_id"])
    assert (root / record["folder"]).exists()
    monkeypatch.setattr(local, "atomic_write_json", write)
    if after_index:
        (root / record["folder"] / "draft_info.json").write_bytes(b"manual saved content")
    restarted = local.LocalDeliveryStore(tmp_path / "data")
    assert restarted.register(record["delivery_id"])["state"] == "delivered"
    assert len(load_json(index)["all_draft_store"]) == 2


def test_external_index_change_is_not_overwritten(tmp_path, monkeypatch):
    store, record, _, _, index = prepared(tmp_path, monkeypatch)
    calls = 0

    def process():
        nonlocal calls
        calls += 1
        if calls == 2:
            value = load_json(index)
            value["external"] = "keep me"
            atomic_write_json(index, value)
        return "stopped"

    monkeypatch.setattr(local, "process_state", process)
    with pytest.raises(local.DeliveryError, match="索引已变化"):
        store.register(record["delivery_id"])
    assert load_json(index)["external"] == "keep me"
    assert store.register(record["delivery_id"])["state"] == "delivered"
    assert load_json(index)["external"] == "keep me"


def test_bad_media_digest_never_registers(tmp_path, monkeypatch):
    store, record, _, root, index = prepared(tmp_path, monkeypatch)
    with zipfile.ZipFile(store.package_path(record["delivery_id"]), "a") as archive:
        archive.writestr(f"{record['folder']}/xm_materials.json", json.dumps({"assets": {"video.mp4": "wrong"}}))
    with pytest.raises(local.DeliveryError, match="素材校验失败"):
        store.register(record["delivery_id"])
    assert len(load_json(index)["all_draft_store"]) == 1
    assert not (root / record["folder"]).exists()


@pytest.mark.parametrize("name", ["CON", "aux.mp4", "lpt9.txt", "尾点. ", "a:b?.mp4", "中文 空格.mp4"])
def test_portable_names(name):
    cleaned = portable_name(name)
    assert cleaned == portable_name(cleaned)
    assert not any(c in cleaned for c in '<>:"/\\|?*')
    assert not cleaned.endswith((".", " "))
    assert cleaned.split(".")[0].upper() not in {"CON", "AUX", "LPT9"}


def test_windows_tasklist_and_unknown_failure(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(local.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        local.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout='"JianyingPro.exe","4321","Console","1","10 K"\n'),
    )
    assert local.process_state() == "running"
    monkeypatch.setattr(
        local.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout='"explorer.exe","123","Console","1","10 K"\n')
    )
    assert local.process_state() == "stopped"
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=""))
    assert local.process_state() == "unknown"


def test_removed_draft_is_observed_without_resurrection(tmp_path, monkeypatch):
    store, record, _, _, index = prepared(tmp_path, monkeypatch)
    delivered = store.register(record["delivery_id"])
    value = load_json(index)
    value["all_draft_store"][0]["tm_draft_removed"] = 123
    atomic_write_json(index, value)
    before = index.read_bytes()
    assert store.observed(delivered)["state"] == "removed"
    store.register(record["delivery_id"])
    assert index.read_bytes() == before


def test_mac_tray_after_normal_exit_does_not_block_registration(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(local.platform, "system", lambda: "Darwin")
    tray = "/Applications/VideoFusion-macOS.app/Contents/Frameworks/VideoFusion-macOSTrayHelper.app/Contents/MacOS/VideoFusion-macOSTrayHelper\n"
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=tray))
    assert local.process_state() == "stopped"
    for process in (
        "/Applications/VideoFusion-macOS.app/Contents/MacOS/VideoFusion-macOS",
        "/Applications/VideoFusion-macOS.app/Contents/Frameworks/VideoFusion-macOS Helper",
    ):
        monkeypatch.setattr(local.subprocess, "run", lambda *a, current=process, **k: SimpleNamespace(stdout=tray + current))
        assert local.process_state() == "running"
