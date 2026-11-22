"""User-visible AI behavior through the Streamlit entry point, with offline responses."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import deepseek_provider, ui_text as ui
from src.insight_provider import (
    PROVIDER_TIMEOUT,
    InsightProviderError,
    MockInsightProvider,
    ProviderUsage,
)
from src.pipeline import PipelineStatus

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data/sample_ecommerce_data.csv"
SUMMARY = "SKU 运营摘要，保留 English 商品标识。"


def response(summary=SUMMARY):
    return json.dumps({
        "version": "2",
        "executive_summary": summary,
        "priority_insights": [],
        "overall_limitations": ["归因还需要更多数据。"],
    }, ensure_ascii=False)


@pytest.fixture
def provider(monkeypatch):
    fake = MockInsightProvider(response(), usage=ProviderUsage(100, 20, 120))
    monkeypatch.setattr(deepseek_provider, "DeepSeekInsightProvider", lambda: fake)
    return fake


def analyze(path=SAMPLE):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    mime = "text/csv" if path.suffix == ".csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    at.file_uploader("operations_data_upload").upload(path.name, path.read_bytes(), mime).run()
    at.button("run_analysis").click().run()
    assert not at.exception
    return at


def generate(at):
    at.button("generate_ai_insights").click().run()
    assert not at.exception


def visible_text(at):
    elements = (*at.error, *at.warning, *at.info, *at.markdown, *at.caption)
    return "\n".join(str(element.value) for element in elements)


def test_without_key_analysis_and_download_work_before_and_after_ai_click(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    at = analyze(ROOT / "data/demo/interview_success.xlsx")
    workbook = at.session_state["excel_bytes"]
    assert workbook and not at.error
    assert "DEEPSEEK_API_KEY" not in visible_text(at)

    generate(at)
    assert "DEEPSEEK_API_KEY" in at.error[0].value
    assert at.session_state["excel_bytes"] == workbook
    assert at.download_button("download_excel_report").label == ui.TEXT["download_excel"]


def test_generate_once_and_passive_reruns_keep_the_result(provider):
    at = analyze()
    assert provider.call_count == 0
    generate(at)
    assert provider.call_count == 1
    assert at.button("generate_ai_insights").label == ui.TEXT["regenerate"]
    assert SUMMARY in visible_text(at)
    assert "合计 120 Token" in visible_text(at)

    at.run()
    at.button("run_analysis").click().run()
    assert provider.call_count == 1
    assert SUMMARY in visible_text(at)
    assert len(at.download_button) == 1


def test_timeout_requires_manual_retry_and_usage_is_optional(provider):
    at = analyze(ROOT / "data/demo/interview_success.xlsx")
    provider.error = InsightProviderError(PROVIDER_TIMEOUT, "SENSITIVE")
    generate(at)
    assert provider.call_count == 1
    assert ui.AI_ERROR_MESSAGES[PROVIDER_TIMEOUT] in at.error[0].value
    assert "SENSITIVE" not in visible_text(at)
    assert at.session_state["ai_generation"] is None
    at.run()
    assert provider.call_count == 1

    provider.error = None
    provider.usage = None
    generate(at)
    assert provider.call_count == 2 and not at.error
    assert SUMMARY in visible_text(at)
    assert "DeepSeek 报告的用量" not in visible_text(at)


def test_evidence_and_suggested_checks_are_shown_for_the_matching_sku(provider):
    payload = json.loads(response())
    payload["priority_insights"] = [{
        "scope": {"sku": "SKU-LOW-CVR"},
        "observation": "观察到转化率偏低。",
        "evidence_codes": ["LOW_CVR", "LOW_ROAS"],
        "possible_explanations": ["可能与归因差异有关。"],
        "recommended_checks": ["核查落地页一致性。"],
        "confidence": "medium",
    }]
    provider.response = json.dumps(payload, ensure_ascii=False)
    at = analyze()
    generate(at)
    for expected in ("观察到转化率偏低。", "LOW_CVR", "核查落地页一致性。", "置信度：中"):
        assert expected in visible_text(at)


@pytest.mark.parametrize("prior_success", [False, True])
def test_invalid_language_does_not_publish_response_or_overwrite_success(provider, caplog, prior_success):
    at = analyze()
    if prior_success:
        generate(at)
    previous = at.session_state["ai_generation"]
    secret = "SECRET_ENGLISH_PROVIDER_OUTPUT_123"
    provider.response = response(secret)
    generate(at)

    assert provider.call_count == (2 if prior_success else 1)
    assert at.session_state["ai_generation"] == previous
    assert ui.AI_REJECTION_MESSAGE in visible_text(at)
    assert (SUMMARY in visible_text(at)) == prior_success
    assert secret not in visible_text(at)
    assert secret not in repr(at.session_state.filtered_state)
    assert secret not in caplog.text

    provider.response = response("重新生成的摘要。")
    generate(at)
    assert "重新生成的摘要。" in visible_text(at)
    assert ui.AI_REJECTION_MESSAGE not in visible_text(at)


@pytest.mark.parametrize("change", ["filename", "bytes", "group", "remove"])
def test_change_of_analysis_clears_ai_result_and_error(provider, change):
    at = analyze()
    generate(at)
    provider.error = InsightProviderError(PROVIDER_TIMEOUT, "SENSITIVE")
    generate(at)
    if change == "group":
        at.selectbox("analysis_level").select("Overall").run()
    elif change == "remove":
        at.file_uploader("operations_data_upload").clear().run()
    else:
        filename = "renamed.csv" if change == "filename" else SAMPLE.name
        content = SAMPLE.read_bytes() if change == "filename" else b"sku\nchanged\n"
        at.file_uploader("operations_data_upload").upload(filename, content, "text/csv").run()

    assert not at.exception
    assert provider.call_count == 2
    assert at.session_state["ai_generation"] is None
    assert at.session_state["ai_error"] is None
    assert SUMMARY not in visible_text(at)


def test_fatal_input_has_no_ai_action(provider):
    at = analyze(ROOT / "data/demo/interview_fatal_missing_country.xlsx")
    assert at.session_state["pipeline_result"].status is PipelineStatus.VALIDATION_FAILED
    assert ui.TEXT["generate"] not in [button.label for button in at.button]
    assert provider.call_count == 0
