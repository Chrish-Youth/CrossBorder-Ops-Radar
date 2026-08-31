"""Execute provider-independent Insight Prompts through a strict raw-JSON boundary."""

from __future__ import annotations

from dataclasses import dataclass
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
INVALID_PROVIDER_USAGE = "INVALID_PROVIDER_USAGE"
PROVIDER_RESPONSE_TOO_LARGE = "PROVIDER_RESPONSE_TOO_LARGE"
INVALID_PROVIDER_JSON = "INVALID_PROVIDER_JSON"


class InsightProviderError(Exception):
    """A stable failure at the Provider transport or raw-response boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_provider_response(message: str) -> InsightProviderError:
    return InsightProviderError(INVALID_PROVIDER_RESPONSE, message)


def _invalid_provider_usage(message: str) -> InsightProviderError:
    return InsightProviderError(INVALID_PROVIDER_USAGE, message)


def _validate_token_count(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid_provider_usage(
            f"{field_name} 必须是非负整数且不能是 bool。"
        )


@dataclass(frozen=True)
class ProviderUsage:
    """Normalized immutable token usage for one Provider generation."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            _validate_token_count(getattr(self, field_name), field_name=field_name)
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise _invalid_provider_usage(
                "total_tokens 必须等于 prompt_tokens + completion_tokens。"
            )

        cache_values = (
            self.prompt_cache_hit_tokens,
            self.prompt_cache_miss_tokens,
        )
        if (cache_values[0] is None) != (cache_values[1] is None):
            raise _invalid_provider_usage(
                "Prompt cache hit/miss token fields 必须同时存在或同时缺失。"
            )
        if cache_values[0] is not None and cache_values[1] is not None:
            _validate_token_count(
                cache_values[0],
                field_name="prompt_cache_hit_tokens",
            )
            _validate_token_count(
                cache_values[1],
                field_name="prompt_cache_miss_tokens",
            )
            if cache_values[0] + cache_values[1] != self.prompt_tokens:
                raise _invalid_provider_usage(
                    "Prompt cache hit/miss tokens 之和必须等于 prompt_tokens。"
                )

        if self.reasoning_tokens is not None:
            _validate_token_count(
                self.reasoning_tokens,
                field_name="reasoning_tokens",
            )
            if self.reasoning_tokens > self.completion_tokens:
                raise _invalid_provider_usage(
                    "reasoning_tokens 不能超过 completion_tokens。"
                )


@dataclass(frozen=True)
class ProviderGeneration:
    """Provider-level content and optional normalized usage metadata."""

    raw_text: str
    usage: ProviderUsage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_text, str):
            raise _invalid_provider_response(
                "ProviderGeneration.raw_text 必须是 str。"
            )
        if self.usage is not None and not isinstance(self.usage, ProviderUsage):
            raise _invalid_provider_usage(
                "ProviderGeneration.usage 必须是 ProviderUsage 或 None。"
            )


@dataclass(frozen=True)
class InsightGenerationResult:
    """One validated InsightOutput and its Provider usage metadata."""

    output: InsightOutput
    usage: ProviderUsage | None


class InsightProvider(Protocol):
    """Minimal execution boundary implemented by every Insight provider."""

    def generate(self, prompt: InsightPrompt) -> ProviderGeneration:
        """Return one explicit Provider generation envelope."""

        ...


class MockInsightProvider:
    """A deterministic offline provider that returns one configured response."""

    def __init__(
        self,
        response: object = "",
        *,
        usage: ProviderUsage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.usage = usage
        self.error = error
        self.call_count = 0
        self.last_prompt: InsightPrompt | None = None

    def generate(self, prompt: InsightPrompt) -> ProviderGeneration:
        """Capture the call, then return or raise the configured outcome."""

        self.call_count += 1
        self.last_prompt = prompt
        if self.error is not None:
            raise self.error
        return ProviderGeneration(
            raw_text=self.response,  # type: ignore[arg-type]
            usage=self.usage,
        )


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


def generate_insight_with_metadata(
    context: InsightContext,
    *,
    provider: InsightProvider,
) -> InsightGenerationResult:
    """Return validated Insight output together with Provider usage metadata."""

    prompt = build_insight_prompt(context)
    generate = _provider_generate(provider)
    try:
        generation = generate(prompt)
    except (InsightPromptError, InsightOutputError, InsightProviderError):
        raise
    except Exception as exc:
        raise InsightProviderError(
            PROVIDER_FAILURE,
            "Provider 调用失败。",
        ) from exc

    if not isinstance(generation, ProviderGeneration):
        raise _invalid_provider_response(
            "Provider 必须返回 ProviderGeneration。"
        )
    raw_response = generation.raw_text
    if not isinstance(raw_response, str):
        raise _invalid_provider_response(
            "ProviderGeneration.raw_text 必须是 str。"
        )
    if generation.usage is not None and not isinstance(
        generation.usage,
        ProviderUsage,
    ):
        raise _invalid_provider_usage(
            "ProviderGeneration.usage 必须是 ProviderUsage 或 None。"
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
    output = validate_insight_output(payload, context=context)
    return InsightGenerationResult(output=output, usage=generation.usage)


def generate_insight(
    context: InsightContext,
    *,
    provider: InsightProvider,
) -> InsightOutput:
    """Return only the validated InsightOutput for backward compatibility."""

    return generate_insight_with_metadata(context, provider=provider).output
