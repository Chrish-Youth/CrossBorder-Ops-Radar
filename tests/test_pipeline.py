from __future__ import annotations

import csv
from collections import Counter
from dataclasses import fields
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
import pytest

import src.pipeline as pipeline_module
from src.config import (
    CLICKS_WITHOUT_ORDERS,
    HIGH_IMPRESSIONS_LOW_CTR,
    HIGH_REFUND_RATE,
    LOW_CVR,
    LOW_ROAS,
    OUT_OF_STOCK,
    REQUIRED_COLUMNS,
    SPEND_WITHOUT_ORDERS,
)
from src.diagnostics import DIAGNOSTIC_ISSUE_COLUMNS, DiagnosticsError
from src.loader import DataLoadError
from src.metrics import (
    BASE_MEASURES,
    DERIVED_METRICS,
    RATIO_METRICS,
    MetricsCalculationError,
)
from src.pipeline import (
    INVALID_STAGE_RESULT,
    PipelineError,
    PipelineResult,
    PipelineStatus,
    run_pipeline,
)
from src.validator import (
    ValidationReport,
    ValidationResult,
)


SAMPLE_PATH = (
    Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"
)


class NamedBytesIO(BytesIO):
    def __init__(self, content: bytes, name: str) -> None:
        super().__init__(content)
        self.name = name


class ReadOnceStream:
    name = "upload.csv"

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._consumed = False

    def read(self) -> bytes:
        if self._consumed:
            return b""
        self._consumed = True
        return self._content


class BrokenSeekStream:
    name = "upload.csv"

    def read(self) -> bytes:
        return b"unreachable"

    def seek(self, position: int) -> None:
        raise OSError("stream is not seekable")


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
    for row in rows:
        writer.writerow(row)
    return text.getvalue().encode("utf-8")


def test_execution_order_and_stage_results_are_passed_by_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source = object()
    requested_group_by = ["marketplace", "country", "sku"]
    raw = pd.DataFrame({"raw": [1]})
    clean = pd.DataFrame({"clean": [1]})
    metrics = pd.DataFrame({"metrics": [1]})
    diagnostics = pd.DataFrame({"diagnostics": [1]})
    validation = ValidationResult(
        clean_data=clean,
        report=ValidationReport(total_rows=1, valid_rows=1),
    )

    def fake_load_file(
        received_source: object,
        filename: str | None = None,
    ) -> pd.DataFrame:
        events.append("load")
        assert received_source is source
        assert filename == "upload.csv"
        return raw

    def fake_validate(dataframe: pd.DataFrame) -> ValidationResult:
        events.append("validate")
        assert dataframe is raw
        return validation

    def fake_metrics(
        dataframe: pd.DataFrame,
        group_by: object = None,
    ) -> pd.DataFrame:
        events.append("metrics")
        assert dataframe is clean
        assert group_by is requested_group_by
        return metrics

    def fake_diagnostics(dataframe: pd.DataFrame) -> pd.DataFrame:
        events.append("diagnostics")
        assert dataframe is metrics
        return diagnostics

    monkeypatch.setattr(pipeline_module, "load_file", fake_load_file)
    monkeypatch.setattr(pipeline_module, "validate_dataframe", fake_validate)
    monkeypatch.setattr(pipeline_module, "calculate_metrics", fake_metrics)
    monkeypatch.setattr(pipeline_module, "diagnose_metrics", fake_diagnostics)

    result = run_pipeline(
        source,  # type: ignore[arg-type]
        filename="upload.csv",
        group_by=requested_group_by,
    )

    assert events == ["load", "validate", "metrics", "diagnostics"]
    assert result.status is PipelineStatus.SUCCESS
    assert result.validation is validation
    assert result.metrics is metrics
    assert result.diagnostics is diagnostics


def test_invalid_validation_result_raises_pipeline_error_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downstream_calls: list[str] = []

    def must_not_calculate(*args: object, **kwargs: object) -> None:
        downstream_calls.append("metrics")

    def must_not_diagnose(*args: object, **kwargs: object) -> None:
        downstream_calls.append("diagnostics")

    monkeypatch.setattr(pipeline_module, "validate_dataframe", lambda _: None)
    monkeypatch.setattr(
        pipeline_module,
        "calculate_metrics",
        must_not_calculate,
    )
    monkeypatch.setattr(
        pipeline_module,
        "diagnose_metrics",
        must_not_diagnose,
    )

    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(csv_content(make_row()), filename="input.csv")

    assert exc_info.value.code == INVALID_STAGE_RESULT
    assert exc_info.value.stage == "validation"
    assert "validate_dataframe" in exc_info.value.message
    assert "ValidationResult" in exc_info.value.message
    assert downstream_calls == []


def test_invalid_metrics_result_raises_pipeline_error_and_stops_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics_called = False

    def must_not_diagnose(*args: object, **kwargs: object) -> None:
        nonlocal diagnostics_called
        diagnostics_called = True

    monkeypatch.setattr(pipeline_module, "calculate_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline_module,
        "diagnose_metrics",
        must_not_diagnose,
    )

    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(csv_content(make_row()), filename="input.csv")

    assert exc_info.value.code == INVALID_STAGE_RESULT
    assert exc_info.value.stage == "metrics"
    assert "calculate_metrics" in exc_info.value.message
    assert "DataFrame" in exc_info.value.message
    assert diagnostics_called is False


def test_invalid_diagnostics_result_raises_pipeline_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "diagnose_metrics",
        lambda _dataframe: None,
    )

    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(csv_content(make_row()), filename="input.csv")

    assert exc_info.value.code == INVALID_STAGE_RESULT
    assert exc_info.value.stage == "diagnostics"
    assert "diagnose_metrics" in exc_info.value.message
    assert "DataFrame" in exc_info.value.message


def test_validation_fatal_short_circuits_metrics_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    columns = tuple(column for column in REQUIRED_COLUMNS if column != "sku")
    content = csv_content(make_row(), columns=columns)

    def must_not_run(*args: object, **kwargs: object) -> None:
        pytest.fail("Fatal Validation 后不得执行下游阶段。")

    monkeypatch.setattr(pipeline_module, "calculate_metrics", must_not_run)
    monkeypatch.setattr(pipeline_module, "diagnose_metrics", must_not_run)

    result = run_pipeline(content, filename="missing-sku.csv", group_by="sku")

    assert result.status is PipelineStatus.VALIDATION_FAILED
    assert result.validation.report.total_rows == 1
    assert result.validation.report.valid_rows == 0
    assert result.validation.report.excluded_rows == 1
    assert [issue.code for issue in result.validation.report.fatal_errors] == [
        "MISSING_REQUIRED_COLUMN"
    ]
    assert result.metrics is None
    assert result.diagnostics is None


def test_metrics_failure_propagates_and_short_circuits_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = MetricsCalculationError("TEST_METRICS_FAILURE", "metrics failed")

    def fail_metrics(*args: object, **kwargs: object) -> None:
        raise failure

    def must_not_diagnose(*args: object, **kwargs: object) -> None:
        pytest.fail("Metrics 失败后不得执行 Diagnostics。")

    monkeypatch.setattr(pipeline_module, "calculate_metrics", fail_metrics)
    monkeypatch.setattr(pipeline_module, "diagnose_metrics", must_not_diagnose)

    with pytest.raises(MetricsCalculationError) as exc_info:
        run_pipeline(csv_content(make_row()), filename="input.csv")

    assert exc_info.value is failure


def test_diagnostics_failure_propagates_without_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = DiagnosticsError("TEST_DIAGNOSTICS_FAILURE", "diagnostics failed")

    def fail_diagnostics(dataframe: pd.DataFrame) -> None:
        raise failure

    monkeypatch.setattr(pipeline_module, "diagnose_metrics", fail_diagnostics)

    with pytest.raises(DiagnosticsError) as exc_info:
        run_pipeline(csv_content(make_row()), filename="input.csv")

    assert exc_info.value is failure


@pytest.mark.parametrize(
    ("source", "filename", "expected_code"),
    [
        (b"data", "input.txt", "UNSUPPORTED_FILE_TYPE"),
        (b"", "input.csv", "EMPTY_FILE"),
        (b"first,second\n1\n", "input.csv", "MALFORMED_CSV"),
        (b"not an xlsx workbook", "input.xlsx", "FILE_READ_ERROR"),
    ],
    ids=["unsupported", "empty", "malformed-csv", "corrupt-xlsx"],
)
def test_loader_failures_propagate_unchanged(
    source: bytes,
    filename: str,
    expected_code: str,
) -> None:
    with pytest.raises(DataLoadError) as exc_info:
        run_pipeline(source, filename=filename)

    assert exc_info.value.code == expected_code


def test_explicit_filename_overrides_path_suffix() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        run_pipeline(SAMPLE_PATH, filename="override.xlsx")

    assert exc_info.value.code == "FILE_READ_ERROR"


def test_explicit_filename_overrides_file_like_name() -> None:
    source = NamedBytesIO(csv_content(make_row()), "actual.csv")

    with pytest.raises(DataLoadError) as exc_info:
        run_pipeline(source, filename="override.xlsx")

    assert exc_info.value.code == "FILE_READ_ERROR"


def test_file_like_name_is_used_when_explicit_filename_is_absent() -> None:
    source = NamedBytesIO(csv_content(make_row()), "actual.csv")

    result = run_pipeline(source, group_by="sku")

    assert result.status is PipelineStatus.SUCCESS
    assert result.metrics is not None
    assert result.metrics["sku"].tolist() == ["SKU-A"]


def test_bytes_without_filename_do_not_trigger_format_sniffing() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        run_pipeline(csv_content(make_row()))

    assert exc_info.value.code == "UNSUPPORTED_FILE_TYPE"


def test_csv_bytes_with_xlsx_filename_are_not_sniffed_as_csv() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        run_pipeline(csv_content(make_row()), filename="upload.xlsx")

    assert exc_info.value.code == "FILE_READ_ERROR"


def test_validation_error_excludes_bad_row_and_continues_pipeline() -> None:
    valid = make_row(
        sku="SKU-LOW-CTR",
        impressions=1000,
        clicks=5,
        orders=1,
        units_sold=1,
        sales=20.0,
        ad_spend=5.0,
    )
    invalid = make_row(
        date="2026-08-25",
        sku="SKU-CLICK-ERROR",
        impressions=10,
        clicks=11,
    )

    result = run_pipeline(
        csv_content(valid, invalid),
        filename="mixed.csv",
        group_by="sku",
    )

    assert result.status is PipelineStatus.SUCCESS
    assert result.validation.report.valid_rows == 1
    assert result.validation.report.excluded_rows == 1
    assert [issue.code for issue in result.validation.report.errors] == [
        "CLICKS_GT_IMPRESSIONS"
    ]
    assert result.metrics is not None
    assert result.metrics["sku"].tolist() == ["SKU-LOW-CTR"]
    assert result.diagnostics is not None
    assert result.diagnostics["code"].tolist() == [
        HIGH_IMPRESSIONS_LOW_CTR
    ]


def test_validation_warnings_are_retained_and_continue_pipeline() -> None:
    warning_row = make_row(
        sku="SKU-WARNING",
        impressions=100,
        clicks=10,
        orders=15,
        units_sold=15,
        sales=300.0,
        ad_spend=50.0,
        refunds=20,
    )

    result = run_pipeline(
        csv_content(warning_row),
        filename="warnings.csv",
        group_by="sku",
    )

    assert result.status is PipelineStatus.SUCCESS
    assert result.validation.report.valid_rows == 1
    assert result.validation.report.excluded_rows == 0
    assert {issue.code for issue in result.validation.report.warnings} == {
        "ORDERS_GT_CLICKS",
        "REFUNDS_GT_ORDERS",
    }
    assert result.metrics is not None
    assert result.metrics.loc[0, "cvr"] == 1.5
    assert result.metrics.loc[0, "refund_rate"] == pytest.approx(20 / 15)
    assert result.diagnostics is not None
    assert result.diagnostics["code"].tolist() == [HIGH_REFUND_RATE]


def test_all_rows_excluded_without_fatal_returns_successful_empty_results() -> None:
    invalid = make_row(impressions=10, clicks=11)

    result = run_pipeline(
        csv_content(invalid),
        filename="all-invalid.csv",
        group_by="sku",
    )

    assert result.status is PipelineStatus.SUCCESS
    assert not result.validation.report.has_fatal_errors
    assert result.validation.report.valid_rows == 0
    assert result.validation.report.excluded_rows == 1
    assert result.metrics is not None
    assert result.metrics.empty
    assert list(result.metrics.columns) == [
        "sku",
        *BASE_MEASURES,
        *DERIVED_METRICS,
    ]
    assert result.metrics["sku"].dtype == result.validation.clean_data["sku"].dtype
    assert all(
        str(result.metrics[column].dtype) == "Int64"
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
        str(result.metrics[column].dtype) == "Float64"
        for column in ("sales", "ad_spend", "gmv")
    )
    assert all(
        str(result.metrics[column].dtype) == "float64"
        for column in RATIO_METRICS
    )
    assert isinstance(result.metrics.index, pd.RangeIndex)
    assert result.diagnostics is not None
    assert result.diagnostics.empty
    assert result.diagnostics.columns.tolist() == [
        "sku",
        *DIAGNOSTIC_ISSUE_COLUMNS,
    ]
    assert result.diagnostics["sku"].dtype == result.metrics["sku"].dtype
    assert all(
        str(result.diagnostics[column].dtype) == "object"
        for column in ("code", "severity", "metric", "evidence", "message")
    )
    assert all(
        str(result.diagnostics[column].dtype) == "Float64"
        for column in ("actual_value", "threshold")
    )
    assert isinstance(result.diagnostics.index, pd.RangeIndex)


@pytest.mark.parametrize(
    ("group_by", "dimensions", "expected_rows"),
    [
        (None, [], 1),
        ([], [], 1),
        ((), [], 1),
        ("sku", ["sku"], 12),
        ("marketplace", ["marketplace"], 2),
        ("country", ["country"], 2),
        (["marketplace", "country"], ["marketplace", "country"], 3),
        (("marketplace", "country"), ["marketplace", "country"], 3),
        (("country", "marketplace"), ["country", "marketplace"], 3),
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
    ids=[
        "overall-none",
        "overall-list",
        "overall-tuple",
        "sku",
        "marketplace",
        "country",
        "marketplace-country-list",
        "marketplace-country-tuple",
        "tuple-order-preserved",
        "marketplace-country-sku",
        "full-business-key",
    ],
)
def test_group_by_is_forwarded_to_real_metrics(
    group_by: object,
    dimensions: list[str],
    expected_rows: int,
) -> None:
    result = run_pipeline(SAMPLE_PATH, group_by=group_by)  # type: ignore[arg-type]

    assert result.status is PipelineStatus.SUCCESS
    assert result.metrics is not None
    assert len(result.metrics) == expected_rows
    assert result.metrics.columns[: len(dimensions)].tolist() == dimensions
    assert result.diagnostics is not None
    assert result.diagnostics.columns[: len(dimensions)].tolist() == dimensions


def test_invalid_group_by_propagates_metrics_error() -> None:
    with pytest.raises(MetricsCalculationError) as exc_info:
        run_pipeline(SAMPLE_PATH, group_by=["sales"])

    assert exc_info.value.code == "INVALID_GROUP_BY"


def test_bytes_and_file_like_sources_use_loader_filename_contract() -> None:
    content = csv_content(make_row())
    byte_result = run_pipeline(content, filename="upload.csv", group_by="sku")
    file_object = BytesIO(content)
    before = file_object.getvalue()
    file_result = run_pipeline(
        file_object,
        filename="upload.csv",
        group_by="sku",
    )

    assert byte_result.status is PipelineStatus.SUCCESS
    assert file_result.status is PipelineStatus.SUCCESS
    assert file_object.getvalue() == before
    assert byte_result.metrics is not None
    assert file_result.metrics is not None
    pd.testing.assert_frame_equal(byte_result.metrics, file_result.metrics)


def test_seekable_file_like_is_rewound_for_reuse_and_left_at_end() -> None:
    content = csv_content(make_row())
    source = BytesIO(content)
    source.seek(7)

    first = run_pipeline(source, filename="upload.csv", group_by="sku")
    first_end_position = source.tell()
    second = run_pipeline(source, filename="upload.csv", group_by="sku")

    assert first.status is PipelineStatus.SUCCESS
    assert second.status is PipelineStatus.SUCCESS
    assert first_end_position == len(content)
    assert source.tell() == len(content)
    assert first.metrics is not None and second.metrics is not None
    pd.testing.assert_frame_equal(first.metrics, second.metrics)


def test_non_seekable_stream_is_consumed_and_not_reusable() -> None:
    source = ReadOnceStream(csv_content(make_row()))

    first = run_pipeline(source, group_by="sku")  # type: ignore[arg-type]

    assert first.status is PipelineStatus.SUCCESS
    with pytest.raises(DataLoadError) as exc_info:
        run_pipeline(source, group_by="sku")  # type: ignore[arg-type]
    assert exc_info.value.code == "EMPTY_FILE"


def test_broken_seek_is_loader_file_read_error() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        run_pipeline(BrokenSeekStream(), group_by="sku")  # type: ignore[arg-type]

    assert exc_info.value.code == "FILE_READ_ERROR"
    assert isinstance(exc_info.value.__cause__, OSError)


def test_xlsx_source_runs_through_complete_pipeline() -> None:
    workbook = BytesIO()
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame([make_row()]).to_excel(writer, index=False)

    result = run_pipeline(
        workbook.getvalue(),
        filename="upload.xlsx",
        group_by="sku",
    )

    assert result.status is PipelineStatus.SUCCESS
    assert result.validation.report.valid_rows == 1
    assert result.metrics is not None
    assert result.metrics["sku"].tolist() == ["SKU-A"]
    assert result.diagnostics is not None


def test_pipeline_result_has_one_source_of_truth_for_stage_data() -> None:
    result = run_pipeline(SAMPLE_PATH, group_by="sku")

    assert [field.name for field in fields(PipelineResult)] == [
        "status",
        "validation",
        "metrics",
        "diagnostics",
    ]
    assert not hasattr(result, "raw_data")
    assert not hasattr(result, "clean_data")
    assert result.validation.clean_data is not None


def test_pipeline_is_deterministic_for_same_source_and_group_by() -> None:
    first = run_pipeline(SAMPLE_PATH, group_by="sku")
    second = run_pipeline(SAMPLE_PATH, group_by="sku")

    assert first.status is second.status is PipelineStatus.SUCCESS
    assert first.validation.report.to_dict() == second.validation.report.to_dict()
    pd.testing.assert_frame_equal(
        first.validation.clean_data,
        second.validation.clean_data,
    )
    assert first.metrics is not None and second.metrics is not None
    assert first.diagnostics is not None and second.diagnostics is not None
    pd.testing.assert_frame_equal(first.metrics, second.metrics)
    pd.testing.assert_frame_equal(first.diagnostics, second.diagnostics)


def test_sample_end_to_end_uses_pipeline_entrypoint() -> None:
    result = run_pipeline(SAMPLE_PATH, group_by="sku")

    assert result.status is PipelineStatus.SUCCESS
    assert result.validation.report.total_rows == 23
    assert result.validation.report.valid_rows == 14
    assert result.validation.report.excluded_rows == 9
    assert len(result.validation.report.fatal_errors) == 0
    assert result.validation.report.warning_rows == 3

    assert result.metrics is not None
    assert len(result.metrics) == 12
    assert result.diagnostics is not None
    assert len(result.diagnostics) == 11
    assert Counter(result.diagnostics["code"].tolist()) == {
        HIGH_IMPRESSIONS_LOW_CTR: 1,
        LOW_CVR: 2,
        CLICKS_WITHOUT_ORDERS: 1,
        SPEND_WITHOUT_ORDERS: 1,
        LOW_ROAS: 3,
        HIGH_REFUND_RATE: 2,
        OUT_OF_STOCK: 1,
    }

    issue_codes_by_sku = {
        sku: group["code"].tolist()
        for sku, group in result.diagnostics.groupby("sku", sort=False)
    }
    assert issue_codes_by_sku["SKU-LOW-CTR"] == [
        HIGH_IMPRESSIONS_LOW_CTR
    ]
    assert issue_codes_by_sku["SKU-LOW-ROAS"] == [LOW_ROAS]
    assert issue_codes_by_sku["SKU-STOCKOUT"] == [OUT_OF_STOCK]
    assert issue_codes_by_sku["SKU-NO-ORDER"] == [
        LOW_CVR,
        CLICKS_WITHOUT_ORDERS,
        SPEND_WITHOUT_ORDERS,
        LOW_ROAS,
    ]
    assert "SKU-NORMAL-US" not in issue_codes_by_sku
    assert "SKU-ORDER-WARNING" not in issue_codes_by_sku
    assert "SKU-CLICK-ERROR" not in result.metrics["sku"].tolist()
    assert "SKU-KEY-CONFLICT" not in result.metrics["sku"].tolist()
