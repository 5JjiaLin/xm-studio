"""Safe diagnostics for a failed text-provider connection, without raw request data."""

from __future__ import annotations

import httpx

from lib.infra.retry import NonRetryableError


class TextProviderConnectionError(NonRetryableError):
    """A transport failure, distinct from an input or schema validation failure.

    NonRetryableError prevents an outer retry loop from replaying a possibly billed
    request. The UI may offer a manual retry after the user checks provider usage.
    """

    def __init__(self, *, provider_id: str, model: str, phase: str, cause_types: tuple[str, ...]):
        self.provider_id = provider_id
        self.model = model
        self.phase = phase
        self.cause_types = cause_types
        self.may_have_been_accepted = phase not in {"connect", "pool"}
        if self.may_have_been_accepted:
            detail = "请求可能已被供应商受理，请先检查供应商状态及用量，再手动重试。"
        else:
            detail = "连接未能建立，请检查服务器到模型供应商的网络及供应商状态后重试。"
        super().__init__(f"文本模型连接失败（{phase}）：{detail}")


def connection_diagnostics(exc: BaseException) -> tuple[str, tuple[str, ...]]:
    """Retain exception types and phase only; messages can contain keys or prompts."""
    causes: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(causes) < 8:
        seen.add(id(current))
        causes.append(current)
        current = current.__cause__
    phase = "unknown"
    for error in causes:
        for kind, types in (
            ("connect", (httpx.ConnectError, httpx.ConnectTimeout)),
            ("read", (httpx.ReadError, httpx.ReadTimeout)),
            ("write", (httpx.WriteError, httpx.WriteTimeout)),
            ("pool", (httpx.PoolTimeout,)),
            ("protocol", (httpx.ProtocolError,)),
        ):
            if isinstance(error, types):
                phase = kind
                break
        if phase != "unknown":
            break
    return phase, tuple(type(error).__name__ for error in causes)
