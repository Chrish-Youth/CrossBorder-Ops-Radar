from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

import src.deepseek_provider as deepseek_module
import src.insight_provider as generic_provider_module
from src.deepseek_provider import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEFAULT_DEEPSEEK_MAX_TOKENS,
    DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
    DeepSeekInsightProvider,
)
from src.insight_prompt import (
    INSIGHT_OUTPUT_VERSION,
    INVALID_INSIGHT_OUTPUT,
    MAX_INSIGHT_OUTPUT_BYTES,
    OUTPUT_TOO_LARGE,
    InsightOutput,
    InsightOutputError,
    InsightPrompt,
)
from src.insight_provider import (
    INVALID_PROVIDER_JSON,
    INVALID_PROVIDER_RESPONSE,
    MAX_PROVIDER_RESPONSE_BYTES,
    PROVIDER_ACCOUNT_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_FAILURE,
    PROVIDER_RATE_LIMITED,
    PROVIDER_REQUEST_REJECTED,
    PROVIDER_RESPONSE_TOO_LARGE,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    InsightProvider,
    InsightProviderError,
    generate_insight,
)
from src.insights import InsightContext, build_insight_context
from src.pipeline import run_pipeline


SAMPLE_PATH = Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"
TEST_API_KEY = "TEST_DEEPSEEK_KEY"


class FakeCompletions:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.call_count = 0
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.call_count += 1
        self.calls.append(deepcopy(kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeClient:
    def __init__(self, outcome: object) -> None:
        self.completions = FakeCompletions(outcome)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeOpenAIFactory:
    def __init__(
        self,
        client: FakeClient | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.client = client
        self.error = error
        self.call_count = 0
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeClient:
        self.call_count += 1
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        assert self.client is not None
        return self.client


class OfflineOpenAIFactory:
    """Build the real SDK client against an in-memory HTTP transport."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.call_count = 0
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> openai.OpenAI:
        self.call_count += 1
        self.calls.append(dict(kwargs))
        http_client = httpx2.Client(transport=httpx2.MockTransport(self.handler))
        return openai.OpenAI(**kwargs, http_client=http_client)  # type: ignore[arg-type]


class FreshClientFactory:
    """Return one independent fake client for every Provider instance."""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.call_count = 0
        self.clients: list[FakeClient] = []

    def __call__(self, **kwargs: object) -> FakeClient:
        self.call_count += 1
        client = FakeClient(self.outcome)
        self.clients.append(client)
        return client


def completion(
    content: object,
    *,
    finish_reason: object = "stop",
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ]
    )


def prompt(
    *,
    system_prompt: str = "SYSTEM_PROMPT",
    user_prompt: str = "USER_PROMPT",
) -> InsightPrompt:
    return InsightPrompt(
        version="1",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )


def install_fake(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
    *,
    api_key: str = TEST_API_KEY,
) -> tuple[DeepSeekInsightProvider, FakeOpenAIFactory, FakeClient]:
    client = FakeClient(outcome)
    factory = FakeOpenAIFactory(client)
    monkeypatch.setenv("DEEPSEEK_API_KEY", api_key)
    monkeypatch.setattr(deepseek_module, "OpenAI", factory)
    provider = DeepSeekInsightProvider()
    return provider, factory, client


def status_error(
    status_code: int,
    *,
    error_type: type[APIStatusError] = APIStatusError,
) -> APIStatusError:
    request = httpx2.Request(
        "POST",
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": "Bearer SECRET_DEEPSEEK_KEY"},
        content=b"SECRET_PROMPT_TEXT",
    )
    response = httpx2.Response(
        status_code,
        request=request,
        content=b"SECRET_RESPONSE_BODY",
    )
    return error_type(
        "SECRET_SDK_MESSAGE",
        response=response,
        body={"detail": "SECRET_RESPONSE_BODY"},
    )


def valid_payload(context: InsightContext) -> dict[str, object]:
    signal = next(
        item
        for item in context.diagnostic_signals
        if item["group"] == {"sku": "SKU-LOW-CTR"}
        and item["code"] == "HIGH_IMPRESSIONS_LOW_CTR"
    )
    return {
        "version": INSIGHT_OUTPUT_VERSION,
        "executive_summary": "One diagnostic pattern warrants review.",
        "priority_insights": [
            {
                "scope": deepcopy(signal["group"]),
                "observation": "The supplied context contains this signal.",
                "evidence_codes": [signal["code"]],
                "possible_explanations": [
                    "A possible association may warrant investigation."
                ],
                "recommended_checks": ["Review the supporting inputs."],
                "confidence": "medium",
            }
        ],
        "overall_limitations": [],
    }


def oversized_valid_payload(context: InsightContext) -> dict[str, object]:
    selected_signals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for signal in context.diagnostic_signals:
        key = (
            json.dumps(signal["group"], sort_keys=True, separators=(",", ":")),
            signal["code"],
        )
        if key in seen:
            continue
        seen.add(key)
        selected_signals.append(signal)
        if len(selected_signals) == 10:
            break
    assert len(selected_signals) == 10

    return {
        "version": INSIGHT_OUTPUT_VERSION,
        "executive_summary": "E" * 1_500,
        "priority_insights": [
            {
                "scope": deepcopy(signal["group"]),
                "observation": "O" * 1_000,
                "evidence_codes": [signal["code"]],
                "possible_explanations": [
                    "possible " + "X" * 991 for _ in range(3)
                ],
                "recommended_checks": ["C" * 1_000 for _ in range(3)],
                "confidence": "medium",
            }
            for signal in selected_signals
        ],
        "overall_limitations": ["L" * 1_000 for _ in range(10)],
    }


@pytest.fixture(scope="module")
def sample_context() -> InsightContext:
    return build_insight_context(run_pipeline(SAMPLE_PATH, group_by="sku"))


def test_openai_sdk_version_matches_requirements_pin() -> None:
    assert openai.__version__ == "3.5.0"


@pytest.mark.parametrize("value", [None, "", " ", "\t\r\n"])
def test_missing_or_blank_api_key_fails_before_client_initialization(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    factory = FakeOpenAIFactory(FakeClient(completion("{}")))
    monkeypatch.setattr(deepseek_module, "OpenAI", factory)
    if value is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", value)

    with pytest.raises(InsightProviderError) as caught:
        DeepSeekInsightProvider()

    assert caught.value.code == PROVIDER_CONFIGURATION_ERROR
    assert factory.call_count == 0
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_client_construction_contract_and_no_network_on_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, factory, client = install_fake(
        monkeypatch,
        completion('{"ok":true}'),
    )

    assert isinstance(provider, DeepSeekInsightProvider)
    assert factory.call_count == 1
    assert factory.calls == [
        {
            "api_key": TEST_API_KEY,
            "base_url": DEEPSEEK_BASE_URL,
            "timeout": DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
            "max_retries": 0,
        }
    ]
    assert client.completions.call_count == 0
    assert set(provider.__dict__) == {"_client"}
    assert not hasattr(provider, "api_key")


def test_client_initialization_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_key = "SECRET_DEEPSEEK_KEY"
    factory = FakeOpenAIFactory(
        error=RuntimeError(
            f"constructor failed with {secret_key} SECRET_RESPONSE_BODY"
        )
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret_key)
    monkeypatch.setattr(deepseek_module, "OpenAI", factory)

    with pytest.raises(InsightProviderError) as caught:
        DeepSeekInsightProvider()

    assert caught.value.code == PROVIDER_CONFIGURATION_ERROR
    assert secret_key not in str(caught.value)
    assert secret_key not in repr(caught.value)
    assert "SECRET_RESPONSE_BODY" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_provider_structurally_satisfies_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _, _ = install_fake(monkeypatch, completion("{}"))
    typed_provider: InsightProvider = provider

    assert typed_provider is provider
    assert callable(typed_provider.generate)


def test_request_shape_is_exact_and_prompt_messages_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = "SYSTEM\nSECRET_PROMPT_TEXT\n中文"
    user = "USER\n{\"json\":true}\n尾部 "
    provider, _, client = install_fake(
        monkeypatch,
        completion(' {"ok":true} '),
    )

    result = provider.generate(prompt(system_prompt=system, user_prompt=user))

    assert result == ' {"ok":true} '
    assert client.completions.call_count == 1
    assert client.completions.calls == [
        {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": DEFAULT_DEEPSEEK_MAX_TOKENS,
            "temperature": 0.0,
            "stream": False,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    ]


def test_real_sdk_serializes_request_contract_through_offline_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"request_count": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["request_count"] = int(captured["request_count"]) + 1
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "offline-completion",
                "object": "chat.completion",
                "created": 0,
                "model": DEEPSEEK_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "  {\"ok\":true}  ",
                            "reasoning_content": "ignored",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    factory = OfflineOpenAIFactory(handler)
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(deepseek_module, "OpenAI", factory)
    provider = DeepSeekInsightProvider()
    request_prompt = prompt(
        system_prompt="SYSTEM_EXACT",
        user_prompt="USER_EXACT",
    )

    try:
        raw = provider.generate(request_prompt)
    finally:
        provider._client.close()  # type: ignore[attr-defined]

    assert raw == '  {"ok":true}  '
    assert factory.call_count == 1
    assert factory.calls == [
        {
            "api_key": TEST_API_KEY,
            "base_url": DEEPSEEK_BASE_URL,
            "timeout": DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
            "max_retries": 0,
        }
    ]
    assert captured["request_count"] == 1
    assert captured["method"] == "POST"
    assert captured["url"] == f"{DEEPSEEK_BASE_URL}/chat/completions"
    assert captured["body"] == {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "SYSTEM_EXACT"},
            {"role": "user", "content": "USER_EXACT"},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": DEFAULT_DEEPSEEK_MAX_TOKENS,
        "temperature": 0.0,
        "stream": False,
        "thinking": {"type": "disabled"},
    }


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (429, PROVIDER_RATE_LIMITED),
        (500, PROVIDER_UNAVAILABLE),
        (503, PROVIDER_UNAVAILABLE),
    ],
)
def test_real_sdk_disables_wire_level_retries_for_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_code: str,
) -> None:
    request_count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        return httpx2.Response(
            status_code,
            json={"error": {"message": "SECRET_RESPONSE_BODY"}},
        )

    factory = OfflineOpenAIFactory(handler)
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(deepseek_module, "OpenAI", factory)
    provider = DeepSeekInsightProvider()

    try:
        with pytest.raises(InsightProviderError) as caught:
            provider.generate(prompt())
    finally:
        provider._client.close()  # type: ignore[attr-defined]

    assert caught.value.code == expected_code
    assert request_count == 1
    assert factory.call_count == 1
    assert factory.calls[0]["max_retries"] == 0
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_client_is_reused_across_generate_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, factory, client = install_fake(
        monkeypatch,
        completion('{"ok":true}'),
    )

    assert provider.generate(prompt()) == '{"ok":true}'
    assert provider.generate(prompt(user_prompt="SECOND")) == '{"ok":true}'
    assert factory.call_count == 1
    assert client.completions.call_count == 2


def test_multiple_choices_use_first_choice_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="FIRST"),
            ),
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="SECOND"),
            ),
        ]
    )
    provider, _, client = install_fake(monkeypatch, response)

    assert provider.generate(prompt()) == "FIRST"
    assert client.completions.call_count == 1


def test_client_reuse_recovers_after_mapped_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, factory, client = install_fake(
        monkeypatch,
        completion("FIRST"),
    )

    assert provider.generate(prompt()) == "FIRST"
    client.completions.outcome = APITimeoutError(
        request=httpx2.Request(
            "POST",
            f"{DEEPSEEK_BASE_URL}/chat/completions",
        )
    )
    with pytest.raises(InsightProviderError) as caught:
        provider.generate(prompt())
    client.completions.outcome = completion("THIRD")
    assert provider.generate(prompt()) == "THIRD"

    assert caught.value.code == PROVIDER_TIMEOUT
    assert factory.call_count == 1
    assert client.completions.call_count == 3


def test_provider_instances_have_independent_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FreshClientFactory(completion("OK"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(deepseek_module, "OpenAI", factory)

    first = DeepSeekInsightProvider()
    second = DeepSeekInsightProvider()

    assert first.generate(prompt()) == "OK"
    assert second.generate(prompt()) == "OK"
    assert factory.call_count == 2
    assert len(factory.clients) == 2
    assert factory.clients[0] is not factory.clients[1]
    assert [client.completions.call_count for client in factory.clients] == [1, 1]


@pytest.mark.parametrize(
    "content",
    ["", " ", "\n\t", None, b"{}", {}, 0],
)
def test_empty_or_non_string_content_is_invalid_provider_response(
    monkeypatch: pytest.MonkeyPatch,
    content: object,
) -> None:
    provider, _, client = install_fake(monkeypatch, completion(content))

    with pytest.raises(InsightProviderError) as caught:
        provider.generate(prompt())

    assert caught.value.code == INVALID_PROVIDER_RESPONSE
    assert client.completions.call_count == 1


@pytest.mark.parametrize(
    "response",
    [SimpleNamespace(), SimpleNamespace(choices=[]), SimpleNamespace(choices=None)],
)
def test_missing_or_empty_choices_are_invalid_provider_response(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    provider, _, client = install_fake(monkeypatch, response)

    with pytest.raises(InsightProviderError) as caught:
        provider.generate(prompt())

    assert caught.value.code == INVALID_PROVIDER_RESPONSE
    assert client.completions.call_count == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "finish_reason",
    ["length", "tool_calls", "content_filter", None, "unknown"],
)
def test_only_stop_finish_reason_is_accepted_and_partial_content_is_never_returned(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: object,
) -> None:
    provider, _, client = install_fake(
        monkeypatch,
        completion('{"partial":', finish_reason=finish_reason),
    )

    with pytest.raises(InsightProviderError) as caught:
        provider.generate(prompt())

    assert caught.value.code == INVALID_PROVIDER_RESPONSE
    assert client.completions.call_count == 1


def test_insufficient_system_resource_is_unavailable_without_parsing_content(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    partial_content = "SECRET_RESPONSE_BODY"
    provider, _, client = install_fake(
        monkeypatch,
        completion(
            partial_content,
            finish_reason="insufficient_system_resource",
        ),
    )
    parser_calls = 0
    validator_calls = 0

    def fail_parser(raw_response: str) -> object:
        nonlocal parser_calls
        parser_calls += 1
        raise AssertionError("strict JSON parser must not run")

    def fail_validator(payload: object, *, context: InsightContext) -> InsightOutput:
        nonlocal validator_calls
        validator_calls += 1
        raise AssertionError("Output Validator must not run")

    monkeypatch.setattr(generic_provider_module, "_strict_json_loads", fail_parser)
    monkeypatch.setattr(
        generic_provider_module,
        "validate_insight_output",
        fail_validator,
    )

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == PROVIDER_UNAVAILABLE
    assert client.completions.call_count == 1
    assert parser_calls == 0
    assert validator_calls == 0
    assert partial_content not in caught.value.message
    assert partial_content not in str(caught.value)
    assert partial_content not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "finish_reason",
    [
        "length",
        "content_filter",
        "tool_calls",
        "insufficient_system_resource",
        "unknown",
    ],
)
def test_non_stop_finish_reason_never_inspects_message_content(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str,
) -> None:
    class ExplodingMessage:
        @property
        def content(self) -> str:
            raise AssertionError("content must not be inspected")

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=ExplodingMessage(),
            )
        ]
    )
    provider, _, client = install_fake(monkeypatch, response)

    with pytest.raises(InsightProviderError) as caught:
        provider.generate(prompt())

    expected = (
        PROVIDER_UNAVAILABLE
        if finish_reason == "insufficient_system_resource"
        else INVALID_PROVIDER_RESPONSE
    )
    assert caught.value.code == expected
    assert client.completions.call_count == 1


def test_missing_message_or_content_is_invalid_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop")]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(),
                )
            ]
        ),
    ]
    for response in outcomes:
        provider, _, _ = install_fake(monkeypatch, response)
        with pytest.raises(InsightProviderError) as caught:
            provider.generate(prompt())
        assert caught.value.code == INVALID_PROVIDER_RESPONSE


def test_adapter_returns_nonempty_raw_content_without_parsing_or_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "\n  not json  \t"
    provider, _, _ = install_fake(monkeypatch, completion(raw))

    assert provider.generate(prompt()) == raw


def test_normal_string_subclass_is_returned_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NormalStr(str):
        pass

    raw = NormalStr("\n  {\"ok\":true}  \t")
    provider, _, _ = install_fake(monkeypatch, completion(raw))

    assert provider.generate(prompt()) is raw


@pytest.mark.parametrize("exception_type", [RuntimeError, TypeError])
def test_pathological_strip_failure_is_sanitized_without_exception_retention(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
    exception_type: type[Exception],
) -> None:
    secret = f"SECRET_{exception_type.__name__.upper()}_FROM_STRIP"

    class ExplodingStr(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            raise exception_type(secret)

    provider, _, client = install_fake(
        monkeypatch,
        completion(ExplodingStr("{}")),
    )

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_RESPONSE
    assert client.completions.call_count == 1
    assert secret not in caught.value.message
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("strip_result", ["", None])
def test_pathological_blank_strip_result_is_invalid_provider_response(
    monkeypatch: pytest.MonkeyPatch,
    strip_result: object,
) -> None:
    class WeirdStr(str):
        def strip(self, *args: object, **kwargs: object) -> object:
            return strip_result

    provider, _, _ = install_fake(monkeypatch, completion(WeirdStr("nonempty")))

    with pytest.raises(InsightProviderError) as caught:
        provider.generate(prompt())

    assert caught.value.code == INVALID_PROVIDER_RESPONSE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_strip_base_exception_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptingStr(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            raise KeyboardInterrupt

    provider, _, _ = install_fake(
        monkeypatch,
        completion(InterruptingStr("{}")),
    )

    with pytest.raises(KeyboardInterrupt):
        provider.generate(prompt())


@pytest.mark.parametrize(
    ("sdk_error", "expected_code"),
    [
        (
            APITimeoutError(
                request=httpx2.Request("POST", f"{DEEPSEEK_BASE_URL}/chat/completions")
            ),
            PROVIDER_TIMEOUT,
        ),
        (status_error(401, error_type=AuthenticationError), PROVIDER_AUTH_FAILED),
        (status_error(403, error_type=PermissionDeniedError), PROVIDER_AUTH_FAILED),
        (status_error(402), PROVIDER_ACCOUNT_ERROR),
        (status_error(429, error_type=RateLimitError), PROVIDER_RATE_LIMITED),
        (
            APIConnectionError(
                message="SECRET_SDK_MESSAGE",
                request=httpx2.Request(
                    "POST", f"{DEEPSEEK_BASE_URL}/chat/completions"
                ),
            ),
            PROVIDER_CONNECTION_FAILED,
        ),
        (status_error(400), PROVIDER_REQUEST_REJECTED),
        (status_error(404), PROVIDER_REQUEST_REJECTED),
        (status_error(422), PROVIDER_REQUEST_REJECTED),
        (status_error(500), PROVIDER_UNAVAILABLE),
        (status_error(503), PROVIDER_UNAVAILABLE),
        (status_error(599), PROVIDER_UNAVAILABLE),
        (status_error(418), PROVIDER_FAILURE),
        (RuntimeError("SECRET_SDK_MESSAGE"), PROVIDER_FAILURE),
    ],
    ids=[
        "timeout",
        "authentication-401",
        "permission-403",
        "balance-402",
        "rate-limit-429",
        "connection",
        "bad-request-400",
        "not-found-404",
        "unprocessable-422",
        "server-500",
        "unavailable-503",
        "other-5xx",
        "unexpected-status",
        "unexpected-runtime",
    ],
)
def test_sdk_errors_are_mapped_without_retry_or_raw_exception_chaining(
    monkeypatch: pytest.MonkeyPatch,
    sdk_error: Exception,
    expected_code: str,
) -> None:
    provider, _, client = install_fake(monkeypatch, sdk_error)

    with pytest.raises(InsightProviderError) as caught:
        provider.generate(
            prompt(
                system_prompt="SECRET_PROMPT_TEXT",
                user_prompt="SECRET_PROMPT_TEXT",
            )
        )

    assert caught.value.code == expected_code
    assert client.completions.call_count == 1
    public_error = f"{caught.value!s} {caught.value!r}"
    for secret in (
        TEST_API_KEY,
        "SECRET_DEEPSEEK_KEY",
        "SECRET_PROMPT_TEXT",
        "SECRET_RESPONSE_BODY",
        "SECRET_SDK_MESSAGE",
        "Authorization",
    ):
        assert secret not in public_error
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("base_exception", [KeyboardInterrupt(), SystemExit()])
def test_base_exceptions_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    base_exception: BaseException,
) -> None:
    provider, _, client = install_fake(monkeypatch, base_exception)

    with pytest.raises(type(base_exception)):
        provider.generate(prompt())

    assert client.completions.call_count == 1


def test_generic_orchestration_rethrows_adapter_error_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    provider, _, _ = install_fake(
        monkeypatch,
        status_error(429, error_type=RateLimitError),
    )

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == PROVIDER_RATE_LIMITED
    assert caught.value.__cause__ is None


def test_fake_sdk_sample_e2e_reaches_strict_parser_and_output_validator(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    payload = valid_payload(sample_context)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    provider, factory, client = install_fake(monkeypatch, completion(raw))

    output = generate_insight(sample_context, provider=provider)

    assert len(sample_context.metric_records) == 12
    assert len(sample_context.diagnostic_signals) == 11
    assert isinstance(output, InsightOutput)
    assert output.to_dict() == payload
    assert output.priority_insights[0].scope == {"sku": "SKU-LOW-CTR"}
    assert output.priority_insights[0].evidence_codes == (
        "HIGH_IMPRESSIONS_LOW_CTR",
    )
    assert factory.call_count == 1
    assert client.completions.call_count == 1


def test_nonempty_malformed_model_content_is_rejected_by_generic_strict_json(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    provider, _, client = install_fake(monkeypatch, completion("not json"))

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_JSON
    assert client.completions.call_count == 1


def test_valid_json_with_fake_evidence_is_rejected_by_existing_output_validator(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    payload = valid_payload(sample_context)
    payload["priority_insights"][0]["evidence_codes"] = ["FAKE_CODE"]  # type: ignore[index]
    provider, _, _ = install_fake(
        monkeypatch,
        completion(json.dumps(payload, separators=(",", ":"))),
    )

    with pytest.raises(InsightOutputError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_INSIGHT_OUTPUT


def test_empty_content_fails_in_adapter_before_generic_json_parser(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    provider, _, _ = install_fake(monkeypatch, completion(" "))

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_RESPONSE


def test_canonical_output_limit_remains_enforced_through_deepseek_path(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    payload = oversized_valid_payload(sample_context)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    canonical_bytes = len(raw.encode("utf-8"))
    assert MAX_INSIGHT_OUTPUT_BYTES < canonical_bytes <= MAX_PROVIDER_RESPONSE_BYTES
    provider, _, client = install_fake(monkeypatch, completion(raw))

    with pytest.raises(InsightOutputError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == OUTPUT_TOO_LARGE
    assert client.completions.call_count == 1


def test_oversized_raw_content_is_still_rejected_by_generic_boundary(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
) -> None:
    provider, _, _ = install_fake(
        monkeypatch,
        completion(" " * 100_001 + "{}"),
    )

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == PROVIDER_RESPONSE_TOO_LARGE


@pytest.mark.parametrize(
    "raw",
    [
        '{"version":"1","version":"1"}',
        '{"version":"1","value":NaN}',
        '{"version":"1","value":1e9999}',
    ],
)
def test_duplicate_keys_and_nonfinite_numbers_still_use_generic_strict_parser(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: InsightContext,
    raw: str,
) -> None:
    provider, _, _ = install_fake(monkeypatch, completion(raw))

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_JSON


def test_no_legacy_model_names_exist_in_adapter_source() -> None:
    source = Path(deepseek_module.__file__).read_text(encoding="utf-8")

    assert DEEPSEEK_MODEL == "deepseek-v4-flash"
    assert "deepseek-chat" not in source
    assert "deepseek-reasoner" not in source
