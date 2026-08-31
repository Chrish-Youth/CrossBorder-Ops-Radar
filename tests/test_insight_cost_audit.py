from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

import src.insight_cost_audit as cost_audit_module
from src.insight_cost_audit import (
    AVAILABLE,
    COST_AUDIT_VERSION,
    INVALID_COST_AUDIT,
    UNAVAILABLE,
    CostAuditError,
    CostAuditMetadata,
    build_cost_audit_metadata,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    DEEPSEEK_FLASH_PRICING_POLICY,
    INVALID_PRICING_INPUT,
    OFF_PEAK,
    PEAK,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    USAGE_UNAVAILABLE,
    PricingError,
    PricingPolicy,
)
from src.insight_pricing_catalog import (
    INVALID_PRICING_CATALOG,
    UNSELECTED_PRICING_POLICY_VERSION,
    PricingCatalogError,
    PricingPolicyCatalog,
)
from src.insight_provider import ProviderUsage


UTC = timezone.utc
SINGAPORE = timezone(timedelta(hours=8))
MONDAY_PEAK = datetime(2026, 8, 17, 1, 0, tzinfo=UTC)
SUNDAY_OFF_PEAK = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)
POLICY_B_EFFECTIVE = datetime(2026, 9, 15, tzinfo=UTC)


def synthetic_policy_b() -> PricingPolicy:
    return replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version="test-deepseek-v4-flash-2026-09-15-v1",
        effective_from_utc=POLICY_B_EFFECTIVE,
        verified_at_utc=POLICY_B_EFFECTIVE + timedelta(days=1),
        off_peak_rates=replace(
            DEEPSEEK_FLASH_PRICING_POLICY.off_peak_rates,
            prompt_cache_miss_usd_per_million=Decimal("0.30"),
        ),
    )


def complete_usage(
    *,
    cache_hit: int = 600,
    cache_miss: int = 400,
    completion: int = 200,
) -> ProviderUsage:
    prompt = cache_hit + cache_miss
    return ProviderUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_cache_hit_tokens=cache_hit,
        prompt_cache_miss_tokens=cache_miss,
    )


def available_audit(
    *,
    reference_at: datetime = MONDAY_PEAK,
) -> CostAuditMetadata:
    return build_cost_audit_metadata(
        complete_usage(),
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=reference_at,
    )


def unavailable_audit(
    reason: str = USAGE_UNAVAILABLE,
) -> CostAuditMetadata:
    return CostAuditMetadata(
        version=COST_AUDIT_VERSION,
        status=UNAVAILABLE,
        pricing_policy_version=DEEPSEEK_FLASH_PRICING_POLICY.version,
        pricing_reference_at=MONDAY_PEAK.isoformat(),
        estimate=None,
        unavailable_reason=reason,
    )


def test_available_cost_audit_is_immutable_and_uses_sealed_pricing() -> None:
    audit = available_audit()

    assert audit.version == COST_AUDIT_VERSION == "1"
    assert audit.status == AVAILABLE == "available"
    assert audit.estimate is not None
    assert audit.estimate.pricing_tier == PEAK
    assert audit.pricing_policy_version == audit.estimate.pricing_policy_version
    assert audit.pricing_policy_version != UNSELECTED_PRICING_POLICY_VERSION
    assert audit.pricing_reference_at == audit.estimate.pricing_reference_at
    assert audit.unavailable_reason is None
    with pytest.raises(FrozenInstanceError):
        audit.status = UNAVAILABLE  # type: ignore[misc]


def test_sunday_reference_consumes_weekend_off_peak_policy() -> None:
    audit = available_audit(reference_at=SUNDAY_OFF_PEAK)

    assert audit.estimate is not None
    assert audit.estimate.pricing_tier == OFF_PEAK
    assert audit.pricing_reference_at == "2026-08-23T01:00:00+00:00"


def test_non_utc_reference_is_normalized_by_instant() -> None:
    singapore_nine_am = datetime(2026, 8, 17, 9, 0, tzinfo=SINGAPORE)

    audit = available_audit(reference_at=singapore_nine_am)

    assert audit.pricing_reference_at == "2026-08-17T01:00:00+00:00"
    assert audit.estimate is not None
    assert audit.estimate.pricing_tier == PEAK


def test_usage_none_is_recorded_as_known_unavailable() -> None:
    audit = build_cost_audit_metadata(
        None,
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=MONDAY_PEAK,
    )

    assert audit == unavailable_audit(USAGE_UNAVAILABLE)


@pytest.mark.parametrize(
    ("provider", "reference_at", "expected_reason"),
    [
        ("unsupported", MONDAY_PEAK, POLICY_NOT_APPLICABLE),
        (
            "deepseek",
            DEEPSEEK_FLASH_PRICING_POLICY.effective_from_utc
            - timedelta(microseconds=1),
            POLICY_NOT_EFFECTIVE,
        ),
    ],
)
def test_policy_selection_error_precedes_usage_unavailable_when_no_policy_selected(
    provider: str,
    reference_at: datetime,
    expected_reason: str,
) -> None:
    audit = build_cost_audit_metadata(
        None,
        provider=provider,
        model="deepseek-v4-flash",
        pricing_reference_at=reference_at,
    )

    assert audit.status == UNAVAILABLE
    assert audit.pricing_policy_version == UNSELECTED_PRICING_POLICY_VERSION
    assert audit.unavailable_reason == expected_reason


def test_missing_cache_breakdown_is_recorded_as_known_unavailable() -> None:
    usage = ProviderUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
    )

    audit = build_cost_audit_metadata(
        usage,
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=MONDAY_PEAK,
    )

    assert audit.status == UNAVAILABLE
    assert audit.estimate is None
    assert audit.unavailable_reason == CACHE_BREAKDOWN_UNAVAILABLE
    assert audit.pricing_policy_version == DEEPSEEK_FLASH_PRICING_POLICY.version


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("other", "deepseek-v4-flash"),
        ("deepseek", "deepseek-v4-pro"),
    ],
)
def test_policy_identity_mismatch_is_recorded_as_unavailable(
    provider: str,
    model: str,
) -> None:
    audit = build_cost_audit_metadata(
        complete_usage(),
        provider=provider,
        model=model,
        pricing_reference_at=MONDAY_PEAK,
    )

    assert audit.status == UNAVAILABLE
    assert audit.unavailable_reason == POLICY_NOT_APPLICABLE
    assert audit.pricing_policy_version == UNSELECTED_PRICING_POLICY_VERSION


def test_default_catalog_selects_the_current_production_policy() -> None:
    audit = build_cost_audit_metadata(
        complete_usage(),
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=MONDAY_PEAK,
    )

    assert audit.status == AVAILABLE
    assert audit.pricing_policy_version == (
        DEEPSEEK_FLASH_PRICING_POLICY.version
    )


def test_synthetic_catalog_selected_policy_version_and_rates_drive_cost() -> None:
    policy_b = synthetic_policy_b()
    catalog = PricingPolicyCatalog(
        (policy_b, DEEPSEEK_FLASH_PRICING_POLICY)
    )
    usage = complete_usage(cache_hit=0, cache_miss=1_000_000, completion=0)

    before_b = build_cost_audit_metadata(
        usage,
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=POLICY_B_EFFECTIVE - timedelta(seconds=1),
        catalog=catalog,
    )
    at_b = build_cost_audit_metadata(
        usage,
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=POLICY_B_EFFECTIVE,
        catalog=catalog,
    )

    assert before_b.estimate is not None
    assert before_b.pricing_policy_version == (
        DEEPSEEK_FLASH_PRICING_POLICY.version
    )
    assert before_b.estimate.total_estimated_cost == Decimal("0.22")
    assert at_b.estimate is not None
    assert at_b.pricing_policy_version == policy_b.version
    assert at_b.estimate.total_estimated_cost == Decimal("0.30")


def test_three_version_catalog_latest_policy_identity_and_rates_drive_cost() -> None:
    policy_b = synthetic_policy_b()
    policy_c_effective = datetime(2026, 10, 1, tzinfo=UTC)
    policy_c = replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version="test-deepseek-v4-flash-2026-10-01-v1",
        effective_from_utc=policy_c_effective,
        verified_at_utc=policy_c_effective + timedelta(days=1),
        off_peak_rates=replace(
            DEEPSEEK_FLASH_PRICING_POLICY.off_peak_rates,
            prompt_cache_miss_usd_per_million=Decimal("0.45"),
        ),
    )
    catalog = PricingPolicyCatalog(
        (policy_b, policy_c, DEEPSEEK_FLASH_PRICING_POLICY)
    )

    audit = build_cost_audit_metadata(
        complete_usage(cache_hit=0, cache_miss=1_000_000, completion=0),
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=policy_c_effective,
        catalog=catalog,
    )

    assert audit.status == AVAILABLE
    assert audit.pricing_policy_version == policy_c.version
    assert audit.estimate is not None
    assert audit.estimate.pricing_policy_version == policy_c.version
    assert audit.estimate.total_estimated_cost == Decimal("0.45")


def test_explicit_policy_override_wins_without_accessing_catalog() -> None:
    usage = complete_usage(cache_hit=0, cache_miss=1_000_000, completion=0)

    audit = build_cost_audit_metadata(
        usage,
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=POLICY_B_EFFECTIVE,
        policy=DEEPSEEK_FLASH_PRICING_POLICY,
        catalog=object(),  # type: ignore[arg-type]
    )

    assert audit.estimate is not None
    assert audit.pricing_policy_version == (
        DEEPSEEK_FLASH_PRICING_POLICY.version
    )
    assert audit.estimate.total_estimated_cost == Decimal("0.22")


def test_explicit_reserved_policy_is_a_hard_failure_without_catalog_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserved = replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version=UNSELECTED_PRICING_POLICY_VERSION,
    )

    def fail_selector(**_: object) -> PricingPolicy:
        raise RuntimeError("SHOULD_NOT_BE_CALLED")

    monkeypatch.setattr(
        cost_audit_module,
        "select_pricing_policy",
        fail_selector,
    )

    with pytest.raises(CostAuditError) as captured:
        build_cost_audit_metadata(
            complete_usage(),
            provider="deepseek",
            model="deepseek-v4-flash",
            pricing_reference_at=MONDAY_PEAK,
            policy=reserved,
            catalog=object(),  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_COST_AUDIT
    assert "SHOULD_NOT_BE_CALLED" not in str(captured.value)


def test_catalog_without_matching_identity_is_known_unavailable() -> None:
    audit = build_cost_audit_metadata(
        complete_usage(),
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=POLICY_B_EFFECTIVE,
        catalog=PricingPolicyCatalog(()),
    )

    assert audit.status == UNAVAILABLE
    assert audit.unavailable_reason == POLICY_NOT_APPLICABLE
    assert audit.pricing_policy_version == UNSELECTED_PRICING_POLICY_VERSION


def test_catalog_before_first_policy_is_known_unavailable() -> None:
    audit = build_cost_audit_metadata(
        complete_usage(),
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=POLICY_B_EFFECTIVE - timedelta(seconds=1),
        catalog=PricingPolicyCatalog((synthetic_policy_b(),)),
    )

    assert audit.status == UNAVAILABLE
    assert audit.unavailable_reason == POLICY_NOT_EFFECTIVE
    assert audit.pricing_policy_version == UNSELECTED_PRICING_POLICY_VERSION


def test_invalid_catalog_is_a_hard_failure_not_unavailable() -> None:
    with pytest.raises(PricingCatalogError) as captured:
        build_cost_audit_metadata(
            complete_usage(),
            provider="deepseek",
            model="deepseek-v4-flash",
            pricing_reference_at=MONDAY_PEAK,
            catalog=object(),  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_PRICING_CATALOG


def test_reference_before_policy_effective_is_recorded_as_unavailable() -> None:
    audit = build_cost_audit_metadata(
        complete_usage(),
        provider="deepseek",
        model="deepseek-v4-flash",
        pricing_reference_at=datetime(2026, 8, 16, 15, 59, 59, tzinfo=UTC),
    )

    assert audit.status == UNAVAILABLE
    assert audit.unavailable_reason == POLICY_NOT_EFFECTIVE
    assert audit.pricing_reference_at == "2026-08-16T15:59:59+00:00"


def test_invalid_pricing_input_is_not_converted_to_unavailable() -> None:
    with pytest.raises(PricingError) as captured:
        build_cost_audit_metadata(
            object(),  # type: ignore[arg-type]
            provider="deepseek",
            model="deepseek-v4-flash",
            pricing_reference_at=MONDAY_PEAK,
        )

    assert captured.value.code == INVALID_PRICING_INPUT


def test_unexpected_pricing_exception_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_estimate(*_: object, **__: object) -> object:
        raise RuntimeError("SECRET_COST_INTERNAL")

    monkeypatch.setattr(
        cost_audit_module,
        "estimate_generation_cost",
        fail_estimate,
    )

    with pytest.raises(RuntimeError, match="SECRET_COST_INTERNAL"):
        available_audit()


@pytest.mark.parametrize(
    "values",
    [
        {"status": "success"},
        {"status": AVAILABLE, "estimate": None},
        {"status": AVAILABLE, "unavailable_reason": USAGE_UNAVAILABLE},
        {"status": UNAVAILABLE},
        {"status": UNAVAILABLE, "estimate": object()},
        {"status": UNAVAILABLE, "unavailable_reason": None},
        {"status": UNAVAILABLE, "unavailable_reason": "UNKNOWN"},
    ],
    ids=[
        "wrong-status",
        "available-no-estimate",
        "available-with-reason",
        "unavailable-with-estimate",
        "unavailable-wrong-estimate",
        "unavailable-no-reason",
        "unstable-reason",
    ],
)
def test_contradictory_or_unknown_states_are_rejected(
    values: dict[str, object],
) -> None:
    audit = available_audit()
    kwargs: dict[str, object] = {
        "version": audit.version,
        "status": audit.status,
        "pricing_policy_version": audit.pricing_policy_version,
        "pricing_reference_at": audit.pricing_reference_at,
        "estimate": audit.estimate,
        "unavailable_reason": audit.unavailable_reason,
    }
    kwargs.update(values)

    with pytest.raises(CostAuditError) as captured:
        CostAuditMetadata(**kwargs)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_COST_AUDIT


def test_available_rejects_policy_and_reference_mismatch() -> None:
    audit = available_audit()
    assert audit.estimate is not None

    with pytest.raises(CostAuditError):
        replace(audit, pricing_policy_version="other-policy")
    with pytest.raises(CostAuditError):
        replace(
            audit,
            pricing_reference_at="2026-08-17T02:00:00+00:00",
        )


def test_available_metadata_rejects_unselected_identity_even_when_estimate_matches(
) -> None:
    audit = available_audit()
    assert audit.estimate is not None
    reserved_estimate = replace(
        audit.estimate,
        pricing_policy_version=UNSELECTED_PRICING_POLICY_VERSION,
    )

    with pytest.raises(CostAuditError) as captured:
        replace(
            audit,
            pricing_policy_version=UNSELECTED_PRICING_POLICY_VERSION,
            estimate=reserved_estimate,
        )

    assert captured.value.code == INVALID_COST_AUDIT


@pytest.mark.parametrize(
    "reason",
    [USAGE_UNAVAILABLE, CACHE_BREAKDOWN_UNAVAILABLE],
)
def test_unselected_identity_rejects_non_policy_unavailable_reasons(
    reason: str,
) -> None:
    with pytest.raises(CostAuditError) as captured:
        CostAuditMetadata(
            version=COST_AUDIT_VERSION,
            status=UNAVAILABLE,
            pricing_policy_version=UNSELECTED_PRICING_POLICY_VERSION,
            pricing_reference_at=MONDAY_PEAK.isoformat(),
            estimate=None,
            unavailable_reason=reason,
        )

    assert captured.value.code == INVALID_COST_AUDIT


@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "not-a-time",
        "2026-08-17T01:00:00",
        "2026-08-17T09:00:00+08:00",
    ],
)
def test_metadata_timestamp_must_be_utc_iso(timestamp: str) -> None:
    with pytest.raises(CostAuditError) as captured:
        replace(unavailable_audit(), pricing_reference_at=timestamp)

    assert captured.value.code == INVALID_COST_AUDIT


def test_to_dict_uses_exact_plain_decimal_strings_and_integer_free_schema() -> None:
    payload = available_audit(reference_at=SUNDAY_OFF_PEAK).to_dict()
    estimate = payload["estimate"]

    assert set(payload) == {
        "version",
        "status",
        "pricing_policy_version",
        "pricing_reference_at",
        "estimate",
        "unavailable_reason",
    }
    assert isinstance(estimate, dict)
    assert estimate["prompt_cache_hit_cost"] == "0.0000042"
    assert estimate["prompt_cache_miss_cost"] == "0.000088"
    assert estimate["completion_cost"] == "0.000132"
    assert estimate["total_estimated_cost"] == "0.0002242"
    assert all(
        isinstance(estimate[field], str)
        for field in (
            "prompt_cache_hit_cost",
            "prompt_cache_miss_cost",
            "completion_cost",
            "total_estimated_cost",
        )
    )
    assert not any(
        isinstance(value, float)
        for value in estimate.values()
    )
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload


def test_zero_and_exponent_decimal_serialize_without_scientific_notation() -> None:
    audit = available_audit()
    assert audit.estimate is not None
    estimate = replace(
        audit.estimate,
        prompt_cache_hit_cost=Decimal("1E-9"),
        prompt_cache_miss_cost=Decimal("0E-12"),
        completion_cost=Decimal("0"),
        total_estimated_cost=Decimal("1E-9"),
    )
    replaced = replace(audit, estimate=estimate)

    payload = replaced.to_dict()["estimate"]
    assert isinstance(payload, dict)
    assert payload["prompt_cache_hit_cost"] == "0.000000001"
    assert payload["prompt_cache_miss_cost"] == "0.000000000000"
    assert payload["total_estimated_cost"] == "0.000000001"


def test_to_dict_returns_fresh_nested_estimate_mapping() -> None:
    audit = available_audit()
    first = audit.to_dict()
    second = audit.to_dict()
    first_estimate = first["estimate"]
    second_estimate = second["estimate"]

    assert isinstance(first_estimate, dict)
    assert isinstance(second_estimate, dict)
    assert first_estimate is not second_estimate
    first_estimate["total_estimated_cost"] = "999"
    assert second_estimate["total_estimated_cost"] != "999"
    assert audit.estimate is not None
    assert audit.estimate.total_estimated_cost != Decimal("999")


def test_unavailable_to_dict_is_explicit_and_json_safe() -> None:
    audit = unavailable_audit(CACHE_BREAKDOWN_UNAVAILABLE)

    assert audit.to_dict() == {
        "version": "1",
        "status": "unavailable",
        "pricing_policy_version": (
            DEEPSEEK_FLASH_PRICING_POLICY.version
        ),
        "pricing_reference_at": "2026-08-17T01:00:00+00:00",
        "estimate": None,
        "unavailable_reason": CACHE_BREAKDOWN_UNAVAILABLE,
    }


def test_validation_error_does_not_echo_private_values() -> None:
    with pytest.raises(CostAuditError) as captured:
        replace(
            unavailable_audit(),
            unavailable_reason="SECRET_POLICY_USAGE_DECIMAL_PROMPT",
        )

    rendered = str(captured.value)
    assert "SECRET_POLICY_USAGE_DECIMAL_PROMPT" not in rendered
    assert "ProviderUsage" not in rendered
    assert "PricingPolicy" not in rendered
