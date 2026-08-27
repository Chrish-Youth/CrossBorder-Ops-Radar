"""Build a bounded, JSON-safe insight context from deterministic results."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from math import isfinite
from numbers import Integral, Real
from operator import index
from typing import Any

import pandas as pd

from src.diagnostics import DIAGNOSTIC_ISSUE_COLUMNS
from src.metrics import BASE_MEASURES, DERIVED_METRICS, GROUP_DIMENSIONS
from src.pipeline import PipelineResult, PipelineStatus
from src.validator import ValidationResult

INSIGHT_CONTEXT_VERSION = "1"

MAX_INSIGHT_METRIC_RECORDS = 200
MAX_INSIGHT_DIAGNOSTIC_SIGNALS = 500
MAX_INSIGHT_EVIDENCE_DEPTH = 20

INVALID_INSIGHT_INPUT = "INVALID_INSIGHT_INPUT"
PIPELINE_NOT_ANALYZABLE = "PIPELINE_NOT_ANALYZABLE"
INSIGHT_CONTEXT_TOO_LARGE = "INSIGHT_CONTEXT_TOO_LARGE"
NON_FINITE_INSIGHT_VALUE = "NON_FINITE_INSIGHT_VALUE"

INSIGHT_CONTEXT_LIMITATIONS: tuple[str, ...] = (
    "Metrics are deterministic outputs calculated by the application.",
    (
        "Diagnostic signals use Demo Default Thresholds and are not industry "
        "standards."
    ),
    "Diagnostic signals are observations, not proven root causes.",
    "Missing ratio values are represented as null when denominators are zero.",
)


class InsightContextError(Exception):
    """A stable failure raised at the Insight Context interface boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class InsightContext:
    """A bounded snapshot of deterministic metrics and diagnostic signals."""

    version: str
    analysis_scope: dict[str, Any]
    metric_records: tuple[dict[str, Any], ...]
    diagnostic_signals: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return an independent structure accepted by strict ``json.dumps``."""

        return {
            "version": self.version,
            "analysis_scope": deepcopy(self.analysis_scope),
            "metric_records": deepcopy(list(self.metric_records)),
            "diagnostic_signals": deepcopy(list(self.diagnostic_signals)),
            "limitations": list(self.limitations),
        }


def _invalid_input(message: str) -> InsightContextError:
    return InsightContextError(INVALID_INSIGHT_INPUT, message)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(
        value,
        (str, bytes, bytearray, date, datetime, Enum, dict, list, tuple),
    ):
        return False
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


def _normalize_value(
    value: Any,
    *,
    path: str,
    ancestors: set[int] | None = None,
    container_depth: int = 0,
    max_container_depth: int | None = None,
) -> Any:
    """Convert a Metrics/Diagnostics value to a JSON-native value."""

    if _is_missing(value):
        return None
    if isinstance(value, Enum):
        return _normalize_value(
            value.value,
            path=path,
            ancestors=ancestors,
            container_depth=container_depth,
            max_container_depth=max_container_depth,
        )
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        normalized = float(value)
        if not isfinite(normalized):
            raise InsightContextError(
                NON_FINITE_INSIGHT_VALUE,
                f"Insight Context 字段 {path} 包含 Infinity 或 -Infinity。",
            )
        return normalized

    if isinstance(value, (dict, list, tuple)):
        if ancestors is None:
            ancestors = set()
        identity = id(value)
        if identity in ancestors:
            raise _invalid_input(f"Insight Context 字段 {path} 包含循环引用。")
        current_depth = container_depth + 1
        if (
            max_container_depth is not None
            and current_depth > max_container_depth
        ):
            raise _invalid_input(
                (
                    f"Insight Context 字段 {path} 的 Evidence 嵌套深度超过上限 "
                    f"{max_container_depth}。"
                )
            )
        ancestors.add(identity)
        try:
            if isinstance(value, dict):
                normalized_dict: dict[str, Any] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise _invalid_input(
                            f"Insight Context 字段 {path} 的 dict key 必须是字符串。"
                        )
                    normalized_dict[key] = _normalize_value(
                        item,
                        path=f"{path}.{key}",
                        ancestors=ancestors,
                        container_depth=current_depth,
                        max_container_depth=max_container_depth,
                    )
                return normalized_dict
            return [
                _normalize_value(
                    item,
                    path=f"{path}[{position}]",
                    ancestors=ancestors,
                    container_depth=current_depth,
                    max_container_depth=max_container_depth,
                )
                for position, item in enumerate(value)
            ]
        finally:
            ancestors.remove(identity)

    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            scalar = item_method()
        except (TypeError, ValueError, OverflowError) as exc:
            raise _invalid_input(
                f"Insight Context 字段 {path} 无法转换为 Python-native 值。"
            ) from exc
        if scalar is not value:
            return _normalize_value(
                scalar,
                path=path,
                ancestors=ancestors,
                container_depth=container_depth,
                max_container_depth=max_container_depth,
            )

    raise _invalid_input(
        f"Insight Context 字段 {path} 包含不支持的值类型 {type(value).__name__}。"
    )


def _validation_count(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise _invalid_input(f"ValidationReport.{field_name} 必须是非负整数。")
    try:
        normalized = index(value)
    except TypeError as exc:
        raise _invalid_input(
            f"ValidationReport.{field_name} 必须是非负整数。"
        ) from exc
    if normalized < 0:
        raise _invalid_input(f"ValidationReport.{field_name} 必须是非负整数。")
    return int(normalized)


def _validate_dataframe_columns(
    dataframe: pd.DataFrame,
    *,
    required: tuple[str, ...],
    name: str,
) -> None:
    if not dataframe.columns.is_unique:
        raise _invalid_input(f"{name} DataFrame 不允许包含重复列名。")
    missing = [column for column in required if column not in dataframe.columns]
    if missing:
        raise _invalid_input(
            f"{name} DataFrame 缺少 Insight Context 必需字段：{', '.join(missing)}。"
        )


def _validate_input(
    pipeline_result: object,
) -> tuple[PipelineResult, pd.DataFrame, pd.DataFrame, tuple[str, ...]]:
    if not isinstance(pipeline_result, PipelineResult):
        raise _invalid_input("pipeline_result 必须是 PipelineResult。")
    if not isinstance(pipeline_result.status, PipelineStatus) or not isinstance(
        pipeline_result.validation,
        ValidationResult,
    ):
        raise _invalid_input(
            "PipelineResult 包含无效的 status 或 validation。"
        )
    if pipeline_result.status is PipelineStatus.VALIDATION_FAILED:
        raise InsightContextError(
            PIPELINE_NOT_ANALYZABLE,
            "VALIDATION_FAILED 没有可供业务 Insight 使用的 Metrics 和 Diagnostics。",
        )
    if pipeline_result.status is not PipelineStatus.SUCCESS:
        raise _invalid_input("PipelineResult 包含不支持的 status。")
    if not isinstance(pipeline_result.metrics, pd.DataFrame) or not isinstance(
        pipeline_result.diagnostics,
        pd.DataFrame,
    ):
        raise _invalid_input(
            "SUCCESS PipelineResult 必须包含 Metrics 和 Diagnostics DataFrame。"
        )

    metrics = pipeline_result.metrics
    diagnostics = pipeline_result.diagnostics
    _validate_dataframe_columns(
        metrics,
        required=(*BASE_MEASURES, *DERIVED_METRICS),
        name="Metrics",
    )
    _validate_dataframe_columns(
        diagnostics,
        required=DIAGNOSTIC_ISSUE_COLUMNS,
        name="Diagnostics",
    )
    dimensions = tuple(
        column for column in metrics.columns if column in GROUP_DIMENSIONS
    )
    diagnostic_dimensions = tuple(
        column for column in diagnostics.columns if column in GROUP_DIMENSIONS
    )
    if diagnostic_dimensions != dimensions:
        raise _invalid_input(
            "Metrics 与 Diagnostics 的 Group Dimensions 不一致。"
        )
    if not dimensions and len(metrics) > 1:
        raise _invalid_input(
            "没有 Group Dimensions 的 SUCCESS Metrics 最多只能包含一行。"
        )
    return pipeline_result, metrics, diagnostics, dimensions


def _check_record_limits(metrics: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
    if len(metrics) > MAX_INSIGHT_METRIC_RECORDS:
        raise InsightContextError(
            INSIGHT_CONTEXT_TOO_LARGE,
            (
                f"Metrics records={len(metrics)} 超过上限 "
                f"{MAX_INSIGHT_METRIC_RECORDS}。"
            ),
        )
    if len(diagnostics) > MAX_INSIGHT_DIAGNOSTIC_SIGNALS:
        raise InsightContextError(
            INSIGHT_CONTEXT_TOO_LARGE,
            (
                f"Diagnostic signals={len(diagnostics)} 超过上限 "
                f"{MAX_INSIGHT_DIAGNOSTIC_SIGNALS}。"
            ),
        )


def _group_record(
    dataframe: pd.DataFrame,
    position: int,
    dimensions: tuple[str, ...],
    *,
    path: str,
) -> dict[str, Any]:
    return {
        dimension: _normalize_value(
            dataframe[dimension].iloc[position],
            path=f"{path}.group.{dimension}",
        )
        for dimension in dimensions
    }


def _metric_records(
    metrics: pd.DataFrame,
    dimensions: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for position in range(len(metrics)):
        path = f"metric_records[{position}]"
        records.append(
            {
                "group": _group_record(
                    metrics,
                    position,
                    dimensions,
                    path=path,
                ),
                "base_measures": {
                    column: _normalize_value(
                        metrics[column].iloc[position],
                        path=f"{path}.base_measures.{column}",
                    )
                    for column in BASE_MEASURES
                },
                "derived_metrics": {
                    column: _normalize_value(
                        metrics[column].iloc[position],
                        path=f"{path}.derived_metrics.{column}",
                    )
                    for column in DERIVED_METRICS
                },
            }
        )
    return tuple(records)


def _diagnostic_signals(
    diagnostics: pd.DataFrame,
    dimensions: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    signals: list[dict[str, Any]] = []
    for position in range(len(diagnostics)):
        path = f"diagnostic_signals[{position}]"
        signal: dict[str, Any] = {
            "group": _group_record(
                diagnostics,
                position,
                dimensions,
                path=path,
            )
        }
        for column in DIAGNOSTIC_ISSUE_COLUMNS:
            signal[column] = _normalize_value(
                diagnostics[column].iloc[position],
                path=f"{path}.{column}",
                max_container_depth=(
                    MAX_INSIGHT_EVIDENCE_DEPTH
                    if column == "evidence"
                    else None
                ),
            )
        signals.append(signal)
    return tuple(signals)


def build_insight_context(pipeline_result: PipelineResult) -> InsightContext:
    """Build a bounded context without recomputing or reordering business facts."""

    result, metrics, diagnostics, dimensions = _validate_input(pipeline_result)
    _check_record_limits(metrics, diagnostics)
    report = result.validation.report
    analysis_scope = {
        "group_dimensions": list(dimensions),
        "metric_group_count": len(metrics),
        "diagnostic_signal_count": len(diagnostics),
        "valid_rows": _validation_count(
            report.valid_rows,
            field_name="valid_rows",
        ),
        "excluded_rows": _validation_count(
            report.excluded_rows,
            field_name="excluded_rows",
        ),
        "warning_rows": _validation_count(
            report.warning_rows,
            field_name="warning_rows",
        ),
    }
    return InsightContext(
        version=INSIGHT_CONTEXT_VERSION,
        analysis_scope=analysis_scope,
        metric_records=_metric_records(metrics, dimensions),
        diagnostic_signals=_diagnostic_signals(diagnostics, dimensions),
        limitations=INSIGHT_CONTEXT_LIMITATIONS,
    )
