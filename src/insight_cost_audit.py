"""Build immutable generation-level audit metadata for cost estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    COST_ESTIMATE_UNAVAILABLE,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    PRICING_POLICY_NOT_APPLICABLE,
    USAGE_UNAVAILABLE,
    GenerationCostEstimate,
    PricingError,
    PricingPolicy,
    estimate_generation_cost,
)
from src.insight_pricing_catalog import (
    DEFAULT_PRICING_POLICY_CATALOG,
    UNSELECTED_PRICING_POLICY_VERSION,
    PricingPolicyCatalog,
    select_pricing_policy,
)
from src.insight_provider import ProviderUsage

COST_AUDIT_VERSION = "1"
INVALID_COST_AUDIT = "INVALID_COST_AUDIT"

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

_UNAVAILABLE_REASONS = frozenset(
    {
        USAGE_UNAVAILABLE,
        CACHE_BREAKDOWN_UNAVAILABLE,
        POLICY_NOT_EFFECTIVE,
        POLICY_NOT_APPLICABLE,
    }
)


class CostAuditError(ValueError):
    """A stable failure at the generation cost-audit boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_cost_audit(message: str) -> CostAuditError:
    return CostAuditError(INVALID_COST_AUDIT, message)


def _validate_nonblank_string(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_cost_audit(f"{field_name} must be a nonblank string.")


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_cost_audit(
            "pricing_reference_at must be a timezone-aware UTC ISO timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_cost_audit(
            "pricing_reference_at must be a timezone-aware UTC ISO timestamp."
        ) from exc
    if parsed.tzinfo is None or offset != timedelta(0):
        raise _invalid_cost_audit(
            "pricing_reference_at must be a timezone-aware UTC ISO timestamp."
        )
    return parsed


def _normalize_reference_at(value: object) -> str:
    if not isinstance(value, datetime):
        raise _invalid_cost_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        )
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_cost_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        ) from exc
    if value.tzinfo is None or offset is None:
        raise _invalid_cost_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        )
    try:
        return value.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_cost_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        ) from exc


def _decimal_string(value: Decimal) -> str:
    """Return one exact plain-decimal JSON representation without rounding."""

    return format(value, "f")


def _estimate_to_dict(estimate: GenerationCostEstimate) -> dict[str, Any]:
    """Serialize the sealed estimate contract without floats or reflection."""

    return {
        "version": estimate.version,
        "pricing_policy_version": estimate.pricing_policy_version,
        "provider": estimate.provider,
        "model": estimate.model,
        "currency": estimate.currency,
        "pricing_tier": estimate.pricing_tier,
        "pricing_reference_at": estimate.pricing_reference_at,
        "prompt_cache_hit_cost": _decimal_string(
            estimate.prompt_cache_hit_cost
        ),
        "prompt_cache_miss_cost": _decimal_string(
            estimate.prompt_cache_miss_cost
        ),
        "completion_cost": _decimal_string(estimate.completion_cost),
        "total_estimated_cost": _decimal_string(
            estimate.total_estimated_cost
        ),
    }


@dataclass(frozen=True)
class CostAuditMetadata:
    """Immutable result of one explicit cost-estimation attempt."""

    version: str
    status: str
    pricing_policy_version: str
    pricing_reference_at: str
    estimate: GenerationCostEstimate | None
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        if self.version != COST_AUDIT_VERSION:
            raise _invalid_cost_audit(
                "Cost audit version does not match the current contract."
            )
        if self.status not in {AVAILABLE, UNAVAILABLE}:
            raise _invalid_cost_audit(
                "status must be available or unavailable."
            )
        _validate_nonblank_string(
            self.pricing_policy_version,
            field_name="pricing_policy_version",
        )
        _parse_utc_timestamp(self.pricing_reference_at)

        if self.status == AVAILABLE:
            if (
                self.pricing_policy_version
                == UNSELECTED_PRICING_POLICY_VERSION
            ):
                raise _invalid_cost_audit(
                    "available cost audit cannot use the unselected identity."
                )
            if not isinstance(self.estimate, GenerationCostEstimate):
                raise _invalid_cost_audit(
                    "available cost audit requires a GenerationCostEstimate."
                )
            if self.unavailable_reason is not None:
                raise _invalid_cost_audit(
                    "available cost audit cannot have unavailable_reason."
                )
            if (
                self.pricing_policy_version
                != self.estimate.pricing_policy_version
            ):
                raise _invalid_cost_audit(
                    "Cost audit and estimate pricing policy versions differ."
                )
            if self.pricing_reference_at != self.estimate.pricing_reference_at:
                raise _invalid_cost_audit(
                    "Cost audit and estimate pricing reference times differ."
                )
            return

        if self.estimate is not None:
            raise _invalid_cost_audit(
                "unavailable cost audit cannot contain an estimate."
            )
        if self.unavailable_reason not in _UNAVAILABLE_REASONS:
            raise _invalid_cost_audit(
                "unavailable cost audit requires a stable unavailable reason."
            )
        if (
            self.pricing_policy_version == UNSELECTED_PRICING_POLICY_VERSION
            and self.unavailable_reason
            not in {POLICY_NOT_EFFECTIVE, POLICY_NOT_APPLICABLE}
        ):
            raise _invalid_cost_audit(
                "unselected identity requires a no-policy unavailable reason."
            )

    def to_dict(self) -> dict[str, Any]:
        """Return an explicit, fresh, JSON-safe public representation."""

        return {
            "version": self.version,
            "status": self.status,
            "pricing_policy_version": self.pricing_policy_version,
            "pricing_reference_at": self.pricing_reference_at,
            "estimate": (
                None
                if self.estimate is None
                else _estimate_to_dict(self.estimate)
            ),
            "unavailable_reason": self.unavailable_reason,
        }


def build_cost_audit_metadata(
    usage: ProviderUsage | None,
    *,
    provider: str,
    model: str,
    pricing_reference_at: datetime,
    policy: PricingPolicy | None = None,
    catalog: PricingPolicyCatalog | None = None,
) -> CostAuditMetadata:
    """Select or accept one snapshot, then derive immutable cost metadata."""

    selected_policy: PricingPolicy | None = policy
    if (
        isinstance(selected_policy, PricingPolicy)
        and selected_policy.version == UNSELECTED_PRICING_POLICY_VERSION
    ):
        raise _invalid_cost_audit(
            "Explicit pricing policy uses a reserved audit identity."
        )
    try:
        if selected_policy is None:
            active_catalog = (
                DEFAULT_PRICING_POLICY_CATALOG
                if catalog is None
                else catalog
            )
            selected_policy = select_pricing_policy(
                provider=provider,
                model=model,
                pricing_reference_at=pricing_reference_at,
                catalog=active_catalog,
            )
        estimate = estimate_generation_cost(
            usage,
            provider=provider,
            model=model,
            occurred_at=pricing_reference_at,
            policy=selected_policy,
        )
    except PricingError as error:
        if (
            error.code
            not in {
                COST_ESTIMATE_UNAVAILABLE,
                PRICING_POLICY_NOT_APPLICABLE,
            }
            or error.reason not in _UNAVAILABLE_REASONS
        ):
            raise
        reference_at = _normalize_reference_at(pricing_reference_at)
        return CostAuditMetadata(
            version=COST_AUDIT_VERSION,
            status=UNAVAILABLE,
            pricing_policy_version=(
                UNSELECTED_PRICING_POLICY_VERSION
                if selected_policy is None
                else selected_policy.version
            ),
            pricing_reference_at=reference_at,
            estimate=None,
            unavailable_reason=error.reason,
        )

    return CostAuditMetadata(
        version=COST_AUDIT_VERSION,
        status=AVAILABLE,
        pricing_policy_version=estimate.pricing_policy_version,
        pricing_reference_at=estimate.pricing_reference_at,
        estimate=estimate,
        unavailable_reason=None,
    )
