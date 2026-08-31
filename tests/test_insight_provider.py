from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from math import isfinite
from pathlib import Path
from typing import Any

import pytest

import src.insight_provider as provider_module
from src.insight_prompt import (
    INSIGHT_OUTPUT_VERSION,
    INVALID_INSIGHT_OUTPUT,
    INVALID_PROMPT_INPUT,
    MAX_CHECKS_PER_INSIGHT,
    MAX_EXECUTIVE_SUMMARY_CHARS,
    MAX_EXPLANATIONS_PER_INSIGHT,
    MAX_INSIGHT_OUTPUT_BYTES,
    MAX_INSIGHT_TEXT_CHARS,
    MAX_OBSERVATION_CHARS,
    MAX_OVERALL_LIMITATIONS,
    MAX_PRIORITY_INSIGHTS,
    OUTPUT_TOO_LARGE,
    PROMPT_TOO_LARGE,
    InsightOutput,
    InsightOutputError,
    InsightPromptError,
    build_insight_prompt,
    validate_insight_output,
)
from src.insight_provider import (
    INVALID_PROVIDER,
    INVALID_PROVIDER_JSON,
    INVALID_PROVIDER_RESPONSE,
    INVALID_PROVIDER_USAGE,
    MAX_PROVIDER_RESPONSE_BYTES,
    PROVIDER_FAILURE,
    PROVIDER_RESPONSE_TOO_LARGE,
    InsightProvider,
    InsightGenerationResult,
    InsightProviderError,
    MockInsightProvider,
    ProviderGeneration,
    ProviderUsage,
    generate_insight,
    generate_insight_with_metadata,
)
from src.insights import (
    INSIGHT_CONTEXT_LIMITATIONS,
    INSIGHT_CONTEXT_VERSION,
    InsightContext,
    build_insight_context,
)
from src.pipeline import run_pipeline


SAMPLE_PATH = Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"


@pytest.fixture(scope="module")
def sample_context() -> InsightContext:
    return build_insight_context(run_pipeline(SAMPLE_PATH, group_by="sku"))


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
                "recommended_checks": ["Review the supporting operational inputs."],
                "confidence": "medium",
            }
        ],
        "overall_limitations": [],
    }


def raw_json(payload: object, **kwargs: object) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, **kwargs)


def raw_response_at_size(
    payload: dict[str, object],
    *,
    target_bytes: int,
    unicode_prefix: str,
) -> str:
    adjusted = deepcopy(payload)
    adjusted["executive_summary"] = f"Summary {unicode_prefix}"
    document = raw_json(adjusted, separators=(",", ":"))
    remaining = target_bytes - len(document.encode("utf-8"))
    assert remaining >= 0
    response = " " * remaining + document
    assert len(response.encode("utf-8")) == target_bytes
    return response


def canonical_size(payload: object) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def output_payload_at_size(
    context: InsightContext,
    *,
    target_bytes: int,
) -> dict[str, object]:
    priorities: list[dict[str, object]] = []
    for signal in context.diagnostic_signals[:MAX_PRIORITY_INSIGHTS]:
        priorities.append(
            {
                "scope": deepcopy(signal["group"]),
                "observation": "x",
                "evidence_codes": [signal["code"]],
                "possible_explanations": [
                    "x" for _ in range(MAX_EXPLANATIONS_PER_INSIGHT)
                ],
                "recommended_checks": [
                    "x" for _ in range(MAX_CHECKS_PER_INSIGHT)
                ],
                "confidence": "medium",
            }
        )
    assert len(priorities) == MAX_PRIORITY_INSIGHTS
    payload: dict[str, object] = {
        "version": INSIGHT_OUTPUT_VERSION,
        "executive_summary": "x",
        "priority_insights": priorities,
        "overall_limitations": ["x" for _ in range(MAX_OVERALL_LIMITATIONS)],
    }
    slots: list[tuple[Any, str | int, int]] = [
        (payload, "executive_summary", MAX_EXECUTIVE_SUMMARY_CHARS)
    ]
    for insight in priorities:
        slots.append((insight, "observation", MAX_OBSERVATION_CHARS))
        for field in ("possible_explanations", "recommended_checks"):
            values = insight[field]
            for position in range(len(values)):  # type: ignore[arg-type]
                slots.append((values, position, MAX_INSIGHT_TEXT_CHARS))
    limitations = payload["overall_limitations"]
    for position in range(len(limitations)):  # type: ignore[arg-type]
        slots.append((limitations, position, MAX_INSIGHT_TEXT_CHARS))

    remaining = target_bytes - canonical_size(payload)
    assert remaining >= 0
    for container, key, max_chars in slots:
        current = container[key]
        added = min(remaining, max_chars - len(current))
        container[key] = current + "x" * added
        remaining -= added
        if remaining == 0:
            break
    assert remaining == 0
    assert canonical_size(payload) == target_bytes
    return payload


def context_with_large_message(context: InsightContext) -> InsightContext:
    signals = deepcopy(context.diagnostic_signals)
    signals[0]["message"] = "x" * 100_000
    return InsightContext(
        version=context.version,
        analysis_scope=deepcopy(context.analysis_scope),
        metric_records=deepcopy(context.metric_records),
        diagnostic_signals=signals,
        limitations=tuple(context.limitations),
    )


def empty_context() -> InsightContext:
    return InsightContext(
        version=INSIGHT_CONTEXT_VERSION,
        analysis_scope={
            "group_dimensions": [],
            "metric_group_count": 0,
            "diagnostic_signal_count": 0,
            "valid_rows": 0,
            "excluded_rows": 0,
            "warning_rows": 0,
        },
        metric_records=(),
        diagnostic_signals=(),
        limitations=INSIGHT_CONTEXT_LIMITATIONS,
    )


def conservative_empty_payload() -> dict[str, object]:
    return {
        "version": INSIGHT_OUTPUT_VERSION,
        "executive_summary": "No diagnostic signals were supplied.",
        "priority_insights": [],
        "overall_limitations": [],
    }


def test_sample_mock_provider_e2e_and_public_contract(
    sample_context: InsightContext,
) -> None:
    payload = valid_payload(sample_context)
    response = raw_json(payload, separators=(",", ":"))
    provider: InsightProvider = MockInsightProvider(response)
    context_before = sample_context.to_dict()
    expected_prompt = build_insight_prompt(sample_context)

    output = generate_insight(sample_context, provider=provider)

    assert len(sample_context.metric_records) == 12
    assert len(sample_context.diagnostic_signals) == 11
    assert isinstance(output, InsightOutput)
    assert output.to_dict() == payload
    assert provider.call_count == 1  # type: ignore[attr-defined]
    assert provider.last_prompt == expected_prompt  # type: ignore[attr-defined]
    assert provider.response == response  # type: ignore[attr-defined]
    assert sample_context.to_dict() == context_before


def test_metadata_api_returns_validated_output_and_exact_usage(
    sample_context: InsightContext,
) -> None:
    payload = valid_payload(sample_context)
    usage = ProviderUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        prompt_cache_hit_tokens=60,
        prompt_cache_miss_tokens=40,
        reasoning_tokens=0,
    )
    provider = MockInsightProvider(raw_json(payload), usage=usage)

    result = generate_insight_with_metadata(sample_context, provider=provider)

    assert isinstance(result, InsightGenerationResult)
    assert result.output.to_dict() == payload
    assert result.usage is usage
    assert provider.call_count == 1
    assert not hasattr(provider, "last_usage")


def test_metadata_api_allows_missing_usage(
    sample_context: InsightContext,
) -> None:
    result = generate_insight_with_metadata(
        sample_context,
        provider=MockInsightProvider(raw_json(valid_payload(sample_context))),
    )

    assert isinstance(result.output, InsightOutput)
    assert result.usage is None


def test_legacy_and_metadata_apis_return_equivalent_outputs(
    sample_context: InsightContext,
) -> None:
    response = raw_json(valid_payload(sample_context))

    legacy = generate_insight(
        sample_context,
        provider=MockInsightProvider(response),
    )
    metadata = generate_insight_with_metadata(
        sample_context,
        provider=MockInsightProvider(response),
    )

    assert isinstance(legacy, InsightOutput)
    assert isinstance(metadata, InsightGenerationResult)
    assert legacy.to_dict() == metadata.output.to_dict()


def test_mock_provider_returns_explicit_generation_envelope(
    sample_context: InsightContext,
) -> None:
    usage = ProviderUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    provider = MockInsightProvider("{}", usage=usage)

    generation = provider.generate(build_insight_prompt(sample_context))

    assert generation == ProviderGeneration(raw_text="{}", usage=usage)
    assert provider.call_count == 1
    assert provider.last_prompt == build_insight_prompt(sample_context)


def test_bare_string_from_legacy_provider_is_rejected(
    sample_context: InsightContext,
) -> None:
    response = raw_json(valid_payload(sample_context))

    class LegacyProvider:
        def generate(self, prompt: object) -> str:
            return response

    with pytest.raises(InsightProviderError) as caught:
        generate_insight_with_metadata(
            sample_context,
            provider=LegacyProvider(),  # type: ignore[arg-type]
        )

    assert caught.value.code == INVALID_PROVIDER_RESPONSE


@pytest.mark.parametrize("value", [None, {}, [], b"{}", 1, True])
def test_invalid_provider_generation_types_are_rejected(
    sample_context: InsightContext,
    value: object,
) -> None:
    class InvalidProvider:
        def generate(self, prompt: object) -> object:
            return value

    with pytest.raises(InsightProviderError) as caught:
        generate_insight_with_metadata(
            sample_context,
            provider=InvalidProvider(),  # type: ignore[arg-type]
        )

    assert caught.value.code == INVALID_PROVIDER_RESPONSE


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt_tokens": -1, "total_tokens": 19},
        {"completion_tokens": -1, "total_tokens": 99},
        {"total_tokens": -1},
        {"prompt_tokens": True},
        {"completion_tokens": True},
        {"total_tokens": True},
        {"total_tokens": 999},
        {"prompt_cache_hit_tokens": 60},
        {"prompt_cache_miss_tokens": 40},
        {
            "prompt_cache_hit_tokens": 60,
            "prompt_cache_miss_tokens": 50,
        },
        {
            "prompt_cache_hit_tokens": -1,
            "prompt_cache_miss_tokens": 101,
        },
        {
            "prompt_cache_hit_tokens": True,
            "prompt_cache_miss_tokens": 99,
        },
        {"reasoning_tokens": -1},
        {"reasoning_tokens": True},
        {"reasoning_tokens": 21},
    ],
)
def test_provider_usage_rejects_invalid_or_inconsistent_values(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": None,
        "prompt_cache_miss_tokens": None,
        "reasoning_tokens": None,
    }
    values.update(overrides)

    with pytest.raises(InsightProviderError) as caught:
        ProviderUsage(**values)  # type: ignore[arg-type]

    assert caught.value.code == INVALID_PROVIDER_USAGE


def test_zero_and_unbounded_python_integer_usage_are_valid() -> None:
    zero = ProviderUsage(
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=0,
        reasoning_tokens=0,
    )
    huge = 10**100
    large = ProviderUsage(
        prompt_tokens=huge,
        completion_tokens=huge,
        total_tokens=huge * 2,
    )

    assert zero.total_tokens == 0
    assert large.total_tokens == huge * 2


def test_generation_contracts_are_frozen() -> None:
    usage = ProviderUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    generation = ProviderGeneration(raw_text="{}", usage=usage)
    result = InsightGenerationResult(
        output=InsightOutput(
            version="1",
            executive_summary="summary",
            priority_insights=(),
            overall_limitations=(),
        ),
        usage=usage,
    )

    with pytest.raises(FrozenInstanceError):
        usage.total_tokens = 3  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        generation.raw_text = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.usage = None  # type: ignore[misc]


def test_valid_usage_does_not_bypass_invalid_json(
    sample_context: InsightContext,
) -> None:
    usage = ProviderUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    with pytest.raises(InsightProviderError) as caught:
        generate_insight_with_metadata(
            sample_context,
            provider=MockInsightProvider("{", usage=usage),
        )

    assert caught.value.code == INVALID_PROVIDER_JSON


def test_valid_usage_does_not_bypass_output_validation(
    sample_context: InsightContext,
) -> None:
    payload = valid_payload(sample_context)
    payload["priority_insights"][0]["evidence_codes"] = ["FAKE_CODE"]  # type: ignore[index]
    usage = ProviderUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    with pytest.raises(InsightOutputError) as caught:
        generate_insight_with_metadata(
            sample_context,
            provider=MockInsightProvider(raw_json(payload), usage=usage),
        )

    assert caught.value.code == INVALID_INSIGHT_OUTPUT


def test_usage_metadata_is_outside_raw_and_canonical_byte_limits(
    sample_context: InsightContext,
) -> None:
    huge = 10**100
    usage = ProviderUsage(
        prompt_tokens=huge,
        completion_tokens=huge,
        total_tokens=huge * 2,
    )

    result = generate_insight_with_metadata(
        sample_context,
        provider=MockInsightProvider(
            raw_json(valid_payload(sample_context)),
            usage=usage,
        ),
    )

    assert result.usage is usage
    assert isinstance(result.output, InsightOutput)


@pytest.mark.parametrize("formatting", ["compact", "pretty", "whitespace", "reordered"])
def test_valid_json_formatting_and_key_order_are_accepted(
    sample_context: InsightContext,
    formatting: str,
) -> None:
    payload = valid_payload(sample_context)
    if formatting == "compact":
        response = raw_json(payload, separators=(",", ":"))
    elif formatting == "pretty":
        response = raw_json(payload, indent=2)
    elif formatting == "whitespace":
        response = f" \n\t{raw_json(payload)}\r\n "
    else:
        response = raw_json(
            {
                "overall_limitations": payload["overall_limitations"],
                "priority_insights": payload["priority_insights"],
                "executive_summary": payload["executive_summary"],
                "version": payload["version"],
            },
            separators=(",", ":"),
        )

    output = generate_insight(
        sample_context,
        provider=MockInsightProvider(response),
    )

    assert output.to_dict() == payload


def test_repeated_generation_is_deterministic_and_provider_state_is_isolated(
    sample_context: InsightContext,
) -> None:
    response = raw_json(valid_payload(sample_context))
    first_provider = MockInsightProvider(response)
    second_provider = MockInsightProvider(response)

    first = generate_insight(sample_context, provider=first_provider)
    second = generate_insight(sample_context, provider=first_provider)

    assert first.to_dict() == second.to_dict()
    assert first_provider.call_count == 2
    assert second_provider.call_count == 0
    assert second_provider.last_prompt is None


def test_mock_error_takes_priority_over_response(
    sample_context: InsightContext,
) -> None:
    original = RuntimeError("configured failure")
    provider = MockInsightProvider(
        raw_json(valid_payload(sample_context)),
        error=original,
    )

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == PROVIDER_FAILURE
    assert caught.value.__cause__ is original
    assert provider.call_count == 1
    assert provider.last_prompt == build_insight_prompt(sample_context)


def test_mock_default_empty_response_is_invalid_json(
    sample_context: InsightContext,
) -> None:
    provider = MockInsightProvider()

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_JSON
    assert provider.call_count == 1
    assert provider.last_prompt == build_insight_prompt(sample_context)


@pytest.mark.parametrize("provider", [None, object()])
def test_missing_provider_generate_is_rejected(
    sample_context: InsightContext,
    provider: object,
) -> None:
    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)  # type: ignore[arg-type]

    assert caught.value.code == INVALID_PROVIDER


def test_noncallable_provider_generate_is_rejected(
    sample_context: InsightContext,
) -> None:
    provider = type("InvalidProvider", (), {"generate": "not callable"})()

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)  # type: ignore[arg-type]

    assert caught.value.code == INVALID_PROVIDER


def test_dynamic_generate_property_failure_is_invalid_provider(
    sample_context: InsightContext,
) -> None:
    original = RuntimeError("property boom")

    class DynamicProvider:
        @property
        def generate(self) -> object:
            raise original

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=DynamicProvider())  # type: ignore[arg-type]

    assert caught.value.code == INVALID_PROVIDER
    assert caught.value.__cause__ is original


def test_wrong_generate_signature_is_provider_failure(
    sample_context: InsightContext,
) -> None:
    class WrongSignatureProvider:
        def generate(self) -> str:
            return "{}"

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=WrongSignatureProvider())  # type: ignore[arg-type]

    assert caught.value.code == PROVIDER_FAILURE
    assert isinstance(caught.value.__cause__, TypeError)


@pytest.mark.parametrize("invalid_context", [None, {}, "context"])
def test_invalid_context_remains_a_prompt_error_without_provider_call(
    invalid_context: object,
) -> None:
    provider = MockInsightProvider("{}")

    with pytest.raises(InsightPromptError) as caught:
        generate_insight(invalid_context, provider=provider)  # type: ignore[arg-type]

    assert caught.value.code == INVALID_PROMPT_INPUT
    assert provider.call_count == 0


def test_prompt_too_large_does_not_call_provider(
    sample_context: InsightContext,
) -> None:
    provider = MockInsightProvider("{}")

    with pytest.raises(InsightPromptError) as caught:
        generate_insight(context_with_large_message(sample_context), provider=provider)

    assert caught.value.code == PROMPT_TOO_LARGE
    assert provider.call_count == 0


def test_provider_runtime_failure_is_wrapped_once_without_secret_leakage(
    sample_context: InsightContext,
) -> None:
    original = RuntimeError("private provider detail")
    provider = MockInsightProvider(error=original)

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == PROVIDER_FAILURE
    assert caught.value.message == "Provider 调用失败。"
    assert "private provider detail" not in str(caught.value)
    assert caught.value.__cause__ is original
    assert provider.call_count == 1


@pytest.mark.parametrize(
    "original",
    [
        InsightProviderError("CUSTOM_PROVIDER_ERROR", "stable"),
        InsightPromptError("CUSTOM_PROMPT_ERROR", "stable"),
        InsightOutputError("CUSTOM_OUTPUT_ERROR", "stable"),
    ],
)
def test_known_contract_errors_from_provider_are_not_rewrapped(
    sample_context: InsightContext,
    original: Exception,
) -> None:
    provider = MockInsightProvider(error=original)

    with pytest.raises(type(original)) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value is original
    assert provider.call_count == 1


def test_keyboard_interrupt_is_not_caught(sample_context: InsightContext) -> None:
    class InterruptingProvider:
        call_count = 0

        def generate(self, prompt: object) -> str:
            self.call_count += 1
            raise KeyboardInterrupt

    provider = InterruptingProvider()

    with pytest.raises(KeyboardInterrupt):
        generate_insight(sample_context, provider=provider)

    assert provider.call_count == 1


def test_non_string_provider_return_types_are_rejected(
    sample_context: InsightContext,
) -> None:
    valid_output = validate_insight_output(
        valid_payload(sample_context),
        context=sample_context,
    )
    for response in (None, {}, [], b"{}", 1, True, valid_output):
        provider = MockInsightProvider(response)
        with pytest.raises(InsightProviderError) as caught:
            generate_insight(sample_context, provider=provider)

        assert caught.value.code == INVALID_PROVIDER_RESPONSE
        assert provider.call_count == 1


def test_non_utf8_encodable_string_is_invalid_provider_response(
    sample_context: InsightContext,
) -> None:
    provider = MockInsightProvider("\ud800")

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_RESPONSE
    assert isinstance(caught.value.__cause__, UnicodeEncodeError)


def test_normal_string_subclass_is_accepted(
    sample_context: InsightContext,
) -> None:
    class NormalString(str):
        pass

    response = NormalString(raw_json(valid_payload(sample_context)))

    output = generate_insight(
        sample_context,
        provider=MockInsightProvider(response),
    )

    assert isinstance(output, InsightOutput)


def test_pathological_string_subclass_encoding_is_structured(
    sample_context: InsightContext,
) -> None:
    class RuntimeEncodingError(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise RuntimeError("private encode boom")

    class TypeEncodingError(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise TypeError("private encode type")

    class InvalidEncodingResult(str):
        def encode(self, *args: object, **kwargs: object) -> None:
            return None

    class TextEncodingResult(str):
        def encode(self, *args: object, **kwargs: object) -> str:
            return "not bytes"

    for response, expected_cause in (
        (RuntimeEncodingError("{}"), RuntimeError),
        (TypeEncodingError("{}"), TypeError),
        (InvalidEncodingResult("{}"), TypeError),
        (TextEncodingResult("{}"), TypeError),
    ):
        provider = MockInsightProvider(response)
        with pytest.raises(InsightProviderError) as caught:
            generate_insight(sample_context, provider=provider)

        assert caught.value.code == INVALID_PROVIDER_RESPONSE
        assert isinstance(caught.value.__cause__, expected_cause)
        assert "private encode" not in caught.value.message
        assert provider.call_count == 1


@pytest.mark.parametrize("unicode_prefix", ["ASCII", "中文", "🚀"])
def test_raw_response_utf8_size_boundary(
    sample_context: InsightContext,
    unicode_prefix: str,
) -> None:
    payload = valid_payload(sample_context)
    at_limit = raw_response_at_size(
        payload,
        target_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        unicode_prefix=unicode_prefix,
    )
    accepted_provider = MockInsightProvider(at_limit)

    output = generate_insight(sample_context, provider=accepted_provider)

    assert isinstance(output, InsightOutput)
    assert accepted_provider.call_count == 1
    over_limit = raw_response_at_size(
        payload,
        target_bytes=MAX_PROVIDER_RESPONSE_BYTES + 1,
        unicode_prefix=unicode_prefix,
    )
    rejected_provider = MockInsightProvider(over_limit)
    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=rejected_provider)

    assert caught.value.code == PROVIDER_RESPONSE_TOO_LARGE
    assert rejected_provider.response == over_limit
    assert rejected_provider.call_count == 1


def test_oversized_raw_response_is_rejected_before_json_parsing(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_called = False

    def unexpected_parse(raw_response: str) -> object:
        nonlocal parser_called
        parser_called = True
        raise AssertionError("parser must not run")

    monkeypatch.setattr(provider_module, "_strict_json_loads", unexpected_parse)
    provider = MockInsightProvider("x" * (MAX_PROVIDER_RESPONSE_BYTES + 1))

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == PROVIDER_RESPONSE_TOO_LARGE
    assert parser_called is False
    assert provider.call_count == 1


@pytest.mark.parametrize(
    "response",
    [
        "",
        "   \n\t",
        "{",
        "{'version':'1'}",
        '{"version":"1",}',
        '```json\n{"version":"1"}\n```',
        'Here is the result: {"version":"1"}',
        '{"version":"1"} Done.',
        "{}\n{}",
        '\ufeff{"version":"1"}',
    ],
)
def test_malformed_or_wrapped_json_is_rejected_without_response_leakage(
    sample_context: InsightContext,
    response: str,
) -> None:
    provider = MockInsightProvider(response)

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_JSON
    assert caught.value.message == "Provider response 不是合法的 strict JSON document。"
    if response:
        assert response not in caught.value.message
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_malformed_json_does_not_retain_secret_raw_response(
    sample_context: InsightContext,
) -> None:
    secret = "SECRET_API_KEY_123"
    response = f'{{"{secret}":"user private data",'

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(
            sample_context,
            provider=MockInsightProvider(response),
        )

    assert caught.value.code == INVALID_PROVIDER_JSON
    for rendered in (
        caught.value.message,
        str(caught.value),
        repr(caught.value),
        repr(caught.value.__cause__),
        repr(caught.value.__context__),
    ):
        assert secret not in rendered
        assert response not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_constants_are_rejected(
    sample_context: InsightContext,
    constant: str,
) -> None:
    response = f'{{"version":"1","value":{constant}}}'

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=MockInsightProvider(response))

    assert caught.value.code == INVALID_PROVIDER_JSON


@pytest.mark.parametrize(
    "response",
    [
        '{"x":1e309}',
        '{"x":-1e309}',
        '{"x":1e9999}',
        '{"x":-1e9999}',
        '{"scope":{"x":1e309}}',
        '{"items":[1e9999]}',
    ],
)
def test_exponent_overflow_is_rejected_by_strict_json_layer(
    sample_context: InsightContext,
    response: str,
) -> None:
    provider = MockInsightProvider(response)

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == INVALID_PROVIDER_JSON
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert provider.call_count == 1


def test_exponent_overflow_does_not_call_output_validator(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator_calls = 0

    def unexpected_validator(payload: object, *, context: InsightContext) -> InsightOutput:
        nonlocal validator_calls
        validator_calls += 1
        raise AssertionError("validator must not run")

    monkeypatch.setattr(
        provider_module,
        "validate_insight_output",
        unexpected_validator,
    )

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(
            sample_context,
            provider=MockInsightProvider('{"x":1e9999}'),
        )

    assert caught.value.code == INVALID_PROVIDER_JSON
    assert validator_calls == 0


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("0.0", 0.0),
        ("-0.0", -0.0),
        ("1.5", 1.5),
        ("1e10", 1e10),
        ("1e-10", 1e-10),
        ("1.7976931348623157e308", 1.7976931348623157e308),
    ],
)
def test_finite_json_floats_remain_supported(token: str, expected: float) -> None:
    parsed = provider_module._strict_json_loads(f'{{"value":{token}}}')

    assert isinstance(parsed, dict)
    assert parsed["value"] == expected
    assert isfinite(parsed["value"])


def test_huge_json_integer_remains_exact() -> None:
    expected = 999999999999999999999999999999999999

    parsed = provider_module._strict_json_loads(f'{{"value":{expected}}}')

    assert isinstance(parsed, dict)
    assert parsed["value"] == expected
    assert isinstance(parsed["value"], int)


@pytest.mark.parametrize(
    "response",
    [
        '{"version":"1","version":"2"}',
        '{"outer":{"value":1,"value":2}}',
        (
            '{"version":"1","executive_summary":"x",'
            '"priority_insights":[{"scope":{"sku":"A","sku":"B"}}],'
            '"overall_limitations":[]}'
        ),
        r'{"scope":{"sku":"A","\u0073ku":"B"}}',
    ],
)
def test_duplicate_json_keys_are_rejected_at_every_depth(
    sample_context: InsightContext,
    response: str,
) -> None:
    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=MockInsightProvider(response))

    assert caught.value.code == INVALID_PROVIDER_JSON


def test_deep_json_recursion_remains_structured() -> None:
    for depth in (500, 900):
        parsed = provider_module._strict_json_loads("[" * depth + "0" + "]" * depth)
        assert isinstance(parsed, list)

    with pytest.raises(InsightProviderError) as caught:
        provider_module._strict_json_loads("[" * 1_000 + "0" + "]" * 1_000)

    assert caught.value.code == INVALID_PROVIDER_JSON
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("response", ["[]", '"hello"', "123", "null"])
def test_valid_json_with_invalid_top_level_reaches_output_validator(
    sample_context: InsightContext,
    response: str,
) -> None:
    with pytest.raises(InsightOutputError) as caught:
        generate_insight(sample_context, provider=MockInsightProvider(response))

    assert caught.value.code == INVALID_INSIGHT_OUTPUT


@pytest.mark.parametrize(
    "invalid_case",
    ["missing_version", "fake_scope", "fake_evidence", "root_cause", "confidence"],
)
def test_schema_invalid_json_remains_an_output_contract_error(
    sample_context: InsightContext,
    invalid_case: str,
) -> None:
    payload = valid_payload(sample_context)
    if invalid_case == "missing_version":
        del payload["version"]
    elif invalid_case == "fake_scope":
        payload["priority_insights"][0]["scope"] = {"sku": "FAKE"}  # type: ignore[index]
    elif invalid_case == "fake_evidence":
        payload["priority_insights"][0]["evidence_codes"] = ["FAKE"]  # type: ignore[index]
    elif invalid_case == "root_cause":
        payload["priority_insights"][0]["root_cause"] = "unsupported"  # type: ignore[index]
    else:
        payload["priority_insights"][0]["confidence"] = "HIGH"  # type: ignore[index]

    with pytest.raises(InsightOutputError) as caught:
        generate_insight(
            sample_context,
            provider=MockInsightProvider(raw_json(payload)),
        )

    assert caught.value.code == INVALID_INSIGHT_OUTPUT


def test_canonical_output_too_large_remains_output_error(
    sample_context: InsightContext,
) -> None:
    payload = output_payload_at_size(
        sample_context,
        target_bytes=MAX_INSIGHT_OUTPUT_BYTES + 1,
    )
    response = raw_json(payload, separators=(",", ":"))
    assert len(response.encode("utf-8")) < MAX_PROVIDER_RESPONSE_BYTES
    provider = MockInsightProvider(response)

    with pytest.raises(InsightOutputError) as caught:
        generate_insight(sample_context, provider=provider)

    assert caught.value.code == OUTPUT_TOO_LARGE
    assert provider.call_count == 1


def test_raw_and_canonical_size_layers_remain_independent(
    sample_context: InsightContext,
) -> None:
    canonical_50k = output_payload_at_size(sample_context, target_bytes=50_000)
    document_50k = raw_json(canonical_50k, separators=(",", ":"))
    raw_90k_valid = " " * (
        90_000 - len(document_50k.encode("utf-8"))
    ) + document_50k

    output = generate_insight(
        sample_context,
        provider=MockInsightProvider(raw_90k_valid),
    )

    assert canonical_size(output.to_dict()) == 50_000

    canonical_70k = output_payload_at_size(sample_context, target_bytes=70_000)
    document_70k = raw_json(canonical_70k, separators=(",", ":"))
    raw_90k_output_too_large = " " * (
        90_000 - len(document_70k.encode("utf-8"))
    ) + document_70k
    with pytest.raises(InsightOutputError) as output_error:
        generate_insight(
            sample_context,
            provider=MockInsightProvider(raw_90k_output_too_large),
        )
    assert output_error.value.code == OUTPUT_TOO_LARGE

    small_document = raw_json(valid_payload(sample_context), separators=(",", ":"))
    raw_110k = " " * (
        110_000 - len(small_document.encode("utf-8"))
    ) + small_document
    with pytest.raises(InsightProviderError) as provider_error:
        generate_insight(
            sample_context,
            provider=MockInsightProvider(raw_110k),
        )
    assert provider_error.value.code == PROVIDER_RESPONSE_TOO_LARGE


def test_invalid_json_does_not_call_output_validator(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator_called = False

    def unexpected_validator(payload: object, *, context: InsightContext) -> InsightOutput:
        nonlocal validator_called
        validator_called = True
        raise AssertionError("validator must not run")

    monkeypatch.setattr(
        provider_module,
        "validate_insight_output",
        unexpected_validator,
    )

    with pytest.raises(InsightProviderError) as caught:
        generate_insight(sample_context, provider=MockInsightProvider("{"))

    assert caught.value.code == INVALID_PROVIDER_JSON
    assert validator_called is False


def test_no_diagnostics_mock_e2e(sample_context: InsightContext) -> None:
    context = InsightContext(
        version=sample_context.version,
        analysis_scope={
            **deepcopy(sample_context.analysis_scope),
            "diagnostic_signal_count": 0,
        },
        metric_records=deepcopy(sample_context.metric_records),
        diagnostic_signals=(),
        limitations=tuple(sample_context.limitations),
    )
    provider = MockInsightProvider(raw_json(conservative_empty_payload()))

    output = generate_insight(context, provider=provider)

    assert output.priority_insights == ()
    assert provider.call_count == 1


def test_empty_context_mock_e2e() -> None:
    context = empty_context()
    before = context.to_dict()
    provider = MockInsightProvider(raw_json(conservative_empty_payload()))

    output = generate_insight(context, provider=provider)

    assert output.priority_insights == ()
    assert output.executive_summary == "No diagnostic signals were supplied."
    assert provider.call_count == 1
    assert context.to_dict() == before


def test_provider_raw_limit_is_distinct_from_canonical_output_limit() -> None:
    assert MAX_PROVIDER_RESPONSE_BYTES == 100_000
    assert MAX_PROVIDER_RESPONSE_BYTES > MAX_INSIGHT_OUTPUT_BYTES
