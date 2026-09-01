from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

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
from src.insight_cost_audit import build_cost_audit_metadata
from src.insight_logical_generation_cost import (
    FINAL_ATTEMPT_COST_UNAVAILABLE,
    FULLY_ESTIMATED,
    INVALID_LOGICAL_GENERATION_COST,
    LOGICAL_GENERATION_COST_SUMMARY_VERSION,
    PRIOR_FAILED_ATTEMPT_COST_UNKNOWN,
    UNAVAILABLE,
    UNKNOWN_TOTAL,
    LogicalGenerationCostError,
    LogicalGenerationCostSummary,
    build_logical_generation_cost_summary,
)
from src.insight_prompt import INSIGHT_OUTPUT_VERSION, InsightOutput
from src.insight_provider import PROVIDER_TIMEOUT, ProviderUsage
from src.insight_receipt import DEEPSEEK_PROVIDER_NAME
from src.insight_retry import RetryPolicy, evaluate_retry
from src.insight_retry_delay import (
    DEFAULT_RETRY_DELAY_POLICY,
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


REFERENCE = datetime(2026, 8, 28, 6, 30, tzinfo=timezone.utc)


def usage() -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=400,
    )


def output() -> InsightOutput:
    return InsightOutput(
        version=INSIGHT_OUTPUT_VERSION,
        executive_summary="Validated summary.",
        priority_insights=(),
        overall_limitations=(),
    )


def succeeded_execution(
    *,
    attempt_count: int,
    cost_available: bool,
) -> RetryExecutionResult:
    policy = RetryPolicy(
        version=f"test-{attempt_count}",
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

    final_usage = usage() if cost_available else None
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
    trail = AttemptAuditTrail(
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
        attempt_audit=trail,
        delay_audit=delay_audit,
        error_code=None,
    )


def failed_execution() -> RetryExecutionResult:
    policy = RetryPolicy(
        version="one-attempt",
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
            max_attempts=policy.max_attempts,
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


def test_single_available_attempt_is_fully_estimated_and_exact() -> None:
    result = succeeded_execution(attempt_count=1, cost_available=True)

    summary = build_logical_generation_cost_summary(result)

    assert summary.version == LOGICAL_GENERATION_COST_SUMMARY_VERSION == "1"
    assert summary.status == FULLY_ESTIMATED
    assert result.final_cost is not None
    assert result.final_cost.estimate is not None
    assert (
        summary.estimated_total_cost_usd
        == result.final_cost.estimate.total_estimated_cost
    )
    assert summary.reason is None
    assert summary.to_dict()["estimated_total_cost_usd"] == "0.0004484"


def test_single_unavailable_attempt_is_unavailable() -> None:
    result = succeeded_execution(attempt_count=1, cost_available=False)

    summary = build_logical_generation_cost_summary(result)

    assert summary == LogicalGenerationCostSummary(
        version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        status=UNAVAILABLE,
        estimated_total_cost_usd=None,
        reason=FINAL_ATTEMPT_COST_UNAVAILABLE,
    )


@pytest.mark.parametrize("cost_available", [True, False])
@pytest.mark.parametrize("attempt_count", [2, 3])
def test_any_prior_failed_attempt_makes_total_unknown(
    attempt_count: int,
    cost_available: bool,
) -> None:
    result = succeeded_execution(
        attempt_count=attempt_count,
        cost_available=cost_available,
    )

    summary = build_logical_generation_cost_summary(result)

    assert summary == LogicalGenerationCostSummary(
        version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        status=UNKNOWN_TOTAL,
        estimated_total_cost_usd=None,
        reason=PRIOR_FAILED_ATTEMPT_COST_UNKNOWN,
    )


def test_failed_execution_and_wrong_type_are_rejected() -> None:
    for value in (failed_execution(), object(), None):
        with pytest.raises(LogicalGenerationCostError) as captured:
            build_logical_generation_cost_summary(value)  # type: ignore[arg-type]
        assert captured.value.code == INVALID_LOGICAL_GENERATION_COST


@pytest.mark.parametrize(
    "amount",
    [True, 0.0, -1.0, Decimal("-1"), Decimal("NaN"), Decimal("Infinity")],
)
def test_fully_estimated_requires_finite_nonnegative_decimal(
    amount: object,
) -> None:
    with pytest.raises(LogicalGenerationCostError):
        LogicalGenerationCostSummary(
            version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
            status=FULLY_ESTIMATED,
            estimated_total_cost_usd=amount,  # type: ignore[arg-type]
            reason=None,
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"version": "2"},
        {"status": "partial"},
        {"status": FULLY_ESTIMATED, "reason": "reason"},
        {"status": UNKNOWN_TOTAL, "estimated_total_cost_usd": Decimal("0")},
        {"status": UNKNOWN_TOTAL, "reason": FINAL_ATTEMPT_COST_UNAVAILABLE},
        {"status": UNAVAILABLE, "estimated_total_cost_usd": Decimal("0")},
        {"status": UNAVAILABLE, "reason": PRIOR_FAILED_ATTEMPT_COST_UNKNOWN},
    ],
)
def test_direct_construction_rejects_status_field_contradictions(
    fields: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "version": LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        "status": FULLY_ESTIMATED,
        "estimated_total_cost_usd": Decimal("0"),
        "reason": None,
    }
    values.update(fields)

    with pytest.raises(LogicalGenerationCostError):
        LogicalGenerationCostSummary(**values)  # type: ignore[arg-type]


def test_summary_is_frozen_and_to_dict_is_fresh() -> None:
    summary = build_logical_generation_cost_summary(
        succeeded_execution(attempt_count=1, cost_available=True)
    )

    first = summary.to_dict()
    second = summary.to_dict()

    assert first == second
    assert first is not second
    first["status"] = "changed"
    assert second["status"] == FULLY_ESTIMATED
    with pytest.raises(FrozenInstanceError):
        summary.status = UNKNOWN_TOTAL  # type: ignore[misc]


def test_summary_module_has_no_time_network_pricing_or_provider_calls() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "insight_logical_generation_cost.py"
    ).read_text(encoding="utf-8")

    assert "datetime.now" not in source
    assert "build_cost_audit_metadata" not in source
    assert "estimate_generation_cost" not in source
    assert "requests" not in source
    assert "httpx" not in source
    assert "openai" not in source
