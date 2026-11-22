"""Presentation contracts are independent from canonical business values."""

import ast
from pathlib import Path
from string import Formatter

import pytest

import app
from src import ui_text as ui


EXPECTED_METRICS = {
    "ctr": "点击率（CTR）", "cvr": "转化率（CVR）",
    "aov": "平均订单金额（AOV）", "cpc": "单次点击成本（CPC）",
    "cpa": "单笔订单广告成本（CPA）", "roas": "广告投入产出比（ROAS）",
    "refund_rate": "退款率（Refund Rate）", "gmv": "商品交易总额（GMV）",
}
EXPECTED_AI_CODES = set("""
PROVIDER_CONFIGURATION_ERROR PROVIDER_AUTH_FAILED PROVIDER_ACCOUNT_ERROR
PROVIDER_TIMEOUT PROVIDER_RATE_LIMITED PROVIDER_CONNECTION_FAILED
PROVIDER_UNAVAILABLE PROVIDER_REQUEST_REJECTED PROVIDER_FAILURE
INVALID_PROVIDER INVALID_PROVIDER_RESPONSE PROVIDER_RESPONSE_TOO_LARGE
INVALID_PROVIDER_JSON INVALID_INSIGHT_OUTPUT OUTPUT_TOO_LARGE INVALID_PROVIDER_USAGE
INVALID_INSIGHT_INPUT INVALID_PROMPT_INPUT PIPELINE_NOT_ANALYZABLE
INSIGHT_CONTEXT_TOO_LARGE PROMPT_TOO_LARGE NON_FINITE_INSIGHT_VALUE
UNEXPECTED_AI_ERROR
""".split())
EXPECTED_ERRORS = set("""
FILE_READ_ERROR EMPTY_FILE MALFORMED_CSV UNSUPPORTED_FILE_TYPE INVALID_STAGE_RESULT
INVALID_GROUP_BY INVALID_METRIC_INPUT INVALID_METRIC_INPUT_VALUE
MISSING_METRIC_INPUT_COLUMN COUNT_AGGREGATION_OVERFLOW MONEY_AGGREGATION_OVERFLOW
NON_FINITE_METRIC_RESULT INVALID_DIAGNOSTIC_INPUT INVALID_DIAGNOSTIC_INPUT_VALUE
MISSING_DIAGNOSTIC_INPUT_COLUMN INVALID_REPORT_INPUT INVALID_REPORT_DATA
INCONSISTENT_REPORT_DATA EXCEL_CELL_TEXT_TOO_LONG EXCEL_ROW_LIMIT_EXCEEDED
EXCEL_EXPORT_ERROR UNEXPECTED_REPORT_ERROR UNEXPECTED_APPLICATION_ERROR
""".split())


def test_resource_module_has_no_runtime_dependencies_or_duplicate_keys() -> None:
    tree = ast.parse(Path(ui.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)) for node in ast.walk(tree))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
            assert len(keys) == len(set(keys)), "Duplicate resource key before dict collapse"


def test_resources_are_nonblank_strings_and_required_categories_exist() -> None:
    categories = {
        "TEXT", "GROUP_LABELS", "SCOPE_LABELS", "METRIC_LABELS", "METRIC_HELP",
        "FIELD_LABELS", "TABLE_CODE_LABELS", "LEVEL_LABELS", "CONFIDENCE_LABELS",
        "VALIDATION_MESSAGES", "DIAGNOSTIC_LABELS",
        "DIAGNOSTIC_MESSAGES", "AI_ERROR_MESSAGES", "ERROR_MESSAGES", "ERROR_TITLES",
    }
    assert categories <= set(vars(ui))
    for name, value in vars(ui).items():
        if name.isupper() and isinstance(value, dict):
            assert value
            assert all(type(k) is str and k.strip() for k in value)
            assert all(type(v) is str and v.strip() for v in value.values())


def test_core_glossary_and_metrics_are_exact() -> None:
    assert ui.METRIC_LABELS == EXPECTED_METRICS
    assert set(ui.METRIC_HELP) == set(EXPECTED_METRICS)
    assert ui.TEXT["ai"] == "AI 洞察"
    assert ui.TEXT["generate"] == "生成 AI 洞察"
    assert ui.TEXT["regenerate"] == "重新生成 AI 洞察"
    assert ui.TEXT["validation"] == "数据校验"
    assert ui.TEXT["diagnostics"] == "诊断信号"
    assert ui.FIELD_LABELS["orders"] == "订单数"
    assert ui.FIELD_LABELS["refunds"] == "退款订单数"
    assert ui.LEVEL_LABELS == {"Fatal": "致命错误", "Error": "错误", "Warning": "警告"}
    assert ui.CONFIDENCE_LABELS == {"low": "低", "medium": "中", "high": "高"}


def test_group_labels_preserve_internal_options() -> None:
    assert ui.GROUP_LABELS == {
        "Overall": "整体", "SKU": "SKU", "Marketplace": "平台", "Country": "国家/地区",
        "Marketplace + Country": "平台 + 国家/地区",
        "Marketplace + Country + SKU": "平台 + 国家/地区 + SKU",
        "Date + Marketplace + Country + SKU": "日期 + 平台 + 国家/地区 + SKU",
    }
    assert set(ui.GROUP_LABELS) == set(app.GROUP_BY_OPTIONS)
    assert app.resolve_group_by("Marketplace + Country") == ["marketplace", "country"]
    with pytest.raises(ValueError):
        app.resolve_group_by("平台 + 国家/地区")


def test_validation_and_diagnostic_maps_are_complete() -> None:
    assert set(ui.VALIDATION_MESSAGES) == set("""
        MISSING_REQUIRED_COLUMN MISSING_REQUIRED_VALUE INVALID_DATE_FORMAT
        INVALID_NUMERIC_VALUE NEGATIVE_VALUE INTEGER_OUT_OF_RANGE CLICKS_GT_IMPRESSIONS
        EXACT_DUPLICATE BUSINESS_KEY_CONFLICT ORDERS_GT_CLICKS REFUNDS_GT_ORDERS
    """.split())
    codes = set("""
        HIGH_IMPRESSIONS_LOW_CTR LOW_CVR CLICKS_WITHOUT_ORDERS SPEND_WITHOUT_ORDERS
        LOW_ROAS HIGH_REFUND_RATE OUT_OF_STOCK
    """.split())
    assert set(ui.DIAGNOSTIC_LABELS) == set(ui.DIAGNOSTIC_MESSAGES) == codes
    assert ui.DIAGNOSTIC_LABELS["LOW_CVR"] == "转化率偏低"
    assert "去重" in ui.VALIDATION_MESSAGES["EXACT_DUPLICATE"]
    assert "该行保留" in ui.VALIDATION_MESSAGES["ORDERS_GT_CLICKS"]


def test_safe_error_inventory_is_complete() -> None:
    assert set(ui.AI_ERROR_MESSAGES) == EXPECTED_AI_CODES
    assert set(ui.ERROR_MESSAGES) == EXPECTED_ERRORS
    assert ui.AI_ERROR_MESSAGES["PROVIDER_TIMEOUT"] == "Provider 请求超时。"
    assert ui.ERROR_MESSAGES["FILE_READ_ERROR"] == "无法读取文件，请检查文件内容与编码。"


@pytest.mark.parametrize("code", sorted(EXPECTED_AI_CODES))
def test_ai_safe_messages_do_not_expose_error_codes(code: str) -> None:
    message = app._ai_error_message(code)
    assert message == ui.AI_ERROR_MESSAGES[code]
    assert code not in message


@pytest.mark.parametrize("code", ["UNKNOWN_CODE", "<SECRET>", "SECRET\nRAW", "", None, []])
def test_unknown_or_untrusted_ai_code_has_safe_fallback(code: object) -> None:
    assert app._ai_error_message(code) == "AI 洞察生成遇到意外错误。"  # type: ignore[arg-type]


def test_all_controlled_templates_format_and_app_references_exist() -> None:
    formatter = Formatter()
    for mapping in (ui.TEXT, ui.VALIDATION_MESSAGES):
        for value in mapping.values():
            fields = {name for _, name, _, _ in formatter.parse(value) if name}
            assert value.format(**dict.fromkeys(fields, 3)).strip()
    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            if isinstance(node.value.value, ast.Name) and node.value.value.id == "ui":
                if isinstance(node.slice, ast.Constant):
                    assert node.slice.value in getattr(ui, node.value.attr)


def test_app_does_not_integrate_token_measurement_or_translation() -> None:
    source = Path(app.__file__).read_text(encoding="utf-8")
    for forbidden in ("insight_token_measurement", "LocalTokenMeasurement", "local_encoded_input_tokens", "translate_output"):
        assert forbidden not in source
