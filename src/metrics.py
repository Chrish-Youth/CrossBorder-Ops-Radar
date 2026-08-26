"""Aggregate validated ecommerce data and calculate the frozen V1 metrics."""

from __future__ import annotations

from collections.abc import Sequence
from math import fsum, isfinite, nan
from operator import index
from typing import Any

import pandas as pd

from src.config import COUNT_MAX_VALUE

GROUP_DIMENSIONS: tuple[str, ...] = (
    "date",
    "marketplace",
    "country",
    "sku",
)

FLOW_COUNT_MEASURES: tuple[str, ...] = (
    "impressions",
    "clicks",
    "orders",
    "units_sold",
    "refunds",
)

MONEY_MEASURES: tuple[str, ...] = (
    "sales",
    "ad_spend",
)

BASE_MEASURES: tuple[str, ...] = (
    "impressions",
    "clicks",
    "orders",
    "units_sold",
    "sales",
    "ad_spend",
    "refunds",
    "inventory",
)

DERIVED_METRICS: tuple[str, ...] = (
    "ctr",
    "cvr",
    "aov",
    "cpc",
    "cpa",
    "roas",
    "refund_rate",
    "gmv",
)

RATIO_METRICS: tuple[str, ...] = DERIVED_METRICS[:-1]

INVENTORY_ENTITY_KEY: tuple[str, ...] = (
    "marketplace",
    "country",
    "sku",
)

METRIC_INPUT_COLUMNS: tuple[str, ...] = (
    *GROUP_DIMENSIONS,
    *BASE_MEASURES,
)


class MetricsCalculationError(Exception):
    """A stable failure raised at the Metrics Engine interface boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def safe_divide(numerator: int | float, denominator: int | float) -> float:
    """Return a raw ratio, using NaN for a zero or missing denominator."""

    if pd.isna(numerator) or pd.isna(denominator) or denominator == 0:
        return nan

    result = numerator / denominator
    if not isfinite(float(result)):
        raise MetricsCalculationError(
            "NON_FINITE_METRIC_RESULT",
            "指标计算产生了非有限结果。",
        )
    return float(result)


def _normalize_group_by(
    group_by: str | Sequence[str] | None,
) -> tuple[str, ...]:
    if group_by is None:
        return ()
    if isinstance(group_by, str):
        dimensions = (group_by,)
    elif isinstance(group_by, Sequence) and not isinstance(
        group_by, (bytes, bytearray)
    ):
        dimensions = tuple(group_by)
    else:
        raise MetricsCalculationError(
            "INVALID_GROUP_BY",
            "group_by 必须是维度名称、有序维度序列或 None。",
        )

    if not dimensions:
        return ()
    if any(not isinstance(item, str) for item in dimensions):
        raise MetricsCalculationError(
            "INVALID_GROUP_BY",
            "group_by 必须是维度名称、有序维度序列或 None。",
        )
    if len(set(dimensions)) != len(dimensions):
        raise MetricsCalculationError(
            "INVALID_GROUP_BY",
            "group_by 不允许包含重复维度。",
        )

    invalid = [item for item in dimensions if item not in GROUP_DIMENSIONS]
    if invalid:
        allowed = ", ".join(GROUP_DIMENSIONS)
        raise MetricsCalculationError(
            "INVALID_GROUP_BY",
            f"不支持的 group_by 维度：{', '.join(invalid)}。允许维度：{allowed}。",
        )
    return dimensions


def _ensure_metric_input_columns(dataframe: pd.DataFrame) -> None:
    missing = [
        column for column in METRIC_INPUT_COLUMNS if column not in dataframe.columns
    ]
    if missing:
        raise MetricsCalculationError(
            "MISSING_METRIC_INPUT_COLUMN",
            f"缺少指标计算必需字段：{', '.join(missing)}。",
        )


def _group_positions(
    dataframe: pd.DataFrame,
    dimensions: tuple[str, ...],
) -> list[tuple[tuple[Any, ...], list[int]]]:
    if not dimensions:
        return [((), list(range(len(dataframe))))]

    groups: dict[tuple[Any, ...], list[int]] = {}
    dimension_values = [dataframe[column].tolist() for column in dimensions]
    for position, values in enumerate(zip(*dimension_values)):
        groups.setdefault(tuple(values), []).append(position)

    try:
        ordered_keys = sorted(groups)
    except TypeError as exc:
        raise MetricsCalculationError(
            "INVALID_METRIC_INPUT_VALUE",
            "分组维度包含无法稳定排序的值；Metrics Engine 只接受 Validator 输出。",
        ) from exc
    return [(key, groups[key]) for key in ordered_keys]


def _count_as_python_int(value: Any, field_name: str) -> int:
    try:
        parsed = index(value)
    except TypeError as exc:
        raise MetricsCalculationError(
            "INVALID_METRIC_INPUT_VALUE",
            f"字段 {field_name} 不是已验证的整数值。",
        ) from exc
    if parsed < 0:
        raise MetricsCalculationError(
            "INVALID_METRIC_INPUT_VALUE",
            f"字段 {field_name} 不是已验证的非负整数值。",
        )
    return parsed


def _sum_counts(
    values: Sequence[Any],
    field_name: str,
    group_key: tuple[Any, ...],
) -> int:
    total = 0
    for value in values:
        total += _count_as_python_int(value, field_name)
        if total > COUNT_MAX_VALUE:
            raise MetricsCalculationError(
                "COUNT_AGGREGATION_OVERFLOW",
                (
                    f"字段 {field_name} 的聚合结果超出 Int64 上限 "
                    f"{COUNT_MAX_VALUE}；group={group_key!r}。"
                ),
            )
    return total


def _sum_count_field(
    dataframe: pd.DataFrame,
    positions: Sequence[int],
    field_name: str,
    group_key: tuple[Any, ...],
) -> int:
    series = dataframe[field_name]
    return _sum_counts(
        [series.iloc[position] for position in positions],
        field_name,
        group_key,
    )


def _sum_money_field(
    dataframe: pd.DataFrame,
    positions: Sequence[int],
    field_name: str,
    group_key: tuple[Any, ...],
) -> float:
    values: list[float] = []
    series = dataframe[field_name]
    for position in positions:
        value = series.iloc[position]
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MetricsCalculationError(
                "INVALID_METRIC_INPUT_VALUE",
                f"字段 {field_name} 不是已验证的金额值。",
            ) from exc
        if not isfinite(parsed):
            raise MetricsCalculationError(
                "INVALID_METRIC_INPUT_VALUE",
                f"字段 {field_name} 不是有限金额值。",
            )
        values.append(parsed)

    try:
        total = fsum(values)
    except OverflowError as exc:
        raise MetricsCalculationError(
            "MONEY_AGGREGATION_OVERFLOW",
            f"字段 {field_name} 的聚合结果溢出；group={group_key!r}。",
        ) from exc
    if not isfinite(total):
        raise MetricsCalculationError(
            "MONEY_AGGREGATION_OVERFLOW",
            f"字段 {field_name} 的聚合结果不是有限值；group={group_key!r}。",
        )
    return total


def _aggregate_inventory(
    dataframe: pd.DataFrame,
    positions: Sequence[int],
    dimensions: tuple[str, ...],
    group_key: tuple[Any, ...],
) -> int:
    if "date" in dimensions:
        return _sum_count_field(dataframe, positions, "inventory", group_key)

    latest_by_entity: dict[tuple[Any, ...], tuple[Any, Any]] = {}
    for position in positions:
        entity_key = tuple(
            dataframe[column].iloc[position] for column in INVENTORY_ENTITY_KEY
        )
        current_date = dataframe["date"].iloc[position]
        existing = latest_by_entity.get(entity_key)
        try:
            is_latest = existing is None or current_date > existing[0]
        except TypeError as exc:
            raise MetricsCalculationError(
                "INVALID_METRIC_INPUT_VALUE",
                "date 不是可比较的 Python datetime.date 值。",
            ) from exc
        if is_latest:
            latest_by_entity[entity_key] = (
                current_date,
                dataframe["inventory"].iloc[position],
            )

    return _sum_counts(
        [snapshot[1] for snapshot in latest_by_entity.values()],
        "inventory",
        group_key,
    )


def _empty_metrics_dataframe(
    dataframe: pd.DataFrame,
    dimensions: tuple[str, ...],
) -> pd.DataFrame:
    columns: dict[str, pd.Series] = {
        dimension: pd.Series(dtype=dataframe[dimension].dtype)
        for dimension in dimensions
    }
    for column in (*FLOW_COUNT_MEASURES, "inventory"):
        columns[column] = pd.Series(dtype="Int64")
    for column in (*MONEY_MEASURES, "gmv"):
        columns[column] = pd.Series(dtype="Float64")
    for column in RATIO_METRICS:
        columns[column] = pd.Series(dtype="float64")
    return pd.DataFrame(columns).loc[:, [*dimensions, *BASE_MEASURES, *DERIVED_METRICS]]


def _build_metrics_dataframe(
    dataframe: pd.DataFrame,
    dimensions: tuple[str, ...],
    records: list[dict[str, Any]],
) -> pd.DataFrame:
    output_columns = [*dimensions, *BASE_MEASURES, *DERIVED_METRICS]
    result = pd.DataFrame.from_records(records, columns=output_columns)
    for dimension in dimensions:
        result[dimension] = pd.Series(
            result[dimension].tolist(),
            index=result.index,
            dtype=dataframe[dimension].dtype,
        )
    for column in (*FLOW_COUNT_MEASURES, "inventory"):
        result[column] = pd.Series(
            result[column].tolist(),
            index=result.index,
            dtype="Int64",
        )
    for column in (*MONEY_MEASURES, "gmv"):
        result[column] = pd.Series(
            result[column].tolist(),
            index=result.index,
            dtype="Float64",
        )
    for column in RATIO_METRICS:
        result[column] = pd.Series(
            result[column].tolist(),
            index=result.index,
            dtype="float64",
        )
    return result.loc[:, output_columns]


def _calculate_metrics(
    dataframe: pd.DataFrame,
    group_by: str | Sequence[str] | None = None,
) -> pd.DataFrame:
    if not isinstance(dataframe, pd.DataFrame):
        raise MetricsCalculationError(
            "INVALID_METRIC_INPUT",
            "dataframe 必须是 pandas.DataFrame。",
        )

    dimensions = _normalize_group_by(group_by)
    _ensure_metric_input_columns(dataframe)
    if dataframe.empty:
        return _empty_metrics_dataframe(dataframe, dimensions)

    records: list[dict[str, Any]] = []
    for group_key, positions in _group_positions(dataframe, dimensions):
        record: dict[str, Any] = dict(zip(dimensions, group_key))
        for field_name in FLOW_COUNT_MEASURES:
            record[field_name] = _sum_count_field(
                dataframe,
                positions,
                field_name,
                group_key,
            )
        for field_name in MONEY_MEASURES:
            record[field_name] = _sum_money_field(
                dataframe,
                positions,
                field_name,
                group_key,
            )
        record["inventory"] = _aggregate_inventory(
            dataframe,
            positions,
            dimensions,
            group_key,
        )

        record["ctr"] = safe_divide(record["clicks"], record["impressions"])
        record["cvr"] = safe_divide(record["orders"], record["clicks"])
        record["aov"] = safe_divide(record["sales"], record["orders"])
        record["cpc"] = safe_divide(record["ad_spend"], record["clicks"])
        record["cpa"] = safe_divide(record["ad_spend"], record["orders"])
        record["roas"] = safe_divide(record["sales"], record["ad_spend"])
        record["refund_rate"] = safe_divide(
            record["refunds"], record["orders"]
        )
        record["gmv"] = record["sales"]
        records.append(record)

    return _build_metrics_dataframe(dataframe, dimensions, records)


def calculate_metrics(
    dataframe: pd.DataFrame,
    group_by: str | Sequence[str] | None = None,
) -> pd.DataFrame:
    """Aggregate a Validator Clean DataFrame into base and derived metrics.

    ``group_by=None`` or an empty sequence returns one overall row for non-empty
    input. A string or ordered sequence can select any combination of the four
    formal dimensions: date, marketplace, country, and sku.
    """

    try:
        return _calculate_metrics(dataframe, group_by)
    except MetricsCalculationError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetricsCalculationError(
            "INVALID_METRIC_INPUT_VALUE",
            "输入包含不符合 Validator Clean DataFrame 契约的值。",
        ) from exc
