from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

import app
import src.deepseek_provider as deepseek_module
import src.insight_receipt_v4 as receipt_v4_module
import src.insight_retry_execution as execution_module
import src.insights as insights_module
from src.config import REQUIRED_COLUMNS
from src.insight_attempt_audit import FAILED, SUCCEEDED
from src.insight_logical_generation_cost import (
    FULLY_ESTIMATED,
    INVALID_LOGICAL_GENERATION_COST,
    UNKNOWN_TOTAL,
    UNAVAILABLE as LOGICAL_COST_UNAVAILABLE,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    USAGE_UNAVAILABLE,
)
from src.insight_prompt import InsightOutput, PriorityInsight
from src.insight_provider import (
    INVALID_PROVIDER,
    INVALID_PROVIDER_JSON,
    INVALID_PROVIDER_RESPONSE,
    INVALID_PROVIDER_USAGE,
    PROVIDER_ACCOUNT_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_FAILURE,
    PROVIDER_RATE_LIMITED,
    PROVIDER_REQUEST_REJECTED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    InsightProviderError,
    ProviderGeneration,
    ProviderUsage,
)
from src.insight_receipt import (
    INSIGHT_RECEIPT_VERSION,
    InsightGenerationReceipt,
)
from src.insight_receipt_v4 import (
    INSIGHT_RECEIPT_V4_VERSION,
    INVALID_RECEIPT_V4_INPUT,
    InsightGenerationReceiptV4,
    InsightReceiptV4Error,
)
from src.insight_retry_delay import DEFAULT_RETRY_DELAY_POLICY
from src.insight_retry_execution import (
    INVALID_RETRY_EXECUTION,
    RetryExecutionError,
    RetryExecutionResult,
)
from src.pipeline import PipelineStatus


PROJECT_ROOT = Path(__file__).parents[1]
APP_PATH = PROJECT_ROOT / "app.py"
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample_ecommerce_data.csv"
PRICING_REFERENCE = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)


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
    priorities: tuple[PriorityInsight, ...] = (),
) -> InsightOutput:
    return InsightOutput(
        version="1",
        executive_summary=summary,
        priority_insights=priorities,
        overall_limitations=(
            "The interpretation is bounded by supplied data.",
        ),
    )


DEFAULT_USAGE = ProviderUsage(
    prompt_tokens=100,
    completion_tokens=20,
    total_tokens=120,
    prompt_cache_hit_tokens=40,
    prompt_cache_miss_tokens=60,
    reasoning_tokens=0,
)


@dataclass(frozen=True)
class ProviderStep:
    outcome: InsightOutput | BaseException | str
    usage: ProviderUsage | None = DEFAULT_USAGE


@dataclass
class AiHarness:
    steps: list[ProviderStep]
    constructor_calls: int = 0
    provider_calls: int = 0
    execution_calls: int = 0
    receipt_calls: int = 0
    sleep_calls: list[int] | None = None
    completed_results: list[RetryExecutionResult] | None = None
    events: list[str] | None = None
    last_context: object | None = None
    last_provider: object | None = None
    execution_error: BaseException | None = None
    receipt_error: BaseException | None = None
    constructor_error: BaseException | None = None
    delay_policy: object | None = None
    clock_calls: int = 0
    receipt_generated_at: list[str] | None = None

    def set_steps(self, *steps: ProviderStep) -> None:
        self.steps = list(steps)

    def success(
        self,
        output: InsightOutput | None = None,
        *,
        usage: ProviderUsage | None = DEFAULT_USAGE,
    ) -> None:
        self.set_steps(ProviderStep(output or insight_output(), usage))


@pytest.fixture
def ai_harness(monkeypatch: pytest.MonkeyPatch) -> AiHarness:
    harness = AiHarness([ProviderStep(insight_output())])
    harness.sleep_calls = []
    harness.completed_results = []
    harness.events = []
    harness.receipt_generated_at = []
    real_execute = execution_module.execute_insight_generation_with_retry
    real_receipt_builder = receipt_v4_module.build_insight_receipt_v4

    class FakeDeepSeekProvider:
        provider_name = "deepseek"
        model = deepseek_module.DEEPSEEK_MODEL

        def __init__(self) -> None:
            harness.constructor_calls += 1
            if harness.constructor_error is not None:
                raise harness.constructor_error

        def generate(self, _prompt: object) -> ProviderGeneration:
            harness.provider_calls += 1
            assert harness.events is not None
            harness.events.append("provider")
            if not harness.steps:
                raise AssertionError("No fake Provider step configured")
            step = harness.steps.pop(0)
            if isinstance(step.outcome, BaseException):
                raise step.outcome
            raw_text = (
                step.outcome
                if isinstance(step.outcome, str)
                else json.dumps(
                    step.outcome.to_dict(),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
            return ProviderGeneration(raw_text=raw_text, usage=step.usage)

    def fake_clock() -> datetime:
        value = PRICING_REFERENCE + timedelta(seconds=harness.clock_calls)
        harness.clock_calls += 1
        return value

    def fake_sleeper(delay_ms: int) -> None:
        assert harness.sleep_calls is not None
        assert harness.events is not None
        harness.sleep_calls.append(delay_ms)
        harness.events.append(f"sleep:{delay_ms}")

    def tracked_execution(
        context: object,
        *,
        provider: object,
    ) -> RetryExecutionResult:
        harness.execution_calls += 1
        harness.last_context = context
        harness.last_provider = provider
        assert harness.events is not None
        harness.events.append("execution:start")
        if harness.execution_error is not None:
            raise harness.execution_error
        result = real_execute(
            context,  # type: ignore[arg-type]
            provider=provider,  # type: ignore[arg-type]
            retry_delay_policy=harness.delay_policy,  # type: ignore[arg-type]
            utc_now=fake_clock,
            sleeper=fake_sleeper,
        )
        assert harness.completed_results is not None
        harness.completed_results.append(result)
        harness.events.append("execution:return")
        return result

    def tracked_receipt_builder(**kwargs: object) -> InsightGenerationReceiptV4:
        harness.receipt_calls += 1
        assert harness.events is not None
        assert harness.receipt_generated_at is not None
        harness.events.append("receipt")
        generated_at = kwargs["generated_at"]
        assert isinstance(generated_at, str)
        harness.receipt_generated_at.append(generated_at)
        if harness.receipt_error is not None:
            raise harness.receipt_error
        return real_receipt_builder(**kwargs)  # type: ignore[arg-type]

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        deepseek_module,
        "DeepSeekInsightProvider",
        FakeDeepSeekProvider,
    )
    monkeypatch.setattr(
        execution_module,
        "execute_insight_generation_with_retry",
        tracked_execution,
    )
    monkeypatch.setattr(
        receipt_v4_module,
        "build_insight_receipt_v4",
        tracked_receipt_builder,
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
    return [str(element.value) for items in collections for element in items]


def state_failure(at: AppTest) -> Any:
    failure = at.session_state["ai_failure"]
    assert type(failure).__name__ == "AiGenerationFailure"
    assert set(type(failure).__dataclass_fields__) == {
        "signature",
        "error_code",
        "execution_result",
    }
    return failure


def timeout_error(detail: str = "SECRET_TIMEOUT") -> InsightProviderError:
    return InsightProviderError(PROVIDER_TIMEOUT, detail)


def terminal_error(
    code: str = PROVIDER_AUTH_FAILED,
    detail: str = "SECRET_TERMINAL",
) -> InsightProviderError:
    return InsightProviderError(code, detail)


def delay_policy_with_timeout(delay_ms: int) -> object:
    rules = tuple(
        (code, delay_ms if code == PROVIDER_TIMEOUT else value)
        for code, value in DEFAULT_RETRY_DELAY_POLICY.base_delays_ms
    )
    return replace(
        DEFAULT_RETRY_DELAY_POLICY,
        version=f"test-delay-{delay_ms}-v1",
        base_delays_ms=rules,
    )


def make_legacy_v3(receipt: InsightGenerationReceiptV4) -> InsightGenerationReceipt:
    return InsightGenerationReceipt(
        version=INSIGHT_RECEIPT_VERSION,
        generated_at=receipt.generated_at,
        analysis_signature=receipt.analysis_signature,
        group_by=receipt.group_by,
        context_version=receipt.context_version,
        prompt_version=receipt.prompt_version,
        output_version=receipt.output_version,
        provider=receipt.provider,
        model=receipt.model,
        metric_record_count=receipt.metric_record_count,
        diagnostic_signal_count=receipt.diagnostic_signal_count,
        priority_insight_count=receipt.priority_insight_count,
        cost=receipt.cost,
        usage=receipt.usage,
    )


def test_app_starts_without_constructing_provider(ai_harness: AiHarness) -> None:
    at = app_test()

    assert list(at.exception) == []
    assert ai_harness.constructor_calls == 0
    assert ai_harness.execution_calls == 0
    assert ai_harness.provider_calls == 0


def test_analysis_never_generates_ai_automatically(ai_harness: AiHarness) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    assert list(at.exception) == []
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS
    assert ai_harness.execution_calls == ai_harness.provider_calls == 0
    assert "Generate AI Insights" in [button.label for button in at.button]


def test_immediate_success_commits_output_v4_and_signature_atomically(
    ai_harness: AiHarness,
) -> None:
    expected = insight_output(summary="Generation A")
    ai_harness.success(expected)
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    receipt = at.session_state["ai_receipt"]
    assert list(at.exception) == []
    assert at.session_state["ai_output"] == expected
    assert isinstance(receipt, InsightGenerationReceiptV4)
    assert receipt.version == INSIGHT_RECEIPT_V4_VERSION == "4"
    assert at.session_state["ai_signature"] == app.build_ai_signature(
        at.session_state["analysis_signature"]
    )
    binding = at.session_state["ai_success_binding"]
    assert isinstance(binding, str)
    assert len(binding) == 64
    assert binding == app.build_ai_success_binding(
        at.session_state["ai_output"],
        receipt,
        at.session_state["ai_signature"],
    )
    assert at.session_state["ai_failure"] is None
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == len(receipt.attempt_audit.attempts) == 1
    assert ai_harness.receipt_calls == 1


def test_success_binding_is_deterministic_and_sensitive_to_every_input(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    output = at.session_state["ai_output"]
    receipt = at.session_state["ai_receipt"]
    signature = at.session_state["ai_signature"]
    binding = at.session_state["ai_success_binding"]

    assert binding == app.build_ai_success_binding(output, receipt, signature)
    assert binding == app.build_ai_success_binding(output, receipt, signature)
    assert binding != app.build_ai_success_binding(
        insight_output(summary="Different output"),
        receipt,
        signature,
    )
    assert binding != app.build_ai_success_binding(
        output,
        replace(receipt, generated_at="2026-08-17T01:02:03+00:00"),
        signature,
    )
    assert binding != app.build_ai_success_binding(output, receipt, "f" * 64)
    assert set(binding) <= set("0123456789abcdef")


def test_transient_failure_then_success_is_one_execution_two_attempts(
    ai_harness: AiHarness,
) -> None:
    recovered = insight_output(summary="Recovered after retry")
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(recovered))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    receipt = at.session_state["ai_receipt"]
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 2
    assert len(receipt.attempt_audit.attempts) == 2
    assert len(receipt.delay_audit.records) == 1
    assert ai_harness.sleep_calls == [1000]
    assert receipt.logical_generation_cost.status == UNKNOWN_TOTAL


def test_two_transient_failures_exhaust_attempt_limit(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(
        ProviderStep(timeout_error("first secret")),
        ProviderStep(timeout_error("second secret")),
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    failure = state_failure(at)
    assert failure.error_code == PROVIDER_TIMEOUT
    assert failure.execution_result is not None
    assert failure.execution_result.status == FAILED
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 2
    assert len(failure.execution_result.attempt_audit.attempts) == 2
    assert len(failure.execution_result.delay_audit.records) == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None


def test_permanent_terminal_failure_uses_one_attempt(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    result = state_failure(at).execution_result
    assert result is not None and result.status == FAILED
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1
    assert len(result.attempt_audit.attempts) == 1
    assert len(result.delay_audit.records) == 0


def test_rerender_does_not_start_retry_execution_or_provider(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]

    at.run()

    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1
    assert at.session_state["ai_receipt"] is receipt


def test_same_signature_analysis_rerun_does_not_start_ai(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    output = at.session_state["ai_output"]
    receipt = at.session_state["ai_receipt"]

    at = click_named_button(at, "Run Analysis")

    assert ai_harness.execution_calls == ai_harness.provider_calls == 1
    assert at.session_state["ai_output"] is output
    assert at.session_state["ai_receipt"] is receipt


def test_excel_and_generation_details_render_have_no_ai_side_effect(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    assert len(at.download_button) == 2

    at.run()

    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1
    assert ai_harness.receipt_calls == 1


def test_receipt_download_serialization_has_no_ai_side_effect(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]

    first = app.build_receipt_json_bytes(receipt)
    second = app.build_receipt_json_bytes(receipt)

    assert first == second
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1
    assert ai_harness.receipt_calls == 1


def test_regenerate_success_replaces_the_whole_success_snapshot(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    first_output = at.session_state["ai_output"]
    first_receipt = at.session_state["ai_receipt"]
    first_binding = at.session_state["ai_success_binding"]
    replacement = insight_output(summary="Generation B")
    ai_harness.success(replacement)

    at = click_named_button(at, "Regenerate AI Insights")

    assert at.session_state["ai_output"] == replacement
    assert at.session_state["ai_output"] is not first_output
    assert at.session_state["ai_receipt"] is not first_receipt
    assert at.session_state["ai_receipt"].priority_insight_count == len(
        replacement.priority_insights
    )
    assert at.session_state["ai_success_binding"] != first_binding
    assert at.session_state["ai_success_binding"] == app.build_ai_success_binding(
        at.session_state["ai_output"],
        at.session_state["ai_receipt"],
        at.session_state["ai_signature"],
    )
    assert at.session_state["ai_failure"] is None
    assert ai_harness.execution_calls == 2
    assert ai_harness.provider_calls == 2


def test_initial_completed_failure_has_no_partial_success(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    excel_bytes = at.session_state["excel_bytes"]

    at = click_named_button(at, "Generate AI Insights")

    failure = state_failure(at)
    assert failure.execution_result is not None
    assert failure.execution_result.status == FAILED
    assert failure.error_code == failure.execution_result.error_code
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert at.session_state["excel_bytes"] == excel_bytes
    assert "Download AI Receipt" not in [b.label for b in at.download_button]


def test_regenerate_completed_failure_preserves_prior_success(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    old_output = at.session_state["ai_output"]
    old_receipt = at.session_state["ai_receipt"]
    old_signature = at.session_state["ai_signature"]
    old_binding = at.session_state["ai_success_binding"]
    ai_harness.set_steps(
        ProviderStep(timeout_error("first")),
        ProviderStep(timeout_error("second")),
    )

    at = click_named_button(at, "Regenerate AI Insights")

    failure = state_failure(at)
    assert at.session_state["ai_output"] is old_output
    assert at.session_state["ai_receipt"] is old_receipt
    assert at.session_state["ai_signature"] == old_signature
    assert at.session_state["ai_success_binding"] == old_binding
    assert failure.execution_result is not None
    assert failure.execution_result.status == FAILED
    assert any(
        "Showing the previous successful result" in warning.value
        for warning in at.warning
    )
    assert "first" not in " ".join(rendered_text(at))
    assert "second" not in " ".join(rendered_text(at))


def test_retry_execution_error_without_prior_success_has_unknown_provenance(
    ai_harness: AiHarness,
) -> None:
    ai_harness.execution_error = RetryExecutionError(
        INVALID_RETRY_EXECUTION,
        "SECRET_EXECUTION_DETAIL",
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    failure = state_failure(at)
    assert failure.error_code == INVALID_RETRY_EXECUTION
    assert failure.execution_result is None
    assert at.session_state["ai_output"] is None
    text = " ".join(rendered_text(at))
    assert "Attempt audit unavailable" in text
    assert "Provider attempts: 0" not in text
    assert "SECRET_EXECUTION_DETAIL" not in text


def test_retry_execution_error_during_regenerate_preserves_success(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    old = (
        at.session_state["ai_output"],
        at.session_state["ai_receipt"],
        at.session_state["ai_signature"],
        at.session_state["ai_success_binding"],
    )
    ai_harness.execution_error = RetryExecutionError(
        INVALID_RETRY_EXECUTION,
        "SECRET",
    )

    at = click_named_button(at, "Regenerate AI Insights")

    assert (
        at.session_state["ai_output"],
        at.session_state["ai_receipt"],
        at.session_state["ai_signature"],
        at.session_state["ai_success_binding"],
    ) == old
    assert state_failure(at).execution_result is None


def test_provider_construction_failure_preserves_prior_success(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    old_output = at.session_state["ai_output"]
    old_receipt = at.session_state["ai_receipt"]
    ai_harness.constructor_error = InsightProviderError(
        PROVIDER_CONFIGURATION_ERROR,
        "SECRET_KEY_DETAIL",
    )

    at = click_named_button(at, "Regenerate AI Insights")

    failure = state_failure(at)
    assert at.session_state["ai_output"] is old_output
    assert at.session_state["ai_receipt"] is old_receipt
    assert failure.execution_result is None
    assert failure.error_code == PROVIDER_CONFIGURATION_ERROR
    assert "SECRET_KEY_DETAIL" not in " ".join(rendered_text(at))


def test_context_failure_has_no_completed_execution_result(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    def fail_context(_result: object) -> object:
        raise insights_module.InsightContextError(
            "INVALID_INSIGHT_INPUT",
            "SECRET_CONTEXT_DETAIL",
        )

    monkeypatch.setattr(insights_module, "build_insight_context", fail_context)
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    failure = state_failure(at)
    assert failure.execution_result is None
    assert failure.error_code == "INVALID_INSIGHT_INPUT"
    assert ai_harness.constructor_calls == 0
    assert ai_harness.execution_calls == 0
    assert ai_harness.provider_calls == 0


def test_initial_receipt_failure_retains_succeeded_execution_without_output(
    ai_harness: AiHarness,
) -> None:
    ai_harness.receipt_error = InsightReceiptV4Error(
        INVALID_RECEIPT_V4_INPUT,
        "SECRET_RECEIPT_DETAIL",
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    failure = state_failure(at)
    assert failure.error_code == INVALID_RECEIPT_V4_INPUT
    assert failure.execution_result is not None
    assert failure.execution_result.status == SUCCEEDED
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert ai_harness.provider_calls == 1
    assert "SECRET_RECEIPT_DETAIL" not in " ".join(rendered_text(at))


def test_unexpected_post_execution_failure_keeps_succeeded_result_and_safe_log(
    caplog: pytest.LogCaptureFixture,
    ai_harness: AiHarness,
) -> None:
    marker = "SECRET_POST_EXECUTION_DETAIL_DO_NOT_LOG"
    caplog.set_level("ERROR")
    ai_harness.receipt_error = RuntimeError(marker)
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    failure = state_failure(at)
    assert failure.error_code == "UNEXPECTED_AI_ERROR"
    assert failure.execution_result is not None
    assert failure.execution_result.status == SUCCEEDED
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert marker not in " ".join(rendered_text(at))
    assert marker not in repr(at.session_state.filtered_state)
    ai_records = [
        record
        for record in caplog.records
        if "Unexpected AI operation failure" in record.getMessage()
    ]
    assert len(ai_records) == 1
    assert "stage=receipt_build" in ai_records[0].getMessage()
    assert "exception_type=RuntimeError" in ai_records[0].getMessage()
    assert marker not in ai_records[0].getMessage()
    assert ai_records[0].exc_info is None
    assert ai_harness.provider_calls == 1


def test_successful_execution_then_receipt_failure_preserves_old_pair_and_new_audit(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    old_output = at.session_state["ai_output"]
    old_receipt = at.session_state["ai_receipt"]
    old_signature = at.session_state["ai_signature"]
    old_binding = at.session_state["ai_success_binding"]
    uncommitted = insight_output(summary="Uncommitted B")
    ai_harness.success(uncommitted)
    ai_harness.receipt_error = InsightReceiptV4Error(
        INVALID_RECEIPT_V4_INPUT,
        "SECRET",
    )

    at = click_named_button(at, "Regenerate AI Insights")

    failure = state_failure(at)
    assert at.session_state["ai_output"] is old_output
    assert at.session_state["ai_receipt"] is old_receipt
    assert at.session_state["ai_signature"] == old_signature
    assert at.session_state["ai_success_binding"] == old_binding
    assert failure.error_code == INVALID_RECEIPT_V4_INPUT
    assert failure.execution_result is not None
    assert failure.execution_result.status == SUCCEEDED
    assert failure.execution_result.output == uncommitted
    assert failure.execution_result.attempt_audit.attempts[-1].status == SUCCEEDED
    assert old_receipt.attempt_audit is not failure.execution_result.attempt_audit
    assert ai_harness.provider_calls == 2


def test_new_failure_replaces_previous_failure(ai_harness: AiHarness) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error(PROVIDER_AUTH_FAILED)))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    first = state_failure(at)
    ai_harness.set_steps(ProviderStep(terminal_error(PROVIDER_ACCOUNT_ERROR)))

    at = click_named_button(at, "Generate AI Insights")

    second = state_failure(at)
    assert second is not first
    assert second.error_code == PROVIDER_ACCOUNT_ERROR
    assert second.execution_result is not first.execution_result


def test_success_after_failure_clears_failure(ai_harness: AiHarness) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    assert state_failure(at).execution_result is not None
    ai_harness.success(insight_output(summary="Recovered"))

    at = click_named_button(at, "Generate AI Insights")

    assert at.session_state["ai_failure"] is None
    assert at.session_state["ai_output"].executive_summary == "Recovered"
    assert isinstance(at.session_state["ai_receipt"], InsightGenerationReceiptV4)


def test_completed_failure_ui_displays_only_audited_counts(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(timeout_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    text = " ".join(rendered_text(at))
    assert "Provider attempts: 2" in text
    assert "Completed retry-delay transitions: 1" in text
    assert "Retry policy:" in text
    assert "Delay policy:" in text


def test_receipt_failure_ui_identifies_succeeded_execution(
    ai_harness: AiHarness,
) -> None:
    ai_harness.receipt_error = InsightReceiptV4Error(
        INVALID_RECEIPT_V4_INPUT,
        "hidden",
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    text = " ".join(rendered_text(at))
    assert "Completed execution status: succeeded" in text
    assert "Provider execution completed successfully" in text
    assert "post-execution application processing failed" in text
    assert "Provider attempts: 1" in text


def test_v4_download_contains_all_retry_aware_contracts(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")

    payload = json.loads(
        app.build_receipt_json_bytes(at.session_state["ai_receipt"])
    )

    assert payload["version"] == "4"
    assert "attempt_audit" in payload
    assert "delay_audit" in payload
    assert "logical_generation_cost" in payload
    assert payload["attempt_audit"]["attempts"][0]["status"] == SUCCEEDED


def test_512_digit_usage_commits_bound_v4_and_strict_json(
    ai_harness: AiHarness,
) -> None:
    maximum = 10**512 - 1
    usage = ProviderUsage(
        prompt_tokens=maximum,
        completion_tokens=0,
        total_tokens=maximum,
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=maximum,
        reasoning_tokens=0,
    )
    ai_harness.success(usage=usage)
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    receipt = at.session_state["ai_receipt"]
    binding = at.session_state["ai_success_binding"]
    assert isinstance(receipt, InsightGenerationReceiptV4)
    assert receipt.usage == usage
    assert isinstance(binding, str) and len(binding) == 64
    assert binding == app.build_ai_success_binding(
        at.session_state["ai_output"],
        receipt,
        at.session_state["ai_signature"],
    )
    json.dumps(receipt.to_dict(), ensure_ascii=False, allow_nan=False)
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1
    assert ai_harness.receipt_calls == 1


def test_legacy_v3_session_pair_is_cleared_not_reconstructed(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    v4 = at.session_state["ai_receipt"]
    at.session_state["ai_receipt"] = make_legacy_v3(v4)

    at.run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert ai_harness.execution_calls == 1
    assert "Generate AI Insights" in [button.label for button in at.button]


def test_legacy_error_keys_are_removed_during_initialization(
    ai_harness: AiHarness,
) -> None:
    at = app_test()
    at.session_state["ai_error_code"] = "OLD"
    at.session_state["ai_error_message"] = "old message"

    at.run()

    state = at.session_state.filtered_state
    assert "ai_error_code" not in state
    assert "ai_error_message" not in state
    assert state["ai_failure"] is None


@pytest.mark.parametrize(
    "present_keys",
    [
        ("ai_output",),
        ("ai_receipt",),
        ("ai_signature",),
        ("ai_success_binding",),
        ("ai_output", "ai_receipt"),
        ("ai_output", "ai_signature"),
        ("ai_output", "ai_success_binding"),
        ("ai_receipt", "ai_signature"),
        ("ai_receipt", "ai_success_binding"),
        ("ai_signature", "ai_success_binding"),
        ("ai_output", "ai_receipt", "ai_signature"),
        ("ai_output", "ai_receipt", "ai_success_binding"),
        ("ai_output", "ai_signature", "ai_success_binding"),
        ("ai_receipt", "ai_signature", "ai_success_binding"),
    ],
)
def test_incomplete_success_snapshot_fails_closed(
    present_keys: tuple[str, ...],
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    original = {
        key: at.session_state[key]
        for key in (
            "ai_output",
            "ai_receipt",
            "ai_signature",
            "ai_success_binding",
        )
    }
    for key in original:
        at.session_state[key] = original[key] if key in present_keys else None

    at.run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert ai_harness.execution_calls == 1


def test_mismatched_receipt_and_output_fail_closed(ai_harness: AiHarness) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    object.__setattr__(receipt, "priority_insight_count", 1)

    at.run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert ai_harness.execution_calls == 1


def test_same_shape_output_b_with_receipt_a_fails_binding_closed(
    ai_harness: AiHarness,
) -> None:
    ai_harness.success(insight_output(summary="Generation A"))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt_a = at.session_state["ai_receipt"]
    binding_a = at.session_state["ai_success_binding"]
    output_b = insight_output(summary="Generation B")
    assert len(output_b.priority_insights) == receipt_a.priority_insight_count

    at.session_state["ai_output"] = output_b
    at.run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert binding_a is not None
    assert ai_harness.execution_calls == 1


def test_output_a_with_compatible_receipt_b_fails_binding_closed(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    output_a = at.session_state["ai_output"]
    receipt_a = at.session_state["ai_receipt"]
    receipt_b = replace(
        receipt_a,
        generated_at="2026-08-17T01:02:03+00:00",
    )
    assert receipt_b.priority_insight_count == len(output_a.priority_insights)

    at.session_state["ai_receipt"] = receipt_b
    at.run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert ai_harness.execution_calls == 1


@pytest.mark.parametrize(
    "malformed_binding",
    [123, "", "0" * 63, "g" * 64, "0" * 64],
)
def test_malformed_or_mismatched_success_binding_fails_closed(
    malformed_binding: object,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    at.session_state["ai_success_binding"] = malformed_binding

    at.run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert ai_harness.execution_calls == 1


def test_success_binding_serialization_failure_is_safe_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    marker = "SECRET_BINDING_SERIALIZATION_DETAIL_DO_NOT_LOG"
    caplog.set_level("ERROR")

    def fail_to_dict(_self: object) -> dict[str, Any]:
        raise RuntimeError(marker)

    monkeypatch.setattr(InsightOutput, "to_dict", fail_to_dict)
    at.run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert marker not in " ".join(rendered_text(at))
    assert marker not in repr(at.session_state.filtered_state)
    ai_records = [
        record
        for record in caplog.records
        if "Unexpected AI operation failure" in record.getMessage()
    ]
    assert len(ai_records) == 1
    assert "stage=success_binding" in ai_records[0].getMessage()
    assert "exception_type=RuntimeError" in ai_records[0].getMessage()
    assert marker not in ai_records[0].getMessage()
    assert ai_records[0].exc_info is None
    assert ai_harness.execution_calls == 1


def test_single_attempt_available_cost_has_distinct_truthful_labels(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    text = " ".join(rendered_text(at))

    assert receipt.logical_generation_cost.status == FULLY_ESTIMATED
    assert "Final successful attempt estimated API cost (USD): $" in text
    assert "Logical-generation estimated total (USD): $" in text
    assert "Estimated total API cost" not in text
    assert "not the provider's final billed amount" in text


def test_single_attempt_unavailable_cost_does_not_render_zero(
    ai_harness: AiHarness,
) -> None:
    ai_harness.success(usage=None)
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    text = " ".join(rendered_text(at))

    assert receipt.cost.status == "unavailable"
    assert receipt.cost.unavailable_reason == USAGE_UNAVAILABLE
    assert receipt.logical_generation_cost.status == LOGICAL_COST_UNAVAILABLE
    assert "Final successful attempt cost estimate unavailable" in text
    assert "Logical-generation total estimate unavailable" in text
    assert "Logical-generation estimated total (USD): $0" not in text


def test_single_attempt_missing_cache_breakdown_is_unavailable(
    ai_harness: AiHarness,
) -> None:
    usage = ProviderUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    ai_harness.success(usage=usage)
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]

    assert receipt.cost.unavailable_reason == CACHE_BREAKDOWN_UNAVAILABLE
    assert "Cache hit/miss breakdown unavailable" in " ".join(rendered_text(at))


def test_multi_attempt_available_final_cost_never_becomes_total(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(insight_output()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    text = " ".join(rendered_text(at))

    assert receipt.cost.status == "available"
    assert receipt.logical_generation_cost.status == UNKNOWN_TOTAL
    assert "Final successful attempt estimated API cost (USD): $" in text
    assert "Logical-generation total spend is unknown" in text
    assert "Estimated total API cost" not in text


def test_multi_attempt_unavailable_final_cost_still_has_unknown_total(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(
        ProviderStep(timeout_error()),
        ProviderStep(insight_output(), usage=None),
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    text = " ".join(rendered_text(at))

    assert receipt.cost.status == "unavailable"
    assert receipt.logical_generation_cost.status == UNKNOWN_TOTAL
    assert "Final successful attempt cost estimate unavailable" in text
    assert "Logical-generation total spend is unknown" in text
    assert "Logical-generation total estimate unavailable because" not in text


def test_requested_delay_ui_uses_exact_milliseconds_not_elapsed_claim(
    ai_harness: AiHarness,
) -> None:
    ai_harness.delay_policy = delay_policy_with_timeout(2500)
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(insight_output()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    text = " ".join(rendered_text(at))
    assert ai_harness.sleep_calls == [2500]
    assert "2,500 ms requested" in text
    assert "actual wait" not in text.lower()
    assert "elapsed" not in text.lower()


def test_receipt_time_is_utc_and_captured_after_execution(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    parsed = datetime.fromisoformat(receipt.generated_at)

    assert parsed.utcoffset() == timedelta(0)
    assert receipt.generated_at != receipt.attempt_audit.attempts[0].pricing_reference_at
    assert ai_harness.events is not None
    assert ai_harness.events.index("execution:return") < ai_harness.events.index(
        "receipt"
    )


def test_different_analysis_clears_success_and_failure(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = click_named_button(at, "Regenerate AI Insights")
    assert at.session_state["ai_failure"] is not None

    at.file_uploader[0].upload(
        "different.csv",
        csv_content(make_row(sku="DIFFERENT")),
        "text/csv",
    ).run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert at.session_state["ai_failure"] is None
    assert ai_harness.execution_calls == 2


@pytest.mark.parametrize("change", ["filename", "bytes", "group"])
def test_each_analysis_identity_change_invalidates_ai_state(
    change: str,
    ai_harness: AiHarness,
) -> None:
    content = SAMPLE_PATH.read_bytes()
    at = upload_and_run(SAMPLE_PATH.name, content)
    at = click_named_button(at, "Generate AI Insights")

    if change == "filename":
        at.file_uploader[0].upload("renamed.csv", content, "text/csv").run()
    elif change == "bytes":
        at.file_uploader[0].upload(
            SAMPLE_PATH.name,
            csv_content(make_row(sku="CHANGED")),
            "text/csv",
        ).run()
    else:
        at.selectbox[0].select("Overall").run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert at.session_state["ai_failure"] is None
    assert ai_harness.execution_calls == 1


def test_analysis_a_to_b_to_a_does_not_restore_old_success_or_binding(
    ai_harness: AiHarness,
) -> None:
    content_a = SAMPLE_PATH.read_bytes()
    at = upload_and_run(SAMPLE_PATH.name, content_a)
    at = click_named_button(at, "Generate AI Insights")
    old_binding = at.session_state["ai_success_binding"]

    at.file_uploader[0].upload(
        "analysis-b.csv",
        csv_content(make_row(sku="ANALYSIS-B")),
        "text/csv",
    ).run()
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None

    at.file_uploader[0].upload(
        SAMPLE_PATH.name,
        content_a,
        "text/csv",
    ).run()

    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["ai_success_binding"] is None
    assert old_binding is not None
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1


def test_ai_signature_contract_does_not_include_retry_versions() -> None:
    analysis_signature = "a" * 64
    before = app.build_ai_signature(analysis_signature)

    original = execution_module.RETRY_EXECUTION_VERSION
    execution_module.RETRY_EXECUTION_VERSION = "test-other-version"
    try:
        after = app.build_ai_signature(analysis_signature)
    finally:
        execution_module.RETRY_EXECUTION_VERSION = original

    assert before == after
    assert before == app.build_ai_signature(analysis_signature)


def test_session_stores_only_canonical_ai_state_not_provider_or_raw_text(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    keys = set(at.session_state.filtered_state)

    assert {
        "ai_output",
        "ai_receipt",
        "ai_signature",
        "ai_success_binding",
        "ai_failure",
    }.issubset(keys)
    binding = at.session_state["ai_success_binding"]
    assert isinstance(binding, str)
    assert len(binding) == 64
    assert "ai_error_code" not in keys
    assert "ai_error_message" not in keys
    assert not any(
        fragment in key.lower()
        for key in keys
        for fragment in ("api_key", "prompt", "raw_response", "client")
    )
    assert not any(
        at.session_state[key] is ai_harness.last_provider for key in keys
    )


def test_receipt_json_contract_is_private_and_strict(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    payload_bytes = app.build_receipt_json_bytes(at.session_state["ai_receipt"])
    payload = json.loads(payload_bytes)
    text = payload_bytes.decode("utf-8")

    assert payload["analysis_signature"] == at.session_state["analysis_signature"]
    assert "executive_summary" not in payload
    assert "raw_response" not in text
    assert "raw_prompt" not in text.lower()
    assert "api_key" not in text.lower()


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
        (INVALID_PROVIDER_USAGE, "metadata that could not be safely accepted"),
        ("INVALID_INSIGHT_OUTPUT", "safely accepted"),
        ("OUTPUT_TOO_LARGE", "safely accepted"),
        ("INVALID_INSIGHT_INPUT", "could not be prepared"),
        ("INSIGHT_CONTEXT_TOO_LARGE", "too large"),
        (INVALID_RETRY_EXECUTION, "retry execution"),
        (INVALID_RECEIPT_V4_INPUT, "valid generation details"),
        ("INVALID_LOGICAL_GENERATION_COST", "safely recorded"),
        ("UNKNOWN_CODE", "unexpected error"),
    ],
)
def test_ai_error_codes_map_to_safe_product_messages(
    code: str,
    message_fragment: str,
) -> None:
    message = app._ai_error_message(code)

    assert message_fragment in message
    assert code not in message


@pytest.mark.parametrize(
    ("analysis_signature", "expected_fragment"),
    [
        ("abc123" * 10, "abc123abc123"),
        ("../evil\\name", "evilname"),
        ("测" * 20, "unknown"),
        ("___", "unknown"),
        ("A-b_C.9", "AbC9"),
    ],
)
def test_receipt_download_filename_is_short_and_path_safe(
    analysis_signature: str,
    expected_fragment: str,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    object.__setattr__(receipt, "analysis_signature", analysis_signature)

    filename = app.build_receipt_download_filename(receipt)

    assert expected_fragment in filename
    assert "/" not in filename and "\\" not in filename
    assert filename.endswith(".json")


def test_ai_output_rendering_supports_scopes_and_optional_sections(
    ai_harness: AiHarness,
) -> None:
    priority = PriorityInsight(
        scope={"sku": "SKU-LOW-CVR"},
        observation="Observed low conversion.",
        evidence_codes=("LOW_CVR", "LOW_ROAS"),
        possible_explanations=("Attribution may differ.",),
        recommended_checks=("Check landing-page consistency.",),
        confidence="medium",
    )
    ai_harness.success(insight_output(priorities=(priority,)))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    text = " ".join(rendered_text(at))
    assert "Observed low conversion" in text
    assert "Possible explanations (hypotheses)" in text
    assert "Recommended checks (investigations)" in text
    assert "Confidence: Medium" in text


def test_empty_priority_output_is_a_valid_renderable_success(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert at.session_state["ai_output"].priority_insights == ()
    assert "No priority insight was produced" in " ".join(rendered_text(at))
    assert isinstance(at.session_state["ai_receipt"], InsightGenerationReceiptV4)


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
def test_empty_success_or_no_diagnostics_still_allows_explicit_generation(
    content: bytes,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run("edge.csv", content)

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1
    assert isinstance(at.session_state["ai_receipt"], InsightGenerationReceiptV4)


def test_validation_failed_never_exposes_ai_action(ai_harness: AiHarness) -> None:
    malformed = b"date,sku\n2026-08-24,A\n"
    at = upload_and_run("fatal.csv", malformed)

    assert at.session_state["pipeline_result"].status is PipelineStatus.VALIDATION_FAILED
    assert "AI Insights" not in [heading.value for heading in at.subheader]
    assert ai_harness.execution_calls == 0
    assert ai_harness.provider_calls == 0


def test_unexpected_execution_exception_is_sanitized(
    caplog: pytest.LogCaptureFixture,
    ai_harness: AiHarness,
) -> None:
    marker = "SECRET_INTERNAL_DETAIL_DO_NOT_LOG"
    caplog.set_level("ERROR")
    ai_harness.execution_error = RuntimeError(marker)
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    failure = state_failure(at)
    assert failure.error_code == "UNEXPECTED_AI_ERROR"
    assert failure.execution_result is None
    assert marker not in " ".join(rendered_text(at))
    assert marker not in repr(at.session_state.filtered_state)
    ai_records = [
        record
        for record in caplog.records
        if "Unexpected AI operation failure" in record.getMessage()
    ]
    assert len(ai_records) == 1
    assert "stage=retry_execution" in ai_records[0].getMessage()
    assert "exception_type=RuntimeError" in ai_records[0].getMessage()
    assert marker not in ai_records[0].getMessage()
    assert ai_records[0].exc_info is None
    assert ai_records[0].exc_text is None
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS


def test_generation_details_exception_is_sanitized_without_state_loss(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    output = at.session_state["ai_output"]
    receipt = at.session_state["ai_receipt"]
    signature = at.session_state["ai_signature"]
    binding = at.session_state["ai_success_binding"]
    marker = "SECRET_RENDER_DETAIL_DO_NOT_LOG"
    caplog.set_level("ERROR")
    real_to_dict = InsightGenerationReceiptV4.to_dict
    calls = 0

    def fail_to_dict(_self: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise RuntimeError(marker)
        return real_to_dict(_self)  # type: ignore[arg-type]

    monkeypatch.setattr(InsightGenerationReceiptV4, "to_dict", fail_to_dict)
    at.run()

    assert at.session_state["ai_output"] is output
    assert at.session_state["ai_receipt"] is receipt
    assert at.session_state["ai_signature"] == signature
    assert at.session_state["ai_success_binding"] == binding
    assert app._AI_AUDIT_PRESENTATION_ERROR_MESSAGE in " ".join(rendered_text(at))
    assert marker not in " ".join(rendered_text(at))
    assert marker not in repr(at.session_state.filtered_state)
    ai_records = [
        record
        for record in caplog.records
        if "Unexpected AI operation failure" in record.getMessage()
    ]
    assert len(ai_records) == 1
    assert "stage=render" in ai_records[0].getMessage()
    assert "exception_type=RuntimeError" in ai_records[0].getMessage()
    assert marker not in ai_records[0].getMessage()
    assert ai_records[0].exc_info is None
    assert ai_records[0].exc_text is None
    assert ai_harness.execution_calls == 1


def test_generation_details_boundary_does_not_swallow_baseexception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(_receipt: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "_render_generation_details", interrupt)
    with pytest.raises(KeyboardInterrupt):
        app._render_generation_details_safely(object())  # type: ignore[arg-type]


def test_invalid_failure_object_is_cleared_without_crashing(
    ai_harness: AiHarness,
) -> None:
    ai_harness.execution_error = RetryExecutionError(
        INVALID_RETRY_EXECUTION,
        "hidden",
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    failure = state_failure(at)
    object.__setattr__(failure, "execution_result", object())

    at.run()

    assert list(at.exception) == []
    assert at.session_state["ai_failure"] is None
    assert ai_harness.execution_calls == 1


def test_failure_signature_mismatch_is_cleared_without_touching_success(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    output = at.session_state["ai_output"]
    receipt = at.session_state["ai_receipt"]
    at.session_state["ai_failure"] = app.AiGenerationFailure(
        signature="different",
        error_code=PROVIDER_TIMEOUT,
        execution_result=None,
    )

    at.run()

    assert at.session_state["ai_failure"] is None
    assert at.session_state["ai_output"] is output
    assert at.session_state["ai_receipt"] is receipt
    assert ai_harness.execution_calls == 1


def test_failure_code_must_match_failed_execution_result(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    failure = state_failure(at)
    object.__setattr__(failure, "error_code", PROVIDER_ACCOUNT_ERROR)

    at.run()

    assert at.session_state["ai_failure"] is None
    assert list(at.exception) == []


def test_post_execution_failure_code_allowlist_is_closed_and_exact() -> None:
    assert app._POST_EXECUTION_AI_FAILURE_CODES == {
        INVALID_RECEIPT_V4_INPUT,
        INVALID_LOGICAL_GENERATION_COST,
        "UNEXPECTED_AI_ERROR",
    }


@pytest.mark.parametrize(
    "error_code",
    [
        INVALID_RECEIPT_V4_INPUT,
        INVALID_LOGICAL_GENERATION_COST,
        "UNEXPECTED_AI_ERROR",
    ],
)
def test_succeeded_execution_accepts_only_real_post_execution_failure_codes(
    error_code: str,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    result = ai_harness.completed_results[-1]  # type: ignore[index]
    at.session_state["ai_failure"] = app.AiGenerationFailure(
        signature=at.session_state["ai_signature"],
        error_code=error_code,
        execution_result=result,
    )

    at.run()

    failure = state_failure(at)
    assert failure.error_code == error_code
    assert failure.execution_result is result
    assert "Provider execution completed successfully" in " ".join(
        rendered_text(at)
    )
    assert ai_harness.execution_calls == 1


@pytest.mark.parametrize(
    "error_code",
    [
        PROVIDER_TIMEOUT,
        PROVIDER_CONNECTION_FAILED,
        PROVIDER_RATE_LIMITED,
        PROVIDER_UNAVAILABLE,
        PROVIDER_AUTH_FAILED,
        PROVIDER_ACCOUNT_ERROR,
        PROVIDER_REQUEST_REJECTED,
        PROVIDER_CONFIGURATION_ERROR,
        INVALID_PROVIDER,
        INVALID_PROVIDER_RESPONSE,
        INVALID_PROVIDER_USAGE,
        INVALID_PROVIDER_JSON,
        PROVIDER_FAILURE,
        INVALID_RETRY_EXECUTION,
    ],
)
def test_succeeded_execution_rejects_provider_or_execution_failure_codes(
    error_code: str,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    success_snapshot = (
        at.session_state["ai_output"],
        at.session_state["ai_receipt"],
        at.session_state["ai_signature"],
        at.session_state["ai_success_binding"],
    )
    result = ai_harness.completed_results[-1]  # type: ignore[index]
    at.session_state["ai_failure"] = app.AiGenerationFailure(
        signature=at.session_state["ai_signature"],
        error_code=error_code,
        execution_result=result,
    )

    at.run()

    assert at.session_state["ai_failure"] is None
    assert (
        at.session_state["ai_output"],
        at.session_state["ai_receipt"],
        at.session_state["ai_signature"],
        at.session_state["ai_success_binding"],
    ) == success_snapshot
    assert ai_harness.execution_calls == 1


def test_failure_with_invalid_execution_status_is_cleared(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    result = ai_harness.completed_results[-1]  # type: ignore[index]
    object.__setattr__(result, "status", "invalid")
    at.session_state["ai_failure"] = app.AiGenerationFailure(
        signature=at.session_state["ai_signature"],
        error_code="UNEXPECTED_AI_ERROR",
        execution_result=result,
    )

    at.run()

    assert at.session_state["ai_failure"] is None
    assert list(at.exception) == []


def test_one_explicit_operation_invokes_retry_execution_exactly_once(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(insight_output()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 2
    assert ai_harness.receipt_calls == 1


def test_provider_call_count_equals_attempt_count_for_every_completed_operation(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(insight_output()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.completed_results is not None
    audited_attempts = sum(
        len(result.attempt_audit.attempts)
        for result in ai_harness.completed_results
    )
    assert ai_harness.provider_calls == audited_attempts == 3
    assert ai_harness.execution_calls == 2


def test_completed_results_have_n_minus_one_delay_records(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(insight_output()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    click_named_button(at, "Generate AI Insights")

    assert ai_harness.completed_results is not None
    for result in ai_harness.completed_results:
        assert len(result.delay_audit.records) == len(
            result.attempt_audit.attempts
        ) - 1


def test_app_does_not_expose_old_direct_generation_symbols() -> None:
    assert not hasattr(app, "generate_insight_with_metadata")
    assert not hasattr(app, "build_cost_audit_metadata")


def test_receipt_json_rejects_legacy_v3(ai_harness: AiHarness) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    legacy = make_legacy_v3(at.session_state["ai_receipt"])

    with pytest.raises(TypeError, match="InsightGenerationReceiptV4"):
        app.build_receipt_json_bytes(legacy)  # type: ignore[arg-type]


def test_same_signature_failure_rerender_preserves_failure_without_new_call(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    failure = at.session_state["ai_failure"]

    at.run()

    assert at.session_state["ai_failure"] is failure
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1


def test_failure_does_not_attach_audit_to_prior_receipt(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    original_audit = receipt.attempt_audit
    ai_harness.set_steps(ProviderStep(terminal_error()))

    at = click_named_button(at, "Regenerate AI Insights")

    failure_result = state_failure(at).execution_result
    assert failure_result is not None
    assert at.session_state["ai_receipt"] is receipt
    assert receipt.attempt_audit is original_audit
    assert receipt.attempt_audit is not failure_result.attempt_audit


def test_receipt_builder_receives_sealed_execution_facts_without_app_overrides(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    result = ai_harness.completed_results[0]  # type: ignore[index]

    assert receipt.provider == result.attempt_audit.attempts[-1].provider
    assert receipt.model == result.attempt_audit.attempts[-1].model
    assert receipt.usage == result.final_usage
    assert receipt.cost == result.final_cost
    assert receipt.attempt_audit is result.attempt_audit
    assert receipt.delay_audit is result.delay_audit


def test_failure_state_has_no_duplicate_message_or_audit_keys(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    state = at.session_state.filtered_state

    assert type(state["ai_failure"]).__name__ == "AiGenerationFailure"
    assert "ai_error_message" not in state
    assert "ai_failure_attempt_audit" not in state
    assert "ai_failure_delay_audit" not in state
    assert "ai_failure_usage" not in state
    assert "ai_failure_cost" not in state


def test_provider_error_details_never_enter_failure_object(
    ai_harness: AiHarness,
) -> None:
    secret = "SECRET_HTTP_BODY_AND_KEY"
    ai_harness.set_steps(ProviderStep(terminal_error(detail=secret)))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    failure = state_failure(at)

    assert secret not in failure.error_code
    assert secret not in repr(failure.execution_result)
    assert secret not in " ".join(rendered_text(at))


def test_retry_execution_result_is_not_persisted_on_success(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")

    state = at.session_state.filtered_state
    assert state["ai_failure"] is None
    assert not any(
        isinstance(value, RetryExecutionResult)
        for value in state.values()
        if value is not None
    )


def test_receipt_generated_at_is_not_attempt_pricing_reference(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]

    assert receipt.generated_at not in {
        attempt.pricing_reference_at for attempt in receipt.attempt_audit.attempts
    }


def test_receipt_group_by_matches_current_analysis_scope(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(
        SAMPLE_PATH.name,
        SAMPLE_PATH.read_bytes(),
        group_label="Marketplace + Country",
    )
    at = click_named_button(at, "Generate AI Insights")

    assert at.session_state["ai_receipt"].group_by == (
        "marketplace",
        "country",
    )


def test_overall_receipt_uses_empty_group_tuple(ai_harness: AiHarness) -> None:
    at = upload_and_run(
        SAMPLE_PATH.name,
        SAMPLE_PATH.read_bytes(),
        group_label="Overall",
    )
    at = click_named_button(at, "Generate AI Insights")

    assert at.session_state["ai_receipt"].group_by == ()


def test_failed_logical_generation_never_calls_receipt_builder(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(terminal_error()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    click_named_button(at, "Generate AI Insights")

    assert ai_harness.receipt_calls == 0
    assert ai_harness.execution_calls == 1
    assert ai_harness.provider_calls == 1


def test_retry_execution_error_never_calls_receipt_builder(
    ai_harness: AiHarness,
) -> None:
    ai_harness.execution_error = RetryExecutionError(
        INVALID_RETRY_EXECUTION,
        "hidden",
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    click_named_button(at, "Generate AI Insights")

    assert ai_harness.receipt_calls == 0
    assert ai_harness.provider_calls == 0


def test_receipt_failure_keeps_completed_attempt_and_delay_provenance(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(insight_output()))
    ai_harness.receipt_error = InsightReceiptV4Error(
        INVALID_RECEIPT_V4_INPUT,
        "hidden",
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    result = state_failure(at).execution_result
    assert result is not None and result.status == SUCCEEDED
    assert len(result.attempt_audit.attempts) == 2
    assert len(result.delay_audit.records) == 1
    assert result.final_usage == DEFAULT_USAGE
    assert result.final_cost is not None
    assert at.session_state["ai_receipt"] is None


def test_regenerate_failure_keeps_receipt_download_for_prior_success(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    prior_receipt = at.session_state["ai_receipt"]
    ai_harness.set_steps(ProviderStep(terminal_error()))

    at = click_named_button(at, "Regenerate AI Insights")

    assert at.session_state["ai_receipt"] is prior_receipt
    assert "Download AI Receipt" in [b.label for b in at.download_button]
    payload = json.loads(app.build_receipt_json_bytes(prior_receipt))
    assert payload["analysis_signature"] == prior_receipt.analysis_signature


def test_failure_ui_uses_code_derived_message_not_persisted_message(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_steps(
        ProviderStep(terminal_error(PROVIDER_RATE_LIMITED)),
        ProviderStep(terminal_error(PROVIDER_RATE_LIMITED)),
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")

    assert state_failure(at).error_code == PROVIDER_RATE_LIMITED
    assert "temporarily rate limited" in " ".join(rendered_text(at))
    assert "ai_error_message" not in at.session_state.filtered_state


def test_success_pair_validation_preserves_valid_v4_on_passive_render(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    snapshot = (
        at.session_state["ai_output"],
        at.session_state["ai_receipt"],
        at.session_state["ai_signature"],
        at.session_state["ai_success_binding"],
    )

    at.run()

    assert (
        at.session_state["ai_output"],
        at.session_state["ai_receipt"],
        at.session_state["ai_signature"],
        at.session_state["ai_success_binding"],
    ) == snapshot


def test_failure_is_frozen_app_local_value_object() -> None:
    failure = app.AiGenerationFailure(
        signature="a" * 64,
        error_code=INVALID_RETRY_EXECUTION,
        execution_result=None,
    )

    with pytest.raises(AttributeError):
        failure.error_code = "CHANGED"  # type: ignore[misc]


def test_receipt_filename_uses_analysis_identity_only(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    expected = app.build_receipt_download_filename(receipt)
    object.__setattr__(receipt, "model", "different-model")

    assert app.build_receipt_download_filename(receipt) == expected


def test_no_real_sleep_is_used_by_test_harness(ai_harness: AiHarness) -> None:
    ai_harness.set_steps(ProviderStep(timeout_error()), ProviderStep(insight_output()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    click_named_button(at, "Generate AI Insights")

    assert ai_harness.sleep_calls == [1000]
    assert ai_harness.events is not None
    assert "sleep:1000" in ai_harness.events
