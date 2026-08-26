"""Deterministic validation for the frozen V1 ecommerce data contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from math import isfinite
import re
from typing import Any, Literal

import pandas as pd

from src.config import (
    BUSINESS_KEY_COLUMNS,
    COUNT_MAX_VALUE,
    COUNT_MIN_VALUE,
    DATE_COLUMN,
    DATE_FORMAT,
    FLOAT_COLUMNS,
    INTEGER_COLUMNS,
    REQUIRED_COLUMNS,
    STRING_COLUMNS,
)

ValidationLevel = Literal["Fatal", "Error", "Warning"]


@dataclass(frozen=True)
class ValidationIssue:
    """One machine-readable and human-readable validation finding."""

    level: ValidationLevel
    code: str
    row: int | None
    field: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    """Summary and issue lists for one validation run.

    ``warning_rows`` counts distinct source rows with warnings, including an exact
    duplicate row that is removed by the explicit deduplication rule.
    """

    total_rows: int
    valid_rows: int = 0
    excluded_rows: int = 0
    warning_rows: int = 0
    fatal_errors: list[ValidationIssue] = field(default_factory=list)
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    @property
    def issues(self) -> list[ValidationIssue]:
        return [*self.fatal_errors, *self.errors, *self.warnings]

    @property
    def has_fatal_errors(self) -> bool:
        return bool(self.fatal_errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "excluded_rows": self.excluded_rows,
            "warning_rows": self.warning_rows,
            "fatal_errors": [issue.to_dict() for issue in self.fatal_errors],
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


@dataclass(frozen=True)
class ValidationResult:
    """Clean rows plus the complete validation report."""

    clean_data: pd.DataFrame
    report: ValidationReport


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _strict_date(value: Any) -> date | None:
    if isinstance(value, pd.Timestamp):
        if pd.isna(value) or value.tzinfo is not None or value.time() != time.min:
            return None
        return value.date()
    if isinstance(value, datetime):
        if value.tzinfo is not None or value.time() != time.min:
            return None
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
    ):
        return None
    try:
        return datetime.strptime(value, DATE_FORMAT).date()
    except ValueError:
        return None


def _parse_count(value: Any) -> tuple[int | None, str | None]:
    if pd.api.types.is_bool(value):
        return None, "INVALID_NUMERIC_VALUE"
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None, "INVALID_NUMERIC_VALUE"

    if not number.is_finite() or number != number.to_integral_value():
        return None, "INVALID_NUMERIC_VALUE"
    if number < COUNT_MIN_VALUE:
        return None, "NEGATIVE_VALUE"
    if number > COUNT_MAX_VALUE:
        return None, "INTEGER_OUT_OF_RANGE"
    return int(number), None


def _is_scalar_missing(value: Any) -> bool:
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if not pd.api.types.is_scalar(missing):
        return False
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _raw_values_equal(left: Any, right: Any) -> bool:
    """Compare raw scalar or container values without requiring hashability."""

    if left is right:
        return True

    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        if left.keys() != right.keys():
            return False
        return all(_raw_values_equal(left[key], right[key]) for key in left)

    sequence_types = (list, tuple)
    if isinstance(left, sequence_types) or isinstance(right, sequence_types):
        if type(left) is not type(right) or len(left) != len(right):
            return False
        return all(
            _raw_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )

    set_types = (set, frozenset)
    if isinstance(left, set_types) or isinstance(right, set_types):
        if type(left) is not type(right):
            return False
        try:
            return bool(left == right)
        except (TypeError, ValueError):
            return False

    left_missing = _is_scalar_missing(left)
    right_missing = _is_scalar_missing(right)
    if left_missing or right_missing:
        return left_missing and right_missing

    try:
        equal = left == right
    except (TypeError, ValueError):
        return False
    if not pd.api.types.is_scalar(equal):
        return False
    try:
        return bool(equal)
    except (TypeError, ValueError):
        return False


def _exact_duplicate_positions(dataframe: pd.DataFrame) -> set[int]:
    """Return duplicate row positions, with a safe path for container values."""

    rows = list(dataframe.itertuples(index=False, name=None))
    requires_safe_comparison = False
    for row in rows:
        for value in row:
            try:
                hash(value)
            except TypeError:
                requires_safe_comparison = True
                break
        if requires_safe_comparison:
            break

    if not requires_safe_comparison:
        duplicate_mask = dataframe.duplicated(keep="first")
        return {
            int(position) for position in duplicate_mask.index[duplicate_mask]
        }

    duplicate_positions: set[int] = set()
    representative_positions: list[int] = []
    for position, row in enumerate(rows):
        is_duplicate = any(
            len(row) == len(rows[representative])
            and all(
                _raw_values_equal(left, right)
                for left, right in zip(row, rows[representative])
            )
            for representative in representative_positions
        )
        if is_duplicate:
            duplicate_positions.add(position)
        else:
            representative_positions.append(position)
    return duplicate_positions


def validate_dataframe(dataframe: pd.DataFrame) -> ValidationResult:
    """Validate raw input and return analysis-ready rows with structured issues.

    Report row numbers are logical positions including a conceptual header row,
    so the first parsed DataFrame record is row 2 for both CSV and XLSX.
    """

    if not isinstance(dataframe, pd.DataFrame):
        raise TypeError("dataframe 必须是 pandas.DataFrame。")

    total_rows = len(dataframe)
    report = ValidationReport(total_rows=total_rows)
    missing_columns = [
        column for column in REQUIRED_COLUMNS if column not in dataframe.columns
    ]
    if missing_columns:
        report.excluded_rows = total_rows
        report.fatal_errors.extend(
            ValidationIssue(
                level="Fatal",
                code="MISSING_REQUIRED_COLUMN",
                row=None,
                field=column,
                message=f"缺少必填字段：{column}。",
            )
            for column in missing_columns
        )
        return ValidationResult(dataframe.iloc[0:0].copy(), report)

    raw_work = dataframe.copy(deep=True).reset_index(drop=True)
    work = raw_work.copy(deep=True)
    source_rows = pd.Series(range(2, total_rows + 2), index=work.index)
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    error_positions: set[int] = set()
    warning_positions: set[int] = set()
    invalid_key_positions: set[int] = set()

    def add_issue(
        level: Literal["Error", "Warning"],
        code: str,
        position: int,
        field_name: str | None,
        message: str,
    ) -> None:
        issue = ValidationIssue(
            level=level,
            code=code,
            row=int(source_rows.at[position]),
            field=field_name,
            message=message,
        )
        if level == "Error":
            errors.append(issue)
            error_positions.add(position)
            if field_name in BUSINESS_KEY_COLUMNS:
                invalid_key_positions.add(position)
        else:
            warnings.append(issue)
            warning_positions.add(position)

    missing_masks: dict[str, pd.Series] = {}
    for column in REQUIRED_COLUMNS:
        missing_mask = work[column].map(_is_missing)
        missing_masks[column] = missing_mask
        for position in work.index[missing_mask]:
            add_issue(
                "Error",
                "MISSING_REQUIRED_VALUE",
                int(position),
                column,
                f"必填字段 {column} 为空。",
            )

    for column in STRING_COLUMNS:
        non_missing = ~missing_masks[column]
        work.loc[non_missing, column] = work.loc[non_missing, column].map(
            lambda value: str(value).strip()
        )

    parsed_dates = pd.Series([None] * total_rows, index=work.index, dtype=object)
    for position in work.index[~missing_masks[DATE_COLUMN]]:
        parsed = _strict_date(work.at[position, DATE_COLUMN])
        if parsed is None:
            add_issue(
                "Error",
                "INVALID_DATE_FORMAT",
                int(position),
                DATE_COLUMN,
                f"日期必须严格使用 YYYY-MM-DD：{work.at[position, DATE_COLUMN]!r}。",
            )
        else:
            parsed_dates.at[position] = parsed
    work[DATE_COLUMN] = parsed_dates

    for column in INTEGER_COLUMNS:
        parsed_counts = pd.Series([pd.NA] * total_rows, index=work.index, dtype="Int64")
        for position in work.index[~missing_masks[column]]:
            parsed, error_code = _parse_count(work.at[position, column])
            if error_code is None:
                parsed_counts.at[position] = parsed
                continue

            if error_code == "INTEGER_OUT_OF_RANGE":
                message = (
                    f"字段 {column} 超出 Int64 非负整数范围 "
                    f"{COUNT_MIN_VALUE}..{COUNT_MAX_VALUE}：{work.at[position, column]!r}。"
                )
            elif error_code == "NEGATIVE_VALUE":
                message = f"字段 {column} 不允许为负数：{work.at[position, column]!r}。"
            else:
                message = (
                    f"字段 {column} 必须是可转换的整数：{work.at[position, column]!r}。"
                )
            add_issue(
                "Error",
                error_code,
                int(position),
                column,
                message,
            )
        work[column] = parsed_counts

    for column in FLOAT_COLUMNS:
        raw_values = work[column]
        converted = pd.Series(
            pd.to_numeric(raw_values, errors="coerce"),
            index=work.index,
            dtype="Float64",
        )
        finite_mask = converted.map(
            lambda value: False if pd.isna(value) else isfinite(float(value))
        )
        bool_mask = raw_values.map(pd.api.types.is_bool)
        valid_mask = converted.notna() & finite_mask & ~bool_mask

        invalid_mask = ~missing_masks[column] & ~valid_mask
        for position in work.index[invalid_mask]:
            add_issue(
                "Error",
                "INVALID_NUMERIC_VALUE",
                int(position),
                column,
                f"字段 {column} 必须是可转换的数值：{raw_values.at[position]!r}。",
            )

        converted = converted.where(valid_mask)
        work[column] = converted.astype("Float64")

    for column in FLOAT_COLUMNS:
        negative_mask = work[column].notna() & work[column].lt(0)
        for position in work.index[negative_mask]:
            add_issue(
                "Error",
                "NEGATIVE_VALUE",
                int(position),
                column,
                f"字段 {column} 不允许为负数：{work.at[position, column]}。",
            )

    valid_click_relation = (
        work["clicks"].notna()
        & work["impressions"].notna()
        & work["clicks"].ge(0)
        & work["impressions"].ge(0)
    )
    clicks_error_mask = valid_click_relation & work["clicks"].gt(work["impressions"])
    for position in work.index[clicks_error_mask]:
        add_issue(
            "Error",
            "CLICKS_GT_IMPRESSIONS",
            int(position),
            "clicks",
            "clicks 不能大于 impressions。",
        )

    duplicate_positions: set[int] = set()
    if total_rows:
        duplicate_positions = _exact_duplicate_positions(
            raw_work.loc[:, list(dataframe.columns)]
        )
        for position in sorted(duplicate_positions):
            add_issue(
                "Warning",
                "EXACT_DUPLICATE",
                position,
                None,
                "该行与之前的记录完全重复，已去重并仅保留首次出现的记录。",
            )

    valid_key_positions = [
        int(position)
        for position in work.index
        if position not in invalid_key_positions
    ]
    key_by_position = {
        position: tuple(work.at[position, column] for column in BUSINESS_KEY_COLUMNS)
        for position in valid_key_positions
    }
    representatives_by_key: dict[tuple[Any, ...], list[int]] = {}
    for position in valid_key_positions:
        if position in duplicate_positions:
            continue
        representatives_by_key.setdefault(key_by_position[position], []).append(position)

    conflict_keys = {
        business_key
        for business_key, positions in representatives_by_key.items()
        if len(positions) > 1
    }
    for position in valid_key_positions:
        if position in duplicate_positions:
            continue
        if key_by_position[position] in conflict_keys:
            add_issue(
                "Error",
                "BUSINESS_KEY_CONFLICT",
                int(position),
                None,
                "相同业务键存在内容不一致的多条记录，冲突记录已全部排除。",
            )

    warning_candidates = [
        int(position)
        for position in work.index
        if position not in error_positions and position not in duplicate_positions
    ]
    if warning_candidates:
        retained = work.loc[warning_candidates]
        orders_warning = (
            retained["orders"].notna()
            & retained["clicks"].notna()
            & retained["orders"].ge(0)
            & retained["clicks"].ge(0)
            & retained["orders"].gt(retained["clicks"])
        )
        for position in retained.index[orders_warning]:
            add_issue(
                "Warning",
                "ORDERS_GT_CLICKS",
                int(position),
                "orders",
                "orders 大于 clicks；订单可能包含自然订单，该行仍保留。",
            )

        refunds_warning = (
            retained["refunds"].notna()
            & retained["orders"].notna()
            & retained["refunds"].ge(0)
            & retained["orders"].ge(0)
            & retained["refunds"].gt(retained["orders"])
        )
        for position in retained.index[refunds_warning]:
            add_issue(
                "Warning",
                "REFUNDS_GT_ORDERS",
                int(position),
                "refunds",
                "refunds 大于当天 orders；退款可能来自历史订单，该行仍保留。",
            )

    excluded_positions = error_positions | duplicate_positions
    clean_positions = [
        position for position in work.index if position not in excluded_positions
    ]
    clean_data = work.loc[clean_positions].reset_index(drop=True)

    report.valid_rows = len(clean_data)
    report.excluded_rows = total_rows - report.valid_rows
    report.warning_rows = len(warning_positions)
    report.errors = errors
    report.warnings = warnings
    return ValidationResult(clean_data=clean_data, report=report)
