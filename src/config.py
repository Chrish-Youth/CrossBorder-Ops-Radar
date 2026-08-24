"""Frozen V1 input data contract shared by loading and validation code."""

REQUIRED_COLUMNS: tuple[str, ...] = (
    "date",
    "marketplace",
    "country",
    "sku",
    "product_name",
    "impressions",
    "clicks",
    "orders",
    "units_sold",
    "sales",
    "ad_spend",
    "refunds",
    "inventory",
)

BUSINESS_KEY_COLUMNS: tuple[str, ...] = (
    "date",
    "marketplace",
    "country",
    "sku",
)

STRING_COLUMNS: tuple[str, ...] = (
    "marketplace",
    "country",
    "sku",
    "product_name",
)

INTEGER_COLUMNS: tuple[str, ...] = (
    "impressions",
    "clicks",
    "orders",
    "units_sold",
    "refunds",
    "inventory",
)

COUNT_MIN_VALUE = 0
COUNT_MAX_VALUE = 2**63 - 1

FLOAT_COLUMNS: tuple[str, ...] = (
    "sales",
    "ad_spend",
)

NUMERIC_COLUMNS: tuple[str, ...] = INTEGER_COLUMNS + FLOAT_COLUMNS
NON_NEGATIVE_COLUMNS: tuple[str, ...] = NUMERIC_COLUMNS

DATE_COLUMN = "date"
DATE_FORMAT = "%Y-%m-%d"
