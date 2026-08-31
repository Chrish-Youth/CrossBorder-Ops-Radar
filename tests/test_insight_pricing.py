from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, getcontext

import pytest

from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    COST_ESTIMATE_UNAVAILABLE,
    COST_ESTIMATE_VERSION,
    DEEPSEEK_FLASH_PRICING_POLICY,
    DEEPSEEK_FLASH_PRICING_POLICY_VERSION,
    INVALID_PRICING_INPUT,
    OFF_PEAK,
    PEAK,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    PRICING_POLICY_NOT_APPLICABLE,
    USAGE_UNAVAILABLE,
    GenerationCostEstimate,
    PricingError,
    PricingPolicy,
    TokenPricingRates,
    estimate_generation_cost,
    resolve_pricing_tier,
)
from src.insight_provider import ProviderUsage


UTC = timezone.utc
SINGAPORE = timezone(timedelta(hours=8))
MONDAY = (2026, 8, 17)
OFF_PEAK_AT = datetime(*MONDAY, 12, 0, tzinfo=UTC)
PEAK_AT = datetime(*MONDAY, 1, 0, tzinfo=UTC)


def usage(
    *,
    cache_hit: int,
    cache_miss: int,
    completion: int,
    reasoning: int | None = None,
) -> ProviderUsage:
    prompt = cache_hit + cache_miss
    return ProviderUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_cache_hit_tokens=cache_hit,
        prompt_cache_miss_tokens=cache_miss,
        reasoning_tokens=reasoning,
    )


def estimate(
    provider_usage: ProviderUsage | None,
    *,
    occurred_at: datetime = OFF_PEAK_AT,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    policy: PricingPolicy = DEEPSEEK_FLASH_PRICING_POLICY,
) -> GenerationCostEstimate:
    return estimate_generation_cost(
        provider_usage,
        provider=provider,
        model=model,
        occurred_at=occurred_at,
        policy=policy,
    )


def test_current_policy_snapshot_is_complete_versioned_and_immutable() -> None:
    policy = DEEPSEEK_FLASH_PRICING_POLICY

    assert policy.version == DEEPSEEK_FLASH_PRICING_POLICY_VERSION
    assert policy.version == "deepseek-v4-flash-2026-08-16-v1"
    assert policy.provider == "deepseek"
    assert policy.model == "deepseek-v4-flash"
    assert policy.currency == "USD"
    assert policy.unit_tokens == 1_000_000
    assert policy.effective_from_utc == datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
    assert policy.verified_at_utc == datetime(
        2026,
        8,
        30,
        4,
        50,
        16,
        tzinfo=UTC,
    )
    assert policy.source == (
        "https://api-docs.deepseek.com/quick_start/pricing/"
    )
    assert policy.peak_weekdays_utc == (0, 1, 2, 3, 4)
    assert policy.peak_windows_utc == (
        (time(1), time(4)),
        (time(6), time(10)),
    )
    with pytest.raises(FrozenInstanceError):
        policy.currency = "CNY"  # type: ignore[misc]


def test_current_rates_are_explicit_decimals_and_not_derived() -> None:
    assert DEEPSEEK_FLASH_PRICING_POLICY.off_peak_rates == TokenPricingRates(
        prompt_cache_hit_usd_per_million=Decimal("0.007"),
        prompt_cache_miss_usd_per_million=Decimal("0.22"),
        completion_usd_per_million=Decimal("0.66"),
    )
    assert DEEPSEEK_FLASH_PRICING_POLICY.peak_rates == TokenPricingRates(
        prompt_cache_hit_usd_per_million=Decimal("0.014"),
        prompt_cache_miss_usd_per_million=Decimal("0.44"),
        completion_usd_per_million=Decimal("1.32"),
    )
    with pytest.raises(FrozenInstanceError):
        DEEPSEEK_FLASH_PRICING_POLICY.peak_rates.completion_usd_per_million = (
            Decimal("9")
        )  # type: ignore[misc]


@pytest.mark.parametrize(
    "invalid",
    [
        Decimal("-0.001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        0.007,
        True,
        "0.007",
    ],
    ids=["negative", "nan", "inf", "negative-inf", "float", "bool", "str"],
)
def test_rates_reject_non_decimal_nonfinite_and_negative_values(
    invalid: object,
) -> None:
    with pytest.raises(PricingError) as captured:
        TokenPricingRates(
            prompt_cache_hit_usd_per_million=invalid,  # type: ignore[arg-type]
            prompt_cache_miss_usd_per_million=Decimal("0.22"),
            completion_usd_per_million=Decimal("0.66"),
        )

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    "field_name",
    [
        "prompt_cache_hit_usd_per_million",
        "prompt_cache_miss_usd_per_million",
        "completion_usd_per_million",
    ],
)
def test_every_rate_field_enforces_decimal_invariants(field_name: str) -> None:
    values = {
        "prompt_cache_hit_usd_per_million": Decimal("0.007"),
        "prompt_cache_miss_usd_per_million": Decimal("0.22"),
        "completion_usd_per_million": Decimal("0.66"),
    }
    values[field_name] = Decimal("-1")

    with pytest.raises(PricingError) as captured:
        TokenPricingRates(**values)

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize("field_name", ["version", "provider", "model", "source"])
def test_policy_rejects_blank_identity_and_provenance(field_name: str) -> None:
    with pytest.raises(PricingError) as captured:
        replace(DEEPSEEK_FLASH_PRICING_POLICY, **{field_name: "  "})

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize("unit", [0, -1, True, 1.0, 1000])
def test_policy_rejects_invalid_or_nonmillion_unit(unit: object) -> None:
    with pytest.raises(PricingError) as captured:
        replace(DEEPSEEK_FLASH_PRICING_POLICY, unit_tokens=unit)

    assert captured.value.code == INVALID_PRICING_INPUT


def test_policy_rejects_non_usd_currency() -> None:
    with pytest.raises(PricingError) as captured:
        replace(DEEPSEEK_FLASH_PRICING_POLICY, currency="CNY")

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("effective_from_utc", datetime(2026, 8, 16, 16, 0)),
        ("verified_at_utc", datetime(2026, 8, 30, 4, 50, 16)),
        (
            "effective_from_utc",
            datetime(2026, 8, 17, 0, 0, tzinfo=SINGAPORE),
        ),
    ],
)
def test_policy_timestamps_must_be_timezone_aware_utc(
    field_name: str,
    value: datetime,
) -> None:
    with pytest.raises(PricingError) as captured:
        replace(DEEPSEEK_FLASH_PRICING_POLICY, **{field_name: value})

    assert captured.value.code == INVALID_PRICING_INPUT


def test_policy_verified_time_cannot_precede_effective_time() -> None:
    with pytest.raises(PricingError) as captured:
        replace(
            DEEPSEEK_FLASH_PRICING_POLICY,
            verified_at_utc=datetime(2026, 8, 16, 15, 59, 59, tzinfo=UTC),
        )

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    "windows",
    [
        ((time(4), time(4)),),
        ((time(5), time(4)),),
        ((time(1), time(4)), (time(3), time(5))),
        ((time(1, tzinfo=UTC), time(4, tzinfo=UTC)),),
        [(time(1), time(4))],
        ((time(1),),),
    ],
    ids=["equal", "reversed", "overlap", "aware-time", "list", "malformed"],
)
def test_policy_rejects_invalid_peak_windows(windows: object) -> None:
    with pytest.raises(PricingError) as captured:
        replace(DEEPSEEK_FLASH_PRICING_POLICY, peak_windows_utc=windows)

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    "weekdays",
    [(), [0, 1], (0, 0), (-1,), (7,), (True,)],
)
def test_policy_rejects_invalid_peak_weekdays(weekdays: object) -> None:
    with pytest.raises(PricingError) as captured:
        replace(DEEPSEEK_FLASH_PRICING_POLICY, peak_weekdays_utc=weekdays)

    assert captured.value.code == INVALID_PRICING_INPUT


def test_policy_rejects_non_rate_nested_objects() -> None:
    with pytest.raises(PricingError) as captured:
        replace(DEEPSEEK_FLASH_PRICING_POLICY, peak_rates=object())

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ((0, 59, 59), OFF_PEAK),
        ((1, 0, 0), PEAK),
        ((3, 59, 59), PEAK),
        ((4, 0, 0), OFF_PEAK),
        ((5, 59, 59), OFF_PEAK),
        ((6, 0, 0), PEAK),
        ((9, 59, 59), PEAK),
        ((10, 0, 0), OFF_PEAK),
    ],
)
def test_tier_resolver_uses_half_open_utc_boundaries(
    clock: tuple[int, int, int],
    expected: str,
) -> None:
    occurred_at = datetime(*MONDAY, *clock, tzinfo=UTC)

    assert resolve_pricing_tier(
        DEEPSEEK_FLASH_PRICING_POLICY,
        occurred_at=occurred_at,
    ) == expected


def test_peak_clock_is_off_peak_on_weekends() -> None:
    saturday_peak_clock = datetime(2026, 8, 22, 1, 0, tzinfo=UTC)

    assert resolve_pricing_tier(
        DEEPSEEK_FLASH_PRICING_POLICY,
        occurred_at=saturday_peak_clock,
    ) == OFF_PEAK


def test_non_utc_aware_datetime_is_resolved_by_instant() -> None:
    singapore_nine_am = datetime(2026, 8, 17, 9, 0, tzinfo=SINGAPORE)

    assert resolve_pricing_tier(
        DEEPSEEK_FLASH_PRICING_POLICY,
        occurred_at=singapore_nine_am,
    ) == PEAK


@pytest.mark.parametrize("occurred_at", [datetime(2026, 8, 17, 1, 0), None])
def test_tier_resolver_rejects_naive_or_non_datetime(
    occurred_at: object,
) -> None:
    with pytest.raises(PricingError) as captured:
        resolve_pricing_tier(
            DEEPSEEK_FLASH_PRICING_POLICY,
            occurred_at=occurred_at,  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_PRICING_INPUT


def test_tier_resolver_rejects_time_before_policy_effective_from() -> None:
    with pytest.raises(PricingError) as captured:
        resolve_pricing_tier(
            DEEPSEEK_FLASH_PRICING_POLICY,
            occurred_at=datetime(2026, 8, 16, 15, 59, 59, tzinfo=UTC),
        )

    assert captured.value.code == PRICING_POLICY_NOT_APPLICABLE
    assert captured.value.reason == POLICY_NOT_EFFECTIVE


def test_policy_is_applicable_at_exact_effective_instant() -> None:
    assert resolve_pricing_tier(
        DEEPSEEK_FLASH_PRICING_POLICY,
        occurred_at=datetime(2026, 8, 16, 16, 0, tzinfo=UTC),
    ) == OFF_PEAK


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("other", "deepseek-v4-flash"),
        ("deepseek", "deepseek-v4-pro"),
    ],
)
def test_estimator_rejects_provider_or_model_policy_mismatch(
    provider: str,
    model: str,
) -> None:
    with pytest.raises(PricingError) as captured:
        estimate(
            usage(cache_hit=0, cache_miss=0, completion=0),
            provider=provider,
            model=model,
        )

    assert captured.value.code == PRICING_POLICY_NOT_APPLICABLE
    assert captured.value.reason == POLICY_NOT_APPLICABLE


def test_usage_none_is_normal_explicit_unavailable_state() -> None:
    with pytest.raises(PricingError) as captured:
        estimate(None)

    assert captured.value.code == COST_ESTIMATE_UNAVAILABLE
    assert captured.value.reason == USAGE_UNAVAILABLE


def test_missing_cache_breakdown_does_not_assume_hit_or_miss() -> None:
    provider_usage = ProviderUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
    )

    with pytest.raises(PricingError) as captured:
        estimate(provider_usage)

    assert captured.value.code == COST_ESTIMATE_UNAVAILABLE
    assert captured.value.reason == CACHE_BREAKDOWN_UNAVAILABLE


def test_estimator_rejects_wrong_usage_type() -> None:
    with pytest.raises(PricingError) as captured:
        estimate_generation_cost(
            object(),  # type: ignore[arg-type]
            provider="deepseek",
            model="deepseek-v4-flash",
            occurred_at=OFF_PEAK_AT,
        )

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    ("provider_usage", "components"),
    [
        (
            usage(cache_hit=1_000_000, cache_miss=0, completion=0),
            (Decimal("0.007"), Decimal("0"), Decimal("0")),
        ),
        (
            usage(cache_hit=0, cache_miss=1_000_000, completion=0),
            (Decimal("0"), Decimal("0.22"), Decimal("0")),
        ),
        (
            usage(cache_hit=0, cache_miss=0, completion=1_000_000),
            (Decimal("0"), Decimal("0"), Decimal("0.66")),
        ),
    ],
    ids=["cache-hit", "cache-miss", "completion"],
)
def test_off_peak_component_prices_match_independent_oracles(
    provider_usage: ProviderUsage,
    components: tuple[Decimal, Decimal, Decimal],
) -> None:
    result = estimate(provider_usage)

    assert result.pricing_tier == OFF_PEAK
    assert (
        result.prompt_cache_hit_cost,
        result.prompt_cache_miss_cost,
        result.completion_cost,
    ) == components
    assert result.total_estimated_cost == sum(components, Decimal(0))


@pytest.mark.parametrize(
    ("provider_usage", "components"),
    [
        (
            usage(cache_hit=1_000_000, cache_miss=0, completion=0),
            (Decimal("0.014"), Decimal("0"), Decimal("0")),
        ),
        (
            usage(cache_hit=0, cache_miss=1_000_000, completion=0),
            (Decimal("0"), Decimal("0.44"), Decimal("0")),
        ),
        (
            usage(cache_hit=0, cache_miss=0, completion=1_000_000),
            (Decimal("0"), Decimal("0"), Decimal("1.32")),
        ),
    ],
    ids=["cache-hit", "cache-miss", "completion"],
)
def test_peak_component_prices_match_independent_oracles(
    provider_usage: ProviderUsage,
    components: tuple[Decimal, Decimal, Decimal],
) -> None:
    result = estimate(provider_usage, occurred_at=PEAK_AT)

    assert result.pricing_tier == PEAK
    assert (
        result.prompt_cache_hit_cost,
        result.prompt_cache_miss_cost,
        result.completion_cost,
    ) == components
    assert result.total_estimated_cost == sum(components, Decimal(0))


def test_mixed_off_peak_cost_uses_each_component_once() -> None:
    result = estimate(usage(cache_hit=600, cache_miss=400, completion=200))

    assert result.prompt_cache_hit_cost == Decimal("0.0000042")
    assert result.prompt_cache_miss_cost == Decimal("0.000088")
    assert result.completion_cost == Decimal("0.000132")
    assert result.total_estimated_cost == Decimal("0.0002242")


def test_one_token_cost_is_exact_and_not_rounded() -> None:
    result = estimate(usage(cache_hit=1, cache_miss=0, completion=0))

    assert result.prompt_cache_hit_cost == Decimal("0.000000007")
    assert result.total_estimated_cost == Decimal("0.000000007")
    assert isinstance(result.total_estimated_cost, Decimal)


def test_custom_high_precision_decimal_rate_is_not_silently_rounded() -> None:
    long_rate = Decimal(
        "123456789012345678901234567890123456789012345678901234567890"
    )
    custom_policy = replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version="high-precision-rate-test",
        off_peak_rates=TokenPricingRates(
            prompt_cache_hit_usd_per_million=long_rate,
            prompt_cache_miss_usd_per_million=Decimal(0),
            completion_usd_per_million=Decimal(0),
        ),
    )
    expected = Decimal(
        (
            0,
            long_rate.as_tuple().digits,
            long_rate.as_tuple().exponent - 6,
        )
    )

    result = estimate(
        usage(cache_hit=1, cache_miss=0, completion=0),
        policy=custom_policy,
    )

    assert result.prompt_cache_hit_cost == expected
    assert result.total_estimated_cost == expected


def test_complete_zero_usage_produces_valid_zero_estimate() -> None:
    result = estimate(usage(cache_hit=0, cache_miss=0, completion=0))

    assert result.prompt_cache_hit_cost == Decimal(0)
    assert result.prompt_cache_miss_cost == Decimal(0)
    assert result.completion_cost == Decimal(0)
    assert result.total_estimated_cost == Decimal(0)


def test_reasoning_tokens_are_not_billed_again() -> None:
    results = [
        estimate(
            usage(
                cache_hit=2,
                cache_miss=3,
                completion=5,
                reasoning=reasoning,
            )
        )
        for reasoning in (None, 0, 5)
    ]

    assert len({result.prompt_cache_hit_cost for result in results}) == 1
    assert len({result.prompt_cache_miss_cost for result in results}) == 1
    assert len({result.completion_cost for result in results}) == 1
    assert len({result.total_estimated_cost for result in results}) == 1


def test_very_large_usage_is_exact_without_default_decimal_rounding() -> None:
    huge = 10**100
    result = estimate(usage(cache_hit=huge, cache_miss=0, completion=0))

    expected = Decimal(7).scaleb(91)
    assert result.prompt_cache_hit_cost == expected
    assert result.total_estimated_cost == expected


def test_10_to_5000_usage_is_deterministic_and_exact() -> None:
    huge = 10**5000
    integer_conversion_limit_before = sys.get_int_max_str_digits()
    decimal_precision_before = getcontext().prec
    provider_usage = usage(
        cache_hit=huge,
        cache_miss=huge,
        completion=huge,
    )

    first = estimate(provider_usage)
    second = estimate(provider_usage)

    assert first == second
    assert first.prompt_cache_hit_cost == Decimal(7).scaleb(4991)
    assert first.prompt_cache_miss_cost == Decimal(22).scaleb(4992)
    assert first.completion_cost == Decimal(66).scaleb(4992)
    assert first.total_estimated_cost == Decimal(887).scaleb(4991)
    assert sys.get_int_max_str_digits() == integer_conversion_limit_before
    assert getcontext().prec == decimal_precision_before


def test_estimate_records_versions_identity_tier_and_utc_reference() -> None:
    singapore_nine_am = datetime(2026, 8, 17, 9, 0, tzinfo=SINGAPORE)
    result = estimate(
        usage(cache_hit=0, cache_miss=0, completion=0),
        occurred_at=singapore_nine_am,
    )

    assert result.version == COST_ESTIMATE_VERSION == "1"
    assert result.pricing_policy_version == (
        DEEPSEEK_FLASH_PRICING_POLICY_VERSION
    )
    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-flash"
    assert result.currency == "USD"
    assert result.pricing_tier == PEAK
    assert result.pricing_reference_at == "2026-08-17T01:00:00+00:00"
    with pytest.raises(FrozenInstanceError):
        result.currency = "CNY"  # type: ignore[misc]


def test_explicit_policy_override_controls_rates_and_is_auditable() -> None:
    custom_policy = replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version="deepseek-v4-flash-test-policy",
        off_peak_rates=TokenPricingRates(
            prompt_cache_hit_usd_per_million=Decimal("1"),
            prompt_cache_miss_usd_per_million=Decimal("2"),
            completion_usd_per_million=Decimal("3"),
        ),
    )

    result = estimate(
        usage(cache_hit=1_000_000, cache_miss=1_000_000, completion=1_000_000),
        policy=custom_policy,
    )

    assert result.pricing_policy_version == "deepseek-v4-flash-test-policy"
    assert result.prompt_cache_hit_cost == Decimal("1")
    assert result.prompt_cache_miss_cost == Decimal("2")
    assert result.completion_cost == Decimal("3")
    assert result.total_estimated_cost == Decimal("6")


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    [
        ("prompt_cache_hit_cost", Decimal("-1")),
        ("prompt_cache_miss_cost", Decimal("NaN")),
        ("completion_cost", Decimal("Infinity")),
        ("total_estimated_cost", 1.0),
    ],
)
def test_estimate_rejects_invalid_decimal_amounts(
    field_name: str,
    invalid: object,
) -> None:
    result = estimate(usage(cache_hit=0, cache_miss=0, completion=0))

    with pytest.raises(PricingError) as captured:
        replace(result, **{field_name: invalid})

    assert captured.value.code == INVALID_PRICING_INPUT


def test_estimate_rejects_total_that_is_not_exact_component_sum() -> None:
    result = estimate(usage(cache_hit=1, cache_miss=1, completion=1))

    with pytest.raises(PricingError) as captured:
        replace(
            result,
            total_estimated_cost=result.total_estimated_cost + Decimal("1"),
        )

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    [
        ("version", "future"),
        ("pricing_policy_version", ""),
        ("pricing_tier", "normal"),
        ("currency", "CNY"),
        ("pricing_reference_at", "not-a-time"),
        ("pricing_reference_at", "2026-08-17T01:00:00"),
    ],
)
def test_estimate_rejects_invalid_contract_metadata(
    field_name: str,
    invalid: object,
) -> None:
    result = estimate(usage(cache_hit=0, cache_miss=0, completion=0))

    with pytest.raises(PricingError) as captured:
        replace(result, **{field_name: invalid})

    assert captured.value.code == INVALID_PRICING_INPUT
