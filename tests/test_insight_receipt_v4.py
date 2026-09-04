from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import sys

import pytest

from src.deepseek_provider import DEEPSEEK_MODEL
from src.insight_attempt_audit import (
    ATTEMPT_AUDIT_VERSION,
    FAILED,
    SUCCEEDED,
    AttemptAuditTrail,
    build_failed_attempt_audit,
    build_succeeded_attempt_audit,
)
from src.insight_cost_audit import AVAILABLE, UNAVAILABLE, build_cost_audit_metadata
from src.insight_logical_generation_cost import (
    FINAL_ATTEMPT_COST_UNAVAILABLE,
    FULLY_ESTIMATED,
    LOGICAL_GENERATION_COST_SUMMARY_VERSION,
    PRIOR_FAILED_ATTEMPT_COST_UNKNOWN,
    UNAVAILABLE as LOGICAL_COST_UNAVAILABLE,
    UNKNOWN_TOTAL,
    LogicalGenerationCostSummary,
)
from src.insight_prompt import INSIGHT_OUTPUT_VERSION, InsightOutput
from src.insight_provider import PROVIDER_TIMEOUT, ProviderUsage
from src.insight_receipt import (
    DEEPSEEK_PROVIDER_NAME,
    INSIGHT_RECEIPT_VERSION,
    InsightGenerationReceipt,
)
from src.insight_receipt_v4 import (
    INSIGHT_RECEIPT_V4_VERSION,
    INVALID_RECEIPT_V4_INPUT,
    MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS,
    InsightGenerationReceiptV4,
    InsightReceiptV4Error,
    build_insight_receipt_v4,
)
from src.insight_retry import RetryPolicy, evaluate_retry
from src.insight_retry_delay import (
    DEFAULT_RETRY_DELAY_POLICY,
    RetryDelayDecision,
    resolve_retry_delay,
)
from src.insight_retry_delay_execution import (
    RETRY_DELAY_EXECUTION_VERSION,
    RetryDelayExecutionAudit,
    RetryDelayExecutionRecord,
)
from src.insight_retry_execution import (
    RETRY_EXECUTION_VERSION,
    RetryExecutionResult,
)
from src.insights import build_insight_context
from src.pipeline import run_pipeline


ROOT = Path(__file__).parents[1]
SAMPLE_PATH = ROOT / "data" / "sample_ecommerce_data.csv"
GENERATED_AT = "2026-08-28T06:31:00+00:00"
REFERENCE = datetime(2026, 8, 28, 6, 30, tzinfo=timezone.utc)


def context():
    return build_insight_context(run_pipeline(SAMPLE_PATH, group_by="sku"))


def output() -> InsightOutput:
    return InsightOutput(
        version=INSIGHT_OUTPUT_VERSION,
        executive_summary="Validated summary.",
        priority_insights=(),
        overall_limitations=(),
    )


def complete_usage() -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=400,
    )


def succeeded_execution(
    *,
    attempt_count: int = 1,
    cost_available: bool = True,
) -> RetryExecutionResult:
    policy = RetryPolicy(
        version=f"receipt-v4-{attempt_count}",
        max_attempts=max(2, attempt_count),
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    attempts = []
    delay_records = []
    for attempt_number in range(1, attempt_count):
        decision = evaluate_retry(
            error_code=PROVIDER_TIMEOUT,
            attempts_completed=attempt_number,
            policy=policy,
        )
        attempts.append(
            build_failed_attempt_audit(
                attempt_number=attempt_number,
                provider=DEEPSEEK_PROVIDER_NAME,
                model=DEEPSEEK_MODEL,
                pricing_reference_at=REFERENCE,
                error_code=PROVIDER_TIMEOUT,
                retry_decision=decision,
            )
        )
        delay_decision = resolve_retry_delay(
            retry_decision=decision,
            policy=DEFAULT_RETRY_DELAY_POLICY,
        )
        delay_records.append(
            RetryDelayExecutionRecord(
                version=RETRY_DELAY_EXECUTION_VERSION,
                after_attempt_number=attempt_number,
                delay_decision=delay_decision,
            )
        )

    final_usage = complete_usage() if cost_available else None
    final_cost = build_cost_audit_metadata(
        final_usage,
        provider=DEEPSEEK_PROVIDER_NAME,
        model=DEEPSEEK_MODEL,
        pricing_reference_at=REFERENCE,
    )
    attempts.append(
        build_succeeded_attempt_audit(
            attempt_number=attempt_count,
            provider=DEEPSEEK_PROVIDER_NAME,
            model=DEEPSEEK_MODEL,
            pricing_reference_at=REFERENCE,
            usage=final_usage,
            cost=final_cost,
        )
    )
    attempt_audit = AttemptAuditTrail(
        version=ATTEMPT_AUDIT_VERSION,
        retry_policy_version=policy.version,
        max_attempts=policy.max_attempts,
        outcome=SUCCEEDED,
        attempts=tuple(attempts),
    )
    delay_audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version=DEFAULT_RETRY_DELAY_POLICY.version,
        records=tuple(delay_records),
    )
    return RetryExecutionResult(
        version=RETRY_EXECUTION_VERSION,
        status=SUCCEEDED,
        output=output(),
        final_usage=final_usage,
        final_cost=final_cost,
        attempt_audit=attempt_audit,
        delay_audit=delay_audit,
        error_code=None,
    )


def failed_execution() -> RetryExecutionResult:
    policy = RetryPolicy(
        version="receipt-v4-failed",
        max_attempts=1,
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    decision = evaluate_retry(
        error_code=PROVIDER_TIMEOUT,
        attempts_completed=1,
        policy=policy,
    )
    attempt = build_failed_attempt_audit(
        attempt_number=1,
        provider=DEEPSEEK_PROVIDER_NAME,
        model=DEEPSEEK_MODEL,
        pricing_reference_at=REFERENCE,
        error_code=PROVIDER_TIMEOUT,
        retry_decision=decision,
    )
    return RetryExecutionResult(
        version=RETRY_EXECUTION_VERSION,
        status=FAILED,
        output=None,
        final_usage=None,
        final_cost=None,
        attempt_audit=AttemptAuditTrail(
            version=ATTEMPT_AUDIT_VERSION,
            retry_policy_version=policy.version,
            max_attempts=1,
            outcome=FAILED,
            attempts=(attempt,),
        ),
        delay_audit=RetryDelayExecutionAudit(
            version=RETRY_DELAY_EXECUTION_VERSION,
            policy_version=DEFAULT_RETRY_DELAY_POLICY.version,
            records=(),
        ),
        error_code=PROVIDER_TIMEOUT,
    )


def build_receipt(
    *,
    attempt_count: int = 1,
    cost_available: bool = True,
) -> tuple[InsightGenerationReceiptV4, RetryExecutionResult]:
    execution = succeeded_execution(
        attempt_count=attempt_count,
        cost_available=cost_available,
    )
    receipt = build_insight_receipt_v4(
        generated_at=GENERATED_AT,
        analysis_signature="a" * 64,
        group_by=["sku"],
        context=context(),
        execution_result=execution,
    )
    return receipt, execution


def test_v4_has_exact_17_field_schema_and_is_frozen() -> None:
    receipt, execution = build_receipt()
    payload = receipt.to_dict()

    assert receipt.version == INSIGHT_RECEIPT_V4_VERSION == "4"
    assert [item.name for item in fields(receipt)] == [
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
        "cost",
        "usage",
        "attempt_audit",
        "delay_audit",
        "logical_generation_cost",
    ]
    assert list(payload) == [
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
        "attempt_audit",
        "delay_audit",
        "logical_generation_cost",
    ]
    assert len(payload) == 17
    assert receipt.attempt_audit is execution.attempt_audit
    assert receipt.delay_audit is execution.delay_audit
    with pytest.raises(FrozenInstanceError):
        receipt.version = "5"  # type: ignore[misc]


def test_v4_preserves_v3_base_field_serialization_semantics() -> None:
    receipt, _ = build_receipt()
    v4_payload = receipt.to_dict()
    v3 = InsightGenerationReceipt(
        version=INSIGHT_RECEIPT_VERSION,
        generated_at=receipt.generated_at,
        analysis_signature=receipt.analysis_signature,
        group_by=receipt.group_by,
        context_version=receipt.context_version,
        prompt_version=receipt.prompt_version,
        output_version=receipt.output_version,
        provider=receipt.provider,
        model=receipt.model,
        metric_record_count=receipt.metric_record_count,
        diagnostic_signal_count=receipt.diagnostic_signal_count,
        priority_insight_count=receipt.priority_insight_count,
        cost=receipt.cost,
        usage=receipt.usage,
    )
    v3_payload = v3.to_dict()

    assert INSIGHT_RECEIPT_VERSION == "3"
    for key in v3_payload:
        if key == "version":
            assert v4_payload[key] == "4"
        else:
            assert v4_payload[key] == v3_payload[key]
            assert type(v4_payload[key]) is type(v3_payload[key])


def test_builder_has_no_caller_override_for_execution_provenance() -> None:
    parameters = inspect.signature(build_insight_receipt_v4).parameters

    assert set(parameters) == {
        "generated_at",
        "analysis_signature",
        "group_by",
        "context",
        "execution_result",
    }
    assert not {
        "provider",
        "provider_name",
        "model",
        "usage",
        "cost",
        "attempt_audit",
        "delay_audit",
    }.intersection(parameters)


def test_single_available_cost_is_final_attempt_and_fully_estimated() -> None:
    receipt, execution = build_receipt(cost_available=True)

    assert receipt.provider == execution.attempt_audit.attempts[-1].provider
    assert receipt.model == execution.attempt_audit.attempts[-1].model
    assert receipt.usage is execution.final_usage
    assert receipt.cost is execution.final_cost
    assert receipt.cost.status == AVAILABLE
    assert receipt.logical_generation_cost.status == FULLY_ESTIMATED
    assert receipt.cost.estimate is not None
    assert (
        receipt.logical_generation_cost.estimated_total_cost_usd
        == receipt.cost.estimate.total_estimated_cost
    )


def test_single_unavailable_cost_has_high_level_unavailable_reason() -> None:
    receipt, execution = build_receipt(cost_available=False)

    assert receipt.cost is execution.final_cost
    assert receipt.cost.status == UNAVAILABLE
    assert receipt.logical_generation_cost == LogicalGenerationCostSummary(
        version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        status=LOGICAL_COST_UNAVAILABLE,
        estimated_total_cost_usd=None,
        reason=FINAL_ATTEMPT_COST_UNAVAILABLE,
    )


@pytest.mark.parametrize("cost_available", [True, False])
@pytest.mark.parametrize("attempt_count", [2, 3])
def test_prior_failure_dominates_final_cost_availability(
    attempt_count: int,
    cost_available: bool,
) -> None:
    receipt, execution = build_receipt(
        attempt_count=attempt_count,
        cost_available=cost_available,
    )

    assert len(receipt.attempt_audit.attempts) == attempt_count
    assert len(receipt.delay_audit.records) == attempt_count - 1
    assert receipt.cost is execution.final_cost
    assert receipt.cost.status == (AVAILABLE if cost_available else UNAVAILABLE)
    assert receipt.logical_generation_cost == LogicalGenerationCostSummary(
        version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        status=UNKNOWN_TOTAL,
        estimated_total_cost_usd=None,
        reason=PRIOR_FAILED_ATTEMPT_COST_UNKNOWN,
    )


def test_failed_execution_cannot_build_a_v4_receipt() -> None:
    with pytest.raises(InsightReceiptV4Error) as captured:
        build_insight_receipt_v4(
            generated_at=GENERATED_AT,
            analysis_signature="a" * 64,
            group_by=["sku"],
            context=context(),
            execution_result=failed_execution(),
        )

    assert captured.value.code == INVALID_RECEIPT_V4_INPUT


def test_delay_audit_serialization_uses_explicit_stable_shape() -> None:
    receipt, _ = build_receipt(attempt_count=2)

    assert receipt.to_dict()["delay_audit"] == {
        "version": "1",
        "policy_version": "1",
        "records": [
            {
                "version": "1",
                "after_attempt_number": 1,
                "delay_decision": {
                    "policy_version": "1",
                    "error_code": PROVIDER_TIMEOUT,
                    "attempts_completed": 1,
                    "delay_ms": 1000,
                },
            }
        ],
    }


def test_attempt_audit_serialization_reuses_sealed_serializer() -> None:
    receipt, _ = build_receipt(attempt_count=2)

    assert receipt.to_dict()["attempt_audit"] == receipt.attempt_audit.to_dict()


def test_to_dict_is_strict_json_safe_and_deeply_fresh() -> None:
    receipt, _ = build_receipt(attempt_count=2)
    first = receipt.to_dict()
    second = receipt.to_dict()

    json.dumps(first, ensure_ascii=False, allow_nan=False)
    assert first is not second
    assert first["usage"] is not second["usage"]
    assert first["cost"] is not second["cost"]
    assert first["attempt_audit"] is not second["attempt_audit"]
    assert first["delay_audit"] is not second["delay_audit"]
    assert (
        first["logical_generation_cost"]
        is not second["logical_generation_cost"]
    )
    first["attempt_audit"]["attempts"][0]["error_code"] = "CHANGED"
    first["delay_audit"]["records"][0]["delay_decision"]["delay_ms"] = 1
    first["usage"]["prompt_tokens"] = 0
    first["cost"]["estimate"]["total_estimated_cost"] = "0"
    first["logical_generation_cost"]["status"] = "changed"
    assert second == receipt.to_dict()


def test_direct_construction_rejects_cross_contract_contradictions() -> None:
    receipt, _ = build_receipt(attempt_count=2)
    single, _ = build_receipt(attempt_count=1)
    unavailable, _ = build_receipt(cost_available=False)
    contradictions = (
        {"provider": "other"},
        {"model": "other"},
        {"usage": None},
        {"cost": unavailable.cost},
        {
            "logical_generation_cost": LogicalGenerationCostSummary(
                version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
                status=FULLY_ESTIMATED,
                estimated_total_cost_usd=(
                    receipt.cost.estimate.total_estimated_cost
                    if receipt.cost.estimate is not None
                    else None
                ),
                reason=None,
            )
        },
        {"attempt_audit": single.attempt_audit},
        {"delay_audit": single.delay_audit},
    )

    for changes in contradictions:
        with pytest.raises(InsightReceiptV4Error):
            replace(receipt, **changes)


def test_direct_construction_rejects_single_attempt_summary_lies() -> None:
    available, _ = build_receipt()
    unavailable, _ = build_receipt(cost_available=False)
    false_unknown = LogicalGenerationCostSummary(
        version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        status=UNKNOWN_TOTAL,
        estimated_total_cost_usd=None,
        reason=PRIOR_FAILED_ATTEMPT_COST_UNKNOWN,
    )
    false_unavailable = LogicalGenerationCostSummary(
        version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        status=LOGICAL_COST_UNAVAILABLE,
        estimated_total_cost_usd=None,
        reason=FINAL_ATTEMPT_COST_UNAVAILABLE,
    )

    with pytest.raises(InsightReceiptV4Error):
        replace(available, logical_generation_cost=false_unknown)
    with pytest.raises(InsightReceiptV4Error):
        replace(available, logical_generation_cost=false_unavailable)
    with pytest.raises(InsightReceiptV4Error):
        replace(unavailable, logical_generation_cost=false_unknown)


def execution_with_delay(delay_ms: int) -> RetryExecutionResult:
    result = succeeded_execution(attempt_count=2, cost_available=True)
    original = result.delay_audit.records[0]
    decision = RetryDelayDecision(
        policy_version=original.delay_decision.policy_version,
        error_code=original.delay_decision.error_code,
        attempts_completed=1,
        delay_ms=delay_ms,
    )
    audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version=result.delay_audit.policy_version,
        records=(
            RetryDelayExecutionRecord(
                version=RETRY_DELAY_EXECUTION_VERSION,
                after_attempt_number=1,
                delay_decision=decision,
            ),
        ),
    )
    return replace(result, delay_audit=audit)


def test_512_digit_delay_is_accepted_and_remains_json_integer() -> None:
    accepted = 10**MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS - 1
    execution = execution_with_delay(accepted)

    receipt = build_insight_receipt_v4(
        generated_at=GENERATED_AT,
        analysis_signature="a" * 64,
        group_by=["sku"],
        context=context(),
        execution_result=execution,
    )
    payload = receipt.to_dict()

    value = payload["delay_audit"]["records"][0]["delay_decision"]["delay_ms"]
    assert MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS == 512
    assert value == accepted
    assert isinstance(value, int)
    json.dumps(payload, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    "field_name",
    [
        "metric_record_count",
        "diagnostic_signal_count",
        "priority_insight_count",
    ],
)
def test_512_digit_top_level_count_remains_strict_json_integer(
    field_name: str,
) -> None:
    receipt, _ = build_receipt()
    accepted = 10**MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS - 1

    reconstructed = replace(receipt, **{field_name: accepted})
    payload = reconstructed.to_dict()

    assert payload[field_name] == accepted
    assert isinstance(payload[field_name], int)
    json.dumps(payload, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    "field_name",
    [
        "metric_record_count",
        "diagnostic_signal_count",
        "priority_insight_count",
    ],
)
def test_513_digit_top_level_count_is_rejected_by_direct_construction(
    field_name: str,
) -> None:
    receipt, _ = build_receipt()
    values = {
        item.name: getattr(receipt, item.name)
        for item in fields(receipt)
    }
    values[field_name] = 10**MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS

    with pytest.raises(InsightReceiptV4Error) as captured:
        InsightGenerationReceiptV4(**values)

    assert captured.value.code == INVALID_RECEIPT_V4_INPUT
    assert captured.value.message == (
        "Receipt V4 top-level count exceeds the JSON integer boundary."
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "metric_record_count",
        "diagnostic_signal_count",
        "priority_insight_count",
    ],
)
def test_extreme_top_level_count_is_rejected_by_replace_without_disclosure(
    field_name: str,
) -> None:
    receipt, _ = build_receipt()
    original_limit = sys.get_int_max_str_digits()
    rejected = 10**5000

    with pytest.raises(InsightReceiptV4Error) as captured:
        replace(receipt, **{field_name: rejected})

    assert captured.value.code == INVALID_RECEIPT_V4_INPUT
    assert captured.value.message == (
        "Receipt V4 top-level count exceeds the JSON integer boundary."
    )
    assert field_name not in str(captured.value)
    assert sys.get_int_max_str_digits() == original_limit


def test_5001_digit_delay_is_rejected_during_receipt_construction() -> None:
    original_limit = sys.get_int_max_str_digits()
    execution = execution_with_delay(10**5000)

    with pytest.raises(InsightReceiptV4Error) as captured:
        build_insight_receipt_v4(
            generated_at=GENERATED_AT,
            analysis_signature="a" * 64,
            group_by=["sku"],
            context=context(),
            execution_result=execution,
        )

    assert captured.value.code == INVALID_RECEIPT_V4_INPUT
    assert "outside the Receipt V4 JSON boundary" in captured.value.message
    assert sys.get_int_max_str_digits() == original_limit


@pytest.mark.parametrize(
    "field_name",
    ["after_attempt_number", "attempts_completed", "delay_ms"],
)
def test_513_digit_value_is_rejected_for_every_delay_json_integer(
    field_name: str,
) -> None:
    execution = succeeded_execution(attempt_count=2, cost_available=True)
    record = execution.delay_audit.records[0]
    decision = RetryDelayDecision(
        policy_version=record.delay_decision.policy_version,
        error_code=record.delay_decision.error_code,
        attempts_completed=1,
        delay_ms=1_000,
    )
    replacement_record = RetryDelayExecutionRecord(
        version=RETRY_DELAY_EXECUTION_VERSION,
        after_attempt_number=1,
        delay_decision=decision,
    )
    audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version=execution.delay_audit.policy_version,
        records=(replacement_record,),
    )
    rejected = 10**MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS
    if field_name == "after_attempt_number":
        object.__setattr__(replacement_record, field_name, rejected)
    else:
        object.__setattr__(decision, field_name, rejected)
    object.__setattr__(execution, "delay_audit", audit)

    with pytest.raises(InsightReceiptV4Error) as captured:
        build_insight_receipt_v4(
            generated_at=GENERATED_AT,
            analysis_signature="a" * 64,
            group_by=["sku"],
            context=context(),
            execution_result=execution,
        )

    assert captured.value.code == INVALID_RECEIPT_V4_INPUT
    assert "outside the Receipt V4 JSON boundary" in captured.value.message


def test_invalid_generated_at_and_group_by_use_v4_boundary() -> None:
    execution = succeeded_execution()
    cases = (
        {"generated_at": "not-a-date", "group_by": ["sku"]},
        {"generated_at": GENERATED_AT, "group_by": "sku"},
    )
    for case in cases:
        with pytest.raises(InsightReceiptV4Error) as captured:
            build_insight_receipt_v4(
                analysis_signature="a" * 64,
                context=context(),
                execution_result=execution,
                **case,  # type: ignore[arg-type]
            )
        assert captured.value.code == INVALID_RECEIPT_V4_INPUT


def test_v4_has_no_asdict_clock_network_pricing_or_output_content() -> None:
    source = (ROOT / "src" / "insight_receipt_v4.py").read_text(
        encoding="utf-8"
    )
    receipt, _ = build_receipt(attempt_count=2)
    payload = receipt.to_dict()

    def keys_in(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(
                *(keys_in(item) for item in value.values())
            )
        if isinstance(value, list):
            return set().union(*(keys_in(item) for item in value))
        return set()

    assert "asdict" not in source
    assert "datetime.now" not in source
    assert "build_cost_audit_metadata" not in source
    assert "estimate_generation_cost" not in source
    assert "sys.set_int_max_str_digits" not in source
    assert "str(value)" not in source
    assert "requests" not in source
    assert "httpx" not in source
    assert "openai" not in source
    assert keys_in(payload).isdisjoint(
        {
        "executive_summary",
        "priority_insights",
        "prompt",
        "raw_prompt",
        "raw_response",
        "api_key",
        "http_body",
        "exception",
        }
    )


def test_app_uses_v4_only_after_retry_execution_succeeds() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "from src.insight_receipt_v4 import" in app_source
    assert "build_insight_receipt_v4(" in app_source
    assert "execute_insight_generation_with_retry(" in app_source
    assert "build_insight_generation_receipt(" not in app_source
    assert INSIGHT_RECEIPT_VERSION == "3"
