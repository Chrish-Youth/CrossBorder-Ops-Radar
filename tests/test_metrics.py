from __future__ import annotations

from datetime import date
from math import isinf
from pathlib import Path

import pandas as pd
import pytest

from src.config import COUNT_MAX_VALUE, REQUIRED_COLUMNS
from src.loader import load_file
from src.metrics import (
    BASE_MEASURES,
    DERIVED_METRICS,
    MetricsCalculationError,
    RATIO_METRICS,
    calculate_metrics,
)
from src.validator import validate_dataframe


def make_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "date": "2026-08-24",
        "marketplace": "Amazon",
        "country": "US",
        "sku": "SKU-A",
        "product_name": "Example Product",
        "impressions": 400,
        "clicks": 50,
        "orders": 10,
        "units_sold": 12,
        "sales": 250.0,
        "ad_spend": 25.0,
        "refunds": 2,
        "inventory": 9,
    }
    row.update(overrides)
    return row


def clean_rows(*rows: dict[str, object]) -> pd.DataFrame:
    result = validate_dataframe(pd.DataFrame(rows))
    assert not result.report.fatal_errors
    assert not result.report.errors
    return result.clean_data


@pytest.mark.parametrize(
    ("metric_name", "expected"),
    [
        ("ctr", 50 / 400),
        ("cvr", 10 / 50),
        ("aov", 250 / 10),
        ("cpc", 25 / 50),
        ("cpa", 25 / 10),
        ("roas", 250 / 25),
        ("refund_rate", 2 / 10),
        ("gmv", 250.0),
    ],
)
def test_each_readme_metric_formula(metric_name: str, expected: float) -> None:
    metrics = calculate_metrics(clean_rows(make_row()))

    assert metrics.loc[0, metric_name] == pytest.approx(expected)


def test_base_measures_are_aggregated_and_retained() -> None:
    clean = clean_rows(
        make_row(sku="SKU-A", inventory=20),
        make_row(
            sku="SKU-B",
            impressions=100,
            clicks=20,
            orders=4,
            units_sold=5,
            sales=80.0,
            ad_spend=10.0,
            refunds=1,
            inventory=30,
        ),
    )

    metrics = calculate_metrics(clean, group_by="date")

    assert metrics.loc[0, list(BASE_MEASURES)].to_dict() == {
        "impressions": 500,
        "clicks": 70,
        "orders": 14,
        "units_sold": 17,
        "sales": 330.0,
        "ad_spend": 35.0,
        "refunds": 3,
        "inventory": 50,
    }


def test_ratio_of_sums_is_not_average_of_row_ratios() -> None:
    clean = clean_rows(
        make_row(date="2026-08-23", impressions=100, clicks=10),
        make_row(date="2026-08-24", impressions=10, clicks=9),
    )

    metrics = calculate_metrics(clean, group_by="sku")

    assert metrics.loc[0, "ctr"] == pytest.approx(19 / 110)
    assert metrics.loc[0, "ctr"] != pytest.approx(0.5)


def test_all_seven_ratios_use_ratio_of_sums() -> None:
    clean = clean_rows(
        make_row(
            date="2026-08-23",
            impressions=1000,
            clicks=10,
            orders=1,
            units_sold=1,
            sales=100.0,
            ad_spend=10.0,
            refunds=0,
        ),
        make_row(
            date="2026-08-24",
            impressions=10,
            clicks=9,
            orders=8,
            units_sold=8,
            sales=8.0,
            ad_spend=90.0,
            refunds=7,
        ),
    )

    metrics = calculate_metrics(clean, group_by="sku")
    expected = {
        "ctr": 19 / 1010,
        "cvr": 9 / 19,
        "aov": 108 / 9,
        "cpc": 100 / 19,
        "cpa": 100 / 9,
        "roas": 108 / 100,
        "refund_rate": 7 / 9,
    }
    average_of_row_ratios = {
        "ctr": (10 / 1000 + 9 / 10) / 2,
        "cvr": (1 / 10 + 8 / 9) / 2,
        "aov": (100 / 1 + 8 / 8) / 2,
        "cpc": (10 / 10 + 90 / 9) / 2,
        "cpa": (10 / 1 + 90 / 8) / 2,
        "roas": (100 / 10 + 8 / 90) / 2,
        "refund_rate": (0 / 1 + 7 / 8) / 2,
    }

    for metric_name, expected_value in expected.items():
        assert metrics.loc[0, metric_name] == pytest.approx(expected_value)
        assert metrics.loc[0, metric_name] != pytest.approx(
            average_of_row_ratios[metric_name]
        )


def test_all_ratio_zero_denominators_return_nan_without_infinity() -> None:
    clean = clean_rows(
        make_row(
            impressions=0,
            clicks=0,
            orders=0,
            units_sold=0,
            sales=0.0,
            ad_spend=0.0,
            refunds=0,
        )
    )

    metrics = calculate_metrics(clean)

    for metric_name in DERIVED_METRICS[:-1]:
        assert pd.isna(metrics.loc[0, metric_name])
    assert metrics.loc[0, "gmv"] == 0.0


def test_zero_numerator_with_nonzero_denominator_is_valid_zero() -> None:
    clean = clean_rows(
        make_row(
            impressions=100,
            clicks=0,
            orders=0,
            units_sold=0,
            sales=0.0,
            ad_spend=10.0,
            refunds=0,
        )
    )

    metrics = calculate_metrics(clean)

    assert metrics.loc[0, "ctr"] == 0.0
    assert metrics.loc[0, "roas"] == 0.0
    assert pd.isna(metrics.loc[0, "cvr"])


def test_positive_numerator_zero_denominator_returns_nan_without_infinity() -> None:
    clean = clean_rows(
        make_row(
            sku="NO-ORDERS",
            impressions=100,
            clicks=0,
            orders=0,
            units_sold=0,
            sales=100.0,
            ad_spend=10.0,
            refunds=5,
        ),
        make_row(
            sku="NO-AD-SPEND",
            impressions=100,
            clicks=10,
            orders=2,
            units_sold=2,
            sales=100.0,
            ad_spend=0.0,
            refunds=0,
        ),
        make_row(
            sku="NO-CLICKS",
            impressions=100,
            clicks=0,
            orders=2,
            units_sold=2,
            sales=100.0,
            ad_spend=10.0,
            refunds=0,
        ),
    )

    metrics = calculate_metrics(clean, group_by="sku").set_index("sku")
    zero_denominator_results = (
        metrics.loc["NO-ORDERS", ["aov", "cpc", "cpa", "refund_rate"]].tolist()
        + metrics.loc["NO-AD-SPEND", ["roas"]].tolist()
        + metrics.loc["NO-CLICKS", ["cvr", "cpc"]].tolist()
    )

    assert all(pd.isna(value) for value in zero_denominator_results)
    assert all(not isinf(float(value)) for value in zero_denominator_results)


def test_warning_ratios_above_one_are_not_rejected_or_clipped() -> None:
    validation = validate_dataframe(
        pd.DataFrame([make_row(clicks=10, orders=15, refunds=20)])
    )
    assert {issue.code for issue in validation.report.warnings} == {
        "ORDERS_GT_CLICKS",
        "REFUNDS_GT_ORDERS",
    }

    metrics = calculate_metrics(validation.clean_data)

    assert metrics.loc[0, "cvr"] == 1.5
    assert metrics.loc[0, "refund_rate"] == pytest.approx(20 / 15)


def aggregation_fixture() -> pd.DataFrame:
    return clean_rows(
        make_row(date="2026-08-23", marketplace="Amazon", country="US", sku="A"),
        make_row(date="2026-08-24", marketplace="Amazon", country="US", sku="A"),
        make_row(date="2026-08-23", marketplace="Amazon", country="DE", sku="B"),
        make_row(date="2026-08-23", marketplace="eBay", country="US", sku="C"),
    )


@pytest.mark.parametrize(
    ("group_by", "dimensions", "expected_rows"),
    [
        (None, [], 1),
        ([], [], 1),
        ((), [], 1),
        ("sku", ["sku"], 3),
        ("marketplace", ["marketplace"], 2),
        ("country", ["country"], 2),
        ("date", ["date"], 2),
        (["marketplace", "country"], ["marketplace", "country"], 3),
        (
            ["marketplace", "country", "sku"],
            ["marketplace", "country", "sku"],
            3,
        ),
        (
            ["date", "marketplace", "country", "sku"],
            ["date", "marketplace", "country", "sku"],
            4,
        ),
        (("date", "sku"), ["date", "sku"], 4),
    ],
)
def test_supported_aggregation_levels(
    group_by: object,
    dimensions: list[str],
    expected_rows: int,
) -> None:
    metrics = calculate_metrics(aggregation_fixture(), group_by=group_by)  # type: ignore[arg-type]

    assert len(metrics) == expected_rows
    assert list(metrics.columns) == [*dimensions, *BASE_MEASURES, *DERIVED_METRICS]


def test_output_order_is_deterministic_for_shuffled_input() -> None:
    clean = aggregation_fixture()
    shuffled = clean.iloc[[3, 1, 0, 2]].copy()

    first = calculate_metrics(clean, group_by=["marketplace", "country", "sku"])
    second = calculate_metrics(
        shuffled,
        group_by=["marketplace", "country", "sku"],
    )

    pd.testing.assert_frame_equal(first, second)
    assert first[["marketplace", "country", "sku"]].to_records(
        index=False
    ).tolist() == [
        ("Amazon", "DE", "B"),
        ("Amazon", "US", "A"),
        ("eBay", "US", "C"),
    ]


def test_nonempty_output_schema_dtypes_and_range_index_are_stable() -> None:
    clean = aggregation_fixture()
    metrics = calculate_metrics(clean, group_by=["date", "sku"])

    assert list(metrics.columns) == [
        "date",
        "sku",
        *BASE_MEASURES,
        *DERIVED_METRICS,
    ]
    assert str(metrics["date"].dtype) == "object"
    assert metrics["sku"].dtype == clean["sku"].dtype
    assert all(
        str(metrics[column].dtype) == "Int64"
        for column in (
            "impressions",
            "clicks",
            "orders",
            "units_sold",
            "refunds",
            "inventory",
        )
    )
    assert all(
        str(metrics[column].dtype) == "Float64"
        for column in ("sales", "ad_spend", "gmv")
    )
    assert all(
        str(metrics[column].dtype) == "float64"
        for column in RATIO_METRICS
    )
    assert isinstance(metrics.index, pd.RangeIndex)
    assert metrics.index.tolist() == list(range(len(metrics)))


@pytest.mark.parametrize(
    "group_by",
    ["sales", "product_name", ["sku", "sku"], {"sku"}, ["sku", 1]],
)
def test_invalid_group_by_is_structured_error(group_by: object) -> None:
    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(aggregation_fixture(), group_by=group_by)  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_GROUP_BY"


def test_unhashable_group_key_is_structured_input_value_error() -> None:
    clean = clean_rows(make_row())
    invalid = clean.assign(sku=pd.Series([["SKU-A"]], dtype=object))

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(invalid, group_by="sku")

    assert exc_info.value.code == "INVALID_METRIC_INPUT_VALUE"
    assert isinstance(exc_info.value.__cause__, TypeError)


def test_invalid_latest_inventory_date_is_structured_input_value_error() -> None:
    clean = clean_rows(
        make_row(date="2026-08-23"),
        make_row(date="2026-08-24"),
    )
    invalid = clean.assign(date=pd.Series([pd.NA, pd.NA], dtype=object))

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(invalid)

    assert exc_info.value.code == "INVALID_METRIC_INPUT_VALUE"
    assert isinstance(exc_info.value.__cause__, TypeError)


def test_extreme_invalid_money_value_is_structured_input_value_error() -> None:
    clean = clean_rows(make_row())
    invalid = clean.assign(sales=10**10000)

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(invalid)

    assert exc_info.value.code == "INVALID_METRIC_INPUT_VALUE"
    assert isinstance(exc_info.value.__cause__, OverflowError)


def test_missing_metric_input_column_is_structured_error() -> None:
    clean = clean_rows(make_row()).drop(columns=["clicks"])

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(clean)

    assert exc_info.value.code == "MISSING_METRIC_INPUT_COLUMN"
    assert "clicks" in exc_info.value.message


def test_non_dataframe_input_is_structured_error() -> None:
    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics([])  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_METRIC_INPUT"


def test_extra_columns_are_ignored() -> None:
    clean = clean_rows(make_row(brand="Example", notes="ignore me"))

    metrics = calculate_metrics(clean, group_by="sku")

    assert "brand" not in metrics.columns
    assert "notes" not in metrics.columns
    assert metrics.loc[0, "sku"] == "SKU-A"


def test_calculate_metrics_does_not_modify_input_dataframe() -> None:
    clean = aggregation_fixture()
    before = clean.copy(deep=True)

    calculate_metrics(clean, group_by=["marketplace", "country"])

    pd.testing.assert_frame_equal(clean, before)


@pytest.mark.parametrize(
    ("group_by", "dimensions"),
    [(None, []), ("sku", ["sku"]), (["date", "sku"], ["date", "sku"])],
)
def test_empty_clean_dataframe_returns_stable_empty_schema(
    group_by: object,
    dimensions: list[str],
) -> None:
    empty_clean = validate_dataframe(
        pd.DataFrame(columns=REQUIRED_COLUMNS)
    ).clean_data

    metrics = calculate_metrics(empty_clean, group_by=group_by)  # type: ignore[arg-type]

    assert metrics.empty
    assert list(metrics.columns) == [*dimensions, *BASE_MEASURES, *DERIVED_METRICS]
    assert all(
        str(metrics[column].dtype) == "Int64"
        for column in ("impressions", "clicks", "orders", "units_sold", "refunds", "inventory")
    )
    assert all(
        str(metrics[column].dtype) == "Float64"
        for column in ("sales", "ad_spend", "gmv")
    )
    assert all(str(metrics[column].dtype) == "float64" for column in RATIO_METRICS)


def test_inventory_same_date_multiple_skus_is_summed() -> None:
    clean = clean_rows(
        make_row(sku="SKU-A", inventory=20),
        make_row(sku="SKU-B", inventory=30),
    )

    metrics = calculate_metrics(clean, group_by="date")

    assert metrics.loc[0, "inventory"] == 50


def test_inventory_multiple_dates_uses_latest_snapshot_not_sum() -> None:
    clean = clean_rows(
        make_row(date="2026-08-25", inventory=60, impressions=100),
        make_row(date="2026-08-23", inventory=100, impressions=100),
        make_row(date="2026-08-24", inventory=80, impressions=100),
    )

    metrics = calculate_metrics(clean, group_by="sku")

    assert metrics.loc[0, "inventory"] == 60
    assert metrics.loc[0, "inventory"] != 240
    assert metrics.loc[0, "impressions"] == 300


def test_date_inventory_does_not_forward_fill_missing_entity_snapshots() -> None:
    clean = clean_rows(
        make_row(date="2026-08-23", sku="SKU-A", inventory=20),
        make_row(date="2026-08-24", sku="SKU-B", inventory=30),
    )

    metrics = calculate_metrics(clean, group_by="date")

    assert metrics["inventory"].tolist() == [20, 30]


def test_inventory_multiple_entities_selects_each_latest_then_sums() -> None:
    clean = clean_rows(
        make_row(date="1600-01-01", sku="SKU-A", inventory=100),
        make_row(date="9999-12-31", sku="SKU-A", inventory=60),
        make_row(date="1600-01-01", sku="SKU-B", inventory=50),
        make_row(date="2262-04-12", sku="SKU-B", inventory=30),
    )

    metrics = calculate_metrics(clean, group_by=["marketplace", "country"])

    assert metrics.loc[0, "inventory"] == 90


def test_same_sku_across_marketplaces_uses_separate_inventory_entities() -> None:
    clean = clean_rows(
        make_row(date="2026-08-23", marketplace="Amazon", inventory=100),
        make_row(date="2026-08-25", marketplace="Amazon", inventory=60),
        make_row(date="2026-08-24", marketplace="eBay", inventory=50),
        make_row(date="2026-08-26", marketplace="eBay", inventory=30),
    )

    metrics = calculate_metrics(clean, group_by="sku")

    assert metrics.loc[0, "inventory"] == 90


def test_same_sku_across_marketplaces_and_countries_isolated_inventory() -> None:
    clean = clean_rows(
        make_row(
            date="2026-08-23",
            marketplace="Amazon",
            country="US",
            inventory=100,
        ),
        make_row(
            date="2026-08-24",
            marketplace="Amazon",
            country="US",
            inventory=80,
        ),
        make_row(
            date="2026-08-23",
            marketplace="Amazon",
            country="DE",
            inventory=50,
        ),
        make_row(
            date="2026-08-24",
            marketplace="Amazon",
            country="DE",
            inventory=40,
        ),
        make_row(
            date="2026-08-23",
            marketplace="TikTok",
            country="US",
            inventory=30,
        ),
        make_row(
            date="2026-08-24",
            marketplace="TikTok",
            country="US",
            inventory=20,
        ),
    )

    overall = calculate_metrics(clean)
    by_sku = calculate_metrics(clean, group_by="sku")

    assert overall.loc[0, "inventory"] == 140
    assert by_sku.loc[0, "inventory"] == 140


def test_full_business_key_snapshot_rollup_reconciles_to_overall() -> None:
    clean = clean_rows(
        make_row(
            date="2026-08-21",
            marketplace="Amazon",
            country="US",
            sku="A",
            impressions=100,
            clicks=10,
            orders=2,
            units_sold=2,
            sales=20.0,
            ad_spend=5.0,
            refunds=0,
            inventory=100,
        ),
        make_row(
            date="2026-08-24",
            marketplace="Amazon",
            country="US",
            sku="A",
            impressions=10,
            clicks=9,
            orders=4,
            units_sold=4,
            sales=80.0,
            ad_spend=20.0,
            refunds=1,
            inventory=60,
        ),
        make_row(
            date="2026-08-22",
            marketplace="Amazon",
            country="DE",
            sku="A",
            impressions=200,
            clicks=20,
            orders=5,
            units_sold=5,
            sales=100.0,
            ad_spend=25.0,
            refunds=1,
            inventory=50,
        ),
        make_row(
            date="2026-08-23",
            marketplace="Amazon",
            country="DE",
            sku="A",
            impressions=300,
            clicks=30,
            orders=6,
            units_sold=6,
            sales=120.0,
            ad_spend=30.0,
            refunds=2,
            inventory=30,
        ),
        make_row(
            date="2026-08-23",
            marketplace="eBay",
            country="US",
            sku="B",
            impressions=400,
            clicks=40,
            orders=8,
            units_sold=8,
            sales=160.0,
            ad_spend=40.0,
            refunds=2,
            inventory=20,
        ),
    )

    full = calculate_metrics(
        clean,
        group_by=["date", "marketplace", "country", "sku"],
    )
    overall = calculate_metrics(clean).iloc[0]

    manual_base = {
        column: sum(int(value) for value in full[column])
        for column in (
            "impressions",
            "clicks",
            "orders",
            "units_sold",
            "refunds",
        )
    }
    manual_base.update(
        {
            column: sum(float(value) for value in full[column])
            for column in ("sales", "ad_spend")
        }
    )
    latest_inventory: dict[tuple[str, str, str], tuple[date, int]] = {}
    for record in full.to_dict(orient="records"):
        entity = (record["marketplace"], record["country"], record["sku"])
        existing = latest_inventory.get(entity)
        if existing is None or record["date"] > existing[0]:
            latest_inventory[entity] = (
                record["date"],
                int(record["inventory"]),
            )
    manual_base["inventory"] = sum(
        snapshot[1] for snapshot in latest_inventory.values()
    )

    for column in BASE_MEASURES:
        assert overall[column] == pytest.approx(manual_base[column])

    manual_ratios = {
        "ctr": manual_base["clicks"] / manual_base["impressions"],
        "cvr": manual_base["orders"] / manual_base["clicks"],
        "aov": manual_base["sales"] / manual_base["orders"],
        "cpc": manual_base["ad_spend"] / manual_base["clicks"],
        "cpa": manual_base["ad_spend"] / manual_base["orders"],
        "roas": manual_base["sales"] / manual_base["ad_spend"],
        "refund_rate": manual_base["refunds"] / manual_base["orders"],
        "gmv": manual_base["sales"],
    }
    for metric_name, expected in manual_ratios.items():
        assert overall[metric_name] == pytest.approx(expected)

    assert sum(int(value) for value in full["inventory"]) == 260
    assert manual_base["inventory"] == 110


def test_wide_python_dates_group_sort_and_inventory_without_timestamp_conversion() -> None:
    clean = clean_rows(
        make_row(date="9999-12-31", sku="SKU-A", inventory=60),
        make_row(date="1600-01-01", sku="SKU-A", inventory=100),
        make_row(date="2262-04-12", sku="SKU-A", inventory=80),
    )

    metrics = calculate_metrics(clean, group_by="date")

    assert metrics["date"].tolist() == [
        date(1600, 1, 1),
        date(2262, 4, 12),
        date(9999, 12, 31),
    ]
    assert all(type(value) is date for value in metrics["date"])
    assert metrics["inventory"].tolist() == [100, 80, 60]


def test_large_count_aggregation_preserves_integer_precision() -> None:
    expected = 9_007_199_254_740_993
    clean = clean_rows(
        make_row(date="2026-08-23", impressions=9_007_199_254_740_992),
        make_row(
            date="2026-08-24",
            impressions=1,
            clicks=0,
            orders=0,
            refunds=0,
        ),
    )

    metrics = calculate_metrics(clean)

    assert metrics.loc[0, "impressions"] == expected
    assert str(metrics["impressions"].dtype) == "Int64"


def test_int64_max_aggregation_result_is_allowed() -> None:
    clean = clean_rows(make_row(impressions=COUNT_MAX_VALUE))

    metrics = calculate_metrics(clean)

    assert metrics.loc[0, "impressions"] == COUNT_MAX_VALUE


def test_flow_count_aggregation_overflow_is_structured_error() -> None:
    clean = clean_rows(
        make_row(date="2026-08-23", impressions=COUNT_MAX_VALUE),
        make_row(
            date="2026-08-24",
            impressions=1,
            clicks=0,
            orders=0,
            refunds=0,
        ),
    )

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(clean)

    assert exc_info.value.code == "COUNT_AGGREGATION_OVERFLOW"


def test_overall_count_overflow_does_not_block_safe_target_groups() -> None:
    clean = clean_rows(
        make_row(
            marketplace="Amazon",
            impressions=COUNT_MAX_VALUE,
        ),
        make_row(
            marketplace="eBay",
            impressions=1,
            clicks=0,
            orders=0,
            units_sold=0,
            refunds=0,
        ),
    )

    grouped = calculate_metrics(clean, group_by="marketplace")

    assert grouped.set_index("marketplace")["impressions"].to_dict() == {
        "Amazon": COUNT_MAX_VALUE,
        "eBay": 1,
    }
    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(clean)
    assert exc_info.value.code == "COUNT_AGGREGATION_OVERFLOW"


def test_inventory_aggregation_overflow_is_structured_error() -> None:
    clean = clean_rows(
        make_row(sku="SKU-A", inventory=COUNT_MAX_VALUE),
        make_row(sku="SKU-B", inventory=1),
    )

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(clean, group_by=["marketplace", "country"])

    assert exc_info.value.code == "COUNT_AGGREGATION_OVERFLOW"


def test_money_aggregation_cannot_return_infinity() -> None:
    clean = clean_rows(
        make_row(date="2026-08-23", sales=1e308),
        make_row(date="2026-08-24", sales=1e308),
    )

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(clean)

    assert exc_info.value.code == "MONEY_AGGREGATION_OVERFLOW"


def test_nonzero_denominator_ratio_overflow_is_structured_error() -> None:
    clean = clean_rows(make_row(sales=1e308, ad_spend=5e-324))

    with pytest.raises(MetricsCalculationError) as exc_info:
        calculate_metrics(clean)

    assert exc_info.value.code == "NON_FINITE_METRIC_RESULT"


def test_existing_metric_named_extra_columns_are_recalculated() -> None:
    clean = clean_rows(make_row(ctr=999, gmv=-1))

    metrics = calculate_metrics(clean)

    assert metrics.loc[0, "ctr"] == pytest.approx(50 / 400)
    assert metrics.loc[0, "gmv"] == pytest.approx(250.0)


def test_sample_loader_validator_metrics_integration() -> None:
    sample_path = Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"

    validation = validate_dataframe(load_file(sample_path))
    overall = calculate_metrics(validation.clean_data)
    by_sku = calculate_metrics(validation.clean_data, group_by="sku")
    by_marketplace = calculate_metrics(
        validation.clean_data,
        group_by="marketplace",
    )

    assert validation.report.valid_rows == 14
    assert len(overall) == 1
    assert len(by_sku) == 12
    assert len(by_marketplace) == 2

    assert overall.loc[0, list(BASE_MEASURES)].to_dict() == {
        "impressions": 68000,
        "clicks": 2235,
        "orders": 198,
        "units_sold": 212,
        "sales": pytest.approx(6368.22),
        "ad_spend": 1675.0,
        "refunds": 33,
        "inventory": 610,
    }
    assert overall.loc[0, "ctr"] == pytest.approx(2235 / 68000)
    assert overall.loc[0, "cvr"] == pytest.approx(198 / 2235)
    assert overall.loc[0, "aov"] == pytest.approx(6368.22 / 198)
    assert overall.loc[0, "cpc"] == pytest.approx(1675 / 2235)
    assert overall.loc[0, "cpa"] == pytest.approx(1675 / 198)
    assert overall.loc[0, "roas"] == pytest.approx(6368.22 / 1675)
    assert overall.loc[0, "refund_rate"] == pytest.approx(33 / 198)
    assert overall.loc[0, "gmv"] == pytest.approx(6368.22)

    normal_us = by_sku.loc[by_sku["sku"] == "SKU-NORMAL-US"].iloc[0]
    assert normal_us["impressions"] == 22000
    assert normal_us["clicks"] == 660
    assert normal_us["sales"] == pytest.approx(1979.34)
    assert normal_us["inventory"] == 95
    assert normal_us["ctr"] == pytest.approx(660 / 22000)

    order_warning = by_sku.loc[by_sku["sku"] == "SKU-ORDER-WARNING"].iloc[0]
    refund_warning = by_sku.loc[by_sku["sku"] == "SKU-REFUND-WARNING"].iloc[0]
    zero_denom = by_sku.loc[by_sku["sku"] == "SKU-ZERO-DENOM"].iloc[0]
    duplicate = by_sku.loc[by_sku["sku"] == "SKU-DUP"].iloc[0]
    assert order_warning["cvr"] == 1.5
    assert refund_warning["refund_rate"] == pytest.approx(20 / 15)
    assert pd.isna(zero_denom["ctr"])
    assert pd.isna(zero_denom["roas"])
    assert duplicate["sales"] == pytest.approx(299.9)

    assert "SKU-CLICK-ERROR" not in by_sku["sku"].tolist()
    assert "SKU-KEY-CONFLICT" not in by_sku["sku"].tolist()

    ebay = by_marketplace.loc[by_marketplace["marketplace"] == "eBay"].iloc[0]
    assert ebay["impressions"] == 10000
    assert ebay["sales"] == pytest.approx(699.9)
    assert ebay["inventory"] == 100
