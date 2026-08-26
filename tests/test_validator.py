from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from src.config import FLOAT_COLUMNS, INTEGER_COLUMNS, REQUIRED_COLUMNS
from src.loader import load_file
from src.validator import ValidationResult, validate_dataframe


def make_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "date": "2026-08-24",
        "marketplace": "Amazon",
        "country": "US",
        "sku": "SKU-1",
        "product_name": "Example Product",
        "impressions": "1000",
        "clicks": "100",
        "orders": "10",
        "units_sold": "10",
        "sales": "299.90",
        "ad_spend": "50.00",
        "refunds": "1",
        "inventory": "20",
    }
    row.update(overrides)
    return row


def issue_codes(result: ValidationResult, category: str) -> list[str]:
    return [issue.code for issue in getattr(result.report, category)]


def load_one_csv_row(**overrides: object) -> pd.DataFrame:
    row = make_row(**overrides)
    header = ",".join(REQUIRED_COLUMNS)
    values = ",".join(str(row[column]) for column in REQUIRED_COLUMNS)
    return load_file((header + "\n" + values + "\n").encode(), filename="input.csv")


def test_valid_schema_and_values() -> None:
    result = validate_dataframe(pd.DataFrame([make_row()]))

    assert result.report.valid_rows == 1
    assert result.report.excluded_rows == 0
    assert result.report.issues == []
    assert list(result.clean_data.columns) == list(REQUIRED_COLUMNS)


def test_missing_required_column_is_fatal() -> None:
    dataframe = pd.DataFrame(
        [make_row(sku=f"SKU-{number}") for number in range(3)]
    ).drop(columns=["sku"])

    result = validate_dataframe(dataframe)

    assert result.report.has_fatal_errors
    assert result.report.total_rows == 3
    assert result.report.valid_rows == 0
    assert result.report.excluded_rows == 3
    assert result.report.total_rows == result.report.valid_rows + result.report.excluded_rows
    assert issue_codes(result, "fatal_errors") == ["MISSING_REQUIRED_COLUMN"]
    assert result.report.fatal_errors[0].field == "sku"
    assert result.report.fatal_errors[0].row is None


def test_extra_columns_are_allowed_and_preserved() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(brand="Example Brand", notes="keep me")])
    )

    assert result.report.valid_rows == 1
    assert result.clean_data.loc[0, "brand"] == "Example Brand"
    assert result.clean_data.loc[0, "notes"] == "keep me"


def test_extra_dict_column_does_not_crash() -> None:
    result = validate_dataframe(
        pd.DataFrame(
            [
                make_row(
                    metadata={"color": "red"},
                    tags=["new", "sale"],
                    channels={"Amazon", "eBay"},
                )
            ]
        )
    )

    assert result.report.errors == []
    assert result.clean_data.loc[0, "metadata"] == {"color": "red"}
    assert result.clean_data.loc[0, "tags"] == ["new", "sale"]
    assert result.clean_data.loc[0, "channels"] == {"Amazon", "eBay"}


def test_equal_container_extra_column_can_be_duplicate() -> None:
    first = make_row(
        metadata={"color": "red"},
        tags=["new", "sale"],
        channels={"Amazon", "eBay"},
    )
    second = make_row(
        metadata={"color": "red"},
        tags=["new", "sale"],
        channels={"eBay", "Amazon"},
    )

    result = validate_dataframe(pd.DataFrame([first, second]))

    assert len(result.clean_data) == 1
    assert issue_codes(result, "warnings") == ["EXACT_DUPLICATE"]
    assert result.report.errors == []


def test_different_container_extra_column_is_not_exact_duplicate() -> None:
    first = make_row(metadata={"color": "red"})
    second = make_row(metadata={"color": "blue"})

    result = validate_dataframe(pd.DataFrame([first, second]))

    assert "EXACT_DUPLICATE" not in issue_codes(result, "warnings")
    assert issue_codes(result, "errors") == [
        "BUSINESS_KEY_CONFLICT",
        "BUSINESS_KEY_CONFLICT",
    ]
    assert result.clean_data.empty


@pytest.mark.parametrize("field_name", ["sku", "country", "sales"])
def test_missing_required_value_is_error_and_excluded(field_name: str) -> None:
    result = validate_dataframe(pd.DataFrame([make_row(**{field_name: "  "})]))

    assert issue_codes(result, "errors") == ["MISSING_REQUIRED_VALUE"]
    assert result.report.errors[0].field == field_name
    assert result.report.errors[0].row == 2
    assert result.clean_data.empty


def test_valid_date_format_is_converted_to_python_date() -> None:
    result = validate_dataframe(pd.DataFrame([make_row(date="2026-08-24")]))

    assert result.report.valid_rows == 1
    assert result.clean_data["date"].dtype == object
    assert type(result.clean_data.loc[0, "date"]) is date
    assert result.clean_data.loc[0, "date"] == date(2026, 8, 24)


@pytest.mark.parametrize(
    "invalid_date",
    ["2026/08/24", "08/24/2026", "24/08/2026", "Aug 24 2026", "2026-8-24"],
)
def test_invalid_date_format_is_error_and_excluded(invalid_date: str) -> None:
    result = validate_dataframe(pd.DataFrame([make_row(date=invalid_date)]))

    assert issue_codes(result, "errors") == ["INVALID_DATE_FORMAT"]
    assert result.clean_data.empty


@pytest.mark.parametrize("invalid_date", ["2026-02-30", "9999-13-01"])
def test_invalid_calendar_date_is_error(invalid_date: str) -> None:
    result = validate_dataframe(pd.DataFrame([make_row(date=invalid_date)]))

    assert issue_codes(result, "errors") == ["INVALID_DATE_FORMAT"]


@pytest.mark.parametrize("valid_date", ["1600-01-01", "2262-04-12", "9999-12-31"])
def test_valid_dates_outside_datetime64_ns_range_are_accepted(valid_date: str) -> None:
    result = validate_dataframe(pd.DataFrame([make_row(date=valid_date)]))

    assert result.report.errors == []
    assert result.clean_data.loc[0, "date"] == date.fromisoformat(valid_date)


@pytest.mark.parametrize(
    "date_with_spaces",
    [" 2026-08-24", "2026-08-24 ", " 2026-08-24 "],
)
def test_date_with_surrounding_spaces_is_rejected(date_with_spaces: str) -> None:
    result = validate_dataframe(pd.DataFrame([make_row(date=date_with_spaces)]))

    assert issue_codes(result, "errors") == ["INVALID_DATE_FORMAT"]
    assert result.clean_data.empty


def test_blank_date_is_missing_instead_of_invalid_format() -> None:
    result = validate_dataframe(pd.DataFrame([make_row(date="   ")]))

    assert issue_codes(result, "errors") == ["MISSING_REQUIRED_VALUE"]
    assert result.report.errors[0].field == "date"


def test_native_excel_date_is_accepted() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(date=pd.Timestamp("2026-08-24"))])
    )

    assert result.report.valid_rows == 1
    assert result.clean_data.loc[0, "date"] == date(2026, 8, 24)


def test_native_excel_datetime_with_nonzero_time_is_rejected() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(date=datetime(2026, 8, 24, 12, 30))])
    )

    assert issue_codes(result, "errors") == ["INVALID_DATE_FORMAT"]


def test_xlsx_native_date_loader_to_validator_integration() -> None:
    buffer = BytesIO()
    source = pd.DataFrame([make_row(date=datetime(2026, 8, 24))])
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        source.to_excel(writer, index=False)

    raw_data = load_file(buffer.getvalue(), filename="input.xlsx")
    result = validate_dataframe(raw_data)

    assert result.report.valid_rows == 1
    assert type(result.clean_data.loc[0, "date"]) is date
    assert result.clean_data.loc[0, "date"] == date(2026, 8, 24)


@pytest.mark.parametrize("invalid_value", ["abc", "N/A", "unknown", float("inf"), 1.5])
def test_invalid_integer_value_is_error(invalid_value: object) -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(impressions=invalid_value)])
    )

    assert issue_codes(result, "errors") == ["INVALID_NUMERIC_VALUE"]
    assert result.report.errors[0].field == "impressions"
    assert result.clean_data.empty


def test_large_integer_is_preserved_exactly_through_loader_and_validator() -> None:
    expected = 9_007_199_254_740_993

    result = validate_dataframe(load_one_csv_row(impressions=str(expected)))

    assert result.report.errors == []
    assert result.clean_data.loc[0, "impressions"] == expected


def test_int64_max_count_is_valid() -> None:
    expected = 9_223_372_036_854_775_807

    result = validate_dataframe(pd.DataFrame([make_row(impressions=str(expected))]))

    assert result.report.errors == []
    assert result.clean_data.loc[0, "impressions"] == expected


def test_count_above_int64_max_is_integer_out_of_range() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(impressions="9223372036854775808")])
    )

    assert issue_codes(result, "errors") == ["INTEGER_OUT_OF_RANGE"]
    assert result.report.errors[0].field == "impressions"
    assert result.clean_data.empty


def test_huge_count_is_rejected_without_crashing() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(impressions="1e1000000")])
    )

    assert issue_codes(result, "errors") == ["INTEGER_OUT_OF_RANGE"]
    assert result.clean_data.empty


def test_fractional_count_is_not_rounded_to_integer() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(impressions="1.0000000000000001")])
    )

    assert issue_codes(result, "errors") == ["INVALID_NUMERIC_VALUE"]
    assert result.clean_data.empty


def test_negative_count_is_negative_value_error() -> None:
    result = validate_dataframe(pd.DataFrame([make_row(inventory="-1")]))

    assert issue_codes(result, "errors") == ["NEGATIVE_VALUE"]
    assert result.report.errors[0].field == "inventory"


def test_negative_value_is_error_and_excluded() -> None:
    result = validate_dataframe(pd.DataFrame([make_row(ad_spend="-1.00")]))

    assert issue_codes(result, "errors") == ["NEGATIVE_VALUE"]
    assert result.report.errors[0].field == "ad_spend"
    assert result.clean_data.empty


def test_clicks_gt_impressions_is_error_and_excluded() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(impressions="10", clicks="11")])
    )

    assert issue_codes(result, "errors") == ["CLICKS_GT_IMPRESSIONS"]
    assert result.clean_data.empty


def test_orders_gt_clicks_is_warning_and_retained() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(clicks="10", orders="15")])
    )

    assert issue_codes(result, "warnings") == ["ORDERS_GT_CLICKS"]
    assert result.report.errors == []
    assert len(result.clean_data) == 1


def test_refunds_gt_orders_is_warning_and_retained() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(orders="5", refunds="8")])
    )

    assert issue_codes(result, "warnings") == ["REFUNDS_GT_ORDERS"]
    assert result.report.errors == []
    assert len(result.clean_data) == 1


def test_exact_duplicate_is_deduplicated_and_generates_warning() -> None:
    row = make_row()
    result = validate_dataframe(pd.DataFrame([row, row.copy()]))

    assert len(result.clean_data) == 1
    assert result.report.excluded_rows == 1
    assert result.report.errors == []
    assert issue_codes(result, "warnings") == ["EXACT_DUPLICATE"]
    assert result.report.warnings[0].row == 3


def test_exact_duplicate_is_resolved_before_business_key_conflict() -> None:
    first = make_row(sales="100.00")
    duplicate = first.copy()
    conflicting = make_row(sales="200.00")

    result = validate_dataframe(pd.DataFrame([first, duplicate, conflicting]))

    assert result.clean_data.empty
    assert issue_codes(result, "warnings") == ["EXACT_DUPLICATE"]
    assert issue_codes(result, "errors") == [
        "BUSINESS_KEY_CONFLICT",
        "BUSINESS_KEY_CONFLICT",
    ]
    assert result.report.warnings[0].row == 3
    assert {issue.row for issue in result.report.errors} == {2, 4}


def test_business_key_conflict_is_error_and_all_rows_are_excluded() -> None:
    first = make_row(sales="100.00")
    second = make_row(sales="200.00")

    result = validate_dataframe(pd.DataFrame([first, second]))

    assert result.clean_data.empty
    assert result.report.excluded_rows == 2
    assert issue_codes(result, "errors") == [
        "BUSINESS_KEY_CONFLICT",
        "BUSINESS_KEY_CONFLICT",
    ]
    assert {issue.row for issue in result.report.errors} == {2, 3}


def test_business_key_conflict_is_detected_when_one_row_has_another_error() -> None:
    first = make_row(sales="100.00")
    second = make_row(sales="abc")

    result = validate_dataframe(pd.DataFrame([first, second]))

    assert result.clean_data.empty
    assert result.report.excluded_rows == 2
    assert [(issue.row, issue.code) for issue in result.report.errors] == [
        (3, "INVALID_NUMERIC_VALUE"),
        (2, "BUSINESS_KEY_CONFLICT"),
        (3, "BUSINESS_KEY_CONFLICT"),
    ]


def test_invalid_business_key_does_not_create_secondary_conflict() -> None:
    first = make_row(date="2026-02-30", sales="100.00")
    second = make_row(date="2026-02-30", sales="200.00")

    result = validate_dataframe(pd.DataFrame([first, second]))

    assert issue_codes(result, "errors") == [
        "INVALID_DATE_FORMAT",
        "INVALID_DATE_FORMAT",
    ]
    assert "BUSINESS_KEY_CONFLICT" not in issue_codes(result, "errors")


def test_distinct_large_counts_are_conflicts_not_exact_duplicates() -> None:
    first = make_row(impressions="9007199254740992")
    second = make_row(impressions="9007199254740993")

    result = validate_dataframe(pd.DataFrame([first, second]))

    assert issue_codes(result, "errors") == [
        "BUSINESS_KEY_CONFLICT",
        "BUSINESS_KEY_CONFLICT",
    ]
    assert "EXACT_DUPLICATE" not in issue_codes(result, "warnings")
    assert result.clean_data.empty


def test_exact_duplicate_is_reported_even_when_rows_have_an_error() -> None:
    row = make_row(impressions="100", clicks="120")

    result = validate_dataframe(pd.DataFrame([row, row.copy()]))

    assert result.clean_data.empty
    assert issue_codes(result, "errors") == [
        "CLICKS_GT_IMPRESSIONS",
        "CLICKS_GT_IMPRESSIONS",
    ]
    assert issue_codes(result, "warnings") == ["EXACT_DUPLICATE"]
    assert result.report.warnings[0].row == 3


def test_error_row_is_excluded_while_valid_row_remains() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(), make_row(sku="SKU-2", sales="bad")])
    )

    assert len(result.clean_data) == 1
    assert result.clean_data.loc[0, "sku"] == "SKU-1"
    assert result.report.excluded_rows == 1


def test_warning_row_is_retained() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(sku="SKU-WARN", clicks="5", orders="7")])
    )

    assert result.report.warning_rows == 1
    assert result.report.excluded_rows == 0
    assert result.clean_data.loc[0, "sku"] == "SKU-WARN"


def test_report_row_number_is_source_row_including_header() -> None:
    dataframe = pd.DataFrame(
        [make_row(), make_row(sku="SKU-2", date="2026/08/24")],
        index=[100, 200],
    )
    result = validate_dataframe(dataframe)

    assert result.report.errors[0].row == 3


def test_report_row_is_logical_position_with_multiline_and_blank_csv_lines() -> None:
    header = ",".join(REQUIRED_COLUMNS)
    first = (
        '2026-08-24,Amazon,US,SKU-1,"Line one\nLine two",1000,100,10,'
        "10,299.90,50.00,1,20"
    )
    second = (
        "2026/08/25,Amazon,US,SKU-2,Product,1000,100,10,"
        "10,299.90,50.00,1,20"
    )
    raw_data = load_file(
        f"{header}\n{first}\n\n{second}\n".encode(),
        filename="input.csv",
    )

    result = validate_dataframe(raw_data)

    assert len(raw_data) == 2
    assert result.report.errors[0].code == "INVALID_DATE_FORMAT"
    assert result.report.errors[0].row == 3


def test_invalid_numeric_does_not_generate_secondary_business_rule_issue() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(clicks="abc", orders="999")])
    )

    assert issue_codes(result, "errors") == ["INVALID_NUMERIC_VALUE"]
    assert result.report.warnings == []


def test_clean_dataframe_uses_stable_numeric_types() -> None:
    result = validate_dataframe(pd.DataFrame([make_row()]))

    assert all(str(result.clean_data[column].dtype) == "Int64" for column in INTEGER_COLUMNS)
    assert all(str(result.clean_data[column].dtype) == "Float64" for column in FLOAT_COLUMNS)


def test_validation_report_to_dict_is_structured() -> None:
    result = validate_dataframe(
        pd.DataFrame([make_row(clicks="5", orders="7")])
    )

    report = result.report.to_dict()
    assert report["total_rows"] == 1
    assert report["valid_rows"] == 1
    assert report["warning_rows"] == 1
    assert report["warnings"][0] == {
        "level": "Warning",
        "code": "ORDERS_GT_CLICKS",
        "row": 2,
        "field": "orders",
        "message": "orders 大于 clicks；订单可能包含自然订单，该行仍保留。",
    }


def test_sample_dataset_loader_to_validator_integration() -> None:
    sample_path = Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"

    raw_data = load_file(sample_path)
    result = validate_dataframe(raw_data)

    assert result.report.total_rows == 23
    assert result.report.valid_rows == 14
    assert result.report.excluded_rows == 9
    assert result.report.warning_rows == 3
    assert len(result.report.fatal_errors) == 0
    assert len(result.report.errors) == 8
    assert len(result.report.warnings) == 3
    assert Counter(issue_codes(result, "errors")) == {
        "MISSING_REQUIRED_VALUE": 2,
        "INVALID_DATE_FORMAT": 1,
        "INVALID_NUMERIC_VALUE": 1,
        "NEGATIVE_VALUE": 1,
        "CLICKS_GT_IMPRESSIONS": 1,
        "BUSINESS_KEY_CONFLICT": 2,
    }
    assert Counter(issue_codes(result, "warnings")) == {
        "ORDERS_GT_CLICKS": 1,
        "REFUNDS_GT_ORDERS": 1,
        "EXACT_DUPLICATE": 1,
    }

    clean_skus = result.clean_data["sku"].tolist()
    assert "SKU-NORMAL-US" in clean_skus
    assert "SKU-ORDER-WARNING" in clean_skus
    assert "SKU-REFUND-WARNING" in clean_skus
    assert "SKU-CLICK-ERROR" not in clean_skus
    assert "SKU-KEY-CONFLICT" not in clean_skus
    assert clean_skus.count("SKU-DUP") == 1
