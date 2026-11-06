"""Use real render/package code, isolated local index, and no model calls."""

import json
import zipfile

import pytest

from lib.edit_timeline.model import ClipSubtitle, EditTimelineContent
from lib.edit_timeline.store import EditTimelineStore
from lib.infra.json_io import atomic_write_json, load_json
from lib.jianying_draft import local_delivery as local
from server.services.presentation.jianying_delivery import delivery_store, execute_delivery
from server.services.presentation.timeline_jianying_draft import TimelineJianyingDraftService
from tests.integration.server.services.presentation.timeline_render_support import (
    _append_revision,
    edited_timeline,
    setup_project,
)


async def test_real_manual_subtitles_packaging_wait_resume_and_independent_new_version(tmp_path, monkeypatch):
    pm, _project = setup_project(tmp_path)
    timeline_id = await edited_timeline(pm)
    content = EditTimelineStore(pm, "demo").find(timeline_id).latest.content
    first, second = content.clips
    cue = ClipSubtitle(
        id="s1",
        text="人工中文字幕",
        start_us=600000,
        end_us=1200000,
        basis_version=1,
        font_size=60,
        color="#FF0000",
        position="top",
    )
    _append_revision(
        pm,
        timeline_id,
        EditTimelineContent(
            clips=(first.model_copy(update={"subtitles": (cue,)}), second.model_copy(update={"subtitles": ()}))
        ),
    )
    service = TimelineJianyingDraftService(pm)
    job = await service.prepare("demo", timeline_id, narration="without_narration")
    root = tmp_path / "剪映 草稿"
    root.mkdir()
    atomic_write_json(root / "root_meta_info.json", {"all_draft_store": [], "future": 42})
    monkeypatch.setattr(local, "compatibility", lambda _: {"ready": True})
    monkeypatch.setattr(local, "process_state", lambda: "running")
    store = delivery_store(pm)
    store.save_settings(local.LocalSettings(enabled=True, app_path=str(tmp_path), draft_root=str(root)))
    args = {
        "project": "demo",
        "episode": 1,
        "timeline_id": timeline_id,
        "revision": job.revision,
        "narration": job.narration,
        "basis": job.basis.digest,
        "name": "中文 测试",
        "new_version": False,
        "previous_id": None,
        "local": True,
    }
    record = store.reserve(**args)
    result = await execute_delivery(pm, record["delivery_id"])
    assert result["state"] == "waiting_for_exit"
    assert not (root / record["folder"]).exists()
    with zipfile.ZipFile(store.package_path(record["delivery_id"])) as archive:
        draft = json.loads(archive.read(f"{record['folder']}/draft_info.json"))
        texts = draft["materials"]["texts"]
        assert len(texts) == 1
        text = json.loads(texts[0]["content"])
        assert text["text"] == "人工中文字幕"
        assert text["styles"][0]["fill"]["content"]["solid"]["color"] == [1.0, 0.0, 0.0]
        segment = next(t for t in draft["tracks"] if t["type"] == "text")["segments"][0]
        assert segment["target_timerange"] == {"start": 100000, "duration": 600000}
        assert segment["clip"]["transform"]["y"] == 0.75
        assert all(m["path"].startswith(str(root / record["folder"])) for m in draft["materials"]["videos"])
    monkeypatch.setattr(local, "process_state", lambda: "stopped")
    result = await execute_delivery(pm, record["delivery_id"])
    assert result["state"] == "delivered"
    target = root / record["folder"] / "draft_info.json"
    target.write_bytes(b"manual encrypted editor result")
    new = store.reserve(**{**args, "new_version": True, "previous_id": record["delivery_id"]})
    assert (await execute_delivery(pm, new["delivery_id"]))["state"] == "delivered"
    assert target.read_bytes() == b"manual encrypted editor result"
    assert len(load_json(root / "root_meta_info.json")["all_draft_store"]) == 2


async def test_invalid_manual_subtitles_are_refused_before_render(tmp_path):
    from lib.jianying_draft.errors import JianyingDraftError

    pm, _ = setup_project(tmp_path)
    timeline_id = await edited_timeline(pm)
    content = EditTimelineStore(pm, "demo").find(timeline_id).latest.content
    first, second = content.clips
    cue = ClipSubtitle(id="s1", text="失效", start_us=0, end_us=100000, basis_version=9)
    _append_revision(
        pm, timeline_id, EditTimelineContent(clips=(first.model_copy(update={"subtitles": (cue,)}), second))
    )
    with pytest.raises(JianyingDraftError):
        await TimelineJianyingDraftService(pm).prepare("demo", timeline_id, narration="without_narration")
