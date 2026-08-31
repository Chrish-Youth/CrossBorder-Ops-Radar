"""Versioned, offline pricing policy and deterministic cost estimation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, localcontext

from src.insight_provider import ProviderUsage

COST_ESTIMATE_VERSION = "1"
DEEPSEEK_FLASH_PRICING_POLICY_VERSION = (
    "deepseek-v4-flash-2026-08-16-v1"
)

INVALID_PRICING_INPUT = "INVALID_PRICING_INPUT"
PRICING_POLICY_NOT_APPLICABLE = "PRICING_POLICY_NOT_APPLICABLE"
COST_ESTIMATE_UNAVAILABLE = "COST_ESTIMATE_UNAVAILABLE"

USAGE_UNAVAILABLE = "USAGE_UNAVAILABLE"
CACHE_BREAKDOWN_UNAVAILABLE = "CACHE_BREAKDOWN_UNAVAILABLE"
POLICY_NOT_EFFECTIVE = "POLICY_NOT_EFFECTIVE"
POLICY_NOT_APPLICABLE = "POLICY_NOT_APPLICABLE"

PEAK = "peak"
OFF_PEAK = "off_peak"

_USD = "USD"
_PRICING_UNIT_TOKENS = 1_000_000
_LOG10_2_UPPER_NUMERATOR = 30_103
_LOG10_2_UPPER_DENOMINATOR = 100_000
_MIN_DECIMAL_PRECISION = 50
_DECIMAL_PRECISION_MARGIN = 16


class PricingError(ValueError):
    """A stable pricing-policy or cost-availability failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.message = message
        super().__init__(f"{code}: {message}")


def _pricing_error(
    code: str,
    message: str,
    *,
    reason: str | None = None,
) -> PricingError:
    return PricingError(code, message, reason=reason)


def _validate_nonblank_string(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            f"{field_name} must be a nonblank string.",
        )


def _validate_utc_datetime(value: object, *, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            f"{field_name} must be a timezone-aware UTC datetime.",
        )
    try:
        offset = value.utcoffset()
    except (TypeError, OverflowError, ValueError) as exc:
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            f"{field_name} must be a timezone-aware UTC datetime.",
        ) from exc
    if value.tzinfo is None or offset is None or offset != timedelta(0):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            f"{field_name} must be a timezone-aware UTC datetime.",
        )


def _validate_decimal_amount(value: object, *, field_name: str) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
    ):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            f"{field_name} must be a finite nonnegative Decimal.",
        )


@dataclass(frozen=True)
class TokenPricingRates:
    """USD prices for one policy tier, expressed per million tokens."""

    prompt_cache_hit_usd_per_million: Decimal
    prompt_cache_miss_usd_per_million: Decimal
    completion_usd_per_million: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "prompt_cache_hit_usd_per_million",
            "prompt_cache_miss_usd_per_million",
            "completion_usd_per_million",
        ):
            _validate_decimal_amount(
                getattr(self, field_name),
                field_name=field_name,
            )


def _validate_peak_windows(
    windows: object,
) -> tuple[tuple[time, time], ...]:
    if not isinstance(windows, tuple):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "peak_windows_utc must be a tuple of time pairs.",
        )
    validated: list[tuple[time, time]] = []
    for window in windows:
        if (
            not isinstance(window, tuple)
            or len(window) != 2
            or not all(isinstance(value, time) for value in window)
        ):
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "Each peak window must contain exactly two time values.",
            )
        start, end = window
        if start.tzinfo is not None or end.tzinfo is not None or start >= end:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "Peak windows must be naive UTC clock times with start < end.",
            )
        validated.append((start, end))

    ordered = sorted(validated, key=lambda item: item[0])
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "Peak windows must not overlap.",
            )
    return tuple(validated)


def _validate_peak_weekdays(weekdays: object) -> tuple[int, ...]:
    if (
        not isinstance(weekdays, tuple)
        or not weekdays
        or any(
            isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6
            for day in weekdays
        )
        or len(set(weekdays)) != len(weekdays)
    ):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "peak_weekdays_utc must contain unique weekday integers from 0 to 6.",
        )
    return weekdays


@dataclass(frozen=True)
class PricingPolicy:
    """One immutable, auditable pricing snapshot."""

    version: str
    provider: str
    model: str
    currency: str
    unit_tokens: int
    effective_from_utc: datetime
    verified_at_utc: datetime
    source: str
    peak_weekdays_utc: tuple[int, ...]
    peak_windows_utc: tuple[tuple[time, time], ...]
    peak_rates: TokenPricingRates
    off_peak_rates: TokenPricingRates

    def __post_init__(self) -> None:
        for field_name in ("version", "provider", "model", "source"):
            _validate_nonblank_string(
                getattr(self, field_name),
                field_name=field_name,
            )
        if self.currency != _USD:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "currency must be USD for the current pricing core.",
            )
        if (
            isinstance(self.unit_tokens, bool)
            or not isinstance(self.unit_tokens, int)
            or self.unit_tokens != _PRICING_UNIT_TOKENS
        ):
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "unit_tokens must equal 1,000,000.",
            )
        _validate_utc_datetime(
            self.effective_from_utc,
            field_name="effective_from_utc",
        )
        _validate_utc_datetime(
            self.verified_at_utc,
            field_name="verified_at_utc",
        )
        if self.verified_at_utc < self.effective_from_utc:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "verified_at_utc must not precede effective_from_utc.",
            )
        _validate_peak_weekdays(self.peak_weekdays_utc)
        _validate_peak_windows(self.peak_windows_utc)
        if not isinstance(self.peak_rates, TokenPricingRates) or not isinstance(
            self.off_peak_rates,
            TokenPricingRates,
        ):
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "peak_rates and off_peak_rates must be TokenPricingRates.",
            )


def _exact_decimal_sum(values: tuple[Decimal, ...]) -> Decimal:
    """Add finite nonnegative Decimals without default-context rounding."""

    minimum_exponent = min(value.as_tuple().exponent for value in values)
    coefficient_digits = max(
        len(value.as_tuple().digits)
        + value.as_tuple().exponent
        - minimum_exponent
        for value in values
    )
    with localcontext() as context:
        context.prec = max(
            _MIN_DECIMAL_PRECISION,
            coefficient_digits + len(values) + 2,
        )
        return sum(values, Decimal(0))


@dataclass(frozen=True)
class GenerationCostEstimate:
    """An exact estimate derived from Usage and one explicit pricing policy."""

    version: str
    pricing_policy_version: str
    provider: str
    model: str
    currency: str
    pricing_tier: str
    pricing_reference_at: str
    prompt_cache_hit_cost: Decimal
    prompt_cache_miss_cost: Decimal
    completion_cost: Decimal
    total_estimated_cost: Decimal

    def __post_init__(self) -> None:
        if self.version != COST_ESTIMATE_VERSION:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "Cost estimate version does not match the current contract.",
            )
        for field_name in (
            "pricing_policy_version",
            "provider",
            "model",
            "pricing_reference_at",
        ):
            _validate_nonblank_string(
                getattr(self, field_name),
                field_name=field_name,
            )
        if self.currency != _USD:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "Cost estimate currency must be USD.",
            )
        if self.pricing_tier not in {PEAK, OFF_PEAK}:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "pricing_tier must be peak or off_peak.",
            )
        try:
            reference_at = datetime.fromisoformat(self.pricing_reference_at)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "pricing_reference_at must be a timezone-aware UTC ISO timestamp.",
            ) from exc
        _validate_utc_datetime(
            reference_at,
            field_name="pricing_reference_at",
        )
        components = (
            self.prompt_cache_hit_cost,
            self.prompt_cache_miss_cost,
            self.completion_cost,
        )
        for field_name, value in zip(
            (
                "prompt_cache_hit_cost",
                "prompt_cache_miss_cost",
                "completion_cost",
                "total_estimated_cost",
            ),
            (*components, self.total_estimated_cost),
        ):
            _validate_decimal_amount(value, field_name=field_name)
        if self.total_estimated_cost != _exact_decimal_sum(components):
            raise _pricing_error(
                INVALID_PRICING_INPUT,
                "total_estimated_cost must equal the exact component sum.",
            )


DEEPSEEK_FLASH_PRICING_POLICY = PricingPolicy(
    version=DEEPSEEK_FLASH_PRICING_POLICY_VERSION,
    provider="deepseek",
    model="deepseek-v4-flash",
    currency=_USD,
    unit_tokens=_PRICING_UNIT_TOKENS,
    effective_from_utc=datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc),
    verified_at_utc=datetime(2026, 8, 30, 4, 50, 16, tzinfo=timezone.utc),
    source="https://api-docs.deepseek.com/quick_start/pricing/",
    peak_weekdays_utc=(0, 1, 2, 3, 4),
    peak_windows_utc=((time(1), time(4)), (time(6), time(10))),
    peak_rates=TokenPricingRates(
        prompt_cache_hit_usd_per_million=Decimal("0.014"),
        prompt_cache_miss_usd_per_million=Decimal("0.44"),
        completion_usd_per_million=Decimal("1.32"),
    ),
    off_peak_rates=TokenPricingRates(
        prompt_cache_hit_usd_per_million=Decimal("0.007"),
        prompt_cache_miss_usd_per_million=Decimal("0.22"),
        completion_usd_per_million=Decimal("0.66"),
    ),
)


def _normalize_pricing_reference(
    policy: PricingPolicy,
    occurred_at: datetime,
) -> datetime:
    if not isinstance(policy, PricingPolicy):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "policy must be a PricingPolicy.",
        )
    if not isinstance(occurred_at, datetime):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "occurred_at must be a timezone-aware datetime.",
        )
    try:
        offset = occurred_at.utcoffset()
    except (TypeError, OverflowError, ValueError) as exc:
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "occurred_at must be a timezone-aware datetime.",
        ) from exc
    if occurred_at.tzinfo is None or offset is None:
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "occurred_at must be a timezone-aware datetime.",
        )
    try:
        occurred_at_utc = occurred_at.astimezone(timezone.utc)
    except (TypeError, OverflowError, ValueError) as exc:
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "occurred_at must be a timezone-aware datetime.",
        ) from exc
    if occurred_at_utc < policy.effective_from_utc:
        raise _pricing_error(
            PRICING_POLICY_NOT_APPLICABLE,
            "The pricing policy was not effective at occurred_at.",
            reason=POLICY_NOT_EFFECTIVE,
        )
    return occurred_at_utc


def resolve_pricing_tier(
    policy: PricingPolicy,
    *,
    occurred_at: datetime,
) -> str:
    """Resolve peak/off-peak by the UTC instant and half-open policy windows."""

    occurred_at_utc = _normalize_pricing_reference(policy, occurred_at)
    utc_clock = occurred_at_utc.time()
    if occurred_at_utc.weekday() in policy.peak_weekdays_utc and any(
        start <= utc_clock < end for start, end in policy.peak_windows_utc
    ):
        return PEAK
    return OFF_PEAK


def _integer_decimal_digits_upper_bound(value: int) -> int:
    if value == 0:
        return 1
    return (
        value.bit_length() * _LOG10_2_UPPER_NUMERATOR
        + _LOG10_2_UPPER_DENOMINATOR
        - 1
    ) // _LOG10_2_UPPER_DENOMINATOR


def _cost_precision(
    token_rate_pairs: tuple[tuple[int, Decimal], ...],
) -> int:
    required_digits = max(
        _integer_decimal_digits_upper_bound(token_count)
        + len(rate.as_tuple().digits)
        for token_count, rate in token_rate_pairs
    )
    return max(
        _MIN_DECIMAL_PRECISION,
        required_digits + _DECIMAL_PRECISION_MARGIN,
    )


def estimate_generation_cost(
    usage: ProviderUsage | None,
    *,
    provider: str,
    model: str,
    occurred_at: datetime,
    policy: PricingPolicy = DEEPSEEK_FLASH_PRICING_POLICY,
) -> GenerationCostEstimate:
    """Estimate one generation cost without network, clock reads, or rounding."""

    if not isinstance(policy, PricingPolicy):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "policy must be a PricingPolicy.",
        )
    for field_name, value in (("provider", provider), ("model", model)):
        _validate_nonblank_string(value, field_name=field_name)
    if provider != policy.provider or model != policy.model:
        raise _pricing_error(
            PRICING_POLICY_NOT_APPLICABLE,
            "The pricing policy does not apply to this provider and model.",
            reason=POLICY_NOT_APPLICABLE,
        )

    occurred_at_utc = _normalize_pricing_reference(policy, occurred_at)
    pricing_tier = resolve_pricing_tier(policy, occurred_at=occurred_at_utc)
    if usage is None:
        raise _pricing_error(
            COST_ESTIMATE_UNAVAILABLE,
            "Provider usage is unavailable.",
            reason=USAGE_UNAVAILABLE,
        )
    if not isinstance(usage, ProviderUsage):
        raise _pricing_error(
            INVALID_PRICING_INPUT,
            "usage must be ProviderUsage or None.",
        )
    cache_hit_tokens = usage.prompt_cache_hit_tokens
    cache_miss_tokens = usage.prompt_cache_miss_tokens
    if cache_hit_tokens is None or cache_miss_tokens is None:
        raise _pricing_error(
            COST_ESTIMATE_UNAVAILABLE,
            "A complete prompt cache hit/miss breakdown is unavailable.",
            reason=CACHE_BREAKDOWN_UNAVAILABLE,
        )

    rates = (
        policy.peak_rates if pricing_tier == PEAK else policy.off_peak_rates
    )
    with localcontext() as context:
        context.prec = _cost_precision(
            (
                (
                    cache_hit_tokens,
                    rates.prompt_cache_hit_usd_per_million,
                ),
                (
                    cache_miss_tokens,
                    rates.prompt_cache_miss_usd_per_million,
                ),
                (
                    usage.completion_tokens,
                    rates.completion_usd_per_million,
                ),
            )
        )
        unit = Decimal(policy.unit_tokens)
        prompt_cache_hit_cost = (
            Decimal(cache_hit_tokens)
            * rates.prompt_cache_hit_usd_per_million
            / unit
        )
        prompt_cache_miss_cost = (
            Decimal(cache_miss_tokens)
            * rates.prompt_cache_miss_usd_per_million
            / unit
        )
        completion_cost = (
            Decimal(usage.completion_tokens)
            * rates.completion_usd_per_million
            / unit
        )
        total_estimated_cost = _exact_decimal_sum(
            (
                prompt_cache_hit_cost,
                prompt_cache_miss_cost,
                completion_cost,
            )
        )

    return GenerationCostEstimate(
        version=COST_ESTIMATE_VERSION,
        pricing_policy_version=policy.version,
        provider=provider,
        model=model,
        currency=policy.currency,
        pricing_tier=pricing_tier,
        pricing_reference_at=occurred_at_utc.isoformat(),
        prompt_cache_hit_cost=prompt_cache_hit_cost,
        prompt_cache_miss_cost=prompt_cache_miss_cost,
        completion_cost=completion_cost,
        total_estimated_cost=total_estimated_cost,
    )
