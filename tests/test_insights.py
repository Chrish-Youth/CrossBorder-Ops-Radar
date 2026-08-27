from __future__ import annotations

import copy
import csv
from datetime import date, datetime
from io import StringIO
import json
from pathlib import Path

import pandas as pd
import pytest

from src.config import REQUIRED_COLUMNS
from src.insights import (
    INSIGHT_CONTEXT_LIMITATIONS,
    INSIGHT_CONTEXT_TOO_LARGE,
    INSIGHT_CONTEXT_VERSION,
    INVALID_INSIGHT_INPUT,
    MAX_INSIGHT_DIAGNOSTIC_SIGNALS,
    MAX_INSIGHT_EVIDENCE_DEPTH,
    MAX_INSIGHT_METRIC_RECORDS,
    NON_FINITE_INSIGHT_VALUE,
    PIPELINE_NOT_ANALYZABLE,
    InsightContext,
    InsightContextError,
    build_insight_context,
)
from src.pipeline import PipelineResult, PipelineStatus, run_pipeline


SAMPLE_PATH = Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"


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


def csv_content(
    *rows: dict[str, object],
    columns: tuple[str, ...] = REQUIRED_COLUMNS,
) -> bytes:
    text = StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue().encode("utf-8")


def sample_result(group_by: object = "sku") -> PipelineResult:
    return run_pipeline(SAMPLE_PATH, group_by=group_by)  # type: ignore[arg-type]


def with_frames(
    source: PipelineResult,
    *,
    metrics: pd.DataFrame,
    diagnostics: pd.DataFrame,
) -> PipelineResult:
    return PipelineResult(
        status=PipelineStatus.SUCCESS,
        validation=source.validation,
        metrics=metrics,
        diagnostics=diagnostics,
    )


def repeated_rows(dataframe: pd.DataFrame, count: int) -> pd.DataFrame:
    if count == 0:
        return dataframe.iloc[0:0].copy()
    return pd.concat(
        [dataframe.iloc[[0]].copy() for _ in range(count)],
        ignore_index=True,
    )


def nested_evidence(depth: int) -> object:
    value: object = "leaf"
    for position in range(depth):
        value = {f"level_{position}": value}
    return value


def with_evidence(source: PipelineResult, evidence: object) -> PipelineResult:
    diagnostics = source.diagnostics.copy(deep=True)  # type: ignore[union-attr]
    diagnostics.at[0, "evidence"] = evidence
    return with_frames(
        source,
        metrics=source.metrics,  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )


def test_sample_context_scope_and_top_level_contract() -> None:
    context = build_insight_context(sample_result())

    assert isinstance(context, InsightContext)
    assert context.version == INSIGHT_CONTEXT_VERSION == "1"
    assert context.analysis_scope == {
        "group_dimensions": ["sku"],
        "metric_group_count": 12,
        "diagnostic_signal_count": 11,
        "valid_rows": 14,
        "excluded_rows": 9,
        "warning_rows": 3,
    }
    assert len(context.metric_records) == 12
    assert len(context.diagnostic_signals) == 11
    assert context.limitations == INSIGHT_CONTEXT_LIMITATIONS
    assert set(context.to_dict()) == {
        "version",
        "analysis_scope",
        "metric_records",
        "diagnostic_signals",
        "limitations",
    }


def test_sample_metric_record_matches_metrics_without_recalculation() -> None:
    result = sample_result()
    context = build_insight_context(result)
    record = next(
        item for item in context.metric_records if item["group"]["sku"] == "SKU-NORMAL-US"
    )
    source = result.metrics.loc[result.metrics["sku"] == "SKU-NORMAL-US"].iloc[0]  # type: ignore[union-attr]

    assert record["group"] == {"sku": "SKU-NORMAL-US"}
    assert record["base_measures"] == {
        "impressions": 22000,
        "clicks": 660,
        "orders": 66,
        "units_sold": 75,
        "sales": pytest.approx(1979.34),
        "ad_spend": pytest.approx(310.0),
        "refunds": 3,
        "inventory": 95,
    }
    assert record["derived_metrics"]["ctr"] == pytest.approx(source["ctr"])
    assert record["derived_metrics"]["cvr"] == pytest.approx(source["cvr"])
    assert record["derived_metrics"]["roas"] == pytest.approx(source["roas"])
    assert record["derived_metrics"]["gmv"] == pytest.approx(source["gmv"])


def test_builder_preserves_existing_metric_value_and_precision() -> None:
    result = sample_result()
    metrics = result.metrics.copy(deep=True)  # type: ignore[union-attr]
    metrics.loc[0, "ctr"] = 0.123456789012345
    modified = with_frames(
        result,
        metrics=metrics,
        diagnostics=result.diagnostics.copy(deep=True),  # type: ignore[union-attr]
    )

    context = build_insight_context(modified)

    assert context.metric_records[0]["derived_metrics"]["ctr"] == (
        0.123456789012345
    )


def test_sample_diagnostic_signal_matches_diagnostics_exactly() -> None:
    result = sample_result()
    context = build_insight_context(result)
    signal = next(
        item
        for item in context.diagnostic_signals
        if item["code"] == "HIGH_IMPRESSIONS_LOW_CTR"
    )
    source = result.diagnostics.loc[  # type: ignore[union-attr]
        result.diagnostics["code"] == "HIGH_IMPRESSIONS_LOW_CTR"  # type: ignore[union-attr]
    ].iloc[0]

    assert signal == {
        "group": {"sku": "SKU-LOW-CTR"},
        "code": "HIGH_IMPRESSIONS_LOW_CTR",
        "severity": "Warning",
        "metric": "ctr",
        "actual_value": pytest.approx(0.005),
        "threshold": pytest.approx(0.01),
        "evidence": {
            "impressions": 10000,
            "minimum_impressions": 1000,
        },
        "message": source["message"],
    }


def test_multiple_diagnostics_for_one_group_remain_separate_and_ordered() -> None:
    context = build_insight_context(sample_result())

    codes = [
        signal["code"]
        for signal in context.diagnostic_signals
        if signal["group"] == {"sku": "SKU-NO-ORDER"}
    ]

    assert codes == [
        "LOW_CVR",
        "CLICKS_WITHOUT_ORDERS",
        "SPEND_WITHOUT_ORDERS",
        "LOW_ROAS",
    ]


def test_metric_and_diagnostic_row_order_is_preserved() -> None:
    result = sample_result()
    context = build_insight_context(result)

    assert [record["group"]["sku"] for record in context.metric_records] == (
        result.metrics["sku"].tolist()  # type: ignore[index]
    )
    assert [signal["code"] for signal in context.diagnostic_signals] == (
        result.diagnostics["code"].tolist()  # type: ignore[index]
    )


def test_missing_ratios_become_none_while_zero_remains_zero() -> None:
    context = build_insight_context(sample_result())
    zero_denom = next(
        record
        for record in context.metric_records
        if record["group"] == {"sku": "SKU-ZERO-DENOM"}
    )
    no_order = next(
        record
        for record in context.metric_records
        if record["group"] == {"sku": "SKU-NO-ORDER"}
    )

    assert zero_denom["derived_metrics"]["ctr"] is None
    assert zero_denom["derived_metrics"]["roas"] is None
    assert zero_denom["derived_metrics"]["gmv"] == 0.0
    assert no_order["derived_metrics"]["cvr"] == 0.0
    assert no_order["derived_metrics"]["aov"] is None


def test_context_is_strictly_json_serializable_with_native_values() -> None:
    context = build_insight_context(sample_result())

    serialized = json.dumps(
        context.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )

    assert isinstance(serialized, str)
    assert '"version": "1"' in serialized
    assert "NaN" not in serialized
    assert "Infinity" not in serialized


def test_date_group_is_iso_text_in_context() -> None:
    result = run_pipeline(
        csv_content(
            make_row(date="1600-01-01", sku="EARLY"),
            make_row(date="9999-12-31", sku="LATE"),
        ),
        filename="dates.csv",
        group_by="date",
    )

    context = build_insight_context(result)

    assert [record["group"] for record in context.metric_records] == [
        {"date": "1600-01-01"},
        {"date": "9999-12-31"},
    ]
    json.dumps(context.to_dict(), allow_nan=False)


def test_overall_group_context_is_always_empty_dict() -> None:
    context = build_insight_context(sample_result(group_by=None))

    assert context.analysis_scope["group_dimensions"] == []
    assert len(context.metric_records) == 1
    assert context.metric_records[0]["group"] == {}
    assert all(signal["group"] == {} for signal in context.diagnostic_signals)


def test_dimensionless_empty_success_is_legal() -> None:
    result = sample_result(group_by=None)
    empty = with_frames(
        result,
        metrics=result.metrics.iloc[0:0].copy(),  # type: ignore[union-attr]
        diagnostics=result.diagnostics.iloc[0:0].copy(),  # type: ignore[union-attr]
    )

    context = build_insight_context(empty)

    assert context.analysis_scope["group_dimensions"] == []
    assert context.analysis_scope["metric_group_count"] == 0
    assert context.metric_records == ()
    assert context.diagnostic_signals == ()


def test_dimensionless_two_metric_rows_are_rejected() -> None:
    result = sample_result(group_by=None)
    invalid = with_frames(
        result,
        metrics=repeated_rows(result.metrics, 2),  # type: ignore[arg-type]
        diagnostics=result.diagnostics,  # type: ignore[arg-type]
    )

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)

    assert caught.value.code == INVALID_INSIGHT_INPUT


def test_dropped_sample_dimensions_do_not_create_overall_like_records() -> None:
    result = sample_result()
    invalid = with_frames(
        result,
        metrics=result.metrics.drop(columns="sku"),  # type: ignore[union-attr]
        diagnostics=result.diagnostics.drop(columns="sku"),  # type: ignore[union-attr]
    )

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)

    assert len(invalid.metrics) == 12  # type: ignore[arg-type]
    assert len(invalid.diagnostics) == 11  # type: ignore[arg-type]
    assert caught.value.code == INVALID_INSIGHT_INPUT


def test_dimensionless_overall_allows_multiple_diagnostic_signals() -> None:
    result = sample_result(group_by=None)
    legal = with_frames(
        result,
        metrics=result.metrics,  # type: ignore[arg-type]
        diagnostics=repeated_rows(result.diagnostics, 3),  # type: ignore[arg-type]
    )

    context = build_insight_context(legal)

    assert len(context.metric_records) == 1
    assert len(context.diagnostic_signals) == 3
    assert context.metric_records[0]["group"] == {}
    assert all(signal["group"] == {} for signal in context.diagnostic_signals)


def test_success_with_no_diagnostics_is_legal_and_does_not_create_signal() -> None:
    result = run_pipeline(
        csv_content(
            make_row(
                impressions=100,
                clicks=10,
                orders=2,
                units_sold=2,
                sales=100.0,
                ad_spend=10.0,
                refunds=0,
                inventory=10,
            )
        ),
        filename="normal.csv",
        group_by="sku",
    )

    context = build_insight_context(result)

    assert len(context.metric_records) == 1
    assert context.diagnostic_signals == ()
    assert context.analysis_scope["diagnostic_signal_count"] == 0


def test_empty_success_is_legal_and_preserves_empty_scope() -> None:
    result = run_pipeline(
        csv_content(make_row(impressions=10, clicks=11)),
        filename="all-excluded.csv",
        group_by="sku",
    )
    assert result.status is PipelineStatus.SUCCESS

    context = build_insight_context(result)

    assert context.analysis_scope == {
        "group_dimensions": ["sku"],
        "metric_group_count": 0,
        "diagnostic_signal_count": 0,
        "valid_rows": 0,
        "excluded_rows": 1,
        "warning_rows": 0,
    }
    assert context.metric_records == ()
    assert context.diagnostic_signals == ()
    json.dumps(context.to_dict(), allow_nan=False)


def test_validation_failed_is_not_analyzable() -> None:
    columns = tuple(column for column in REQUIRED_COLUMNS if column != "sku")
    result = run_pipeline(
        csv_content(make_row(), columns=columns),
        filename="missing-sku.csv",
        group_by="sku",
    )
    assert result.status is PipelineStatus.VALIDATION_FAILED

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(result)

    assert caught.value.code == PIPELINE_NOT_ANALYZABLE


@pytest.mark.parametrize("invalid", [None, {}, pd.DataFrame()])
def test_invalid_input_type_has_stable_error(invalid: object) -> None:
    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)  # type: ignore[arg-type]

    assert caught.value.code == INVALID_INSIGHT_INPUT


def test_success_with_invalid_stage_objects_is_rejected() -> None:
    result = sample_result()
    invalid = PipelineResult(
        status=PipelineStatus.SUCCESS,
        validation=result.validation,
        metrics=None,
        diagnostics=None,
    )

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)

    assert caught.value.code == INVALID_INSIGHT_INPUT


@pytest.mark.parametrize("frame_name", ["metrics", "diagnostics"])
def test_missing_required_context_column_is_rejected(frame_name: str) -> None:
    result = sample_result()
    metrics = result.metrics.copy(deep=True)  # type: ignore[union-attr]
    diagnostics = result.diagnostics.copy(deep=True)  # type: ignore[union-attr]
    if frame_name == "metrics":
        metrics.drop(columns="ctr", inplace=True)
    else:
        diagnostics.drop(columns="code", inplace=True)
    invalid = with_frames(result, metrics=metrics, diagnostics=diagnostics)

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)

    assert caught.value.code == INVALID_INSIGHT_INPUT


def test_mismatched_group_dimensions_are_rejected() -> None:
    result = sample_result()
    diagnostics = result.diagnostics.drop(columns="sku")  # type: ignore[union-attr]
    invalid = with_frames(
        result,
        metrics=result.metrics,  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)

    assert caught.value.code == INVALID_INSIGHT_INPUT


def test_metric_limit_accepts_maximum_and_rejects_next_record() -> None:
    result = sample_result()
    diagnostics = result.diagnostics.iloc[0:0].copy()  # type: ignore[union-attr]
    maximum = repeated_rows(result.metrics, MAX_INSIGHT_METRIC_RECORDS)  # type: ignore[arg-type]
    accepted = with_frames(result, metrics=maximum, diagnostics=diagnostics)

    assert len(build_insight_context(accepted).metric_records) == (
        MAX_INSIGHT_METRIC_RECORDS
    )

    oversized = with_frames(
        result,
        metrics=repeated_rows(result.metrics, MAX_INSIGHT_METRIC_RECORDS + 1),  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )
    with pytest.raises(InsightContextError) as caught:
        build_insight_context(oversized)

    assert caught.value.code == INSIGHT_CONTEXT_TOO_LARGE


def test_diagnostic_limit_accepts_maximum_and_rejects_next_signal() -> None:
    result = sample_result()
    maximum = repeated_rows(
        result.diagnostics,  # type: ignore[arg-type]
        MAX_INSIGHT_DIAGNOSTIC_SIGNALS,
    )
    accepted = with_frames(
        result,
        metrics=result.metrics,  # type: ignore[arg-type]
        diagnostics=maximum,
    )

    assert len(build_insight_context(accepted).diagnostic_signals) == (
        MAX_INSIGHT_DIAGNOSTIC_SIGNALS
    )

    oversized = with_frames(
        result,
        metrics=result.metrics,  # type: ignore[arg-type]
        diagnostics=repeated_rows(
            result.diagnostics,  # type: ignore[arg-type]
            MAX_INSIGHT_DIAGNOSTIC_SIGNALS + 1,
        ),
    )
    with pytest.raises(InsightContextError) as caught:
        build_insight_context(oversized)

    assert caught.value.code == INSIGHT_CONTEXT_TOO_LARGE


@pytest.mark.parametrize("non_finite", [float("inf"), float("-inf")])
def test_non_finite_values_are_structured_failures(non_finite: float) -> None:
    result = sample_result()
    metrics = result.metrics.copy(deep=True)  # type: ignore[union-attr]
    metrics.loc[0, "roas"] = non_finite
    invalid = with_frames(
        result,
        metrics=metrics,
        diagnostics=result.diagnostics,  # type: ignore[arg-type]
    )

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)

    assert caught.value.code == NON_FINITE_INSIGHT_VALUE


def test_nested_evidence_is_native_json_data_and_independent() -> None:
    result = sample_result()
    diagnostics = result.diagnostics.copy(deep=True)  # type: ignore[union-attr]
    evidence = {
        "count": pd.Series([5], dtype="Int64").iloc[0],
        "ratio": pd.Series([0.25], dtype="Float64").iloc[0],
        "when": date(2026, 8, 27),
        "at": datetime(2026, 8, 27, 12, 34, 56),
        "timestamp": pd.Timestamp("2026-08-27T12:34:56"),
        "missing": pd.NA,
        "nan": float("nan"),
        "unicode": "跨境电商",
        "items": (1, {"zero": 0}, ["stable"]),
    }
    diagnostics.at[0, "evidence"] = evidence
    modified = with_frames(
        result,
        metrics=result.metrics,  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )

    context = build_insight_context(modified)
    normalized = context.diagnostic_signals[0]["evidence"]

    assert normalized == {
        "count": 5,
        "ratio": 0.25,
        "when": "2026-08-27",
        "at": "2026-08-27T12:34:56",
        "timestamp": "2026-08-27T12:34:56",
        "missing": None,
        "nan": None,
        "unicode": "跨境电商",
        "items": [1, {"zero": 0}, ["stable"]],
    }
    json.dumps(context.to_dict(), allow_nan=False)
    normalized["items"][1]["zero"] = 99
    assert evidence["items"][1]["zero"] == 0


def test_evidence_at_maximum_depth_is_accepted() -> None:
    result = sample_result()

    context = build_insight_context(
        with_evidence(result, nested_evidence(MAX_INSIGHT_EVIDENCE_DEPTH))
    )

    json.dumps(context.to_dict(), ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    "depth",
    [MAX_INSIGHT_EVIDENCE_DEPTH + 1, 500],
)
def test_evidence_beyond_maximum_depth_is_a_structured_failure(depth: int) -> None:
    result = sample_result()

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(with_evidence(result, nested_evidence(depth)))

    assert caught.value.code == INVALID_INSIGHT_INPUT
    assert "深度" in caught.value.message


def test_cyclic_evidence_is_a_distinct_structured_failure() -> None:
    result = sample_result()
    evidence: dict[str, object] = {}
    evidence["self"] = evidence

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(with_evidence(result, evidence))

    assert caught.value.code == INVALID_INSIGHT_INPUT
    assert "循环引用" in caught.value.message


def test_set_evidence_is_rejected_instead_of_becoming_nondeterministic() -> None:
    result = sample_result()

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(with_evidence(result, {"unstable", "order"}))

    assert caught.value.code == INVALID_INSIGHT_INPUT


@pytest.mark.parametrize(
    ("location", "non_finite"),
    [("evidence", float("inf")), ("group", float("-inf"))],
)
def test_non_finite_representative_context_paths_are_rejected(
    location: str,
    non_finite: float,
) -> None:
    result = sample_result()
    if location == "evidence":
        invalid = with_evidence(result, {"value": non_finite})
    else:
        metrics = result.metrics.copy(deep=True)  # type: ignore[union-attr]
        metrics["sku"] = metrics["sku"].astype(object)
        metrics.at[0, "sku"] = non_finite
        invalid = with_frames(
            result,
            metrics=metrics,
            diagnostics=result.diagnostics,  # type: ignore[arg-type]
        )

    with pytest.raises(InsightContextError) as caught:
        build_insight_context(invalid)

    assert caught.value.code == NON_FINITE_INSIGHT_VALUE


def test_missing_group_and_nested_evidence_values_become_none() -> None:
    result = sample_result()
    metrics = result.metrics.copy(deep=True)  # type: ignore[union-attr]
    metrics.at[0, "sku"] = pd.NA
    diagnostics = result.diagnostics.copy(deep=True)  # type: ignore[union-attr]
    diagnostics.at[0, "evidence"] = {
        "nan": float("nan"),
        "missing": pd.NA,
    }

    context = build_insight_context(
        with_frames(result, metrics=metrics, diagnostics=diagnostics)
    )

    assert context.metric_records[0]["group"]["sku"] is None
    assert context.diagnostic_signals[0]["evidence"] == {
        "nan": None,
        "missing": None,
    }
    json.dumps(context.to_dict(), allow_nan=False)


def test_extra_stage_columns_do_not_leak_into_context_schema() -> None:
    result = sample_result()
    metrics = result.metrics.copy(deep=True)  # type: ignore[union-attr]
    diagnostics = result.diagnostics.copy(deep=True)  # type: ignore[union-attr]
    metrics["extra_debug_column"] = "METRICS-EXTRA-MARKER"
    metrics["temporary_score"] = 99
    diagnostics["internal_note"] = "DIAGNOSTICS-EXTRA-MARKER"
    diagnostics["random_extra"] = {"not": "included"}

    payload = build_insight_context(
        with_frames(result, metrics=metrics, diagnostics=diagnostics)
    ).to_dict()
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)

    assert "extra_debug_column" not in serialized
    assert "temporary_score" not in serialized
    assert "internal_note" not in serialized
    assert "random_extra" not in serialized
    assert "METRICS-EXTRA-MARKER" not in serialized
    assert "DIAGNOSTICS-EXTRA-MARKER" not in serialized


@pytest.mark.parametrize(
    ("group_by", "expected_dimensions", "expected_metric_records"),
    [
        (None, [], 1),
        ("sku", ["sku"], 12),
        ("marketplace", ["marketplace"], 2),
        ("country", ["country"], 2),
        (["marketplace", "country"], ["marketplace", "country"], 3),
        (
            ["marketplace", "country", "sku"],
            ["marketplace", "country", "sku"],
            12,
        ),
        (
            ["date", "marketplace", "country", "sku"],
            ["date", "marketplace", "country", "sku"],
            14,
        ),
    ],
)
def test_supported_dimension_matrix_is_stable(
    group_by: object,
    expected_dimensions: list[str],
    expected_metric_records: int,
) -> None:
    context = build_insight_context(sample_result(group_by=group_by))

    assert context.analysis_scope["group_dimensions"] == expected_dimensions
    assert len(context.metric_records) == expected_metric_records
    assert all(
        list(record["group"]) == expected_dimensions
        for record in context.metric_records
    )


def test_record_limits_are_checked_before_value_normalization() -> None:
    result = sample_result()
    oversized_metrics = repeated_rows(
        result.metrics,  # type: ignore[arg-type]
        MAX_INSIGHT_METRIC_RECORDS + 1,
    )
    oversized_metrics.loc[0, "roas"] = float("inf")
    no_diagnostics = result.diagnostics.iloc[0:0].copy()  # type: ignore[union-attr]

    with pytest.raises(InsightContextError) as metrics_error:
        build_insight_context(
            with_frames(
                result,
                metrics=oversized_metrics,
                diagnostics=no_diagnostics,
            )
        )

    oversized_diagnostics = repeated_rows(
        result.diagnostics,  # type: ignore[arg-type]
        MAX_INSIGHT_DIAGNOSTIC_SIGNALS + 1,
    )
    oversized_diagnostics.at[0, "evidence"] = {"unsupported"}

    with pytest.raises(InsightContextError) as diagnostics_error:
        build_insight_context(
            with_frames(
                result,
                metrics=result.metrics,  # type: ignore[arg-type]
                diagnostics=oversized_diagnostics,
            )
        )

    assert metrics_error.value.code == INSIGHT_CONTEXT_TOO_LARGE
    assert diagnostics_error.value.code == INSIGHT_CONTEXT_TOO_LARGE


def test_build_does_not_modify_pipeline_frames_or_evidence() -> None:
    result = sample_result()
    metrics_before = result.metrics.copy(deep=True)  # type: ignore[union-attr]
    diagnostics_before = result.diagnostics.copy(deep=True)  # type: ignore[union-attr]
    evidence_before = copy.deepcopy(result.diagnostics.loc[0, "evidence"])  # type: ignore[union-attr]

    context = build_insight_context(result)
    context.metric_records[0]["base_measures"]["impressions"] = -1
    context.diagnostic_signals[0]["evidence"]["orders"] = -1

    pd.testing.assert_frame_equal(result.metrics, metrics_before)
    pd.testing.assert_frame_equal(result.diagnostics, diagnostics_before)
    assert result.diagnostics.loc[0, "evidence"] == evidence_before  # type: ignore[union-attr]


def test_to_dict_returns_an_independent_copy() -> None:
    context = build_insight_context(sample_result())
    payload = context.to_dict()

    payload["analysis_scope"]["metric_group_count"] = -1
    payload["metric_records"][0]["group"]["sku"] = "CHANGED"
    payload["diagnostic_signals"][0]["evidence"].clear()

    assert context.analysis_scope["metric_group_count"] == 12
    assert context.metric_records[0]["group"]["sku"] == "SKU-DUP"
    assert context.diagnostic_signals[0]["evidence"]


def test_context_build_is_deterministic() -> None:
    result = sample_result()

    first = build_insight_context(result).to_dict()
    second = build_insight_context(result).to_dict()

    assert first == second
    assert json.dumps(first, sort_keys=True, allow_nan=False) == json.dumps(
        second,
        sort_keys=True,
        allow_nan=False,
    )


def test_context_contains_no_raw_clean_filename_or_validation_issue_details() -> None:
    payload = build_insight_context(sample_result()).to_dict()
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)

    assert "raw_data" not in payload
    assert "clean_data" not in payload
    assert "filename" not in payload
    assert "validation_issues" not in payload
    assert "sample_ecommerce_data.csv" not in serialized


def test_builder_does_not_call_pipeline_stages_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = sample_result()

    def must_not_run(*args: object, **kwargs: object) -> None:
        pytest.fail("Insight Context Builder must not invoke pipeline stages")

    monkeypatch.setattr("src.loader.load_file", must_not_run)
    monkeypatch.setattr("src.validator.validate_dataframe", must_not_run)
    monkeypatch.setattr("src.metrics.calculate_metrics", must_not_run)
    monkeypatch.setattr("src.diagnostics.diagnose_metrics", must_not_run)

    context = build_insight_context(result)

    assert context.analysis_scope["metric_group_count"] == 12
