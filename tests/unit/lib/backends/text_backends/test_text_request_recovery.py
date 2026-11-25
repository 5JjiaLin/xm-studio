"""Real SDK wire contracts: ambiguous generations are never replayed or downgraded."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from anthropic import APIError as AnthropicAPIError
from google.genai.errors import APIError as GeminiAPIError
from openai import APIError, BadRequestError
from pydantic import BaseModel
from volcenginesdkarkruntime._exceptions import ArkAPIError

from lib.backends.text_backends.anthropic import AnthropicTextBackend
from lib.backends.text_backends.ark import ArkTextBackend
from lib.backends.text_backends.base import TextCapability, TextGenerationRequest
from lib.backends.text_backends.gemini import GeminiTextBackend
from lib.backends.text_backends.openai import OpenAITextBackend
from lib.backends.text_backends.openai_responses import OpenAIResponsesTextBackend
from lib.custom_provider.factory import create_custom_backend
from lib.infra.text_utils import DuplicateJSONKeyError
from tests.fakes import bounded_poll_clock
from tests.http_capture import capture_http, request_json
from tests.unit.lib.backends.text_backends.test_anthropic_text_backend import _sse as messages_sse
from tests.unit.lib.backends.text_backends.test_openai_long_thinking import _chat_events, _sse


class _Output(BaseModel):
    text: str


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "ark", "gemini"])
async def test_ambiguous_json_response_is_terminal_without_paid_fallback(protocol):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(return_value=_success(protocol, '{"text":"first","text":"second"}'))
            with pytest.raises(DuplicateJSONKeyError):
                await backend.generate(TextGenerationRequest(prompt="test", response_schema=_Output))
            assert route.call_count == 1


@asynccontextmanager
async def _backend(protocol):
    kwargs = {"api_key": "test", "model": "arbitrary-model", "base_url": "https://relay.test/v1"}
    if protocol == "gemini":
        backend = GeminiTextBackend(**{**kwargs, "base_url": "https://relay.test"})
        url = "https://relay.test/v1/models/arbitrary-model:generateContent"
    elif protocol == "ark":
        # Declare native support explicitly: test catalog snapshots need not contain it.
        backend = ArkTextBackend(**{**kwargs, "model": "doubao-seed-2-1-lite-260915"})
        backend._capabilities.add(TextCapability.STRUCTURED_OUTPUT)
        url = "https://relay.test/v1/chat/completions"
    else:
        backend_type = {
            "chat": OpenAITextBackend,
            "responses": OpenAIResponsesTextBackend,
            "anthropic": AnthropicTextBackend,
        }[protocol]
        backend = backend_type(**{**kwargs, **({"base_url": "https://relay.test"} if protocol == "anthropic" else {})})
        url = (
            "https://relay.test/v1/"
            + {"chat": "chat/completions", "responses": "responses", "anthropic": "messages"}[protocol]
        )
    try:
        yield backend, url
    finally:
        if isinstance(backend, ArkTextBackend):
            backend._client.close()
            backend._openai_client.close()
        elif isinstance(backend, GeminiTextBackend):
            await backend._client.aio.aclose()
            backend._client.close()
        else:
            await backend._client.close()


def _success(protocol, text='{"text":"完整提示词"}'):
    if protocol == "anthropic":
        return messages_sse(text, usage={"input_tokens": 10}, output_tokens=5)
    if protocol == "gemini":
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]},
        )
    if protocol == "responses":
        return httpx.Response(
            200,
            json={
                "id": "r1",
                "object": "response",
                "created_at": 0,
                "model": "arbitrary-model",
                "status": "completed",
                "output": [
                    {
                        "id": "m1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            },
        )
    return httpx.Response(
        200,
        json={
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "arbitrary-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        },
    )


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "ark", "gemini"])
@pytest.mark.parametrize("failure", [400, 524, "read", "write"])
async def test_unknown_failure_never_replays_or_changes_protocol(protocol, failure):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url)
            if isinstance(failure, int):
                route.respond(failure, json={"error": {"message": "Upstream request failed"}})
            else:
                route.mock(
                    side_effect=httpx.ReadTimeout("read timeout")
                    if failure == "read"
                    else httpx.WriteError("write failed")
                )
            with (
                bounded_poll_clock(),
                pytest.raises(
                    (APIError, AnthropicAPIError, ArkAPIError, GeminiAPIError, httpx.TransportError)
                ) as raised,
            ):
                await backend.generate(TextGenerationRequest(prompt="完整上下文", response_schema=_Output))
            assert route.call_count == 1, (repr(raised.value), repr(raised.value.__cause__))
            body = request_json(route.calls[0].request)
            assert any(key in body for key in ("response_format", "text", "output_config", "generationConfig"))


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "ark", "gemini"])
async def test_explicit_rate_limit_retries_only_rejected_call(protocol):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(
                side_effect=[
                    httpx.Response(429, json={"error": {"message": "rate limit exceeded"}}),
                    _success(protocol),
                ]
            )
            with bounded_poll_clock():
                result = await backend.generate(TextGenerationRequest(prompt="写提示词", response_schema=_Output))
            assert json.loads(result.text) == {"text": "完整提示词"}
            assert route.call_count == 2
            assert request_json(route.calls[0].request) == request_json(route.calls[1].request)


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "ark", "gemini"])
async def test_connect_failure_can_retry_before_submission(protocol):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(side_effect=[httpx.ConnectError("connection refused"), _success(protocol)])
            with bounded_poll_clock():
                result = await backend.generate(TextGenerationRequest(prompt="写提示词"))
            assert json.loads(result.text) == {"text": "完整提示词"}
            assert route.call_count == 2


@pytest.mark.parametrize(
    ("protocol", "param"),
    [
        ("chat", "response_format"),
        ("responses", "text.format"),
        ("anthropic", "output_config"),
        ("ark", "response_format"),
        ("gemini", "response_schema"),
    ],
)
async def test_only_explicit_schema_rejection_permits_validated_fallback(protocol, param):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(
                side_effect=[
                    httpx.Response(400, json={"error": {"message": f"{param} is not supported"}}),
                    _success(protocol),
                ]
            )
            # Chat/Ark dict schema uses json_object, the other protocols inject into prompt.
            schema = (
                {"type": "object", "properties": {"text": {"type": "string"}}}
                if protocol in ("chat", "ark")
                else _Output
            )
            result = await backend.generate(TextGenerationRequest(prompt="保持质量", response_schema=schema))
            assert json.loads(result.text) == {"text": "完整提示词"}
            assert route.call_count == 2
            first, second = [request_json(call.request) for call in route.calls]
            assert first != second


@pytest.mark.parametrize("protocol", ["chat", "ark", "gemini"])
async def test_fallback_read_failure_does_not_replay_native_or_fallback(protocol):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(
                side_effect=[_success(protocol, "非 JSON 散文"), httpx.ReadTimeout("read timeout")]
            )
            with bounded_poll_clock(), pytest.raises((APIError, GeminiAPIError, httpx.TransportError)):
                await backend.generate(TextGenerationRequest(prompt="写提示词", response_schema=_Output))
            assert route.call_count == 2
            first, second = [request_json(call.request) for call in route.calls]
            assert first != second
            assert any(key in first for key in ("response_format", "generationConfig"))


async def test_chat_stream_options_rejection_only_removes_options():
    async with _backend("chat") as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(
                side_effect=[
                    httpx.Response(400, json={"error": {"message": "Unsupported parameter: stream_options"}}),
                    _sse(_chat_events()),
                ]
            )
            result = await backend.generate(
                TextGenerationRequest(prompt="写提示词", response_schema=_Output, prefer_stream=True)
            )
            bodies = [request_json(call.request) for call in route.calls]
            assert json.loads(result.text) == {"text": "完整提示词"}
            assert len(bodies) == 2
            assert all(body["stream"] for body in bodies)
            assert "stream_options" in bodies[0]
            assert "stream_options" not in bodies[1]


async def test_stream_options_cannot_be_misread_as_stream_rejection():
    async with _backend("chat") as (backend, url):
        with capture_http() as router:
            route = router.post(url).respond(400, json={"error": {"message": "Unsupported parameter: stream_options"}})
            with pytest.raises(BadRequestError):
                await backend.generate(
                    TextGenerationRequest(prompt="写提示词", response_schema=_Output, prefer_stream=True)
                )
            assert route.call_count == 2
            assert all(request_json(call.request)["stream"] for call in route.calls)


@pytest.mark.parametrize(("failure", "expected_calls"), [("429", 2), ("read", 1)])
async def test_custom_openai_path_override_keeps_the_same_retry_boundary(failure, expected_calls):
    from lib.db.models.custom_provider import CustomProvider

    provider = CustomProvider(
        id=7,
        display_name="relay",
        discovery_format="openai",
        base_url="https://relay.test/proxy",
        api_key="test",
    )
    backend = create_custom_backend(
        provider=provider,
        model_id="relay-model",
        endpoint="openai-chat",
        path_overrides={"generate": "/v7/chat/completions"},
    )
    url = "https://relay.test/proxy/v7/chat/completions"
    try:
        with capture_http() as router:
            route = router.post(url)
            route.mock(
                side_effect=(
                    [httpx.Response(429, json={"error": {"message": "rate limit exceeded"}}), _success("chat")]
                    if failure == "429"
                    else [httpx.ReadTimeout("acceptance unknown")]
                )
            )
            if failure == "429":
                with bounded_poll_clock():
                    result = await backend.generate(TextGenerationRequest(prompt="test"))
                assert json.loads(result.text) == {"text": "完整提示词"}
            else:
                with bounded_poll_clock(), pytest.raises(APIError):
                    await backend.generate(TextGenerationRequest(prompt="test"))
        assert route.call_count == expected_calls
    finally:
        await backend._delegate._client.close()


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_explicit_stream_rejection_can_use_non_stream(protocol):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(
                side_effect=[
                    httpx.Response(400, json={"error": {"message": "stream is not supported"}}),
                    _success(protocol),
                ]
            )
            result = await backend.generate(
                TextGenerationRequest(prompt="写提示词", response_schema=_Output, prefer_stream=True)
            )
            assert json.loads(result.text) == {"text": "完整提示词"}
            bodies = [request_json(call.request) for call in route.calls]
            assert bodies[0]["stream"] is True
            assert not bodies[1].get("stream", False)


async def test_schema_is_not_mutated_to_advertise_strict_support():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "extra": {"type": "object"}},
        "required": ["text"],
    }
    original = json.dumps(schema)
    async with _backend("chat") as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(return_value=_success("chat"))
            await backend.generate(TextGenerationRequest(prompt="写提示词", response_schema=schema))
            format_schema = request_json(route.calls[0].request)["response_format"]["json_schema"]
            assert format_schema["schema"] == schema
            assert "strict" not in format_schema
            assert json.dumps(schema) == original


@pytest.mark.parametrize(("protocol", "attempts"), [("chat", 4), ("responses", 4), ("anthropic", 4), ("ark", 3), ("gemini", 3)])
async def test_plain_json_repair_rate_limit_has_a_finite_transport_budget(protocol, attempts):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).respond(429, json={"error": {"message": "rate limit exceeded"}})
            with bounded_poll_clock(), pytest.raises((APIError, AnthropicAPIError, ArkAPIError, GeminiAPIError)):
                await backend.generate(TextGenerationRequest(prompt="仅返回按错误修复的 JSON", prefer_stream=True))
            assert route.call_count == attempts
            bodies = [request_json(call.request) for call in route.calls]
            assert all(body == bodies[0] for body in bodies)


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "ark", "gemini"])
async def test_plain_json_repair_never_replays_an_ambiguous_failure(protocol):
    async with _backend(protocol) as (backend, url):
        with capture_http() as router:
            route = router.post(url).mock(side_effect=httpx.ReadTimeout("generation acceptance unknown"))
            with bounded_poll_clock(), pytest.raises((APIError, AnthropicAPIError, ArkAPIError, GeminiAPIError, httpx.TransportError)):
                await backend.generate(TextGenerationRequest(prompt="仅返回按错误修复的 JSON", prefer_stream=True))
            assert route.call_count == 1
