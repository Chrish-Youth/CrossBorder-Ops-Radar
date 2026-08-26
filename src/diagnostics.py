"""Deterministic diagnostics for Phase 3 Metrics DataFrames."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from operator import index
from typing import Any, Callable

import pandas as pd

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
    DEMO_MIN_UNITS_SOLD_FOR_STOCKOUT,
    DEMO_NO_ORDERS_VALUE,
    DEMO_OUT_OF_STOCK_INVENTORY,
    DEMO_POSITIVE_AD_SPEND_FLOOR,
    DIAGNOSTIC_RULE_ORDER,
    DIAGNOSTIC_SEVERITY_WARNING,
    HIGH_IMPRESSIONS_LOW_CTR,
    HIGH_REFUND_RATE,
    LOW_CVR,
    LOW_ROAS,
    OUT_OF_STOCK,
    SPEND_WITHOUT_ORDERS,
)
from src.metrics import BASE_MEASURES, DERIVED_METRICS, GROUP_DIMENSIONS

DIAGNOSTIC_INPUT_COLUMNS: tuple[str, ...] = (
    *BASE_MEASURES,
    *DERIVED_METRICS,
)

DIAGNOSTIC_ISSUE_COLUMNS: tuple[str, ...] = (
    "code",
    "severity",
    "metric",
    "actual_value",
    "threshold",
    "evidence",
    "message",
)


class DiagnosticsError(Exception):
    """A stable failure raised at the Diagnostics Engine boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class DiagnosticIssue:
    """One deterministic diagnostic signal for one Metrics group."""

    context: dict[str, Any]
    code: str
    severity: str
    metric: str
    actual_value: int | float
    threshold: int | float
    evidence: dict[str, int | float]
    message: str

    def to_record(self) -> dict[str, Any]:
        return {
            **self.context,
            "code": self.code,
            "severity": self.severity,
            "metric": self.metric,
            "actual_value": self.actual_value,
            "threshold": self.threshold,
            "evidence": self.evidence,
            "message": self.message,
        }


@dataclass(frozen=True)
class _DiagnosticValues:
    impressions: int
    clicks: int
    orders: int
    units_sold: int
    ad_spend: float
    inventory: int
    ctr: float | None
    cvr: float | None
    roas: float | None
    refund_rate: float | None


def _ensure_input_columns(dataframe: pd.DataFrame) -> None:
    missing = [
        column
        for column in DIAGNOSTIC_INPUT_COLUMNS
        if column not in dataframe.columns
    ]
    if missing:
        raise DiagnosticsError(
            "MISSING_DIAGNOSTIC_INPUT_COLUMN",
            f"缺少诊断必需的 Metrics 字段：{', '.join(missing)}。",
        )


def _count_value(dataframe: pd.DataFrame, position: int, column: str) -> int:
    value = dataframe[column].iloc[position]
    if pd.api.types.is_bool(value):
        raise TypeError(f"字段 {column} 不是 Metrics Count 值。")
    return index(value)


def _number_value(
    dataframe: pd.DataFrame,
    position: int,
    column: str,
    *,
    allow_nan: bool,
) -> float | None:
    value = dataframe[column].iloc[position]
    if pd.api.types.is_bool(value) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"字段 {column} 不是 Metrics 数值。")

    missing = pd.isna(value)
    if missing:
        if allow_nan:
            return None
        raise ValueError(f"字段 {column} 不允许缺失。")

    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError(f"字段 {column} 不是有限 Metrics 数值。")
    return parsed


def _read_values(dataframe: pd.DataFrame, position: int) -> _DiagnosticValues:
    ad_spend = _number_value(
        dataframe,
        position,
        "ad_spend",
        allow_nan=False,
    )
    assert ad_spend is not None
    return _DiagnosticValues(
        impressions=_count_value(dataframe, position, "impressions"),
        clicks=_count_value(dataframe, position, "clicks"),
        orders=_count_value(dataframe, position, "orders"),
        units_sold=_count_value(dataframe, position, "units_sold"),
        ad_spend=ad_spend,
        inventory=_count_value(dataframe, position, "inventory"),
        ctr=_number_value(dataframe, position, "ctr", allow_nan=True),
        cvr=_number_value(dataframe, position, "cvr", allow_nan=True),
        roas=_number_value(dataframe, position, "roas", allow_nan=True),
        refund_rate=_number_value(
            dataframe,
            position,
            "refund_rate",
            allow_nan=True,
        ),
    )


def _issue(
    context: dict[str, Any],
    code: str,
    metric: str,
    actual_value: int | float,
    threshold: int | float,
    evidence: dict[str, int | float],
    message: str,
) -> DiagnosticIssue:
    return DiagnosticIssue(
        context=context.copy(),
        code=code,
        severity=DIAGNOSTIC_SEVERITY_WARNING,
        metric=metric,
        actual_value=actual_value,
        threshold=threshold,
        evidence=evidence,
        message=message,
    )


def _check_high_impressions_low_ctr(
    context: dict[str, Any],
    values: _DiagnosticValues,
) -> DiagnosticIssue | None:
    if values.impressions < DEMO_MIN_IMPRESSIONS_FOR_LOW_CTR:
        return None
    if values.ctr is None or values.ctr >= DEMO_LOW_CTR_THRESHOLD:
        return None
    return _issue(
        context,
        HIGH_IMPRESSIONS_LOW_CTR,
        "ctr",
        values.ctr,
        DEMO_LOW_CTR_THRESHOLD,
        {
            "impressions": values.impressions,
            "minimum_impressions": DEMO_MIN_IMPRESSIONS_FOR_LOW_CTR,
        },
        "CTR 低于 Demo 默认阈值，且曝光量达到最小样本要求。",
    )


def _check_low_cvr(
    context: dict[str, Any],
    values: _DiagnosticValues,
) -> DiagnosticIssue | None:
    if values.clicks < DEMO_MIN_CLICKS_FOR_LOW_CVR:
        return None
    if values.cvr is None or values.cvr >= DEMO_LOW_CVR_THRESHOLD:
        return None
    return _issue(
        context,
        LOW_CVR,
        "cvr",
        values.cvr,
        DEMO_LOW_CVR_THRESHOLD,
        {
            "clicks": values.clicks,
            "minimum_clicks": DEMO_MIN_CLICKS_FOR_LOW_CVR,
        },
        "CVR 低于 Demo 默认阈值，且点击量达到最小样本要求。",
    )


def _check_clicks_without_orders(
    context: dict[str, Any],
    values: _DiagnosticValues,
) -> DiagnosticIssue | None:
    if values.clicks < DEMO_MIN_CLICKS_WITHOUT_ORDERS:
        return None
    if values.orders != DEMO_NO_ORDERS_VALUE:
        return None
    return _issue(
        context,
        CLICKS_WITHOUT_ORDERS,
        "orders",
        values.orders,
        DEMO_NO_ORDERS_VALUE,
        {
            "clicks": values.clicks,
            "minimum_clicks": DEMO_MIN_CLICKS_WITHOUT_ORDERS,
        },
        "点击量达到最小样本要求，但订单量为 0。",
    )


def _check_spend_without_orders(
    context: dict[str, Any],
    values: _DiagnosticValues,
) -> DiagnosticIssue | None:
    if values.clicks < DEMO_MIN_CLICKS_WITHOUT_ORDERS:
        return None
    if values.ad_spend <= DEMO_POSITIVE_AD_SPEND_FLOOR:
        return None
    if values.orders != DEMO_NO_ORDERS_VALUE:
        return None
    return _issue(
        context,
        SPEND_WITHOUT_ORDERS,
        "orders",
        values.orders,
        DEMO_NO_ORDERS_VALUE,
        {
            "clicks": values.clicks,
            "minimum_clicks": DEMO_MIN_CLICKS_WITHOUT_ORDERS,
            "ad_spend": values.ad_spend,
        },
        "存在广告花费且点击量达到最小样本要求，但订单量为 0。",
    )


def _check_low_roas(
    context: dict[str, Any],
    values: _DiagnosticValues,
) -> DiagnosticIssue | None:
    if values.ad_spend <= DEMO_POSITIVE_AD_SPEND_FLOOR:
        return None
    if values.roas is None or values.roas >= DEMO_LOW_ROAS_THRESHOLD:
        return None
    return _issue(
        context,
        LOW_ROAS,
        "roas",
        values.roas,
        DEMO_LOW_ROAS_THRESHOLD,
        {"ad_spend": values.ad_spend},
        "ROAS 低于 Demo 默认阈值，且广告花费大于 0。",
    )


def _check_high_refund_rate(
    context: dict[str, Any],
    values: _DiagnosticValues,
) -> DiagnosticIssue | None:
    if values.orders < DEMO_MIN_ORDERS_FOR_HIGH_REFUND_RATE:
        return None
    if (
        values.refund_rate is None
        or values.refund_rate <= DEMO_HIGH_REFUND_RATE_THRESHOLD
    ):
        return None
    return _issue(
        context,
        HIGH_REFUND_RATE,
        "refund_rate",
        values.refund_rate,
        DEMO_HIGH_REFUND_RATE_THRESHOLD,
        {
            "orders": values.orders,
            "minimum_orders": DEMO_MIN_ORDERS_FOR_HIGH_REFUND_RATE,
        },
        "退款率高于 Demo 默认阈值，且订单量达到最小样本要求。",
    )


def _check_out_of_stock(
    context: dict[str, Any],
    values: _DiagnosticValues,
) -> DiagnosticIssue | None:
    if values.units_sold < DEMO_MIN_UNITS_SOLD_FOR_STOCKOUT:
        return None
    if values.inventory != DEMO_OUT_OF_STOCK_INVENTORY:
        return None
    return _issue(
        context,
        OUT_OF_STOCK,
        "inventory",
        values.inventory,
        DEMO_OUT_OF_STOCK_INVENTORY,
        {
            "units_sold": values.units_sold,
            "minimum_units_sold": DEMO_MIN_UNITS_SOLD_FOR_STOCKOUT,
        },
        "最新库存快照为 0，且分析期内销量大于 0。",
    )


RuleFunction = Callable[
    [dict[str, Any], _DiagnosticValues],
    DiagnosticIssue | None,
]

_RULES_BY_CODE: dict[str, RuleFunction] = {
    HIGH_IMPRESSIONS_LOW_CTR: _check_high_impressions_low_ctr,
    LOW_CVR: _check_low_cvr,
    CLICKS_WITHOUT_ORDERS: _check_clicks_without_orders,
    SPEND_WITHOUT_ORDERS: _check_spend_without_orders,
    LOW_ROAS: _check_low_roas,
    HIGH_REFUND_RATE: _check_high_refund_rate,
    OUT_OF_STOCK: _check_out_of_stock,
}


def _dimension_columns(dataframe: pd.DataFrame) -> tuple[str, ...]:
    return tuple(
        column for column in dataframe.columns if column in GROUP_DIMENSIONS
    )


def _empty_result(
    dataframe: pd.DataFrame,
    dimensions: tuple[str, ...],
) -> pd.DataFrame:
    columns: dict[str, pd.Series] = {
        dimension: pd.Series(dtype=dataframe[dimension].dtype)
        for dimension in dimensions
    }
    for column in ("code", "severity", "metric", "evidence", "message"):
        columns[column] = pd.Series(dtype="object")
    for column in ("actual_value", "threshold"):
        columns[column] = pd.Series(dtype="Float64")
    output_columns = [*dimensions, *DIAGNOSTIC_ISSUE_COLUMNS]
    return pd.DataFrame(columns).loc[:, output_columns]


def _build_result(
    dataframe: pd.DataFrame,
    dimensions: tuple[str, ...],
    issues: list[DiagnosticIssue],
) -> pd.DataFrame:
    if not issues:
        return _empty_result(dataframe, dimensions)

    output_columns = [*dimensions, *DIAGNOSTIC_ISSUE_COLUMNS]
    result = pd.DataFrame.from_records(
        [issue.to_record() for issue in issues],
        columns=output_columns,
    )
    for dimension in dimensions:
        result[dimension] = pd.Series(
            result[dimension].tolist(),
            index=result.index,
            dtype=dataframe[dimension].dtype,
        )
    for column in ("code", "severity", "metric", "evidence", "message"):
        result[column] = pd.Series(
            result[column].tolist(),
            index=result.index,
            dtype="object",
        )
    for column in ("actual_value", "threshold"):
        result[column] = pd.Series(
            result[column].tolist(),
            index=result.index,
            dtype="Float64",
        )
    return result.loc[:, output_columns]


def _diagnose_metrics(dataframe: pd.DataFrame) -> pd.DataFrame:
    _ensure_input_columns(dataframe)
    dimensions = _dimension_columns(dataframe)
    if dataframe.empty:
        return _empty_result(dataframe, dimensions)

    issues: list[DiagnosticIssue] = []
    for position in range(len(dataframe)):
        context = {
            dimension: dataframe[dimension].iloc[position]
            for dimension in dimensions
        }
        values = _read_values(dataframe, position)
        for code in DIAGNOSTIC_RULE_ORDER:
            issue = _RULES_BY_CODE[code](context, values)
            if issue is not None:
                issues.append(issue)
    return _build_result(dataframe, dimensions, issues)


def diagnose_metrics(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return one deterministic output row per triggered diagnostic rule."""

    if not isinstance(dataframe, pd.DataFrame):
        raise DiagnosticsError(
            "INVALID_DIAGNOSTIC_INPUT",
            "dataframe 必须是 Phase 3 Metrics DataFrame。",
        )

    try:
        return _diagnose_metrics(dataframe)
    except DiagnosticsError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise DiagnosticsError(
            "INVALID_DIAGNOSTIC_INPUT_VALUE",
            "输入包含不符合 Phase 3 Metrics DataFrame 契约的值。",
        ) from exc
