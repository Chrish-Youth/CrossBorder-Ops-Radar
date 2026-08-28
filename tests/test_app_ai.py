from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

import app
import src.deepseek_provider as deepseek_module
import src.insight_provider as provider_module
from src.config import REQUIRED_COLUMNS
from src.insight_prompt import InsightOutput, PriorityInsight
from src.insight_provider import (
    INVALID_PROVIDER,
    INVALID_PROVIDER_JSON,
    INVALID_PROVIDER_RESPONSE,
    PROVIDER_ACCOUNT_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_FAILURE,
    PROVIDER_RATE_LIMITED,
    PROVIDER_REQUEST_REJECTED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    InsightProviderError,
)
from src.pipeline import PipelineStatus


PROJECT_ROOT = Path(__file__).parents[1]
APP_PATH = PROJECT_ROOT / "app.py"
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample_ecommerce_data.csv"


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
    writer.writerows(rows)
    return text.getvalue().encode("utf-8")


def insight_output(
    *,
    summary: str = "Validated AI summary.",
    priorities: tuple[PriorityInsight, ...] | None = None,
) -> InsightOutput:
    if priorities is None:
        priorities = (
            PriorityInsight(
                scope={},
                observation="Overall observation.",
                evidence_codes=("LOW_ROAS", "LOW_CVR"),
                possible_explanations=(),
                recommended_checks=(),
                confidence="high",
            ),
            PriorityInsight(
                scope={"sku": "SKU-A"},
                observation="SKU observation.",
                evidence_codes=("LOW_CVR",),
                possible_explanations=("A cautious hypothesis.",),
                recommended_checks=(),
                confidence="medium",
            ),
            PriorityInsight(
                scope={"marketplace": "Amazon", "country": "US"},
                observation="Marketplace and country observation.",
                evidence_codes=("OUT_OF_STOCK",),
                possible_explanations=(),
                recommended_checks=("Inspect the inventory feed.",),
                confidence="low",
            ),
        )
    return InsightOutput(
        version="1",
        executive_summary=summary,
        priority_insights=priorities,
        overall_limitations=("The interpretation is bounded by supplied data.",),
    )


@dataclass
class AiHarness:
    outcome: object
    constructor_calls: int = 0
    generate_calls: int = 0
    last_context: object | None = None
    last_provider: object | None = None

    def set_outcome(self, outcome: object) -> None:
        self.outcome = outcome


@pytest.fixture
def ai_harness(monkeypatch: pytest.MonkeyPatch) -> AiHarness:
    harness = AiHarness(insight_output())

    class FakeDeepSeekProvider:
        def __init__(self) -> None:
            harness.constructor_calls += 1

    def fake_generate_insight(context: object, *, provider: object) -> InsightOutput:
        harness.generate_calls += 1
        harness.last_context = context
        harness.last_provider = provider
        if isinstance(harness.outcome, BaseException):
            raise harness.outcome
        return harness.outcome  # type: ignore[return-value]

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        deepseek_module,
        "DeepSeekInsightProvider",
        FakeDeepSeekProvider,
    )
    monkeypatch.setattr(
        provider_module,
        "generate_insight",
        fake_generate_insight,
    )
    return harness


def app_test() -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=20).run()


def upload_and_run(
    filename: str,
    content: bytes,
    *,
    group_label: str = "SKU",
) -> AppTest:
    at = app_test()
    at.file_uploader[0].upload(filename, content, "text/csv").run()
    at.selectbox[0].select(group_label).run()
    return at.button[0].click().run(timeout=20)


def click_named_button(at: AppTest, label: str) -> AppTest:
    button = next(button for button in at.button if button.label == label)
    return button.click().run(timeout=20)


def rendered_text(at: AppTest) -> list[str]:
    collections = (
        at.error,
        at.warning,
        at.info,
        at.markdown,
        at.caption,
        at.text,
    )
    return [str(element.value) for elements in collections for element in elements]


def test_app_without_credential_starts_without_constructing_provider(
    ai_harness: AiHarness,
) -> None:
    at = app_test()

    assert list(at.exception) == []
    assert ai_harness.constructor_calls == 0
    assert ai_harness.generate_calls == 0
    assert "AI Insights" not in [heading.value for heading in at.subheader]


def test_upload_and_run_never_generate_ai_automatically(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    assert list(at.exception) == []
    assert ai_harness.constructor_calls == 0
    assert ai_harness.generate_calls == 0
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS
    assert "AI Insights" in [heading.value for heading in at.subheader]
    assert "Generate AI Insights" in [button.label for button in at.button]
    assert at.session_state["ai_output"] is None


def test_generate_click_calls_public_ai_path_once_and_stores_validated_output(
    ai_harness: AiHarness,
) -> None:
    expected = ai_harness.outcome
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.constructor_calls == 1
    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] == expected
    assert at.session_state["ai_error_code"] is None
    assert at.session_state["ai_error_message"] is None
    assert at.session_state["ai_signature"] == app.build_ai_signature(
        at.session_state["analysis_signature"]
    )
    assert "Regenerate AI Insights" in [button.label for button in at.button]


def test_same_signature_rerender_and_download_render_do_not_generate_again(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    assert len(at.download_button) == 1

    at.run()

    assert ai_harness.constructor_calls == 1
    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is not None


def test_explicit_regenerate_is_one_new_request_and_replaces_output(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    replacement = insight_output(summary="Replacement summary.")
    ai_harness.set_outcome(replacement)

    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.constructor_calls == 2
    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == replacement
    assert at.session_state["ai_error_message"] is None


def test_same_analysis_rerun_preserves_paid_output_without_ai_call(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    expected = at.session_state["ai_output"]

    at.button[0].click().run(timeout=20)

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] == expected
    assert "Regenerate AI Insights" in [button.label for button in at.button]


@pytest.mark.parametrize("change", ["file", "group"])
def test_upstream_change_immediately_invalidates_ai_state(
    change: str,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    assert at.session_state["ai_output"] is not None

    if change == "file":
        at.file_uploader[0].upload(
            "changed.csv",
            csv_content(make_row(sku="SKU-CHANGED")),
            "text/csv",
        ).run()
    else:
        at.selectbox[0].select("Overall").run()

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_error_code"] is None
    assert at.session_state["ai_error_message"] is None
    assert at.session_state["ai_signature"] is None
    assert "AI Insights" not in [heading.value for heading in at.subheader]


def test_validation_failed_never_exposes_ai_action(
    ai_harness: AiHarness,
) -> None:
    columns = tuple(column for column in REQUIRED_COLUMNS if column != "sku")
    at = upload_and_run(
        "fatal.csv",
        csv_content(make_row(), columns=columns),
    )

    assert at.session_state["pipeline_result"].status is (
        PipelineStatus.VALIDATION_FAILED
    )
    assert ai_harness.constructor_calls == 0
    assert ai_harness.generate_calls == 0
    assert "AI Insights" not in [heading.value for heading in at.subheader]
    assert all("AI Insights" not in button.label for button in at.button)


@pytest.mark.parametrize(
    "content",
    [
        csv_content(make_row(clicks=101, impressions=100)),
        csv_content(
            make_row(
                impressions=100,
                clicks=10,
                orders=1,
                units_sold=1,
                sales=20,
                ad_spend=5,
                refunds=0,
                inventory=10,
            )
        ),
    ],
    ids=["empty-success", "no-diagnostics"],
)
def test_empty_success_and_no_diagnostics_still_allow_explicit_generation(
    content: bytes,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run("edge.csv", content)
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is not None


def test_first_generation_failure_is_safe_and_preserves_deterministic_results(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_outcome(
        InsightProviderError(
            PROVIDER_CONFIGURATION_ERROR,
            "SECRET_API_KEY SECRET_PROMPT SECRET_RESPONSE",
        )
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    excel_bytes = at.session_state["excel_bytes"]

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_error_code"] == PROVIDER_CONFIGURATION_ERROR
    assert "not configured" in at.session_state["ai_error_message"]
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS
    assert at.session_state["excel_bytes"] == excel_bytes
    assert len(at.download_button) == 1
    assert all(
        secret not in " ".join(rendered_text(at))
        for secret in ("SECRET_API_KEY", "SECRET_PROMPT", "SECRET_RESPONSE")
    )


def test_regenerate_failure_retains_previous_success_and_labels_it(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous = at.session_state["ai_output"]
    ai_harness.set_outcome(
        InsightProviderError(PROVIDER_TIMEOUT, "SECRET_TIMEOUT_DETAIL")
    )

    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == previous
    assert at.session_state["ai_error_code"] == PROVIDER_TIMEOUT
    assert any(
        "Showing the previous successful result" in warning.value
        for warning in at.warning
    )
    assert "Overall observation." in " ".join(rendered_text(at))
    assert "SECRET_TIMEOUT_DETAIL" not in " ".join(rendered_text(at))


def test_manual_generation_after_first_failure_can_recover_cleanly(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_outcome(
        InsightProviderError(PROVIDER_TIMEOUT, "SECRET_TIMEOUT_DETAIL")
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    assert at.session_state["ai_error_code"] == PROVIDER_TIMEOUT
    recovered = insight_output(summary="Recovered summary.")
    ai_harness.set_outcome(recovered)

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == recovered
    assert at.session_state["ai_error_code"] is None
    assert at.session_state["ai_error_message"] is None
    assert "Regenerate AI Insights" in [button.label for button in at.button]


def test_unexpected_ai_exception_is_sanitized_without_clearing_analysis(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_outcome(RuntimeError("SECRET_INTERNAL_DETAIL"))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_error_code"] == "UNEXPECTED_AI_ERROR"
    assert "SECRET_INTERNAL_DETAIL" not in at.session_state["ai_error_message"]
    assert "SECRET_INTERNAL_DETAIL" not in " ".join(rendered_text(at))
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS
    assert len(at.download_button) == 1


@pytest.mark.parametrize(
    ("code", "message_fragment"),
    [
        (PROVIDER_CONFIGURATION_ERROR, "not configured"),
        (PROVIDER_AUTH_FAILED, "authentication failed"),
        (PROVIDER_ACCOUNT_ERROR, "account cannot"),
        (PROVIDER_TIMEOUT, "timed out"),
        (PROVIDER_RATE_LIMITED, "rate limited"),
        (PROVIDER_CONNECTION_FAILED, "Could not connect"),
        (PROVIDER_UNAVAILABLE, "temporarily unavailable"),
        (PROVIDER_REQUEST_REJECTED, "rejected the request"),
        (PROVIDER_FAILURE, "could not complete"),
        (INVALID_PROVIDER, "safely accepted"),
        (INVALID_PROVIDER_RESPONSE, "safely accepted"),
        (INVALID_PROVIDER_JSON, "safely accepted"),
        ("INVALID_INSIGHT_OUTPUT", "safely accepted"),
        ("OUTPUT_TOO_LARGE", "safely accepted"),
    ],
)
def test_ai_error_codes_map_to_safe_product_messages(
    code: str,
    message_fragment: str,
) -> None:
    message = app._ai_error_message(code)

    assert message_fragment in message
    assert code not in message


def test_ai_rendering_supports_scope_confidence_evidence_and_optional_sections(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    text = " ".join(rendered_text(at))

    assert "Validated AI summary." in text
    assert [expander.label for expander in at.expander] == [
        "Overall",
        "SKU: SKU-A",
        "Marketplace: Amazon · Country: US",
    ]
    assert "Confidence: High" in text
    assert "Confidence: Medium" in text
    assert "Confidence: Low" in text
    assert "LOW_ROAS" in text and "LOW_CVR" in text
    assert "Possible explanations (hypotheses)" in text
    assert "Recommended checks (investigations)" in text
    assert "The interpretation is bounded by supplied data." in text
    assert "Root Causes" not in text
    assert "Diagnostic signals are observations" in text
    assert "not proven root causes or guaranteed actions" in text


def test_empty_priority_insights_render_as_a_valid_empty_state(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_outcome(insight_output(priorities=()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert at.session_state["ai_output"].priority_insights == ()
    assert any(
        "No priority insight was produced" in message.value
        for message in at.info
    )
    assert "Validated AI summary." in " ".join(rendered_text(at))


@pytest.mark.parametrize(
    "constant_name",
    [
        "INSIGHT_CONTEXT_VERSION",
        "INSIGHT_PROMPT_VERSION",
        "INSIGHT_OUTPUT_VERSION",
        "DEEPSEEK_MODEL",
    ],
)
def test_ai_signature_binds_analysis_and_contract_without_credentials(
    constant_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SECRET_API_KEY")
    original = app.build_ai_signature("analysis-a")

    assert original == app.build_ai_signature("analysis-a")
    assert original != app.build_ai_signature("analysis-b")
    monkeypatch.setattr(app, constant_name, "future")
    assert original != app.build_ai_signature("analysis-a")
    assert "SECRET_API_KEY" not in original


def test_session_state_never_stores_provider_prompt_raw_response_or_key(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")

    state_keys = set(at.session_state.filtered_state)
    assert {
        "ai_output",
        "ai_error_code",
        "ai_error_message",
        "ai_signature",
    }.issubset(state_keys)
    forbidden_fragments = ("api_key", "prompt", "raw_response", "provider", "client")
    assert all(
        fragment not in key.lower()
        for key in state_keys
        for fragment in forbidden_fragments
    )
    assert not any(
        isinstance(at.session_state[key], type(ai_harness.last_provider))
        for key in state_keys
        if at.session_state[key] is not None
    )
