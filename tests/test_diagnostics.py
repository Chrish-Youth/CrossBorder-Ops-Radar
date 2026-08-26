from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.config import (
    CLICKS_WITHOUT_ORDERS,
    DEMO_HIGH_REFUND_RATE_THRESHOLD,
    DEMO_LOW_CTR_THRESHOLD,
    DEMO_LOW_CVR_THRESHOLD,
    DEMO_LOW_ROAS_THRESHOLD,
    DEMO_MIN_CLICKS_FOR_LOW_CVR,
    DEMO_MIN_CLICKS_WITHOUT_ORDERS,
    DEMO_MIN_IMPRESSIONS_FOR_LOW_CTR,
    DEMO_MIN_ORDERS_FOR_HIGH_REFUND_RATE,
    DIAGNOSTIC_RULE_ORDER,
    HIGH_IMPRESSIONS_LOW_CTR,
    HIGH_REFUND_RATE,
    LOW_CVR,
    LOW_ROAS,
    OUT_OF_STOCK,
    REQUIRED_COLUMNS,
    SPEND_WITHOUT_ORDERS,
)
from src.diagnostics import (
    DIAGNOSTIC_ISSUE_COLUMNS,
    DiagnosticsError,
    diagnose_metrics,
)
from src.loader import load_file
from src.metrics import calculate_metrics
from src.validator import validate_dataframe


def make_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "date": "2026-08-24",
        "marketplace": "Amazon",
        "country": "US",
        "sku": "SKU-A",
        "product_name": "Example Product",
        "impressions": 2000,
        "clicks": 100,
        "orders": 10,
        "units_sold": 10,
        "sales": 200.0,
        "ad_spend": 50.0,
        "refunds": 0,
        "inventory": 20,
    }
    row.update(overrides)
    return row


def metrics_from_rows(
    *rows: dict[str, object],
    group_by: object = "sku",
) -> pd.DataFrame:
    validation = validate_dataframe(pd.DataFrame(rows))
    assert not validation.report.fatal_errors
    assert not validation.report.errors
    return calculate_metrics(validation.clean_data, group_by=group_by)  # type: ignore[arg-type]


def codes(diagnostics: pd.DataFrame) -> list[str]:
    return diagnostics["code"].tolist()


@pytest.mark.parametrize(
    ("code", "overrides", "expected"),
    [
        (
            HIGH_IMPRESSIONS_LOW_CTR,
            {"impressions": 1000, "clicks": 9, "orders": 1},
            True,
        ),
        (
            HIGH_IMPRESSIONS_LOW_CTR,
            {"impressions": 1000, "clicks": 10, "orders": 1},
            False,
        ),
        (
            HIGH_IMPRESSIONS_LOW_CTR,
            {"impressions": 1000, "clicks": 11, "orders": 1},
            False,
        ),
        (LOW_CVR, {"clicks": 100, "orders": 1}, True),
        (LOW_CVR, {"clicks": 100, "orders": 2}, False),
        (LOW_CVR, {"clicks": 100, "orders": 3}, False),
        (LOW_ROAS, {"sales": 50.0, "ad_spend": 100.0}, True),
        (LOW_ROAS, {"sales": 100.0, "ad_spend": 100.0}, False),
        (LOW_ROAS, {"sales": 150.0, "ad_spend": 100.0}, False),
        (HIGH_REFUND_RATE, {"orders": 10, "refunds": 0}, False),
        (HIGH_REFUND_RATE, {"orders": 10, "refunds": 1}, False),
        (HIGH_REFUND_RATE, {"orders": 10, "refunds": 2}, True),
    ],
    ids=[
        "ctr-below",
        "ctr-equal",
        "ctr-above",
        "cvr-below",
        "cvr-equal",
        "cvr-above",
        "roas-below",
        "roas-equal",
        "roas-above",
        "refund-below",
        "refund-equal",
        "refund-above",
    ],
)
def test_ratio_threshold_boundaries(
    code: str,
    overrides: dict[str, object],
    expected: bool,
) -> None:
    diagnostics = diagnose_metrics(metrics_from_rows(make_row(**overrides)))

    assert (code in codes(diagnostics)) is expected


def test_non_ratio_rule_boundaries() -> None:
    no_orders = diagnose_metrics(
        metrics_from_rows(
            make_row(
                clicks=20,
                orders=0,
                units_sold=0,
                sales=0.0,
                ad_spend=10.0,
            )
        )
    )
    one_order = diagnose_metrics(
        metrics_from_rows(make_row(clicks=20, orders=1, ad_spend=10.0))
    )
    no_spend = diagnose_metrics(
        metrics_from_rows(
            make_row(
                clicks=20,
                orders=0,
                units_sold=0,
                sales=0.0,
                ad_spend=0.0,
            )
        )
    )
    stockout = diagnose_metrics(
        metrics_from_rows(make_row(units_sold=1, inventory=0))
    )
    in_stock = diagnose_metrics(
        metrics_from_rows(make_row(units_sold=1, inventory=1))
    )

    assert CLICKS_WITHOUT_ORDERS in codes(no_orders)
    assert SPEND_WITHOUT_ORDERS in codes(no_orders)
    assert CLICKS_WITHOUT_ORDERS not in codes(one_order)
    assert SPEND_WITHOUT_ORDERS not in codes(one_order)
    assert SPEND_WITHOUT_ORDERS not in codes(no_spend)
    assert OUT_OF_STOCK in codes(stockout)
    assert OUT_OF_STOCK not in codes(in_stock)


def test_minimum_impressions_boundary() -> None:
    observed = []
    for impressions in (999, 1000, 1001):
        diagnostics = diagnose_metrics(
            metrics_from_rows(
                make_row(
                    impressions=impressions,
                    clicks=0,
                    orders=0,
                    units_sold=0,
                    sales=0.0,
                    ad_spend=0.0,
                    refunds=0,
                )
            )
        )
        observed.append(HIGH_IMPRESSIONS_LOW_CTR in codes(diagnostics))

    assert observed == [False, True, True]


def test_minimum_clicks_for_low_cvr_boundary() -> None:
    observed = []
    for clicks in (49, 50, 51):
        diagnostics = diagnose_metrics(
            metrics_from_rows(
                make_row(
                    impressions=500,
                    clicks=clicks,
                    orders=0,
                    units_sold=0,
                    sales=0.0,
                    ad_spend=0.0,
                    refunds=0,
                )
            )
        )
        observed.append(LOW_CVR in codes(diagnostics))

    assert observed == [False, True, True]


def test_minimum_clicks_without_orders_boundary() -> None:
    click_signal = []
    spend_signal = []
    for clicks in (19, 20, 21):
        diagnostics = diagnose_metrics(
            metrics_from_rows(
                make_row(
                    impressions=500,
                    clicks=clicks,
                    orders=0,
                    units_sold=0,
                    sales=0.0,
                    ad_spend=10.0,
                    refunds=0,
                )
            )
        )
        click_signal.append(CLICKS_WITHOUT_ORDERS in codes(diagnostics))
        spend_signal.append(SPEND_WITHOUT_ORDERS in codes(diagnostics))

    assert click_signal == [False, True, True]
    assert spend_signal == [False, True, True]


def test_minimum_orders_for_high_refund_boundary() -> None:
    observed = []
    for orders in (9, 10, 11):
        diagnostics = diagnose_metrics(
            metrics_from_rows(
                make_row(
                    clicks=100,
                    orders=orders,
                    units_sold=orders,
                    refunds=2,
                )
            )
        )
        observed.append(HIGH_REFUND_RATE in codes(diagnostics))

    assert observed == [False, True, True]


def test_minimum_units_sold_for_stockout_boundary() -> None:
    observed = []
    for units_sold in (0, 1, 2):
        diagnostics = diagnose_metrics(
            metrics_from_rows(
                make_row(units_sold=units_sold, inventory=0)
            )
        )
        observed.append(OUT_OF_STOCK in codes(diagnostics))

    assert observed == [False, True, True]


def test_positive_ad_spend_gate_is_strict() -> None:
    zero_spend = diagnose_metrics(
        metrics_from_rows(
            make_row(
                clicks=20,
                orders=0,
                units_sold=0,
                sales=0.0,
                ad_spend=0.0,
            )
        )
    )
    positive_spend = diagnose_metrics(
        metrics_from_rows(
            make_row(
                clicks=20,
                orders=0,
                units_sold=0,
                sales=0.0,
                ad_spend=0.01,
            )
        )
    )

    assert SPEND_WITHOUT_ORDERS not in codes(zero_spend)
    assert LOW_ROAS not in codes(zero_spend)
    assert SPEND_WITHOUT_ORDERS in codes(positive_spend)
    assert LOW_ROAS in codes(positive_spend)


def test_structured_issue_preserves_group_context_and_evidence() -> None:
    metrics = metrics_from_rows(
        make_row(
            date="2026-08-25",
            marketplace="Amazon",
            country="DE",
            sku="SKU-LOW-CTR",
            impressions=1000,
            clicks=9,
            orders=1,
        ),
        group_by=["date", "marketplace", "country", "sku"],
    )

    issue = diagnose_metrics(metrics).iloc[0]

    assert issue["date"] == date(2026, 8, 25)
    assert issue["marketplace"] == "Amazon"
    assert issue["country"] == "DE"
    assert issue["sku"] == "SKU-LOW-CTR"
    assert issue["code"] == HIGH_IMPRESSIONS_LOW_CTR
    assert issue["severity"] == "Warning"
    assert issue["metric"] == "ctr"
    assert issue["actual_value"] == pytest.approx(0.009)
    assert issue["threshold"] == pytest.approx(DEMO_LOW_CTR_THRESHOLD)
    assert issue["evidence"] == {
        "impressions": 1000,
        "minimum_impressions": DEMO_MIN_IMPRESSIONS_FOR_LOW_CTR,
    }
    assert "Demo 默认阈值" in issue["message"]


def test_nan_ratios_do_not_trigger_ratio_diagnostics() -> None:
    metrics = metrics_from_rows(
        make_row(
            impressions=0,
            clicks=0,
            orders=0,
            units_sold=0,
            sales=0.0,
            ad_spend=0.0,
            refunds=0,
            inventory=0,
        )
    )
    assert pd.isna(metrics.loc[0, "ctr"])
    assert pd.isna(metrics.loc[0, "cvr"])
    assert pd.isna(metrics.loc[0, "roas"])
    assert pd.isna(metrics.loc[0, "refund_rate"])

    diagnostics = diagnose_metrics(metrics)

    assert diagnostics.empty


def test_warning_ratios_above_one_are_not_clipped_or_rejected() -> None:
    validation = validate_dataframe(
        pd.DataFrame(
            [
                make_row(
                    impressions=100,
                    clicks=10,
                    orders=15,
                    units_sold=15,
                    refunds=20,
                )
            ]
        )
    )
    assert {issue.code for issue in validation.report.warnings} == {
        "ORDERS_GT_CLICKS",
        "REFUNDS_GT_ORDERS",
    }
    metrics = calculate_metrics(validation.clean_data, group_by="sku")
    assert metrics.loc[0, "cvr"] == 1.5
    assert metrics.loc[0, "refund_rate"] == pytest.approx(20 / 15)

    diagnostics = diagnose_metrics(metrics)

    assert codes(diagnostics) == [HIGH_REFUND_RATE]
    assert diagnostics.loc[0, "actual_value"] == pytest.approx(20 / 15)


def test_multiple_issues_follow_fixed_rule_order() -> None:
    metrics = metrics_from_rows(
        make_row(
            sku="SKU-NO-ORDER",
            impressions=3000,
            clicks=120,
            orders=0,
            units_sold=0,
            sales=0.0,
            ad_spend=140.0,
            refunds=0,
            inventory=45,
        )
    )

    diagnostics = diagnose_metrics(metrics)

    assert codes(diagnostics) == [
        LOW_CVR,
        CLICKS_WITHOUT_ORDERS,
        SPEND_WITHOUT_ORDERS,
        LOW_ROAS,
    ]
    assert codes(diagnostics) == [
        code for code in DIAGNOSTIC_RULE_ORDER if code in codes(diagnostics)
    ]


def test_normal_group_returns_zero_issues() -> None:
    diagnostics = diagnose_metrics(metrics_from_rows(make_row()))

    assert diagnostics.empty
    assert "NORMAL" not in diagnostics.get("code", pd.Series(dtype=object)).tolist()


def test_empty_metrics_returns_stable_empty_schema() -> None:
    empty_clean = validate_dataframe(
        pd.DataFrame(columns=REQUIRED_COLUMNS)
    ).clean_data
    empty_metrics = calculate_metrics(
        empty_clean,
        group_by=["sku", "country"],
    )

    diagnostics = diagnose_metrics(empty_metrics)

    assert diagnostics.empty
    assert list(diagnostics.columns) == [
        "sku",
        "country",
        *DIAGNOSTIC_ISSUE_COLUMNS,
    ]
    assert diagnostics["sku"].dtype == empty_metrics["sku"].dtype
    assert diagnostics["country"].dtype == empty_metrics["country"].dtype
    assert str(diagnostics["actual_value"].dtype) == "Float64"
    assert str(diagnostics["threshold"].dtype) == "Float64"
    assert isinstance(diagnostics.index, pd.RangeIndex)


def test_nonempty_output_contract_is_stable() -> None:
    metrics = metrics_from_rows(
        make_row(
            marketplace="eBay",
            country="DE",
            sku="SKU-LOW-CTR",
            impressions=1000,
            clicks=9,
            orders=1,
        ),
        group_by=["marketplace", "country", "sku"],
    )

    diagnostics = diagnose_metrics(metrics)

    assert list(diagnostics.columns) == [
        "marketplace",
        "country",
        "sku",
        *DIAGNOSTIC_ISSUE_COLUMNS,
    ]
    assert all(
        str(diagnostics[column].dtype) == "object"
        for column in ("code", "severity", "metric", "evidence", "message")
    )
    assert str(diagnostics["actual_value"].dtype) == "Float64"
    assert str(diagnostics["threshold"].dtype) == "Float64"
    assert isinstance(diagnostics.index, pd.RangeIndex)


def test_input_dataframe_is_not_modified_and_output_is_deterministic() -> None:
    metrics = metrics_from_rows(
        make_row(
            sku="A",
            impressions=1000,
            clicks=9,
            orders=1,
        ),
        make_row(
            sku="B",
            clicks=100,
            orders=1,
        ),
    )
    before = metrics.copy(deep=True)

    first = diagnose_metrics(metrics)
    second = diagnose_metrics(metrics)

    pd.testing.assert_frame_equal(metrics, before)
    pd.testing.assert_frame_equal(first, second)


def test_missing_diagnostic_input_column_is_structured_error() -> None:
    metrics = metrics_from_rows(make_row()).drop(columns=["ctr"])

    with pytest.raises(DiagnosticsError) as exc_info:
        diagnose_metrics(metrics)

    assert exc_info.value.code == "MISSING_DIAGNOSTIC_INPUT_COLUMN"
    assert "ctr" in exc_info.value.message


def test_non_dataframe_input_is_structured_error() -> None:
    with pytest.raises(DiagnosticsError) as exc_info:
        diagnose_metrics([])  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_DIAGNOSTIC_INPUT"


def test_invalid_metric_value_is_structured_error() -> None:
    metrics = metrics_from_rows(make_row()).assign(ctr="abc")

    with pytest.raises(DiagnosticsError) as exc_info:
        diagnose_metrics(metrics)

    assert exc_info.value.code == "INVALID_DIAGNOSTIC_INPUT_VALUE"
    assert isinstance(exc_info.value.__cause__, TypeError)


def test_sample_loader_validator_metrics_diagnostics_integration() -> None:
    sample_path = Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"

    raw_data = load_file(sample_path)
    validation = validate_dataframe(raw_data)
    metrics = calculate_metrics(validation.clean_data, group_by="sku")
    diagnostics = diagnose_metrics(metrics)

    assert len(raw_data) == 23
    assert validation.report.valid_rows == 14
    assert len(metrics) == 12
    assert len(diagnostics) == 11
    assert Counter(codes(diagnostics)) == {
        HIGH_IMPRESSIONS_LOW_CTR: 1,
        LOW_CVR: 2,
        CLICKS_WITHOUT_ORDERS: 1,
        SPEND_WITHOUT_ORDERS: 1,
        LOW_ROAS: 3,
        HIGH_REFUND_RATE: 2,
        OUT_OF_STOCK: 1,
    }

    issues_by_sku = {
        sku: group["code"].tolist()
        for sku, group in diagnostics.groupby("sku", sort=False)
    }
    assert issues_by_sku == {
        "SKU-HIGH-REFUND": [HIGH_REFUND_RATE],
        "SKU-LOW-CTR": [HIGH_IMPRESSIONS_LOW_CTR],
        "SKU-LOW-CVR": [LOW_CVR, LOW_ROAS],
        "SKU-LOW-ROAS": [LOW_ROAS],
        "SKU-NO-ORDER": [
            LOW_CVR,
            CLICKS_WITHOUT_ORDERS,
            SPEND_WITHOUT_ORDERS,
            LOW_ROAS,
        ],
        "SKU-REFUND-WARNING": [HIGH_REFUND_RATE],
        "SKU-STOCKOUT": [OUT_OF_STOCK],
    }
    assert "SKU-NORMAL-US" not in issues_by_sku
    assert "SKU-ZERO-DENOM" not in issues_by_sku
    assert "SKU-ORDER-WARNING" not in issues_by_sku
    assert "SKU-CLICK-ERROR" not in metrics["sku"].tolist()
    assert "SKU-KEY-CONFLICT" not in metrics["sku"].tolist()

    assert DEMO_LOW_CTR_THRESHOLD == 0.01
    assert DEMO_LOW_CVR_THRESHOLD == 0.02
    assert DEMO_LOW_ROAS_THRESHOLD == 1.0
    assert DEMO_HIGH_REFUND_RATE_THRESHOLD == 0.10
    assert DEMO_MIN_IMPRESSIONS_FOR_LOW_CTR == 1000
    assert DEMO_MIN_CLICKS_FOR_LOW_CVR == 50
    assert DEMO_MIN_CLICKS_WITHOUT_ORDERS == 20
    assert DEMO_MIN_ORDERS_FOR_HIGH_REFUND_RATE == 10
