"""Execute provider-independent Insight Prompts through a strict raw-JSON boundary."""

from __future__ import annotations

import json
from math import isfinite
from typing import Any, Protocol

from src.insight_prompt import (
    InsightOutput,
    InsightOutputError,
    InsightPrompt,
    InsightPromptError,
    build_insight_prompt,
    validate_insight_output,
)
from src.insights import InsightContext

MAX_PROVIDER_RESPONSE_BYTES = 100_000

INVALID_PROVIDER = "INVALID_PROVIDER"
PROVIDER_FAILURE = "PROVIDER_FAILURE"
PROVIDER_CONFIGURATION_ERROR = "PROVIDER_CONFIGURATION_ERROR"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
PROVIDER_ACCOUNT_ERROR = "PROVIDER_ACCOUNT_ERROR"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
PROVIDER_CONNECTION_FAILED = "PROVIDER_CONNECTION_FAILED"
PROVIDER_REQUEST_REJECTED = "PROVIDER_REQUEST_REJECTED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
PROVIDER_RESPONSE_TOO_LARGE = "PROVIDER_RESPONSE_TOO_LARGE"
INVALID_PROVIDER_JSON = "INVALID_PROVIDER_JSON"


class InsightProvider(Protocol):
    """Minimal execution boundary implemented by every Insight provider."""

    def generate(self, prompt: InsightPrompt) -> str:
        """Return one raw JSON response string for the supplied Prompt."""

        ...


class InsightProviderError(Exception):
    """A stable failure at the Provider transport or raw-response boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MockInsightProvider:
    """A deterministic offline provider that returns one configured response."""

    def __init__(
        self,
        response: object = "",
        *,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.call_count = 0
        self.last_prompt: InsightPrompt | None = None

    def generate(self, prompt: InsightPrompt) -> str:
        """Capture the call, then return or raise the configured outcome."""

        self.call_count += 1
        self.last_prompt = prompt
        if self.error is not None:
            raise self.error
        return self.response  # type: ignore[return-value]


def _provider_generate(provider: object) -> Any:
    try:
        generate = getattr(provider, "generate", None)
    except Exception as exc:
        raise InsightProviderError(
            INVALID_PROVIDER,
            "provider 必须提供可调用的 generate(prompt) 方法。",
        ) from exc
    if not callable(generate):
        raise InsightProviderError(
            INVALID_PROVIDER,
            "provider 必须提供可调用的 generate(prompt) 方法。",
        )
    return generate


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _parse_strict_json_float(token: str) -> float:
    value = float(token)
    if not isfinite(value):
        raise ValueError("JSON floating-point value must be finite")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_loads(raw_response: str) -> object:
    try:
        return json.loads(
            raw_response,
            parse_float=_parse_strict_json_float,
            parse_constant=_reject_nonstandard_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (ValueError, RecursionError):
        pass
    # Raise outside the parser exception handler so JSONDecodeError.doc and the
    # complete raw response are not retained through __cause__ or __context__.
    raise InsightProviderError(
        INVALID_PROVIDER_JSON,
        "Provider response 不是合法的 strict JSON document。",
    )


def generate_insight(
    context: InsightContext,
    *,
    provider: InsightProvider,
) -> InsightOutput:
    """Build, execute, parse, and validate one Insight generation request."""

    prompt = build_insight_prompt(context)
    generate = _provider_generate(provider)
    try:
        raw_response = generate(prompt)
    except (InsightPromptError, InsightOutputError, InsightProviderError):
        raise
    except Exception as exc:
        raise InsightProviderError(
            PROVIDER_FAILURE,
            "Provider 调用失败。",
        ) from exc

    if not isinstance(raw_response, str):
        raise InsightProviderError(
            INVALID_PROVIDER_RESPONSE,
            "Provider response 必须是 str。",
        )
    try:
        encoded_response = raw_response.encode("utf-8")
        if not isinstance(encoded_response, bytes):
            raise TypeError("str.encode() must return bytes")
        response_bytes = len(encoded_response)
    except Exception as exc:
        raise InsightProviderError(
            INVALID_PROVIDER_RESPONSE,
            "Provider response 必须可编码为 UTF-8。",
        ) from exc
    if response_bytes > MAX_PROVIDER_RESPONSE_BYTES:
        raise InsightProviderError(
            PROVIDER_RESPONSE_TOO_LARGE,
            (
                f"Provider response UTF-8 bytes={response_bytes} 超过上限 "
                f"{MAX_PROVIDER_RESPONSE_BYTES}。"
            ),
        )

    payload = _strict_json_loads(raw_response)
    return validate_insight_output(payload, context=context)
