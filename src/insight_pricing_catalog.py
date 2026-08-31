"""Immutable pricing-policy catalog and deterministic historical selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.insight_pricing import (
    DEEPSEEK_FLASH_PRICING_POLICY,
    INVALID_PRICING_INPUT,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    PRICING_POLICY_NOT_APPLICABLE,
    PricingError,
    PricingPolicy,
)

INVALID_PRICING_CATALOG = "INVALID_PRICING_CATALOG"
UNSELECTED_PRICING_POLICY_VERSION = "unselected"


class PricingCatalogError(ValueError):
    """A stable pricing-catalog configuration failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_catalog(message: str) -> PricingCatalogError:
    return PricingCatalogError(INVALID_PRICING_CATALOG, message)


@dataclass(frozen=True)
class PricingPolicyCatalog:
    """An immutable collection of auditable pricing snapshots."""

    policies: tuple[PricingPolicy, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policies, tuple):
            raise _invalid_catalog("policies must be a tuple.")
        if any(not isinstance(policy, PricingPolicy) for policy in self.policies):
            raise _invalid_catalog(
                "Every catalog entry must be a PricingPolicy."
            )
        if any(
            policy.version == UNSELECTED_PRICING_POLICY_VERSION
            for policy in self.policies
        ):
            raise _invalid_catalog(
                "PricingPolicy.version uses a reserved audit identity."
            )

        versions = [policy.version for policy in self.policies]
        if len(versions) != len(set(versions)):
            raise _invalid_catalog(
                "PricingPolicy.version must be globally unique in a catalog."
            )

        effective_keys = [
            (
                policy.provider,
                policy.model,
                policy.effective_from_utc,
            )
            for policy in self.policies
        ]
        if len(effective_keys) != len(set(effective_keys)):
            raise _invalid_catalog(
                "provider/model/effective_from_utc must be unique in a catalog."
            )


DEFAULT_PRICING_POLICY_CATALOG = PricingPolicyCatalog(
    policies=(DEEPSEEK_FLASH_PRICING_POLICY,)
)


def _invalid_selection(message: str) -> PricingError:
    return PricingError(INVALID_PRICING_INPUT, message)


def _normalize_reference_at(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise _invalid_selection(
            "pricing_reference_at must be a timezone-aware datetime."
        )
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_selection(
            "pricing_reference_at must be a timezone-aware datetime."
        ) from exc
    if value.tzinfo is None or offset is None:
        raise _invalid_selection(
            "pricing_reference_at must be a timezone-aware datetime."
        )
    try:
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_selection(
            "pricing_reference_at must be a timezone-aware datetime."
        ) from exc


def _validate_identity(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_selection(f"{field_name} must be a nonblank string.")


def select_pricing_policy(
    *,
    provider: str,
    model: str,
    pricing_reference_at: datetime,
    catalog: PricingPolicyCatalog | None = None,
) -> PricingPolicy:
    """Select the latest applicable snapshot for one historical instant."""

    active_catalog = (
        DEFAULT_PRICING_POLICY_CATALOG if catalog is None else catalog
    )
    if not isinstance(active_catalog, PricingPolicyCatalog):
        raise _invalid_catalog("catalog must be a PricingPolicyCatalog.")
    _validate_identity(provider, field_name="provider")
    _validate_identity(model, field_name="model")
    reference_at_utc = _normalize_reference_at(pricing_reference_at)

    matching = tuple(
        policy
        for policy in active_catalog.policies
        if policy.provider == provider and policy.model == model
    )
    if not matching:
        raise PricingError(
            PRICING_POLICY_NOT_APPLICABLE,
            "No pricing policy applies to this provider and model.",
            reason=POLICY_NOT_APPLICABLE,
        )

    applicable = tuple(
        policy
        for policy in matching
        if policy.effective_from_utc <= reference_at_utc
    )
    if not applicable:
        raise PricingError(
            PRICING_POLICY_NOT_APPLICABLE,
            "No pricing policy is effective at this reference time.",
            reason=POLICY_NOT_EFFECTIVE,
        )

    return max(applicable, key=lambda policy: policy.effective_from_utc)
