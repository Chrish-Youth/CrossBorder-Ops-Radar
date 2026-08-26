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

# Phase 4 deterministic diagnostics contract. These values are Demo defaults,
# not industry standards or marketplace recommendations.
DIAGNOSTIC_SEVERITY_WARNING = "Warning"

HIGH_IMPRESSIONS_LOW_CTR = "HIGH_IMPRESSIONS_LOW_CTR"
LOW_CVR = "LOW_CVR"
CLICKS_WITHOUT_ORDERS = "CLICKS_WITHOUT_ORDERS"
SPEND_WITHOUT_ORDERS = "SPEND_WITHOUT_ORDERS"
LOW_ROAS = "LOW_ROAS"
HIGH_REFUND_RATE = "HIGH_REFUND_RATE"
OUT_OF_STOCK = "OUT_OF_STOCK"

DIAGNOSTIC_RULE_ORDER: tuple[str, ...] = (
    HIGH_IMPRESSIONS_LOW_CTR,
    LOW_CVR,
    CLICKS_WITHOUT_ORDERS,
    SPEND_WITHOUT_ORDERS,
    LOW_ROAS,
    HIGH_REFUND_RATE,
    OUT_OF_STOCK,
)

DEMO_MIN_IMPRESSIONS_FOR_LOW_CTR = 1000
DEMO_LOW_CTR_THRESHOLD = 0.01

DEMO_MIN_CLICKS_FOR_LOW_CVR = 50
DEMO_LOW_CVR_THRESHOLD = 0.02

DEMO_MIN_CLICKS_WITHOUT_ORDERS = 20
DEMO_NO_ORDERS_VALUE = 0
DEMO_POSITIVE_AD_SPEND_FLOOR = 0.0

DEMO_LOW_ROAS_THRESHOLD = 1.0

DEMO_MIN_ORDERS_FOR_HIGH_REFUND_RATE = 10
DEMO_HIGH_REFUND_RATE_THRESHOLD = 0.10

DEMO_OUT_OF_STOCK_INVENTORY = 0
DEMO_MIN_UNITS_SOLD_FOR_STOCKOUT = 1
