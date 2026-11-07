"""长思考调用：通过真实 SDK + HTTP 替身验证流式聚合、超时和完整性。"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from lib.backends.text_backends.base import TextGenerationRequest, TextOutputTruncatedError
from lib.backends.text_backends.openai import OpenAITextBackend
from lib.backends.text_backends.openai_responses import OpenAIResponsesTextBackend
from tests.http_capture import PathPrefixRewrite, capture_http, only_request, request_json


class _Script(BaseModel):
    text: str


def _sse(events: list[dict]) -> httpx.Response:
    # 注释模拟思考期间的 SSE 心跳，不应混入最终正文。
    body = ": thinking\n\n" + "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body + "data: [DONE]\n\n")


def _chat_events(*, finish_reason: str | None = "stop") -> list[dict]:
    def chunk(choices: list[dict], **extra) -> dict:
        return {
            "id": "chat-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-6-astra",
            "choices": choices,
            **extra,
        }

    return [
        chunk(
            [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "", "reasoning_content": "thinking"},
                    "finish_reason": None,
                }
            ]
        ),
        chunk([{"index": 0, "delta": {"content": '{"text":'}, "finish_reason": None}]),
        chunk([{"index": 0, "delta": {"content": '"完整提示词"}'}, "finish_reason": finish_reason}]),
        chunk(
            [],
            usage={
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "total_tokens": 70,
                "prompt_tokens_details": {"cached_tokens": 10},
            },
        ),
    ]


def _responses_events(*, status: str = "completed") -> list[dict]:
    response = {
        "id": "resp-1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-astra",
        "status": status,
        "error": {"code": "server_error", "message": "upstream failed"} if status == "failed" else None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "output": [
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"text":"完整提示词"}', "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 50,
            "output_tokens": 20,
            "total_tokens": 70,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": [], "usage": None},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg-1",
            "output_index": 0,
            "content_index": 0,
            "delta": '{"text":',
        },
        {"type": f"response.{status}", "sequence_number": 2, "response": response},
    ]


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("with_transport", [False, True])
async def test_astra_stream_preserves_schema_usage_and_long_timeout(protocol, with_transport):
    backend_type = OpenAITextBackend if protocol == "chat" else OpenAIResponsesTextBackend
    path = "/chat/completions" if protocol == "chat" else "/responses"
    events = _chat_events() if protocol == "chat" else _responses_events()
    layer = PathPrefixRewrite("/v1", "/proxy/v1") if with_transport else None
    prefix = "/proxy/v1" if with_transport else "/v1"
    backend = backend_type(api_key="test", model="gpt-6-astra", base_url="https://relay.test/v1", transport_layer=layer)
    try:
        with capture_http() as router:
            route = router.post(f"https://relay.test{prefix}{path}").mock(return_value=_sse(events))
            result = await backend.generate(TextGenerationRequest(prompt="编写提示词", response_schema=_Script))
        request = only_request(route)
        body = request_json(request)
        assert body["stream"] is True
        assert ("response_format" if protocol == "chat" else "text") in body
        assert request.extensions["timeout"]["read"] == 1800.0
        assert request.extensions["timeout"]["connect"] == 30.0
        assert json.loads(result.text) == {"text": "完整提示词"}
        assert (result.input_tokens, result.output_tokens, result.cache_read_tokens) == (50, 20, 10)
    finally:
        await backend._client.close()


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_stream_without_final_signal_is_not_saved_or_replayed(protocol):
    backend_type = OpenAITextBackend if protocol == "chat" else OpenAIResponsesTextBackend
    path = "/chat/completions" if protocol == "chat" else "/responses"
    events = _chat_events(finish_reason=None) if protocol == "chat" else _responses_events()[:-1]
    backend = backend_type(api_key="test", model="gpt-6-astra", base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post(f"https://relay.test/v1{path}").mock(return_value=_sse(events))
            with pytest.raises(RuntimeError, match="不完整结果"):
                await backend.generate(TextGenerationRequest(prompt="编写提示词", response_schema=_Script))
        assert route.call_count == 1
    finally:
        await backend._client.close()


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_stream_truncation_keeps_structured_output_guard(protocol):
    backend_type = OpenAITextBackend if protocol == "chat" else OpenAIResponsesTextBackend
    path = "/chat/completions" if protocol == "chat" else "/responses"
    events = _chat_events(finish_reason="length") if protocol == "chat" else _responses_events(status="incomplete")
    backend = backend_type(api_key="test", model="gpt-6-astra", base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post(f"https://relay.test/v1{path}").mock(return_value=_sse(events))
            with pytest.raises(TextOutputTruncatedError):
                await backend.generate(TextGenerationRequest(prompt="编写提示词", response_schema=_Script))
        assert route.call_count == 1
    finally:
        await backend._client.close()


async def test_failed_responses_stream_is_not_success():
    backend = OpenAIResponsesTextBackend(api_key="test", model="gpt-6-astra", base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post("https://relay.test/v1/responses").mock(
                return_value=_sse(_responses_events(status="failed"))
            )
            with pytest.raises(RuntimeError, match="upstream failed"):
                await backend.generate(TextGenerationRequest(prompt="编写提示词"))
        assert route.call_count == 1
    finally:
        await backend._client.close()
