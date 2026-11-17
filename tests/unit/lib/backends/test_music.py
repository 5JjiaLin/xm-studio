"""Official MiniMax synchronous request contract without paid calls."""
import httpx
import pytest

from lib.backends.music import MiniMaxMusicBackend, MusicRejectedError, MusicRequest, decode_music
from tests.http_capture import capture_http, only_request, request_json


async def test_official_single_post_decodes_hex_and_measures_milliseconds(tmp_path):
    output = tmp_path / "score.mp3"
    with capture_http() as router:
        route = router.post("https://api.minimax.cn/v1/music_generation").respond(200, json={
            "base_resp": {"status_code": 0}, "data": {"status": 2, "audio": b"ID3music".hex()},
            "extra_info": {"music_duration": 32100}})
        result = await MiniMaxMusicBackend(api_key="test").generate(MusicRequest("quiet instrumental", output))
    request = only_request(route)
    assert request.headers["authorization"] == "Bearer test"
    assert request_json(request) == {"model": "music-3.0", "prompt": "quiet instrumental", "stream": False,
        "output_format": "hex", "is_instrumental": True, "audio_setting": {"format": "mp3", "sample_rate": 44100, "bitrate": 256000}}
    assert output.read_bytes() == b"ID3music"
    assert result.duration_seconds == 32.1


async def test_ambiguous_submission_is_not_retried(tmp_path):
    output = tmp_path / "score.mp3"
    with capture_http() as router:
        route = router.post("https://relay.example/v1/music_generation").mock(side_effect=httpx.ReadTimeout("lost"))
        with pytest.raises(Exception, match=r"ambiguous|可能|lost"):
            await MiniMaxMusicBackend(api_key="test", base_url="https://relay.example").generate(MusicRequest("score", output))
    assert route.call_count == 1
    assert not output.exists()


@pytest.mark.parametrize("data", [{}, {"status": 1}, {"status": 2, "audio": "nohex"}, {"status": 2, "audio": " "}])
def test_incomplete_audio_remains_unknown(data):
    with pytest.raises(RuntimeError):
        decode_music({"base_resp": {"status_code": 0}, "data": data})


def test_explicit_rejection_does_not_expose_provider_message():
    with pytest.raises(MusicRejectedError, match="2013") as error:
        decode_music({"base_resp": {"status_code": 2013, "status_msg": "secret"}})
    assert "secret" not in str(error.value)
