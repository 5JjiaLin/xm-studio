"""长思考调用：通过真实 SDK + HTTP 替身验证流式聚合、超时和完整性。"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from lib.backends.text_backends.base import TextCapability, TextGenerationRequest, TextOutputTruncatedError
from lib.backends.text_backends.openai import OpenAITextBackend
from lib.backends.text_backends.openai_responses import OpenAIResponsesTextBackend
from lib.script.script_models import build_reference_units_script_plan_model
from tests.http_capture import PathPrefixRewrite, capture_http, only_request, request_json


class _Script(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


@pytest.mark.parametrize(
    ("model", "prefer_stream"),
    [("gpt-6-astra", False), ("gpt-6.1-sol", False), ("any-compatible-model", True)],
)
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("with_transport", [False, True])
async def test_astra_stream_preserves_schema_usage_and_long_timeout(protocol, with_transport, model, prefer_stream):
    backend_type = OpenAITextBackend if protocol == "chat" else OpenAIResponsesTextBackend
    path = "/chat/completions" if protocol == "chat" else "/responses"
    events = _chat_events() if protocol == "chat" else _responses_events()
    layer = PathPrefixRewrite("/v1", "/proxy/v1") if with_transport else None
    prefix = "/proxy/v1" if with_transport else "/v1"
    backend = backend_type(api_key="test", model=model, base_url="https://relay.test/v1", transport_layer=layer)
    try:
        with capture_http() as router:
            route = router.post(f"https://relay.test{prefix}{path}").mock(return_value=_sse(events))
            result = await backend.generate(
                TextGenerationRequest(prompt="编写提示词", response_schema=_Script, prefer_stream=prefer_stream)
            )
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


@pytest.mark.parametrize(
    ("model", "prefer_stream"),
    [("gpt-6-astra", False), ("gpt-6.1-sol", False), ("any-compatible-model", True)],
)
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_stream_without_final_signal_is_not_saved_or_replayed(protocol, model, prefer_stream):
    backend_type = OpenAITextBackend if protocol == "chat" else OpenAIResponsesTextBackend
    path = "/chat/completions" if protocol == "chat" else "/responses"
    events = _chat_events(finish_reason=None) if protocol == "chat" else _responses_events()[:-1]
    backend = backend_type(api_key="test", model=model, base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post(f"https://relay.test/v1{path}").mock(return_value=_sse(events))
            with pytest.raises(RuntimeError, match="不完整结果"):
                await backend.generate(
                    TextGenerationRequest(prompt="编写提示词", response_schema=_Script, prefer_stream=prefer_stream)
                )
        assert route.call_count == 1
    finally:
        await backend._client.close()


@pytest.mark.parametrize(
    ("model", "prefer_stream"),
    [("gpt-6-astra", False), ("gpt-6.1-sol", False), ("any-compatible-model", True)],
)
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_stream_truncation_keeps_structured_output_guard(protocol, model, prefer_stream):
    backend_type = OpenAITextBackend if protocol == "chat" else OpenAIResponsesTextBackend
    path = "/chat/completions" if protocol == "chat" else "/responses"
    events = _chat_events(finish_reason="length") if protocol == "chat" else _responses_events(status="incomplete")
    backend = backend_type(api_key="test", model=model, base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post(f"https://relay.test/v1{path}").mock(return_value=_sse(events))
            with pytest.raises(TextOutputTruncatedError):
                await backend.generate(
                    TextGenerationRequest(prompt="编写提示词", response_schema=_Script, prefer_stream=prefer_stream)
                )
        assert route.call_count == 1
    finally:
        await backend._client.close()


async def test_failed_responses_stream_is_not_success():
    model = "gpt-6-astra"
    backend = OpenAIResponsesTextBackend(api_key="test", model=model, base_url="https://relay.test/v1")
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


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol"])
@pytest.mark.parametrize("schema_rejected", [False, True])
async def test_chat_long_thinking_schema_fallback_keeps_stream_and_validation(model, schema_rejected):
    backend = OpenAITextBackend(api_key="test", model=model, base_url="https://relay.test/v1")
    if not schema_rejected:
        backend._capabilities.discard(TextCapability.STRUCTURED_OUTPUT)
    responses = [_sse(_chat_events())]
    if schema_rejected:
        responses.insert(0, httpx.Response(400, json={"error": {"message": "Invalid schema for response_format"}}))
    try:
        with capture_http() as router:
            route = router.post("https://relay.test/v1/chat/completions").mock(side_effect=responses)
            result = await backend.generate(TextGenerationRequest(prompt="拆解整集剧本", response_schema=_Script))
        assert route.call_count == len(responses)
        body = request_json(route.calls[-1].request)
        assert body["stream"] is True
        assert "response_format" not in body
        assert "tools" not in body
        assert "拆解整集剧本" in body["messages"][0]["content"]
        assert '"properties"' in body["messages"][0]["content"]
        assert json.loads(result.text) == {"text": "完整提示词"}
        assert (result.input_tokens, result.output_tokens) == (50, 20)
    finally:
        await backend._client.close()


@pytest.mark.parametrize("finish_reason", [None, "length"])
async def test_chat_long_thinking_rejected_schema_fallback_does_not_save_partial_result(finish_reason):
    backend = OpenAITextBackend(api_key="test", model="gpt-6.1-sol", base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post("https://relay.test/v1/chat/completions").mock(
                side_effect=[
                    httpx.Response(400, json={"error": {"message": "Invalid schema for response_format"}}),
                    _sse(_chat_events(finish_reason=finish_reason)),
                ]
            )
            with pytest.raises((RuntimeError, TextOutputTruncatedError)):
                await backend.generate(TextGenerationRequest(prompt="拆解整集剧本", response_schema=_Script))
        assert route.call_count == 2
    finally:
        await backend._client.close()


@pytest.mark.parametrize('native', [False, True])
async def test_authoring_raw_response_is_not_retried_or_overwritten(native):
    from lib.script.reference_video.authoring_response import authoring_response_model

    backend = OpenAITextBackend(api_key='test', model='MiniMax-M3.1-Flash', base_url='https://relay.test/v1', provider_name='minimax')
    events = _chat_events()
    events[1]['choices'][0]['delta']['content'] = '{"title":"t","units":[{"text":"first","text":"second"}]}'
    events[2]['choices'][0]['delta']['content'] = ''
    try:
        with capture_http() as router:
            route = router.post('https://relay.test/v1/chat/completions').mock(return_value=_sse(events))
            result = await backend.generate(TextGenerationRequest(
                prompt='批量编写', response_schema=authoring_response_model(3), prefer_stream=True,
                preserve_raw_response=True, prefer_native_schema=native,
            ))
        body = request_json(only_request(route))
        assert ('response_format' in body) is native
        if native:
            assert body['response_format']['json_schema']['strict'] is True
        else:
            assert '"minItems": 3' in body['messages'][-1]['content']
        assert result.text.count('"text"') == 2
        assert result.output_tokens == 20
    finally:
        await backend._client.close()


async def test_plan_with_optional_fields_uses_one_stream_and_preserves_its_schema():
    schema = build_reference_units_script_plan_model([8], use_source_spans=True)
    payload = {
        "units": [{"duration_seconds": 8, "source_span_start": "S001", "source_span_end": "S001", "text": "画面"}]
    }
    events = _chat_events()
    events[1]["choices"][0]["delta"]["content"] = json.dumps(payload, ensure_ascii=False)
    events[2]["choices"][0]["delta"]["content"] = ""
    backend = OpenAITextBackend(api_key="test", model="gpt-6.1-sol", base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post("https://relay.test/v1/chat/completions").mock(return_value=_sse(events))
            result = await backend.generate(
                TextGenerationRequest(prompt="拆解剧本", response_schema=schema, prefer_stream=True)
            )
        body = request_json(only_request(route))
        assert body["stream"] is True
        assert "response_format" not in body
        assert "tools" not in body
        assert "source_span_start" in body["messages"][0]["content"]
        content = json.loads(result.text)
        assert content["new_assets"] == []  # Optional fields keep their business defaults.
        assert content["units"][0]["source_span_start"] == "S001"
        assert (result.input_tokens, result.output_tokens) == (50, 20)
    finally:
        await backend._client.close()



async def test_explicit_schema_rejection_keeps_the_fallback_streaming():
    class ClosedScript(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str

    backend = OpenAITextBackend(api_key="test", model="gpt-6.1-sol", base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post("https://relay.test/v1/chat/completions").mock(
                side_effect=[
                    httpx.Response(
                        400,
                        json={
                            "error": {"message": "Invalid schema for response_format", "type": "invalid_request_error"}
                        },
                    ),
                    _sse(_chat_events()),
                ]
            )
            result = await backend.generate(
                TextGenerationRequest(prompt="拆解", response_schema=ClosedScript, prefer_stream=True)
            )
        assert route.call_count == 2
        bodies = [request_json(call.request) for call in route.calls]
        assert all(body["stream"] for body in bodies)
        assert bodies[0]["response_format"]["json_schema"]["strict"] is True
        assert "response_format" not in bodies[1]
        assert "tools" not in bodies[1]
        assert json.loads(result.text) == {"text": "完整提示词"}
    finally:
        await backend._client.close()



async def test_broken_plan_stream_does_not_replay_an_accepted_request():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": thinking\n\n"
            raise httpx.ReadError("upstream disconnected")

    backend = OpenAITextBackend(api_key="test", model="gpt-6.1-sol", base_url="https://relay.test/v1")
    try:
        with capture_http() as router:
            route = router.post("https://relay.test/v1/chat/completions").mock(
                return_value=httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=BrokenStream(),
                )
            )
            with pytest.raises(httpx.ReadError):
                await backend.generate(
                    TextGenerationRequest(prompt="拆解", response_schema=_Script, prefer_stream=True)
                )
        assert route.call_count == 1
    finally:
        await backend._client.close()
