"""Local spectral-onset analysis. All public times are integer microseconds.

Onsets are observed transients, not an invented uniform BPM grid. Weak or
non-periodic material still exposes a waveform but never enables safe recuts.
"""

import asyncio
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from lib.infra.ffmpeg import ffmpeg_executable, local_file_input
from lib.infra.subprocess_deadline import Spawner, run_with_deadline

SAMPLE_RATE = 11025
HOP = 220
MAX_ANALYSIS_SECONDS = 1200
ANALYSIS_VERSION = 2


@dataclass(frozen=True)
class BeatAnalysis:
    bpm: float | None
    candidates: tuple[float, ...]
    confidence: float
    beats_us: tuple[int, ...]
    waveform: tuple[float, ...]

    def __post_init__(self) -> None:
        if (not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
                or (self.bpm is not None and (not math.isfinite(self.bpm) or not 55 <= self.bpm <= 181))
                or any(not math.isfinite(v) or not 55 <= v <= 181 for v in self.candidates)
                or any(type(t) is not int or t < 0 for t in self.beats_us)
                or list(self.beats_us) != sorted(set(self.beats_us))
                or len(self.waveform) > 256
                or any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.waveform)):
            raise ValueError("Invalid beat analysis cache")

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_samples(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> BeatAnalysis:
    samples = np.asarray(samples, dtype=np.float32)
    empty = BeatAnalysis(None, (), 0.0, (), ())
    if len(samples) < 2048 or not np.isfinite(samples).all():
        return empty
    chunks = np.array_split(np.abs(samples), min(256, len(samples)))
    wave = np.array([float(np.max(chunk)) for chunk in chunks])
    peak = float(wave.max())
    waveform = tuple(np.round(wave / peak, 3).tolist()) if peak > 1e-5 else tuple(0.0 for _ in wave)
    if peak < 1e-5:
        return BeatAnalysis(None, (), 0.0, (), waveform)
    frames = np.lib.stride_tricks.sliding_window_view(samples, 1024)[::HOP]
    window = np.hanning(1024)
    flux = np.zeros(len(frames))
    previous = np.zeros(513)
    for start in range(0, len(frames), 1024):
        spectrum = np.abs(np.fft.rfft(frames[start:start + 1024] * window, axis=1))
        diff = np.diff(spectrum, axis=0, prepend=previous[None, :])
        flux[start:start + len(spectrum)] = np.maximum(diff, 0).mean(axis=1)
        previous = spectrum[-1]
    if not np.any(flux):
        return BeatAnalysis(None, (), 0.0, (), waveform)
    # Reject the background floor, then merge transients within 180 ms.
    threshold = float(np.median(flux) + 0.2 * (flux.max() - np.median(flux)))
    peaks = np.flatnonzero((flux[1:-1] > flux[:-2]) & (flux[1:-1] >= flux[2:]) & (flux[1:-1] > threshold)) + 1
    picked: list[int] = []
    separation = max(1, round(0.18 * sample_rate / HOP))
    for index in peaks.tolist():
        if picked and index - picked[-1] < separation:
            if flux[index] > flux[picked[-1]]:
                picked[-1] = index
        else:
            picked.append(index)
    # Spectral peaks occur before a transient reaches the window centre. Locate
    # the energy rise inside that window instead of adding a fixed timing bias.
    energy = np.convolve(samples.astype(np.float64) ** 2, np.ones(55) / 55, mode="same")
    located = []
    for index in picked:
        start = index * HOP
        end = min(len(samples), start + 1024)
        rise = np.diff(energy[start:end], prepend=energy[max(0, start - 1)])
        located.append(round((start + int(np.argmax(rise))) / sample_rate * 1_000_000))
    beats = tuple(sorted(set(located)))
    if len(beats) < 4:
        return BeatAnalysis(None, (), 0.0, beats, waveform)
    envelope = np.maximum(flux - np.median(flux), 0)
    norm = float(envelope @ envelope)
    min_lag = max(1, round(60 * sample_rate / (180 * HOP)))
    max_lag = min(len(flux) // 2, round(60 * sample_rate / (55 * HOP)))
    scores = [(float(envelope[:-lag] @ envelope[lag:]) / norm, lag)
              for lag in range(min_lag, max_lag + 1)] if norm else []
    scores.sort(reverse=True)
    candidates: list[float] = []
    for _, lag in scores:
        bpm = round(60 * sample_rate / (HOP * lag), 1)
        if all(abs(bpm - item) > 5 for item in candidates):
            candidates.append(bpm)
        if len(candidates) == 3:
            break
    confidence = round(min(1.0, max(0.0, scores[0][0])) if scores else 0.0, 3)
    return BeatAnalysis(candidates[0] if candidates else None, tuple(candidates), confidence, beats, waveform)


async def analyze_audio(path: Path, *, duration_us: int, spawn: Spawner | None = None) -> BeatAnalysis:
    if duration_us > MAX_ANALYSIS_SECONDS * 1_000_000:
        raise ValueError("Music analysis supports up to 20 minutes per track")
    ffmpeg = await asyncio.to_thread(ffmpeg_executable)
    decoded = await run_with_deadline([
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *local_file_input(path),
        "-map", "0:a:0", "-t", str(MAX_ANALYSIS_SECONDS), "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "pipe:1",
    ], deadline_seconds=120, capture_stdout=True, capture_stderr=True, spawn=spawn)
    if decoded.returncode:
        raise ValueError("Cannot decode music for beat analysis")
    return await asyncio.to_thread(analyze_samples, np.frombuffer(decoded.stdout, dtype="<f4"))
