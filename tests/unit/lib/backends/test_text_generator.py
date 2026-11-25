"""Tests for TextGenerator wrapper."""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import TextGenerationRequest, TextGenerationResult, TextOutputTruncatedError
from lib.backends.text_backends.connection_errors import TextProviderConnectionError
from lib.backends.text_generator import TextGenerator
from lib.billing.ledger import Ledger
from lib.db.models.api_call import ApiCall


@dataclass
class _Wired:
    """记账写侧注入 Ledger，读侧直查 api_calls 行，共享同一内存库。"""

    ledger: Ledger
    session_factory: async_sessionmaker[AsyncSession]

    async def stored_calls(self, project_name: str | None = None) -> list[ApiCall]:
        """按 id 升序读回落库的 api_calls 行。"""
        stmt = select(ApiCall).order_by(ApiCall.id)
        if project_name is not None:
            stmt = stmt.where(ApiCall.project_name == project_name)
        async with self.session_factory() as session:
            return list((await session.execute(stmt)).scalars().all())


@pytest.fixture
async def wired(db_factory):
    return _Wired(ledger=Ledger(session_factory=db_factory), session_factory=db_factory)


def _make_backend(provider="gemini", model="gemini-3-flash-preview"):
    backend = AsyncMock()
    backend.name = provider
    backend.model = model
    backend.generate = AsyncMock(
        return_value=TextGenerationResult(
            text="生成的文本",
            provider=provider,
            model=model,
            input_tokens=100,
            output_tokens=50,
        )
    )
    return backend


class TestTextGenerator:
    async def test_generate_records_usage_on_success(self, wired):
        # backend.name 为裸 "gemini"，记账须取解析层 provider_id（"gemini-aistudio"）。
        backend = _make_backend()
        gen = TextGenerator(backend, wired.ledger, "gemini-aistudio", purpose=CallPurpose.SCRIPT_GENERATION)

        result = await gen.generate(
            TextGenerationRequest(prompt="测试"),
            project_name="demo",
        )

        assert result.text == "生成的文本"
        assert result.input_tokens == 100
        assert result.output_tokens == 50

        calls = await wired.stored_calls("demo")
        assert len(calls) == 1
        item = calls[0]
        assert item.call_type == "text"
        assert item.status == "success"
        assert item.input_tokens == 100
        assert item.output_tokens == 50
        assert item.provider == "gemini-aistudio"
        assert item.cost_amount == pytest.approx((100 * 0.50 + 50 * 3.00) / 1_000_000)

    async def test_generate_records_usage_on_failure(self, wired):
        backend = _make_backend()
        backend.generate = AsyncMock(side_effect=RuntimeError("API 超时"))
        gen = TextGenerator(backend, wired.ledger, "gemini-aistudio", purpose=CallPurpose.SCRIPT_GENERATION)

        with pytest.raises(RuntimeError, match="API 超时"):
            await gen.generate(
                TextGenerationRequest(prompt="测试"),
                project_name="demo",
            )

        calls = await wired.stored_calls("demo")
        assert len(calls) == 1
        item = calls[0]
        assert item.status == "failed"
        assert item.cost_amount == 0.0
        assert "API 超时" in item.error_message

    async def test_generate_without_project_name(self, wired):
        backend = _make_backend()
        gen = TextGenerator(backend, wired.ledger, "gemini-aistudio", purpose=CallPurpose.SCRIPT_GENERATION)

        result = await gen.generate(TextGenerationRequest(prompt="工具箱调用"))

        assert result.text == "生成的文本"
        calls = await wired.stored_calls()
        assert len(calls) == 1
        item = calls[0]
        assert item.project_name == ""


@pytest.mark.parametrize(
    ("transport_error", "phase", "accepted"),
    [
        (httpx.ConnectError, "connect", False),
        (httpx.ReadTimeout, "read", True),
        (httpx.RemoteProtocolError, "protocol", True),
    ],
)
async def test_connection_failure_has_safe_diagnostics_and_is_recorded_once(
    wired, caplog, transport_error, phase, accepted
):
    secret = "key-and-private-prompt-must-not-be-logged"
    request = httpx.Request("POST", f"https://relay.test/v1/chat/completions?key={secret}")
    failure = APIConnectionError(request=request)
    failure.__cause__ = transport_error(secret)
    backend = _make_backend(provider="openai", model="gpt-6.1-sol")
    backend.generate.side_effect = failure
    gen = TextGenerator(backend, wired.ledger, "custom-1", purpose=CallPurpose.SCRIPT_GENERATION)

    with pytest.raises(TextProviderConnectionError) as caught:
        await gen.generate(TextGenerationRequest(prompt="测试", prefer_stream=True))

    assert caught.value.phase == phase
    assert caught.value.may_have_been_accepted is accepted
    assert caught.value.cause_types == ("APIConnectionError", transport_error.__name__)
    assert caught.value.__cause__ is failure
    assert backend.generate.await_count == 1
    assert "host=relay.test" in caplog.text
    assert f"phase={phase}" in caplog.text
    assert secret not in caplog.text
    calls = await wired.stored_calls()
    assert len(calls) == 1
    assert calls[0].status == "failed"
    assert secret not in calls[0].error_message
    assert "文本模型连接失败" in calls[0].error_message


async def test_requestless_transport_failure_preserves_connection_diagnostics(wired, caplog):
    backend = _make_backend(provider="openai")
    backend.generate.side_effect = httpx.ReadError("private prompt")
    gen = TextGenerator(backend, wired.ledger, "custom-1", purpose=CallPurpose.SCRIPT_GENERATION)

    with pytest.raises(TextProviderConnectionError) as caught:
        await gen.generate(TextGenerationRequest(prompt="测试", prefer_stream=True))

    assert caught.value.phase == "read"
    assert caught.value.cause_types == ("ReadError",)
    assert "host=unknown" in caplog.text
    assert "private prompt" not in caplog.text
    assert len(await wired.stored_calls()) == 1


async def test_total_generation_timeout_cancels_stream_and_is_never_silently_retried(wired, monkeypatch):
    monkeypatch.setattr("lib.backends.text_generator.TEXT_GENERATION_TOTAL_TIMEOUT_SECONDS", 0.01)
    backend = _make_backend()
    started = asyncio.Event()

    async def never_finishes(_request):
        started.set()
        await asyncio.Event().wait()

    backend.generate.side_effect = never_finishes
    gen = TextGenerator(backend, wired.ledger, "openai", purpose=CallPurpose.SCRIPT_GENERATION)
    from lib.backends.text_backends.base import TextGenerationTimeoutError

    with pytest.raises(TextGenerationTimeoutError) as caught:
        await gen.generate(TextGenerationRequest(prompt="test", prefer_stream=True), project_name="demo")

    assert started.is_set()
    assert backend.generate.await_count == 1
    assert caught.value.timeout_seconds == 0.01
    calls = await wired.stored_calls("demo")
    assert len(calls) == 1
    assert calls[0].status == "failed"


async def test_heartbeat_only_openai_stream_is_closed_at_total_deadline(wired, monkeypatch):
    from lib.backends.text_backends.base import TextGenerationTimeoutError
    from lib.backends.text_backends.openai import OpenAITextBackend
    from tests.http_capture import PathPrefixRewrite, capture_http

    class Heartbeats(httpx.AsyncByteStream):
        closed = False
        count = 0

        async def __aiter__(self):
            while True:
                self.count += 1
                yield b": keep-alive\n\n"
                await asyncio.sleep(0.001)

        async def aclose(self):
            self.closed = True

    stream = Heartbeats()
    layer = PathPrefixRewrite("/v1/", "/mock/")
    backend = OpenAITextBackend(
        api_key="offline-test",
        model="offline-test",
        base_url="https://relay.test/v1",
        transport_layer=layer,
    )
    # Leave enough time for the SDK's lazy initialization on a cold Windows run.
    monkeypatch.setattr("lib.backends.text_generator.TEXT_GENERATION_TOTAL_TIMEOUT_SECONDS", 2.0)
    gen = TextGenerator(backend, wired.ledger, "openai", purpose=CallPurpose.SCRIPT_GENERATION)
    try:
        with capture_http() as router:
            route = router.post("https://relay.test/mock/chat/completions").mock(
                return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
            )
            with pytest.raises(TextGenerationTimeoutError):
                await gen.generate(TextGenerationRequest(prompt="offline", prefer_stream=True))
            assert route.call_count == 1
    finally:
        await backend._client.close()

    assert len(layer.seen) == 1
    assert stream.count > 1
    assert stream.closed is True
    assert (await wired.stored_calls())[0].status == "failed"


class TestOutputTokenCeiling:
    """文本请求的输出上限取 min(登记值, 64000)，未登记按 64000。"""

    @pytest.mark.parametrize(
        ("registered", "requested", "sent"),
        [
            (8192, 64000, 8192),
            (8192, None, 8192),
            (None, None, 64000),
            (None, 64000, 64000),
            (128000, 64000, 64000),
            (128000, None, 64000),
            (8192, 4000, 4000),
        ],
    )
    async def test_request_carries_effective_ceiling(self, wired, registered, requested, sent):
        backend = _make_backend()
        gen = TextGenerator(
            backend,
            wired.ledger,
            "gemini-aistudio",
            purpose=CallPurpose.SCRIPT_GENERATION,
            registered_max_output_tokens=registered,
        )

        await gen.generate(TextGenerationRequest(prompt="测试", max_output_tokens=requested))

        assert backend.generate.await_args.args[0].max_output_tokens == sent

    async def test_truncation_reports_the_resolved_provider(self, wired):
        backend = _make_backend()
        backend.generate = AsyncMock(
            side_effect=TextOutputTruncatedError(provider="openai", model="my-model", output_tokens=8192)
        )
        gen = TextGenerator(backend, wired.ledger, "custom-7", purpose=CallPurpose.EPISODE_PLANNING, custom_model=True)

        with pytest.raises(TextOutputTruncatedError) as caught:
            await gen.generate(TextGenerationRequest(prompt="测试", response_schema={"type": "object"}))

        assert caught.value.provider_id == "custom-7"
        assert caught.value.model == "gemini-3-flash-preview"
        assert caught.value.custom_model is True

    async def test_truncated_free_text_raises_only_when_the_caller_needs_it_whole(self, wired):
        backend = _make_backend()
        backend.generate = AsyncMock(
            return_value=TextGenerationResult(
                text='{"segments": [', provider="openai", model="my-model", output_tokens=8192, truncated=True
            )
        )
        gen = TextGenerator(backend, wired.ledger, "custom-7", purpose=CallPurpose.SCRIPT_GENERATION, custom_model=True)

        result = await gen.generate(TextGenerationRequest(prompt="测试"))
        assert result.truncated is True

        with pytest.raises(TextOutputTruncatedError) as caught:
            await gen.generate(TextGenerationRequest(prompt="测试"), require_complete=True)

        assert caught.value.provider_id == "custom-7"
        assert caught.value.model == "gemini-3-flash-preview"
        assert caught.value.output_tokens == 8192
        assert caught.value.custom_model is True
