"""Music candidate contract and MiniMax synchronous instrumental adapter.

Contract verified 2026-11-08 against the official music-generation.md. No duration
field or polling API exists; target duration remains a creative prompt constraint.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.backend_runtime import submit_post
from lib.backends.minimax_shared import minimax_headers, minimax_text_base_url, resolve_minimax_api_key
from lib.backends.transport_layer import TransportLayer
from lib.infra.async_thread import run_noninterruptible_sync

DEFAULT_MUSIC_MODEL = "music-3.0"
MUSIC_PROVIDER = "minimax"


class MusicRejectedError(RuntimeError):
    """A complete provider response explicitly rejected this candidate."""


@dataclass(frozen=True)
class MusicRequest:
    prompt: str
    output_path: Path


@dataclass(frozen=True)
class MusicResult:
    provider: str
    model: str
    output_path: Path
    duration_seconds: float | None


class MusicBackend(Protocol):
    async def generate(self, request: MusicRequest) -> MusicResult: ...


def decode_music(body: object) -> tuple[bytes, float | None]:
    if not isinstance(body, dict) or not isinstance(body.get("base_resp"), dict):
        raise RuntimeError("MiniMax music response invalid; do not automatically resubmit")
    code = body["base_resp"].get("status_code")
    if isinstance(code, int) and code != 0:
        # Only a numeric code is retained; provider messages can echo request secrets.
        raise MusicRejectedError(f"MiniMax music rejected: status_code={code}")
    if code != 0:
        raise RuntimeError("MiniMax music status missing; do not automatically resubmit")
    data = body.get("data") or {}
    if not isinstance(data, dict) or data.get("status") != 2:
        raise RuntimeError("MiniMax music response incomplete; do not automatically resubmit")
    audio = data.get("audio")
    if not isinstance(audio, str) or not audio or len(audio) > 200 * 1024 * 1024:
        raise RuntimeError("MiniMax music audio missing or too large")
    try:
        content = bytes.fromhex(audio)
    except ValueError as exc:
        raise RuntimeError("MiniMax music audio is not hex") from exc
    if not content:
        raise RuntimeError("MiniMax music returned empty audio")
    extra = body.get("extra_info")
    duration = extra.get("music_duration") if isinstance(extra, dict) else None
    return content, duration / 1000 if isinstance(duration, (float, int)) and math.isfinite(duration) and duration > 0 else None


class MiniMaxMusicBackend:
    def __init__(self, *, api_key: str, base_url: str | None = None,
                 transport_layer: TransportLayer | None = None) -> None:
        self._key = resolve_minimax_api_key(api_key)
        self._base = minimax_text_base_url(base_url or "https://api.minimax.cn/v1")
        self._transport = transport_layer

    async def generate(self, request: MusicRequest) -> MusicResult:
        if not 1 <= len(request.prompt.strip()) <= 2000:
            raise ValueError("Music prompt must contain 1–2000 characters")
        async with artifact_http_client(timeout=600, transport_layer=self._transport) as client:
            # Exactly one paid POST. Ambiguous transport failures are never retried.
            response = await submit_post(lambda: client.post(
                f"{self._base}/music_generation", headers=minimax_headers(self._key), json={
                    "model": DEFAULT_MUSIC_MODEL, "prompt": request.prompt,
                    "stream": False, "output_format": "hex", "is_instrumental": True,
                    "audio_setting": {"format": "mp3", "sample_rate": 44100, "bitrate": 256000},
                }), provider=MUSIC_PROVIDER, include_provider_reason=False)
        content, duration = await run_noninterruptible_sync(lambda: decode_music(response.json()))
        def install() -> None:
            staged = request.output_path.with_suffix(".part")
            staged.write_bytes(content)
            staged.replace(request.output_path)
        await run_noninterruptible_sync(install)
        return MusicResult(MUSIC_PROVIDER, DEFAULT_MUSIC_MODEL, request.output_path, duration)
