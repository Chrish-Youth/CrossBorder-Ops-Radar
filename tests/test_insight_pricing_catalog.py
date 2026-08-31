from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.insight_pricing import (
    DEEPSEEK_FLASH_PRICING_POLICY,
    INVALID_PRICING_INPUT,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    PRICING_POLICY_NOT_APPLICABLE,
    PricingError,
    PricingPolicy,
)
from src.insight_pricing_catalog import (
    DEFAULT_PRICING_POLICY_CATALOG,
    INVALID_PRICING_CATALOG,
    UNSELECTED_PRICING_POLICY_VERSION,
    PricingCatalogError,
    PricingPolicyCatalog,
    select_pricing_policy,
)


UTC = timezone.utc
SINGAPORE = timezone(timedelta(hours=8))
T1 = datetime(2026, 9, 1, tzinfo=UTC)
T2 = datetime(2026, 9, 15, tzinfo=UTC)
T3 = datetime(2026, 10, 1, tzinfo=UTC)


def synthetic_policy(
    version: str,
    effective_at: datetime,
    *,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    cache_miss_rate: str = "0.22",
    verified_at: datetime | None = None,
) -> PricingPolicy:
    off_peak_rates = replace(
        DEEPSEEK_FLASH_PRICING_POLICY.off_peak_rates,
        prompt_cache_miss_usd_per_million=Decimal(cache_miss_rate),
    )
    return replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version=version,
        provider=provider,
        model=model,
        effective_from_utc=effective_at,
        verified_at_utc=(
            effective_at + timedelta(days=1)
            if verified_at is None
            else verified_at
        ),
        off_peak_rates=off_peak_rates,
    )


def select(
    catalog: PricingPolicyCatalog,
    reference_at: datetime,
    *,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
) -> PricingPolicy:
    return select_pricing_policy(
        provider=provider,
        model=model,
        pricing_reference_at=reference_at,
        catalog=catalog,
    )


def test_default_catalog_contains_only_the_verified_production_snapshot() -> None:
    assert DEFAULT_PRICING_POLICY_CATALOG.policies == (
        DEEPSEEK_FLASH_PRICING_POLICY,
    )


def test_catalog_is_frozen() -> None:
    catalog = PricingPolicyCatalog(())

    with pytest.raises(FrozenInstanceError):
        catalog.policies = (DEEPSEEK_FLASH_PRICING_POLICY,)  # type: ignore[misc]


@pytest.mark.parametrize("policies", [[], {}, None, set()])
def test_catalog_requires_a_tuple(policies: object) -> None:
    with pytest.raises(PricingCatalogError) as captured:
        PricingPolicyCatalog(policies)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_PRICING_CATALOG


@pytest.mark.parametrize("item", [None, {}, object()])
def test_catalog_rejects_non_policy_entries(item: object) -> None:
    with pytest.raises(PricingCatalogError) as captured:
        PricingPolicyCatalog((item,))  # type: ignore[arg-type]

    assert captured.value.code == INVALID_PRICING_CATALOG
    assert repr(item) not in str(captured.value)


def test_catalog_rejects_reserved_unselected_policy_version() -> None:
    reserved = replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version=UNSELECTED_PRICING_POLICY_VERSION,
    )

    with pytest.raises(PricingCatalogError) as captured:
        PricingPolicyCatalog((reserved,))

    assert captured.value.code == INVALID_PRICING_CATALOG
    assert repr(reserved) not in str(captured.value)


@pytest.mark.parametrize("version", ["UNSELECTED", "Unselected", "unselected "])
def test_reserved_policy_version_check_preserves_exact_identity_semantics(
    version: str,
) -> None:
    policy = replace(DEEPSEEK_FLASH_PRICING_POLICY, version=version)

    assert PricingPolicyCatalog((policy,)).policies == (policy,)


def test_catalog_allows_empty_and_selector_reports_not_applicable() -> None:
    with pytest.raises(PricingError) as captured:
        select(PricingPolicyCatalog(()), T1)

    assert captured.value.code == PRICING_POLICY_NOT_APPLICABLE
    assert captured.value.reason == POLICY_NOT_APPLICABLE


def test_catalog_rejects_globally_duplicate_versions() -> None:
    first = synthetic_policy("same-version", T1)
    second = synthetic_policy(
        "same-version",
        T2,
        provider="other-provider",
    )

    with pytest.raises(PricingCatalogError) as captured:
        PricingPolicyCatalog((first, second))

    assert captured.value.code == INVALID_PRICING_CATALOG


def test_catalog_rejects_duplicate_effective_routing_key() -> None:
    first = synthetic_policy("policy-a", T1)
    second = synthetic_policy("policy-b", T1, cache_miss_rate="0.30")

    with pytest.raises(PricingCatalogError) as captured:
        PricingPolicyCatalog((first, second))

    assert captured.value.code == INVALID_PRICING_CATALOG


def test_same_effective_time_is_valid_for_different_models_and_providers() -> None:
    flash = synthetic_policy("flash", T1)
    other_model = synthetic_policy("other-model", T1, model="model-x")
    other_provider = synthetic_policy(
        "other-provider",
        T1,
        provider="provider-x",
    )
    catalog = PricingPolicyCatalog((other_model, other_provider, flash))

    assert select(catalog, T1) is flash
    assert select(catalog, T1, model="model-x") is other_model
    assert select(catalog, T1, provider="provider-x") is other_provider


def test_single_policy_before_exact_and_after_boundaries() -> None:
    policy = synthetic_policy("policy-a", T1)
    catalog = PricingPolicyCatalog((policy,))

    with pytest.raises(PricingError) as captured:
        select(catalog, T1 - timedelta(microseconds=1))
    assert captured.value.reason == POLICY_NOT_EFFECTIVE
    assert select(catalog, T1) is policy
    assert select(catalog, T1 + timedelta(days=100)) is policy


def test_two_versions_select_before_at_and_after_second_boundary() -> None:
    first = synthetic_policy("policy-a", T1)
    second = synthetic_policy("policy-b", T2, cache_miss_rate="0.30")
    catalog = PricingPolicyCatalog((second, first))

    assert select(catalog, T2 - timedelta(microseconds=1)) is first
    assert select(catalog, T2) is second
    assert select(catalog, T2 + timedelta(days=1)) is second


def test_selection_is_independent_of_catalog_input_order() -> None:
    first = synthetic_policy("policy-a", T1)
    second = synthetic_policy("policy-b", T2)
    before = T2 - timedelta(seconds=1)
    after = T2 + timedelta(seconds=1)

    assert select(PricingPolicyCatalog((first, second)), before) is first
    assert select(PricingPolicyCatalog((second, first)), before) is first
    assert select(PricingPolicyCatalog((first, second)), after) is second
    assert select(PricingPolicyCatalog((second, first)), after) is second


def test_three_version_selection_uses_greatest_effective_timestamp() -> None:
    first = synthetic_policy("policy-a", T1)
    second = synthetic_policy("policy-b", T2)
    third = synthetic_policy("policy-c", T3)
    catalog = PricingPolicyCatalog((third, first, second))

    assert select(catalog, T2 - timedelta(seconds=1)) is first
    assert select(catalog, T2) is second
    assert select(catalog, T3 - timedelta(seconds=1)) is second
    assert select(catalog, T3) is third


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("other", "deepseek-v4-flash"),
        ("deepseek", "other-model"),
    ],
)
def test_unsupported_provider_or_model_never_falls_back(
    provider: str,
    model: str,
) -> None:
    catalog = PricingPolicyCatalog((synthetic_policy("policy-a", T1),))

    with pytest.raises(PricingError) as captured:
        select(catalog, T2, provider=provider, model=model)

    assert captured.value.code == PRICING_POLICY_NOT_APPLICABLE
    assert captured.value.reason == POLICY_NOT_APPLICABLE


def test_other_model_future_policy_does_not_affect_flash_selection() -> None:
    flash = synthetic_policy("flash-a", T1)
    other = synthetic_policy("other-b", T2, model="other-model")
    catalog = PricingPolicyCatalog((other, flash))

    assert select(catalog, T3) is flash


def test_non_utc_reference_is_normalized_by_instant() -> None:
    policy = synthetic_policy("policy-a", T1)
    catalog = PricingPolicyCatalog((policy,))
    singapore_boundary = datetime(2026, 9, 1, 8, tzinfo=SINGAPORE)

    assert select(catalog, singapore_boundary) is policy


def test_new_york_reference_rolls_forward_to_the_correct_utc_date() -> None:
    policy = synthetic_policy("policy-a", T1)
    catalog = PricingPolicyCatalog((policy,))
    new_york_boundary = datetime(
        2026,
        8,
        31,
        20,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    assert select(catalog, new_york_boundary) is policy


@pytest.mark.parametrize("reference", [datetime(2026, 9, 1), object()])
def test_naive_or_non_datetime_reference_is_rejected(reference: object) -> None:
    catalog = PricingPolicyCatalog((synthetic_policy("policy-a", T1),))

    with pytest.raises(PricingError) as captured:
        select(catalog, reference)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_PRICING_INPUT


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        (None, "deepseek-v4-flash"),
        ("", "deepseek-v4-flash"),
        (" ", "deepseek-v4-flash"),
        ("deepseek", None),
        ("deepseek", ""),
        ("deepseek", " "),
    ],
)
def test_blank_routing_identity_is_invalid_input(
    provider: object,
    model: object,
) -> None:
    with pytest.raises(PricingError) as captured:
        select_pricing_policy(
            provider=provider,  # type: ignore[arg-type]
            model=model,  # type: ignore[arg-type]
            pricing_reference_at=T2,
            catalog=PricingPolicyCatalog(()),
        )

    assert captured.value.code == INVALID_PRICING_INPUT


def test_selection_ignores_version_sorting_and_verified_timestamp() -> None:
    first = synthetic_policy(
        "zzzz-old-effective",
        T1,
        verified_at=T3 + timedelta(days=10),
    )
    second = synthetic_policy(
        "aaaa-new-effective",
        T2,
        verified_at=T2 + timedelta(days=1),
    )
    catalog = PricingPolicyCatalog((second, first))

    assert select(catalog, T3) is second


def test_selector_rejects_wrong_catalog_type_without_echoing_value() -> None:
    secret_catalog = {"SECRET_RATE": Decimal("999")}

    with pytest.raises(PricingCatalogError) as captured:
        select_pricing_policy(
            provider="deepseek",
            model="deepseek-v4-flash",
            pricing_reference_at=T2,
            catalog=secret_catalog,  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_PRICING_CATALOG
    assert "SECRET_RATE" not in str(captured.value)
