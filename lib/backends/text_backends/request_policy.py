"""Conservative text request recovery shared by provider protocols."""

from __future__ import annotations

import re

import httpx


def explicit_parameter_rejection(detail: str, *parameters: str) -> bool:
    """A named parameter alone is not evidence that a protocol is unsupported."""
    detail = detail.lower()
    rejection = re.search(
        r"unsupported|not supported|does not support|not support|not allowed|not permitted|"
        r"unrecognized|unknown (?:parameter|field)|unexpected (?:parameter|keyword)|"
        r"invalid|not valid|must be|only supports|not available|not enabled|not implemented|"
        r"额外的字段|不支持|无效|不允许",
        detail,
    )
    return bool(rejection) and any(
        re.search(r"(?<![a-z0-9_])" + re.escape(parameter.lower()) + r"(?![a-z0-9_])", detail)
        for parameter in parameters
    )


def retry_before_acceptance(exc: BaseException, *, status_code: int | None = None) -> bool:
    """Only retry explicit rate rejection or a connection failure before submission.

    HTTP 5xx, read/write timeouts and broken streams may follow a billed generation.
    Error-message numbers never determine retry eligibility.
    """
    if status_code is not None:
        detail = str(exc).lower()
        return status_code == 429 and not any(term in detail for term in ("insufficient_quota", "quota exhausted"))
    cause = exc if isinstance(exc, httpx.TransportError) else exc.__cause__
    return isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout))
