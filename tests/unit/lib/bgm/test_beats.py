"""Known transient timing, confidence and conservative boundary protection."""
from dataclasses import replace

import numpy as np
import pytest

from lib.bgm.beat_sync import propose
from lib.bgm.beats import SAMPLE_RATE, BeatAnalysis, analyze_samples
from lib.edit_timeline.model import (
    EditTimelineContent,
    EditTimelineDocument,
    RevisionAuthor,
    TimelineRevision,
)
from lib.edit_timeline.sources import BgmMedia, EpisodeScriptUnits, EpisodeSources, ScriptUnit, UnitMedia
from lib.speech.speech_composition import SpeechMode


def test_pulses_preserve_observed_times_and_estimate_tempo():
    samples = np.zeros(SAMPLE_RATE * 12, dtype=np.float32)
    times = np.arange(.5, 11.6, .5)
    for time in times:
        start = round(time * SAMPLE_RATE)
        samples[start:start + 120] = np.hanning(120)
    result = analyze_samples(samples)
    assert result.bpm == pytest.approx(120, abs=2)
    assert result.confidence > .7
    assert len(result.beats_us) == len(times)
    assert max(abs(t / 1e6 - expected) for t, expected in zip(result.beats_us, times, strict=True)) < .025
    assert list(result.beats_us) == sorted(set(result.beats_us))
    assert len(result.waveform) == 256


@pytest.mark.parametrize("samples", [np.zeros(40000), np.zeros(100), np.array([np.nan] * 40000)])
def test_unusable_samples_never_enable_safe_recuts(samples):
    result = analyze_samples(samples)
    assert result.bpm is None
    assert result.confidence == 0
    assert result.beats_us == ()


@pytest.mark.parametrize("change", [{"confidence": float('nan')}, {"beats_us": [1, 1]}, {"beats_us": [1.5]}, {"waveform": [2]}, {"bpm": 0}])
def test_invalid_cached_facts_are_rejected(change):
    with pytest.raises(ValueError, match="Invalid beat analysis cache"):
        BeatAnalysis(**{"bpm": 120, "candidates": (120,), "confidence": .8, "beats_us": (1, 2), "waveform": (.5,), **change})


def scene():
    content = EditTimelineContent.model_validate({"clips": [
        {"id": "c1", "unit_id": "U1", "source_volume": 1, "trim": {"in_us": 0, "out_us": 1000000, "basis_version": 1}},
        {"id": "c2", "unit_id": "U2", "source_volume": 1, "trim": {"in_us": 200000, "out_us": 1200000, "basis_version": 1}},
    ], "bgm": [{"id": "b1", "bgm_id": "song", "start_us": 0, "in_us": 0, "out_us": 2000000, "volume": .5}]})
    doc = EditTimelineDocument(id="tl-01234567", episode=1, name="test", created_at="2026-11-08", next_clip_number=3, next_bgm_number=2,
        revisions=(TimelineRevision(number=1, parent=None, author=RevisionAuthor(kind="creator", user_id="u1"), summary="test", created_at="2026-11-08", content=content),))
    script = EpisodeScriptUnits(1, "video_units", tuple(ScriptUnit(u, SpeechMode.SILENT, 1000000) for u in ("U1", "U2")))
    sources = EpisodeSources(script, {u: UnitMedia(1, 2000000, None) for u in ("U1", "U2")}, False, {"song": BgmMedia("song", 2000000)})
    return doc, sources


def test_safe_move_preserves_duration_and_speech_changes_invalidate_basis():
    doc, sources = scene()
    music = {"song": BeatAnalysis(120, (120,), .9, (1050000,), (.5,))}
    report = propose(doc, sources, music, {"song": "fingerprint"})
    cut = report["suggestions"][0]
    assert cut["status"] == "near"
    assert cut["offset_us"] == 50000
    assert cut["operations"][0]["trim"]["source_out"] == 1.05
    assert cut["operations"][1]["trim"]["source_in"] == .25
    script = replace(sources.script, units=(replace(sources.script.units[0], subtitle_text="hello"), sources.script.units[1]))
    protected = propose(doc, replace(sources, script=script), music, {"song": "fingerprint"})
    assert protected["basis"] != report["basis"]
    assert protected["suggestions"][0]["status"] == "conflict"
    assert protected["suggestions"][0]["operations"] == []


def test_low_confidence_and_distant_beats_require_review():
    doc, sources = scene()
    for confidence, beat in ((.1, 1050000), (.9, 1500000)):
        report = propose(doc, sources, {"song": BeatAnalysis(120, (120,), confidence, (beat,), (.5,))}, {})
        assert report["suggestions"][0]["status"] == "unconfirmed"
        assert report["suggestions"][0]["operations"] == []
