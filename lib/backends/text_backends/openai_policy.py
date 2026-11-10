"""OpenAI-compatible text requests: wire compatibility and safe retry boundaries."""

from __future__ import annotations

from typing import Any

from openai import APIConnectionError, APIStatusError

from .request_policy import explicit_parameter_rejection, retry_before_acceptance


def rejects_parameter(exc: BaseException, *parameters: str) -> bool:
    """Only an explicit parameter rejection permits changing the wire protocol."""
    if not isinstance(exc, APIStatusError) or exc.status_code not in (400, 422):
        return False
    return explicit_parameter_rejection(str(exc), *parameters)


def is_schema_rejection(exc: BaseException) -> bool:
    """Do not turn model, context, policy or generic upstream errors into fallbacks."""
    # Some compatible gateways wrap a clear upstream schema rejection in HTTP 429.
    # Keep that contract, but never infer a schema failure from a generic status code.
    if isinstance(exc, APIStatusError) and exc.status_code == 429:
        detail = str(exc).lower()
        return explicit_parameter_rejection(detail, "schema", "json_schema", "response_schema")
    return rejects_parameter(exc, "response_format", "response_schema", "json_schema", "text.format", "invalid schema")


def retry_text_request(exc: BaseException) -> bool:
    """Retry known admission failures, never an ambiguously accepted generation."""
    if isinstance(exc, APIStatusError):
        return not is_schema_rejection(exc) and retry_before_acceptance(exc, status_code=exc.status_code)
    if isinstance(exc, APIConnectionError):
        return retry_before_acceptance(exc)
    return False


def supports_strict_schema(schema: dict[str, Any]) -> bool:
    """Only declare strict for closed objects with no optional properties.

    Keep the business schema unchanged: requiring optional fields or closing a free-form
    mapping would change its meaning. Local validation remains mandatory either way.
    """

    def compatible(node: Any) -> bool:
        if isinstance(node, list):
            return all(compatible(value) for value in node)
        if not isinstance(node, dict):
            return True
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                return False
            if set(node.get("required", [])) != set(node.get("properties", {})):
                return False
        return all(compatible(value) for value in node.values())

    return compatible(schema)
