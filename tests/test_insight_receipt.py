from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import src.insight_receipt as receipt_module
from src.deepseek_provider import DEEPSEEK_MODEL
from src.insight_cost_audit import (
    AVAILABLE,
    UNAVAILABLE,
    CostAuditMetadata,
    build_cost_audit_metadata,
)
from src.insight_pricing import USAGE_UNAVAILABLE
from src.insight_prompt import (
    INSIGHT_OUTPUT_VERSION,
    INSIGHT_PROMPT_VERSION,
    InsightOutput,
    PriorityInsight,
)
from src.insight_receipt import (
    DEEPSEEK_PROVIDER_NAME,
    INSIGHT_RECEIPT_VERSION,
    INVALID_RECEIPT_INPUT,
    MAX_RECEIPT_TOKEN_DECIMAL_DIGITS,
    InsightGenerationReceipt,
    InsightReceiptError,
    build_insight_generation_receipt,
)
from src.insight_provider import ProviderUsage
from src.insights import INSIGHT_CONTEXT_VERSION, build_insight_context
from src.pipeline import run_pipeline


PROJECT_ROOT = Path(__file__).parents[1]
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample_ecommerce_data.csv"
FIXED_TIME = datetime(2026, 8, 28, 6, 30, 12, 123456, tzinfo=timezone.utc)
PRICING_REFERENCE_AT = datetime(
    2026,
    8,
    28,
    6,
    30,
    tzinfo=timezone.utc,
)


def sample_context():
    return build_insight_context(run_pipeline(SAMPLE_PATH, group_by="sku"))


def sample_output(*, priority_count: int = 2) -> InsightOutput:
    return InsightOutput(
        version=INSIGHT_OUTPUT_VERSION,
        executive_summary="Validated summary.",
        priority_insights=tuple(
            PriorityInsight(
                scope={"sku": f"SKU-{position}"},
                observation="Validated observation.",
                evidence_codes=("LOW_CVR",),
                possible_explanations=(),
                recommended_checks=(),
                confidence="low",
            )
            for position in range(priority_count)
        ),
        overall_limitations=(),
    )


def build_receipt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_by: list[str] | tuple[str, ...] | None = ("sku",),
    usage: ProviderUsage | None = None,
    cost: CostAuditMetadata | None = None,
) -> InsightGenerationReceipt:
    monkeypatch.setattr(receipt_module, "_utc_now", lambda: FIXED_TIME)
    if cost is None:
        cost = build_cost_audit_metadata(
            usage,
            provider=DEEPSEEK_PROVIDER_NAME,
            model=DEEPSEEK_MODEL,
            pricing_reference_at=PRICING_REFERENCE_AT,
        )
    return build_insight_generation_receipt(
        analysis_signature="a" * 64,
        group_by=group_by,
        context=sample_context(),
        output=sample_output(),
        usage=usage,
        cost=cost,
    )


def receipt_kwargs() -> dict[str, object]:
    return {
        "version": INSIGHT_RECEIPT_VERSION,
        "generated_at": FIXED_TIME.isoformat(),
        "analysis_signature": "a" * 64,
        "group_by": ("sku",),
        "context_version": INSIGHT_CONTEXT_VERSION,
        "prompt_version": INSIGHT_PROMPT_VERSION,
        "output_version": INSIGHT_OUTPUT_VERSION,
        "provider": DEEPSEEK_PROVIDER_NAME,
        "model": DEEPSEEK_MODEL,
        "metric_record_count": 12,
        "diagnostic_signal_count": 11,
        "priority_insight_count": 2,
        "usage": None,
        "cost": build_cost_audit_metadata(
            None,
            provider=DEEPSEEK_PROVIDER_NAME,
            model=DEEPSEEK_MODEL,
            pricing_reference_at=PRICING_REFERENCE_AT,
        ),
    }


def test_builds_complete_immutable_receipt_from_existing_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = build_receipt(monkeypatch)

    assert receipt == InsightGenerationReceipt(**receipt_kwargs())
    assert receipt.version == INSIGHT_RECEIPT_VERSION == "3"
    assert receipt.context_version == INSIGHT_CONTEXT_VERSION
    assert receipt.prompt_version == INSIGHT_PROMPT_VERSION
    assert receipt.output_version == INSIGHT_OUTPUT_VERSION
    assert receipt.provider == DEEPSEEK_PROVIDER_NAME == "deepseek"
    assert receipt.model == DEEPSEEK_MODEL
    with pytest.raises(FrozenInstanceError):
        receipt.model = "changed"  # type: ignore[misc]


def test_generated_at_is_created_after_output_as_timezone_aware_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = build_receipt(monkeypatch)
    parsed = datetime.fromisoformat(receipt.generated_at)

    assert receipt.generated_at == "2026-08-28T06:30:12.123456+00:00"
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        (None, ()),
        ([], ()),
        ((), ()),
        (["sku"], ("sku",)),
        (
            ("marketplace", "country"),
            ("marketplace", "country"),
        ),
    ],
)
def test_group_by_is_normalized_to_immutable_tuple(
    group_by: list[str] | tuple[str, ...] | None,
    expected: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert build_receipt(monkeypatch, group_by=group_by).group_by == expected


def test_counts_come_only_from_context_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(receipt_module, "_utc_now", lambda: FIXED_TIME)
    context = sample_context()
    output = sample_output(priority_count=3)

    receipt = build_insight_generation_receipt(
        analysis_signature="b" * 64,
        group_by=["sku"],
        context=context,
        output=output,
        cost=build_cost_audit_metadata(
            None,
            provider=DEEPSEEK_PROVIDER_NAME,
            model=DEEPSEEK_MODEL,
            pricing_reference_at=PRICING_REFERENCE_AT,
        ),
    )

    assert receipt.metric_record_count == len(context.metric_records) == 12
    assert (
        receipt.diagnostic_signal_count
        == len(context.diagnostic_signals)
        == 11
    )
    assert receipt.priority_insight_count == len(output.priority_insights) == 3


def test_to_dict_is_explicit_json_safe_and_returns_fresh_group_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = build_receipt(monkeypatch)
    first = receipt.to_dict()
    second = receipt.to_dict()

    assert set(first) == {
        "version",
        "generated_at",
        "analysis_signature",
        "group_by",
        "context_version",
        "prompt_version",
        "output_version",
        "provider",
        "model",
        "metric_record_count",
        "diagnostic_signal_count",
        "priority_insight_count",
        "usage",
        "cost",
    }
    assert len(first) == 14
    assert first["group_by"] == ["sku"]
    assert first["group_by"] is not second["group_by"]
    first["group_by"].append("country")  # type: ignore[union-attr]
    assert receipt.group_by == ("sku",)
    assert first["usage"] is None
    assert first["cost"]["status"] == UNAVAILABLE
    json.dumps(receipt.to_dict(), ensure_ascii=False, allow_nan=False)


def test_usage_is_nested_immutable_and_serialized_with_fixed_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = ProviderUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=100,
        reasoning_tokens=0,
    )
    receipt = build_receipt(monkeypatch, usage=usage)

    assert receipt.usage is usage
    assert receipt.to_dict()["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 100,
        "reasoning_tokens": 0,
    }
    with pytest.raises(FrozenInstanceError):
        receipt.usage.prompt_tokens = 999  # type: ignore[misc,union-attr]


def test_available_cost_is_required_nested_provenance_with_decimal_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = ProviderUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=400,
    )
    receipt = build_receipt(monkeypatch, usage=usage)
    payload = receipt.to_dict()

    assert receipt.cost.status == AVAILABLE
    assert receipt.cost.estimate is not None
    assert payload["cost"]["status"] == AVAILABLE
    estimate = payload["cost"]["estimate"]
    assert isinstance(estimate, dict)
    assert estimate["prompt_cache_hit_cost"] == "0.0000084"
    assert estimate["prompt_cache_miss_cost"] == "0.000176"
    assert estimate["completion_cost"] == "0.000264"
    assert estimate["total_estimated_cost"] == "0.0004484"
    assert isinstance(payload["usage"]["prompt_tokens"], int)


def test_unavailable_cost_is_explicit_when_usage_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = build_receipt(monkeypatch, usage=None)

    assert receipt.cost.status == UNAVAILABLE
    assert receipt.cost.estimate is None
    assert receipt.cost.unavailable_reason == USAGE_UNAVAILABLE
    assert receipt.to_dict()["cost"]["unavailable_reason"] == (
        USAGE_UNAVAILABLE
    )


def test_to_dict_returns_fresh_nested_cost_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = ProviderUsage(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        prompt_cache_hit_tokens=1,
        prompt_cache_miss_tokens=0,
    )
    receipt = build_receipt(monkeypatch, usage=usage)
    first = receipt.to_dict()
    second = receipt.to_dict()

    assert first["cost"] is not second["cost"]
    assert first["cost"]["estimate"] is not second["cost"]["estimate"]
    first["cost"]["estimate"]["total_estimated_cost"] = "999"
    assert second["cost"]["estimate"]["total_estimated_cost"] != "999"


@pytest.mark.parametrize("cost", [None, {}, object()])
def test_receipt_rejects_missing_or_wrong_cost_type(cost: object) -> None:
    values = receipt_kwargs()
    values["cost"] = cost

    with pytest.raises(InsightReceiptError) as captured:
        InsightGenerationReceipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT


def test_builder_rejects_wrong_cost_type() -> None:
    with pytest.raises(InsightReceiptError) as captured:
        build_insight_generation_receipt(
            analysis_signature="a" * 64,
            group_by=["sku"],
            context=sample_context(),
            output=sample_output(),
            usage=None,
            cost={},  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_RECEIPT_INPUT


def test_available_cost_rejects_missing_usage_or_cache_breakdown() -> None:
    complete = ProviderUsage(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        prompt_cache_hit_tokens=4,
        prompt_cache_miss_tokens=6,
    )
    available = build_cost_audit_metadata(
        complete,
        provider=DEEPSEEK_PROVIDER_NAME,
        model=DEEPSEEK_MODEL,
        pricing_reference_at=PRICING_REFERENCE_AT,
    )
    for invalid_usage in (
        None,
        ProviderUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    ):
        values = receipt_kwargs()
        values["usage"] = invalid_usage
        values["cost"] = available
        with pytest.raises(InsightReceiptError) as captured:
            InsightGenerationReceipt(**values)  # type: ignore[arg-type]
        assert captured.value.code == INVALID_RECEIPT_INPUT


def test_available_cost_provider_model_must_match_receipt() -> None:
    usage = ProviderUsage(
        prompt_tokens=1,
        completion_tokens=0,
        total_tokens=1,
        prompt_cache_hit_tokens=1,
        prompt_cache_miss_tokens=0,
    )
    audit = build_cost_audit_metadata(
        usage,
        provider=DEEPSEEK_PROVIDER_NAME,
        model=DEEPSEEK_MODEL,
        pricing_reference_at=PRICING_REFERENCE_AT,
    )
    assert audit.estimate is not None
    mismatched_estimate = replace(audit.estimate, provider="other")
    mismatched_audit = replace(audit, estimate=mismatched_estimate)
    values = receipt_kwargs()
    values["usage"] = usage
    values["cost"] = mismatched_audit

    with pytest.raises(InsightReceiptError) as captured:
        InsightGenerationReceipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT


def test_to_dict_returns_fresh_usage_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = build_receipt(
        monkeypatch,
        usage=ProviderUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
    )
    first = receipt.to_dict()
    first_usage = first["usage"]
    assert isinstance(first_usage, dict)
    first_usage["prompt_tokens"] = 999

    second_usage = receipt.to_dict()["usage"]
    assert isinstance(second_usage, dict)
    assert second_usage["prompt_tokens"] == 2
    assert receipt.usage is not None
    assert receipt.usage.prompt_tokens == 2


@pytest.mark.parametrize(
    "usage",
    [{}, [], object()],
    ids=["dict", "list", "sdk-like-object"],
)
def test_wrong_usage_type_is_rejected(usage: object) -> None:
    values = receipt_kwargs()
    values["usage"] = usage

    with pytest.raises(InsightReceiptError) as captured:
        InsightGenerationReceipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT


def test_builder_rejects_wrong_usage_type() -> None:
    with pytest.raises(InsightReceiptError) as captured:
        build_insight_generation_receipt(
            analysis_signature="a" * 64,
            group_by=["sku"],
            context=sample_context(),
            output=sample_output(),
            usage={},  # type: ignore[arg-type]
            cost=receipt_kwargs()["cost"],  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_RECEIPT_INPUT


@pytest.mark.parametrize(
    "usage",
    [
        ProviderUsage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=0,
            reasoning_tokens=0,
        ),
        ProviderUsage(
            prompt_tokens=10**100,
            completion_tokens=10**100,
            total_tokens=2 * 10**100,
        ),
    ],
    ids=["zero", "huge-python-integers"],
)
def test_zero_and_huge_usage_json_roundtrip(
    usage: ProviderUsage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = build_receipt(monkeypatch, usage=usage).to_dict()
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )

    assert json.loads(encoded) == payload


def test_provider_usage_keeps_arbitrary_precision_integer_contract() -> None:
    huge = 10**5000

    usage = ProviderUsage(
        prompt_tokens=huge,
        completion_tokens=0,
        total_tokens=huge,
    )

    assert usage.prompt_tokens == huge
    assert usage.total_tokens == huge


def test_receipt_accepts_512_digit_usage_and_strict_json_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = 10**MAX_RECEIPT_TOKEN_DECIMAL_DIGITS - 1
    usage = ProviderUsage(
        prompt_tokens=accepted,
        completion_tokens=0,
        total_tokens=accepted,
    )

    payload = build_receipt(monkeypatch, usage=usage).to_dict()
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )

    assert MAX_RECEIPT_TOKEN_DECIMAL_DIGITS == 512
    assert json.loads(encoded)["usage"]["prompt_tokens"] == accepted


@pytest.mark.parametrize(
    "field_name",
    [
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "reasoning_tokens",
    ],
)
def test_receipt_rejects_513_digits_in_every_persisted_usage_field(
    field_name: str,
) -> None:
    usage = ProviderUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    rejected = 10**MAX_RECEIPT_TOKEN_DECIMAL_DIGITS
    # Bypass the frozen Provider object only to prove Receipt checks every field;
    # semantic token arithmetic remains solely ProviderUsage's responsibility.
    object.__setattr__(usage, field_name, rejected)
    values = receipt_kwargs()
    values["usage"] = usage

    with pytest.raises(InsightReceiptError) as captured:
        InsightGenerationReceipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT
    assert captured.value.message == (
        "usage 包含超出 Receipt 可表示范围的 token count。"
    )


def test_receipt_rejects_provider_valid_5001_digit_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge = 10**5000
    usage = ProviderUsage(
        prompt_tokens=huge,
        completion_tokens=0,
        total_tokens=huge,
    )
    with pytest.raises(InsightReceiptError) as captured:
        build_receipt(monkeypatch, usage=usage)

    assert captured.value.code == INVALID_RECEIPT_INPUT
    assert captured.value.message == (
        "usage 包含超出 Receipt 可表示范围的 token count。"
    )


@pytest.mark.parametrize("signature", [None, 1, "", "   "])
def test_invalid_analysis_signature_is_rejected(signature: object) -> None:
    with pytest.raises(InsightReceiptError) as captured:
        build_insight_generation_receipt(
            analysis_signature=signature,  # type: ignore[arg-type]
            group_by=["sku"],
            context=sample_context(),
            output=sample_output(),
            cost=receipt_kwargs()["cost"],  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_RECEIPT_INPUT


@pytest.mark.parametrize(
    "group_by",
    ["sku", b"sku", {"sku"}, [""], ["   "], [None], [1]],
)
def test_invalid_group_by_is_rejected(group_by: object) -> None:
    with pytest.raises(InsightReceiptError) as captured:
        build_insight_generation_receipt(
            analysis_signature="a" * 64,
            group_by=group_by,  # type: ignore[arg-type]
            context=sample_context(),
            output=sample_output(),
            cost=receipt_kwargs()["cost"],  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_RECEIPT_INPUT


@pytest.mark.parametrize(
    "generated_at",
    ["", "not-a-date", "2026-08-28T06:30:12", "2026-08-28T14:30:12+08:00"],
)
def test_invalid_or_non_utc_timestamp_is_rejected(generated_at: str) -> None:
    values = receipt_kwargs()
    values["generated_at"] = generated_at

    with pytest.raises(InsightReceiptError) as captured:
        InsightGenerationReceipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT


@pytest.mark.parametrize(
    "field_name",
    ["metric_record_count", "diagnostic_signal_count", "priority_insight_count"],
)
@pytest.mark.parametrize("invalid_count", [True, False, -1, 1.0, "1"])
def test_bool_negative_and_non_integer_counts_are_rejected(
    field_name: str,
    invalid_count: object,
) -> None:
    values = receipt_kwargs()
    values[field_name] = invalid_count

    with pytest.raises(InsightReceiptError) as captured:
        InsightGenerationReceipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("version", "future"),
        ("context_version", "future"),
        ("prompt_version", "future"),
        ("output_version", "future"),
        ("provider", "other"),
        ("model", "other"),
    ],
)
def test_contract_identity_fields_cannot_be_overridden(
    field_name: str,
    invalid_value: str,
) -> None:
    values = receipt_kwargs()
    values[field_name] = invalid_value

    with pytest.raises(InsightReceiptError) as captured:
        InsightGenerationReceipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT


@pytest.mark.parametrize("field_name", ["context", "output"])
def test_builder_rejects_wrong_context_or_output_type(field_name: str) -> None:
    values = {
        "analysis_signature": "a" * 64,
        "group_by": ["sku"],
        "context": sample_context(),
        "output": sample_output(),
        "cost": receipt_kwargs()["cost"],
    }
    values[field_name] = object()

    with pytest.raises(InsightReceiptError) as captured:
        build_insight_generation_receipt(**values)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RECEIPT_INPUT
