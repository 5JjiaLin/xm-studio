"""Beat suggestions must remain tied to the actual project revision."""

import pytest

from lib.bgm.beat_sync import BeatSyncService
from lib.bgm.beats import BeatAnalysis
from lib.bgm.service import BgmLibraryService
from lib.edit_timeline import RevisionAuthor
from lib.edit_timeline.operations import TimelineOperationAdapter
from tests.factories import wav_bytes


async def test_beat_apply_creates_revision_and_stale_report_is_rejected(pm, service, install_video, monkeypatch):
    script = pm.load_script_readonly("demo", "episode_1.json")
    script["video_units"] = [
        {"unit_id": "E1U1", "text": "画面一", "duration_seconds": 2},
        {"unit_id": "E1U2", "text": "画面二", "duration_seconds": 2},
    ]
    pm.save_script("demo", script, "episode_1.json")
    install_video("E1U1", 2)
    install_video("E1U2", 2)
    author = RevisionAuthor(kind="creator", user_id="u1")
    created = await service.create_from_script("demo", episode=1, name="初剪", author=author)
    timeline = created.timeline.id
    song = await BgmLibraryService(pm).upload("demo", filename="beat.wav", content=wav_bytes(3, tone_hz=330))
    operations = [
        {"op": "set_trim", "clip": "c1", "trim": {"source_in": 0, "source_out": 1}},
        {"op": "set_trim", "clip": "c2", "trim": {"source_in": .2, "source_out": 1.2}},
        {"op": "insert_bgm", "bgm_id": song.id, "start": 0, "source_out": 2},
    ]
    await service.edit("demo", timeline, base_revision=1, summary="准备", author=author,
                       operations=[TimelineOperationAdapter.validate_python(op) for op in operations])

    async def fake_analysis(*args, **kwargs):
        return BeatAnalysis(120, (120,), .9, (1050000,), (.5,))

    monkeypatch.setattr("lib.bgm.beat_sync.analyze_audio", fake_analysis)
    beats = BeatSyncService(pm)
    report = await beats.analyze("demo", timeline)
    assert report["suggestions"][0]["status"] == "near"
    await beats.apply("demo", timeline, basis=report["basis"], ids=["c1/c2"], author=author)
    aligned = await service.read("demo", timeline)
    assert aligned.revision == 3
    assert aligned.duration == pytest.approx(2)
    with pytest.raises(ValueError, match="expired"):
        await beats.apply("demo", timeline, basis=report["basis"], ids=["c1/c2"], author=author)
    await service.restore("demo", timeline, revision=2, expected_latest=3, author=author)
    assert (await service.read("demo", timeline)).revision == 4
