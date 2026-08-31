from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

import src.insight_attempt_audit as audit_module
from src.insight_attempt_audit import (
    ATTEMPT_AUDIT_VERSION,
    COST_AVAILABLE,
    COST_UNAVAILABLE,
    COST_UNKNOWN,
    FAILED,
    INVALID_ATTEMPT_AUDIT,
    MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS,
    SUCCEEDED,
    USAGE_RECORDED,
    USAGE_UNAVAILABLE,
    USAGE_UNKNOWN,
    AttemptAuditError,
    AttemptAuditTrail,
    ProviderAttemptAudit,
    build_failed_attempt_audit,
    build_succeeded_attempt_audit,
)
from src.insight_cost_audit import (
    AVAILABLE,
    COST_AUDIT_VERSION,
    UNAVAILABLE,
    CostAuditMetadata,
    build_cost_audit_metadata,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    COST_ESTIMATE_VERSION,
    OFF_PEAK,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    USAGE_UNAVAILABLE as COST_REASON_USAGE_UNAVAILABLE,
    GenerationCostEstimate,
)
from src.insight_provider import (
    PROVIDER_AUTH_FAILED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    ProviderUsage,
)
from src.insight_retry import (
    ATTEMPT_LIMIT_REACHED,
    DO_NOT_RETRY,
    ERROR_NOT_RETRYABLE,
    RETRY,
    RETRYABLE_TRANSIENT_ERROR,
    RetryDecision,
)


UTC = timezone.utc
REFERENCE_AT = datetime(2026, 8, 17, 1, 0, tzinfo=UTC)
PROVIDER = "deepseek"
MODEL = "deepseek-v4-flash"
POLICY_VERSION = "retry-policy-v1"
MAX_AUDIT_JSON_INTEGER = (
    10**MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS - 1
)
FIRST_UNREPRESENTABLE_AUDIT_INTEGER = (
    10**MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS
)


def complete_usage() -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=400,
        reasoning_tokens=100,
    )


def incomplete_cache_usage() -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
    )


def available_cost(
    *,
    reference_at: datetime = REFERENCE_AT,
) -> CostAuditMetadata:
    return build_cost_audit_metadata(
        complete_usage(),
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=reference_at,
    )


def usage_unavailable_cost(
    *,
    reference_at: datetime = REFERENCE_AT,
) -> CostAuditMetadata:
    return build_cost_audit_metadata(
        None,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=reference_at,
    )


def cache_unavailable_cost() -> CostAuditMetadata:
    return build_cost_audit_metadata(
        incomplete_cache_usage(),
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
    )


def policy_unavailable_cost(
    usage: ProviderUsage | None,
    *,
    provider: str = "provider-a",
    model: str = "model-a",
    reference_at: datetime = REFERENCE_AT,
) -> CostAuditMetadata:
    return build_cost_audit_metadata(
        usage,
        provider=provider,
        model=model,
        pricing_reference_at=reference_at,
    )


def policy_not_effective_cost(
    usage: ProviderUsage | None,
) -> CostAuditMetadata:
    return build_cost_audit_metadata(
        usage,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=datetime(
            2026,
            8,
            16,
            15,
            59,
            59,
            tzinfo=UTC,
        ),
    )


def synthetic_available_cost(
    *,
    provider: str,
    model: str,
    reference_at: datetime = REFERENCE_AT,
    amount: Decimal = Decimal("0.000000001"),
) -> CostAuditMetadata:
    estimate = GenerationCostEstimate(
        version=COST_ESTIMATE_VERSION,
        pricing_policy_version="synthetic-policy-v1",
        provider=provider,
        model=model,
        currency="USD",
        pricing_tier=OFF_PEAK,
        pricing_reference_at=reference_at.isoformat(),
        prompt_cache_hit_cost=amount,
        prompt_cache_miss_cost=Decimal("0"),
        completion_cost=Decimal("0"),
        total_estimated_cost=amount,
    )
    return CostAuditMetadata(
        version=COST_AUDIT_VERSION,
        status=AVAILABLE,
        pricing_policy_version="synthetic-policy-v1",
        pricing_reference_at=reference_at.isoformat(),
        estimate=estimate,
        unavailable_reason=None,
    )


def oversized_usage_for(field_name: str, value: int) -> ProviderUsage:
    if field_name == "prompt_tokens":
        return ProviderUsage(value, 0, value)
    if field_name == "completion_tokens":
        return ProviderUsage(0, value, value)
    if field_name == "total_tokens":
        half = value // 2
        return ProviderUsage(half, value - half, value)
    if field_name == "prompt_cache_hit_tokens":
        return ProviderUsage(value, 0, value, value, 0)
    if field_name == "prompt_cache_miss_tokens":
        return ProviderUsage(value, 0, value, 0, value)
    if field_name == "reasoning_tokens":
        return ProviderUsage(0, value, value, reasoning_tokens=value)
    raise AssertionError(f"Unsupported test field: {field_name}")


def retry_decision(
    *,
    attempt_number: int,
    max_attempts: int = 2,
    error_code: str = PROVIDER_TIMEOUT,
    policy_version: str = POLICY_VERSION,
) -> RetryDecision:
    return RetryDecision(
        policy_version=policy_version,
        error_code=error_code,
        action=RETRY,
        reason=RETRYABLE_TRANSIENT_ERROR,
        attempts_completed=attempt_number,
        max_attempts=max_attempts,
    )


def stop_decision(
    *,
    attempt_number: int,
    max_attempts: int = 2,
    error_code: str = PROVIDER_UNAVAILABLE,
    policy_version: str = POLICY_VERSION,
) -> RetryDecision:
    return RetryDecision(
        policy_version=policy_version,
        error_code=error_code,
        action=DO_NOT_RETRY,
        reason=(
            ATTEMPT_LIMIT_REACHED
            if attempt_number >= max_attempts
            else ERROR_NOT_RETRYABLE
        ),
        attempts_completed=attempt_number,
        max_attempts=max_attempts,
    )


def succeeded_attempt(
    attempt_number: int = 1,
    *,
    provider: str = PROVIDER,
    model: str = MODEL,
    reference_at: datetime = REFERENCE_AT,
    usage: ProviderUsage | None = None,
    cost: CostAuditMetadata | None = None,
) -> ProviderAttemptAudit:
    selected_usage = complete_usage() if usage is None and cost is None else usage
    selected_cost = (
        available_cost(reference_at=reference_at)
        if cost is None and selected_usage is not None
        else usage_unavailable_cost(reference_at=reference_at)
        if cost is None
        else cost
    )
    return build_succeeded_attempt_audit(
        attempt_number=attempt_number,
        provider=provider,
        model=model,
        pricing_reference_at=reference_at,
        usage=selected_usage,
        cost=selected_cost,
    )


def failed_attempt(
    attempt_number: int = 1,
    *,
    max_attempts: int = 2,
    error_code: str = PROVIDER_TIMEOUT,
    action: str = RETRY,
    provider: str = PROVIDER,
    model: str = MODEL,
    policy_version: str = POLICY_VERSION,
) -> ProviderAttemptAudit:
    decision = (
        retry_decision(
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            error_code=error_code,
            policy_version=policy_version,
        )
        if action == RETRY
        else stop_decision(
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            error_code=error_code,
            policy_version=policy_version,
        )
    )
    return build_failed_attempt_audit(
        attempt_number=attempt_number,
        provider=provider,
        model=model,
        pricing_reference_at=REFERENCE_AT,
        error_code=error_code,
        retry_decision=decision,
    )


def trail(
    attempts: tuple[ProviderAttemptAudit, ...],
    *,
    outcome: str,
    max_attempts: int = 2,
    policy_version: str = POLICY_VERSION,
) -> AttemptAuditTrail:
    return AttemptAuditTrail(
        version=ATTEMPT_AUDIT_VERSION,
        retry_policy_version=policy_version,
        max_attempts=max_attempts,
        outcome=outcome,
        attempts=attempts,
    )


def test_successful_recorded_usage_and_available_cost_are_immutable() -> None:
    attempt = succeeded_attempt()

    assert attempt.version == ATTEMPT_AUDIT_VERSION == "1"
    assert attempt.status == SUCCEEDED
    assert attempt.usage_status == USAGE_RECORDED
    assert isinstance(attempt.usage, ProviderUsage)
    assert attempt.cost_status == COST_AVAILABLE
    assert attempt.cost is not None and attempt.cost.status == AVAILABLE
    assert attempt.error_code is None
    assert attempt.retry_decision is None
    with pytest.raises(FrozenInstanceError):
        attempt.status = FAILED  # type: ignore[misc]


def test_successful_unavailable_usage_is_not_unknown() -> None:
    cost = usage_unavailable_cost()
    attempt = succeeded_attempt(usage=None, cost=cost)

    assert attempt.usage_status == USAGE_UNAVAILABLE
    assert attempt.usage is None
    assert attempt.cost_status == COST_UNAVAILABLE
    assert attempt.cost is cost
    assert cost.unavailable_reason == COST_REASON_USAGE_UNAVAILABLE


def test_failed_attempt_uses_unknown_usage_and_cost() -> None:
    attempt = failed_attempt()

    assert attempt.status == FAILED
    assert attempt.error_code == PROVIDER_TIMEOUT
    assert isinstance(attempt.retry_decision, RetryDecision)
    assert attempt.usage_status == USAGE_UNKNOWN
    assert attempt.usage is None
    assert attempt.cost_status == COST_UNKNOWN
    assert attempt.cost is None


@pytest.mark.parametrize("attempt_number", [0, -1, True, False, 1.0, None])
def test_invalid_attempt_number_is_rejected(attempt_number: object) -> None:
    with pytest.raises(AttemptAuditError) as captured:
        build_failed_attempt_audit(
            attempt_number=attempt_number,  # type: ignore[arg-type]
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            error_code=PROVIDER_TIMEOUT,
            retry_decision=retry_decision(attempt_number=1),
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT


def test_normal_int_subclass_is_accepted() -> None:
    class AttemptNumber(int):
        pass

    attempt = build_failed_attempt_audit(
        attempt_number=AttemptNumber(1),
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
        error_code=PROVIDER_TIMEOUT,
        retry_decision=retry_decision(attempt_number=AttemptNumber(1)),
    )
    result = trail(
        (succeeded_attempt(),),
        outcome=SUCCEEDED,
        max_attempts=AttemptNumber(2),
    )

    assert attempt.attempt_number == 1
    assert result.max_attempts == 2


def test_512_digit_provider_usage_and_trail_are_strict_json_safe() -> None:
    usage = ProviderUsage(
        prompt_tokens=MAX_AUDIT_JSON_INTEGER,
        completion_tokens=0,
        total_tokens=MAX_AUDIT_JSON_INTEGER,
        prompt_cache_hit_tokens=MAX_AUDIT_JSON_INTEGER,
        prompt_cache_miss_tokens=0,
        reasoning_tokens=0,
    )
    cost = build_cost_audit_metadata(
        usage,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
    )
    attempt = build_succeeded_attempt_audit(
        attempt_number=1,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
        usage=usage,
        cost=cost,
    )
    result = trail((attempt,), outcome=SUCCEEDED, max_attempts=1)
    payload = result.to_dict()

    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    serialized_usage = payload["attempts"][0]["usage"]
    assert isinstance(serialized_usage["prompt_tokens"], int)
    assert serialized_usage["prompt_tokens"] == MAX_AUDIT_JSON_INTEGER
    assert serialized_usage["prompt_cache_miss_tokens"] == 0


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
def test_each_513_digit_provider_usage_field_is_rejected_by_audit(
    field_name: str,
) -> None:
    usage = oversized_usage_for(
        field_name,
        FIRST_UNREPRESENTABLE_AUDIT_INTEGER,
    )
    cost = build_cost_audit_metadata(
        usage,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
    )

    with pytest.raises(AttemptAuditError) as captured:
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            usage=usage,
            cost=cost,
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert f"usage.{field_name}" in captured.value.message


def test_5001_digit_usage_remains_valid_provider_fact_but_audit_rejects() -> None:
    huge = 10**5000
    usage = ProviderUsage(
        prompt_tokens=huge,
        completion_tokens=0,
        total_tokens=huge,
        prompt_cache_hit_tokens=huge,
        prompt_cache_miss_tokens=0,
        reasoning_tokens=0,
    )
    cost = build_cost_audit_metadata(
        usage,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
    )

    assert usage.prompt_tokens == huge
    with pytest.raises(AttemptAuditError) as captured:
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            usage=usage,
            cost=cost,
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert len(str(captured.value)) < 300


def test_direct_attempt_constructor_rejects_unrepresentable_usage() -> None:
    huge = 10**5000
    usage = ProviderUsage(huge, 0, huge, huge, 0, 0)
    cost = build_cost_audit_metadata(
        usage,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
    )

    with pytest.raises(AttemptAuditError) as captured:
        replace(succeeded_attempt(), usage=usage, cost=cost)

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert len(str(captured.value)) < 300


def test_optional_usage_fields_remain_null_json_values() -> None:
    usage = ProviderUsage(10, 2, 12)
    cost = build_cost_audit_metadata(
        usage,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
    )
    attempt = build_succeeded_attempt_audit(
        attempt_number=1,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
        usage=usage,
        cost=cost,
    )

    payload = attempt.to_dict()["usage"]
    json.dumps(payload, allow_nan=False)
    assert payload["prompt_cache_hit_tokens"] is None
    assert payload["prompt_cache_miss_tokens"] is None
    assert payload["reasoning_tokens"] is None


def test_attempt_number_512_digit_boundary_is_json_safe() -> None:
    attempt = build_succeeded_attempt_audit(
        attempt_number=MAX_AUDIT_JSON_INTEGER,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
        usage=complete_usage(),
        cost=available_cost(),
    )
    payload = attempt.to_dict()

    json.dumps(payload, allow_nan=False)
    assert isinstance(payload["attempt_number"], int)
    assert payload["attempt_number"] == MAX_AUDIT_JSON_INTEGER


def test_attempt_number_513_digit_boundary_is_rejected() -> None:
    with pytest.raises(AttemptAuditError) as captured:
        build_succeeded_attempt_audit(
            attempt_number=FIRST_UNREPRESENTABLE_AUDIT_INTEGER,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            usage=complete_usage(),
            cost=available_cost(),
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert len(str(captured.value)) < 300


def test_trail_max_attempts_512_digit_boundary_is_json_safe() -> None:
    result = trail(
        (succeeded_attempt(),),
        outcome=SUCCEEDED,
        max_attempts=MAX_AUDIT_JSON_INTEGER,
    )
    payload = result.to_dict()

    json.dumps(payload, allow_nan=False)
    assert isinstance(payload["max_attempts"], int)
    assert payload["max_attempts"] == MAX_AUDIT_JSON_INTEGER


def test_trail_max_attempts_513_digit_boundary_is_rejected() -> None:
    with pytest.raises(AttemptAuditError) as captured:
        trail(
            (succeeded_attempt(),),
            outcome=SUCCEEDED,
            max_attempts=FIRST_UNREPRESENTABLE_AUDIT_INTEGER,
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert len(str(captured.value)) < 300


def test_retry_decision_512_digit_integer_boundaries_are_json_safe() -> None:
    decision = RetryDecision(
        policy_version=POLICY_VERSION,
        error_code=PROVIDER_TIMEOUT,
        action=DO_NOT_RETRY,
        reason=ATTEMPT_LIMIT_REACHED,
        attempts_completed=MAX_AUDIT_JSON_INTEGER,
        max_attempts=MAX_AUDIT_JSON_INTEGER,
    )
    attempt = build_failed_attempt_audit(
        attempt_number=MAX_AUDIT_JSON_INTEGER,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
        error_code=PROVIDER_TIMEOUT,
        retry_decision=decision,
    )
    payload = attempt.to_dict()

    json.dumps(payload, allow_nan=False)
    assert isinstance(
        payload["retry_decision"]["attempts_completed"],
        int,
    )
    assert isinstance(payload["retry_decision"]["max_attempts"], int)


def test_retry_decision_unrepresentable_attempts_completed_is_rejected() -> None:
    decision = RetryDecision(
        policy_version=POLICY_VERSION,
        error_code=PROVIDER_TIMEOUT,
        action=DO_NOT_RETRY,
        reason=ATTEMPT_LIMIT_REACHED,
        attempts_completed=FIRST_UNREPRESENTABLE_AUDIT_INTEGER,
        max_attempts=FIRST_UNREPRESENTABLE_AUDIT_INTEGER,
    )

    with pytest.raises(AttemptAuditError) as captured:
        build_failed_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            error_code=PROVIDER_TIMEOUT,
            retry_decision=decision,
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert "retry_decision.attempts_completed" in captured.value.message


def test_retry_decision_unrepresentable_max_attempts_is_rejected() -> None:
    decision = RetryDecision(
        policy_version=POLICY_VERSION,
        error_code=PROVIDER_TIMEOUT,
        action=RETRY,
        reason=RETRYABLE_TRANSIENT_ERROR,
        attempts_completed=1,
        max_attempts=FIRST_UNREPRESENTABLE_AUDIT_INTEGER,
    )

    with pytest.raises(AttemptAuditError) as captured:
        build_failed_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            error_code=PROVIDER_TIMEOUT,
            retry_decision=decision,
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert "retry_decision.max_attempts" in captured.value.message


@pytest.mark.parametrize("field_name", ["provider", "model"])
@pytest.mark.parametrize("value", [None, "", " ", b"identity"])
def test_invalid_provider_or_model_is_rejected(
    field_name: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "attempt_number": 1,
        "provider": PROVIDER,
        "model": MODEL,
        "pricing_reference_at": REFERENCE_AT,
        "usage": complete_usage(),
        "cost": available_cost(),
    }
    values[field_name] = value

    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, "not-a-time", 1, object()])
def test_wrong_pricing_reference_type_or_value_is_rejected(value: object) -> None:
    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=value,  # type: ignore[arg-type]
            usage=complete_usage(),
            cost=available_cost(),
        )


def test_naive_pricing_reference_is_rejected() -> None:
    with pytest.raises(AttemptAuditError):
        build_failed_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=datetime(2026, 8, 17, 1, 0),
            error_code=PROVIDER_TIMEOUT,
            retry_decision=retry_decision(attempt_number=1),
        )


def test_non_utc_references_normalize_to_the_same_instant() -> None:
    singapore = timezone(timedelta(hours=8))
    new_york = timezone(timedelta(hours=-4))
    singapore_time = datetime(2026, 8, 17, 9, 0, tzinfo=singapore)
    new_york_time = datetime(2026, 8, 16, 21, 0, tzinfo=new_york)

    singapore_attempt = build_failed_attempt_audit(
        attempt_number=1,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=singapore_time,
        error_code=PROVIDER_TIMEOUT,
        retry_decision=retry_decision(attempt_number=1),
    )
    new_york_attempt = build_failed_attempt_audit(
        attempt_number=1,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=new_york_time,
        error_code=PROVIDER_TIMEOUT,
        retry_decision=retry_decision(attempt_number=1),
    )

    assert singapore_attempt.pricing_reference_at == (
        "2026-08-17T01:00:00+00:00"
    )
    assert new_york_attempt.pricing_reference_at == (
        singapore_attempt.pricing_reference_at
    )


@pytest.mark.parametrize(
    "attempt_reference",
    [
        datetime(
            2026,
            8,
            17,
            9,
            0,
            tzinfo=timezone(timedelta(hours=8)),
        ),
        datetime(
            2026,
            8,
            16,
            21,
            0,
            tzinfo=timezone(timedelta(hours=-4)),
        ),
    ],
)
def test_success_cost_reference_accepts_same_instant_across_timezones(
    attempt_reference: datetime,
) -> None:
    attempt = build_succeeded_attempt_audit(
        attempt_number=1,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=attempt_reference,
        usage=complete_usage(),
        cost=available_cost(reference_at=REFERENCE_AT),
    )

    assert attempt.pricing_reference_at == REFERENCE_AT.isoformat()
    assert attempt.cost is not None
    assert attempt.cost.pricing_reference_at == REFERENCE_AT.isoformat()


@pytest.mark.parametrize(
    "status",
    ["pending", "running", "cancelled", "unknown", "retrying", "", None],
)
def test_invalid_attempt_status_is_rejected(status: object) -> None:
    with pytest.raises(AttemptAuditError):
        replace(succeeded_attempt(), status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("status", []),
        ("usage_status", []),
        ("usage_status", "pending"),
        ("cost_status", {}),
        ("cost_status", "pending"),
    ],
)
def test_invalid_or_unhashable_status_values_are_domain_errors(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(AttemptAuditError) as captured:
        replace(succeeded_attempt(), **{field_name: value})

    assert captured.value.code == INVALID_ATTEMPT_AUDIT


@pytest.mark.parametrize(
    "changes",
    [
        {"usage": object()},
        {"cost": object()},
        {"retry_decision": object()},
    ],
)
def test_wrong_nested_domain_types_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(AttemptAuditError) as captured:
        replace(succeeded_attempt(), **changes)

    assert captured.value.code == INVALID_ATTEMPT_AUDIT


@pytest.mark.parametrize(
    ("changes", "case_id"),
    [
        ({"error_code": PROVIDER_TIMEOUT}, "success-error"),
        (
            {"retry_decision": retry_decision(attempt_number=1)},
            "success-decision",
        ),
        (
            {"usage_status": USAGE_UNKNOWN, "usage": None},
            "success-unknown-usage",
        ),
        (
            {"cost_status": COST_UNKNOWN, "cost": None},
            "success-unknown-cost",
        ),
        (
            {"cost_status": COST_UNAVAILABLE, "cost": None},
            "success-missing-cost",
        ),
        (
            {"cost_status": COST_UNAVAILABLE},
            "success-cost-status-mismatch",
        ),
        (
            {"usage_status": USAGE_RECORDED, "usage": None},
            "recorded-without-usage",
        ),
        (
            {"usage_status": USAGE_UNAVAILABLE, "usage": complete_usage()},
            "unavailable-with-usage",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_success_contradictions_are_rejected(
    changes: dict[str, object],
    case_id: str,
) -> None:
    del case_id
    with pytest.raises(AttemptAuditError):
        replace(succeeded_attempt(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"error_code": None},
        {"retry_decision": None},
        {"usage_status": USAGE_RECORDED},
        {"usage_status": USAGE_UNAVAILABLE},
        {"usage": complete_usage()},
        {"cost_status": COST_AVAILABLE},
        {"cost_status": COST_UNAVAILABLE},
        {"cost": usage_unavailable_cost()},
    ],
)
def test_failure_contradictions_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(AttemptAuditError):
        replace(failed_attempt(), **changes)


@pytest.mark.parametrize(
    "error_code",
    ["", " ", "timeout", "RuntimeError('SECRET_INTERNAL')", "A-B"],
)
def test_failed_attempt_requires_stable_error_identifier(
    error_code: str,
) -> None:
    with pytest.raises(AttemptAuditError) as captured:
        build_failed_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            error_code=error_code,
            retry_decision=retry_decision(attempt_number=1),
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert "SECRET_INTERNAL" not in captured.value.message


def test_decision_error_code_and_attempt_number_must_match() -> None:
    with pytest.raises(AttemptAuditError):
        build_failed_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            error_code=PROVIDER_TIMEOUT,
            retry_decision=retry_decision(
                attempt_number=1,
                error_code=PROVIDER_UNAVAILABLE,
            ),
        )
    with pytest.raises(AttemptAuditError):
        build_failed_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            error_code=PROVIDER_TIMEOUT,
            retry_decision=retry_decision(
                attempt_number=2,
                max_attempts=3,
            ),
        )


def test_success_pricing_reference_must_match_cost_reference() -> None:
    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT + timedelta(seconds=1),
            usage=complete_usage(),
            cost=available_cost(),
        )


@pytest.mark.parametrize(
    ("provider", "model"),
    [("other-provider", MODEL), (PROVIDER, "other-model")],
)
def test_available_cost_provider_and_model_must_match_attempt(
    provider: str,
    model: str,
) -> None:
    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=provider,
            model=model,
            pricing_reference_at=REFERENCE_AT,
            usage=complete_usage(),
            cost=available_cost(),
        )


def test_available_cost_requires_recorded_usage() -> None:
    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            usage=None,
            cost=available_cost(),
        )


def test_usage_unavailable_reason_requires_unavailable_usage() -> None:
    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            usage=complete_usage(),
            cost=usage_unavailable_cost(),
        )


def test_cache_breakdown_reason_requires_recorded_usage() -> None:
    cost = cache_unavailable_cost()
    assert cost.unavailable_reason == CACHE_BREAKDOWN_UNAVAILABLE

    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            usage=None,
            cost=cost,
        )


@pytest.mark.parametrize("usage", [complete_usage(), None])
def test_policy_unavailable_reason_accepts_recorded_or_unavailable_usage(
    usage: ProviderUsage | None,
) -> None:
    cost = policy_unavailable_cost(usage)
    attempt = build_succeeded_attempt_audit(
        attempt_number=1,
        provider="provider-a",
        model="model-a",
        pricing_reference_at=REFERENCE_AT,
        usage=usage,
        cost=cost,
    )

    assert cost.unavailable_reason == POLICY_NOT_APPLICABLE
    assert attempt.usage_status == (
        USAGE_RECORDED if usage is not None else USAGE_UNAVAILABLE
    )


@pytest.mark.parametrize("usage", [complete_usage(), None])
def test_policy_not_effective_accepts_recorded_or_unavailable_usage(
    usage: ProviderUsage | None,
) -> None:
    cost = policy_not_effective_cost(usage)
    attempt = build_succeeded_attempt_audit(
        attempt_number=1,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=datetime(
            2026,
            8,
            16,
            15,
            59,
            59,
            tzinfo=UTC,
        ),
        usage=usage,
        cost=cost,
    )

    assert cost.unavailable_reason == POLICY_NOT_EFFECTIVE
    assert attempt.usage_status == (
        USAGE_RECORDED if usage is not None else USAGE_UNAVAILABLE
    )


@pytest.mark.parametrize("value", ["future", "", None])
def test_attempt_version_is_independent_and_fixed(value: object) -> None:
    with pytest.raises(AttemptAuditError):
        replace(succeeded_attempt(), version=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "not-a-time",
        "2026-08-17T01:00:00",
        "2026-08-17T09:00:00+08:00",
    ],
)
def test_stored_attempt_reference_must_be_canonical_utc(value: str) -> None:
    with pytest.raises(AttemptAuditError):
        replace(succeeded_attempt(), pricing_reference_at=value)


def test_single_success_and_single_terminal_failure_trails_are_valid() -> None:
    success = trail((succeeded_attempt(),), outcome=SUCCEEDED)
    terminal_failure = trail(
        (
            failed_attempt(
                error_code=PROVIDER_AUTH_FAILED,
                action=DO_NOT_RETRY,
            ),
        ),
        outcome=FAILED,
    )

    assert success.outcome == SUCCEEDED
    assert terminal_failure.outcome == FAILED


def test_retry_to_success_and_retry_to_exhaustion_are_valid() -> None:
    success = trail(
        (failed_attempt(), succeeded_attempt(2)),
        outcome=SUCCEEDED,
    )
    exhausted = trail(
        (
            failed_attempt(),
            failed_attempt(
                2,
                error_code=PROVIDER_UNAVAILABLE,
                action=DO_NOT_RETRY,
            ),
        ),
        outcome=FAILED,
    )

    assert len(success.attempts) == 2
    assert exhausted.attempts[-1].retry_decision is not None
    assert exhausted.attempts[-1].retry_decision.reason == (
        ATTEMPT_LIMIT_REACHED
    )


def test_retry_to_terminal_at_limit_preserves_limit_priority() -> None:
    result = trail(
        (
            failed_attempt(),
            failed_attempt(
                2,
                error_code=PROVIDER_AUTH_FAILED,
                action=DO_NOT_RETRY,
            ),
        ),
        outcome=FAILED,
    )

    decision = result.attempts[-1].retry_decision
    assert decision is not None
    assert decision.error_code == PROVIDER_AUTH_FAILED
    assert decision.reason == ATTEMPT_LIMIT_REACHED


def test_three_attempt_success_and_failure_are_supported() -> None:
    first = failed_attempt(max_attempts=3)
    second = failed_attempt(2, max_attempts=3)
    success = trail(
        (first, second, succeeded_attempt(3)),
        outcome=SUCCEEDED,
        max_attempts=3,
    )
    failure = trail(
        (
            first,
            second,
            failed_attempt(3, max_attempts=3, action=DO_NOT_RETRY),
        ),
        outcome=FAILED,
        max_attempts=3,
    )

    assert len(success.attempts) == 3
    assert len(failure.attempts) == 3


@pytest.mark.parametrize("outcome", [SUCCEEDED, FAILED])
def test_five_attempt_trails_are_not_limited_to_two_or_three(
    outcome: str,
) -> None:
    attempts: list[ProviderAttemptAudit] = [
        failed_attempt(number, max_attempts=5)
        for number in range(1, 5)
    ]
    attempts.append(
        succeeded_attempt(5)
        if outcome == SUCCEEDED
        else failed_attempt(5, max_attempts=5, action=DO_NOT_RETRY)
    )

    result = trail(tuple(attempts), outcome=outcome, max_attempts=5)

    assert len(result.attempts) == 5
    assert result.attempts[-1].status == outcome


def test_different_provider_and_model_across_attempts_are_supported() -> None:
    first = failed_attempt(
        provider="provider-a",
        model="model-a",
        error_code="FUTURE_TRANSIENT_ERROR",
    )
    second_usage = None
    second_cost = policy_unavailable_cost(
        second_usage,
        provider="provider-b",
        model="model-b",
    )
    second = succeeded_attempt(
        2,
        provider="provider-b",
        model="model-b",
        usage=second_usage,
        cost=second_cost,
    )

    result = trail((first, second), outcome=SUCCEEDED)

    assert result.attempts[0].provider == "provider-a"
    assert result.attempts[1].provider == "provider-b"


def test_mixed_provider_trail_preserves_available_final_cost_provenance() -> None:
    first = failed_attempt(
        provider="provider-a",
        model="model-a",
        error_code="FUTURE_TRANSIENT_ERROR",
    )
    usage = complete_usage()
    cost = synthetic_available_cost(
        provider="provider-b",
        model="model-b",
    )
    second = build_succeeded_attempt_audit(
        attempt_number=2,
        provider="provider-b",
        model="model-b",
        pricing_reference_at=REFERENCE_AT,
        usage=usage,
        cost=cost,
    )

    result = trail((first, second), outcome=SUCCEEDED)

    assert result.attempts[0].provider == "provider-a"
    assert result.attempts[1].provider == "provider-b"
    assert result.attempts[1].cost is cost


def test_mixed_provider_final_cost_cannot_reuse_prior_provenance() -> None:
    cost = synthetic_available_cost(
        provider="provider-a",
        model="model-a",
    )

    with pytest.raises(AttemptAuditError):
        build_succeeded_attempt_audit(
            attempt_number=2,
            provider="provider-b",
            model="model-b",
            pricing_reference_at=REFERENCE_AT,
            usage=complete_usage(),
            cost=cost,
        )


@pytest.mark.parametrize(
    "attempts",
    [
        (),
        (succeeded_attempt(2),),
        (failed_attempt(max_attempts=3), succeeded_attempt(3)),
        (failed_attempt(), succeeded_attempt(1)),
        (
            failed_attempt(2, max_attempts=3),
            succeeded_attempt(1),
        ),
    ],
    ids=["empty", "starts-at-two", "gap", "duplicate", "out-of-order"],
)
def test_trail_requires_contiguous_ordered_numbering(
    attempts: tuple[ProviderAttemptAudit, ...],
) -> None:
    with pytest.raises(AttemptAuditError):
        trail(attempts, outcome=SUCCEEDED, max_attempts=3)


def test_trail_rejects_attempt_count_over_maximum() -> None:
    with pytest.raises(AttemptAuditError):
        trail(
            (failed_attempt(), succeeded_attempt(2)),
            outcome=SUCCEEDED,
            max_attempts=1,
        )


@pytest.mark.parametrize("attempts", [[], {}, None, object()])
def test_trail_requires_attempt_tuple(attempts: object) -> None:
    with pytest.raises(AttemptAuditError):
        AttemptAuditTrail(
            version=ATTEMPT_AUDIT_VERSION,
            retry_policy_version=POLICY_VERSION,
            max_attempts=2,
            outcome=SUCCEEDED,
            attempts=attempts,  # type: ignore[arg-type]
        )


def test_trail_rejects_non_attempt_tuple_member() -> None:
    with pytest.raises(AttemptAuditError):
        AttemptAuditTrail(
            version=ATTEMPT_AUDIT_VERSION,
            retry_policy_version=POLICY_VERSION,
            max_attempts=2,
            outcome=SUCCEEDED,
            attempts=(object(),),  # type: ignore[arg-type]
        )


def test_trail_rejects_retry_policy_version_and_max_linkage_mismatch() -> None:
    with pytest.raises(AttemptAuditError):
        trail(
            (
                failed_attempt(
                    error_code=PROVIDER_AUTH_FAILED,
                    action=DO_NOT_RETRY,
                    policy_version="historical-policy",
                ),
            ),
            outcome=FAILED,
        )
    with pytest.raises(AttemptAuditError):
        trail(
            (
                failed_attempt(
                    max_attempts=3,
                    error_code=PROVIDER_AUTH_FAILED,
                    action=DO_NOT_RETRY,
                ),
            ),
            outcome=FAILED,
            max_attempts=2,
        )


def test_historical_policy_identity_is_not_checked_against_current_default() -> None:
    result = trail(
        (
            failed_attempt(
                error_code=PROVIDER_AUTH_FAILED,
                action=DO_NOT_RETRY,
                policy_version="historical-policy-v99",
            ),
        ),
        outcome=FAILED,
        policy_version="historical-policy-v99",
    )

    assert result.retry_policy_version == "historical-policy-v99"


def test_intermediate_success_and_do_not_retry_are_rejected() -> None:
    with pytest.raises(AttemptAuditError):
        trail(
            (succeeded_attempt(), succeeded_attempt(2)),
            outcome=SUCCEEDED,
        )
    with pytest.raises(AttemptAuditError):
        trail(
            (
                failed_attempt(
                    error_code=PROVIDER_AUTH_FAILED,
                    action=DO_NOT_RETRY,
                ),
                succeeded_attempt(2),
            ),
            outcome=SUCCEEDED,
        )


def test_completed_trail_cannot_end_with_retry_decision() -> None:
    with pytest.raises(AttemptAuditError):
        trail((failed_attempt(),), outcome=FAILED)


def test_trail_outcome_must_match_final_attempt() -> None:
    with pytest.raises(AttemptAuditError):
        trail((succeeded_attempt(),), outcome=FAILED)
    with pytest.raises(AttemptAuditError):
        trail(
            (
                failed_attempt(
                    error_code=PROVIDER_AUTH_FAILED,
                    action=DO_NOT_RETRY,
                ),
            ),
            outcome=SUCCEEDED,
        )


@pytest.mark.parametrize("outcome", ["pending", "unknown", "", None, []])
def test_invalid_trail_outcome_is_rejected(outcome: object) -> None:
    with pytest.raises(AttemptAuditError):
        trail(
            (succeeded_attempt(),),
            outcome=outcome,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("maximum", [0, -1, True, False, 1.0, None])
def test_invalid_trail_max_attempts_is_rejected(maximum: object) -> None:
    with pytest.raises(AttemptAuditError):
        AttemptAuditTrail(
            version=ATTEMPT_AUDIT_VERSION,
            retry_policy_version=POLICY_VERSION,
            max_attempts=maximum,  # type: ignore[arg-type]
            outcome=SUCCEEDED,
            attempts=(succeeded_attempt(),),
        )


@pytest.mark.parametrize("version", [None, "", "2", b"1", []])
def test_trail_version_is_independent_and_fixed(version: object) -> None:
    with pytest.raises(AttemptAuditError):
        AttemptAuditTrail(
            version=version,  # type: ignore[arg-type]
            retry_policy_version=POLICY_VERSION,
            max_attempts=2,
            outcome=SUCCEEDED,
            attempts=(succeeded_attempt(),),
        )


def test_attempt_trail_is_immutable() -> None:
    result = trail((succeeded_attempt(),), outcome=SUCCEEDED)

    with pytest.raises(FrozenInstanceError):
        result.outcome = FAILED  # type: ignore[misc]


@pytest.mark.parametrize("version", [None, "", " ", b"1"])
def test_invalid_retry_policy_version_is_rejected(version: object) -> None:
    with pytest.raises(AttemptAuditError):
        AttemptAuditTrail(
            version=ATTEMPT_AUDIT_VERSION,
            retry_policy_version=version,  # type: ignore[arg-type]
            max_attempts=2,
            outcome=SUCCEEDED,
            attempts=(succeeded_attempt(),),
        )


def test_attempt_to_dict_has_fixed_json_safe_schema() -> None:
    payload = succeeded_attempt().to_dict()

    assert set(payload) == {
        "version",
        "attempt_number",
        "provider",
        "model",
        "pricing_reference_at",
        "status",
        "error_code",
        "retry_decision",
        "usage_status",
        "usage",
        "cost_status",
        "cost",
    }
    assert isinstance(payload["usage"]["prompt_tokens"], int)
    assert payload["cost"]["estimate"]["total_estimated_cost"] == (
        "0.0004484"
    )
    assert isinstance(
        payload["cost"]["estimate"]["total_estimated_cost"],
        str,
    )
    json.dumps(payload, ensure_ascii=False, allow_nan=False)


def test_large_cost_decimal_remains_an_exact_json_string() -> None:
    cost = synthetic_available_cost(
        provider=PROVIDER,
        model=MODEL,
        amount=Decimal("1E+5000"),
    )
    attempt = build_succeeded_attempt_audit(
        attempt_number=1,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=REFERENCE_AT,
        usage=complete_usage(),
        cost=cost,
    )
    payload = trail((attempt,), outcome=SUCCEEDED).to_dict()
    serialized_cost = payload["attempts"][0]["cost"]["estimate"][
        "total_estimated_cost"
    ]

    json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert isinstance(serialized_cost, str)
    assert len(serialized_cost) == 5001
    assert serialized_cost.startswith("1")
    assert set(serialized_cost[1:]) == {"0"}


def test_failed_attempt_serializes_unknown_states_as_null() -> None:
    payload = failed_attempt().to_dict()

    assert payload["usage_status"] == USAGE_UNKNOWN
    assert payload["usage"] is None
    assert payload["cost_status"] == COST_UNKNOWN
    assert payload["cost"] is None
    assert payload["retry_decision"] == {
        "policy_version": POLICY_VERSION,
        "error_code": PROVIDER_TIMEOUT,
        "action": RETRY,
        "reason": RETRYABLE_TRANSIENT_ERROR,
        "attempts_completed": 1,
        "max_attempts": 2,
    }


def test_unavailable_success_serializes_differently_from_unknown_failure() -> None:
    success = succeeded_attempt(
        usage=None,
        cost=usage_unavailable_cost(),
    ).to_dict()
    failure = failed_attempt().to_dict()

    assert success["usage_status"] == USAGE_UNAVAILABLE
    assert success["usage"] is None
    assert success["cost_status"] == COST_UNAVAILABLE
    assert isinstance(success["cost"], dict)
    assert failure["usage_status"] == USAGE_UNKNOWN
    assert failure["cost_status"] == COST_UNKNOWN
    assert failure["cost"] is None


def test_trail_to_dict_is_strict_json_safe_and_deeply_fresh() -> None:
    result = trail(
        (failed_attempt(), succeeded_attempt(2)),
        outcome=SUCCEEDED,
    )
    first = result.to_dict()
    second = result.to_dict()

    json.dumps(first, ensure_ascii=False, allow_nan=False)
    assert first is not second
    assert first["attempts"] is not second["attempts"]
    first["attempts"][0]["retry_decision"]["action"] = "changed"
    first["attempts"][1]["usage"]["prompt_tokens"] = 999
    first["attempts"][1]["cost"]["estimate"][
        "total_estimated_cost"
    ] = "999"

    fresh = result.to_dict()
    assert fresh["attempts"][0]["retry_decision"]["action"] == RETRY
    assert fresh["attempts"][1]["usage"]["prompt_tokens"] == 1000
    assert fresh["attempts"][1]["cost"]["estimate"][
        "total_estimated_cost"
    ] == "0.0004484"


def test_serialization_contains_no_domain_objects_or_private_content() -> None:
    payload = trail(
        (failed_attempt(), succeeded_attempt(2)),
        outcome=SUCCEEDED,
    ).to_dict()
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    forbidden = {
        "prompt",
        "raw_response",
        "exception_message",
        "api_key",
        "business_rows",
        "insight_output",
        "SECRET_INTERNAL",
    }

    assert forbidden.isdisjoint(encoded.lower())

    def assert_primitives(value: object) -> None:
        if value is None or isinstance(value, (str, int, bool)):
            return
        if isinstance(value, list):
            for member in value:
                assert_primitives(member)
            return
        if isinstance(value, dict):
            for key, member in value.items():
                assert isinstance(key, str)
                assert_primitives(member)
            return
        pytest.fail(f"Unexpected serialized type: {type(value).__name__}")

    assert_primitives(payload)


def test_error_messages_do_not_dump_nested_objects() -> None:
    secret = "SECRET_INTERNAL"
    with pytest.raises(AttemptAuditError) as captured:
        build_failed_attempt_audit(
            attempt_number=1,
            provider=PROVIDER,
            model=MODEL,
            pricing_reference_at=REFERENCE_AT,
            error_code=f"RuntimeError({secret})",
            retry_decision=retry_decision(attempt_number=1),
        )

    assert captured.value.code == INVALID_ATTEMPT_AUDIT
    assert secret not in str(captured.value)


def test_attempt_audit_core_has_no_execution_time_or_network_dependencies() -> None:
    source_path = Path(audit_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert "dataclasses.asdict" not in source
    assert "set_int_max_str_digits" not in source
    assert "MAX_RECEIPT_TOKEN_DECIMAL_DIGITS" not in source
    assert "evaluate_retry" not in source
    assert ".generate(" not in source
    assert "generate_insight" not in source
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert "monotonic" not in source
    assert "time" not in imports
    assert "random" not in imports
    assert "os" not in imports
    assert "requests" not in imports
    assert "httpx" not in imports
    assert "urllib" not in imports
    assert "openai" not in imports

    representability_helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_validate_json_integer_representability"
    )
    helper_calls = {
        node.func.id
        for node in ast.walk(representability_helper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "str" not in helper_calls
    assert "repr" not in helper_calls
    assert "format" not in helper_calls
