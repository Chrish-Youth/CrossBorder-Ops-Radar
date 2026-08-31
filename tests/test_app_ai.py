from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

import app
import src.deepseek_provider as deepseek_module
import src.insight_cost_audit as cost_audit_module
import src.insight_receipt as receipt_module
import src.insight_provider as provider_module
from src.config import REQUIRED_COLUMNS
from src.insight_prompt import InsightOutput, PriorityInsight
from src.insight_cost_audit import (
    AVAILABLE,
    UNAVAILABLE,
    CostAuditMetadata,
    build_cost_audit_metadata,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    DEEPSEEK_FLASH_PRICING_POLICY,
    GenerationCostEstimate,
    INVALID_PRICING_INPUT,
    OFF_PEAK,
    PEAK,
    POLICY_NOT_APPLICABLE,
    USAGE_UNAVAILABLE,
    PricingError,
    PricingPolicy,
)
from src.insight_pricing_catalog import (
    DEFAULT_PRICING_POLICY_CATALOG,
    INVALID_PRICING_CATALOG,
    UNSELECTED_PRICING_POLICY_VERSION,
    PricingPolicyCatalog,
)
from src.insight_provider import (
    INVALID_PROVIDER,
    INVALID_PROVIDER_JSON,
    INVALID_PROVIDER_RESPONSE,
    INVALID_PROVIDER_USAGE,
    PROVIDER_ACCOUNT_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_FAILURE,
    PROVIDER_RATE_LIMITED,
    PROVIDER_REQUEST_REJECTED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    InsightGenerationResult,
    InsightProviderError,
    ProviderUsage,
)
from src.insight_receipt import (
    DEEPSEEK_PROVIDER_NAME,
    INSIGHT_RECEIPT_VERSION,
    INVALID_RECEIPT_INPUT,
    MAX_RECEIPT_TOKEN_DECIMAL_DIGITS,
    InsightGenerationReceipt,
    InsightReceiptError,
)
from src.pipeline import PipelineStatus, run_pipeline


PROJECT_ROOT = Path(__file__).parents[1]
APP_PATH = PROJECT_ROOT / "app.py"
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample_ecommerce_data.csv"
FIXED_PRICING_REFERENCE = datetime(
    2026,
    8,
    17,
    1,
    0,
    tzinfo=timezone.utc,
)
POLICY_B_EFFECTIVE = datetime(2026, 9, 15, tzinfo=timezone.utc)


def synthetic_policy_b() -> PricingPolicy:
    return replace(
        DEEPSEEK_FLASH_PRICING_POLICY,
        version="test-deepseek-v4-flash-2026-09-15-v1",
        effective_from_utc=POLICY_B_EFFECTIVE,
        verified_at_utc=POLICY_B_EFFECTIVE + timedelta(days=1),
        off_peak_rates=replace(
            DEEPSEEK_FLASH_PRICING_POLICY.off_peak_rates,
            prompt_cache_miss_usd_per_million=Decimal("0.30"),
            completion_usd_per_million=Decimal("0.90"),
        ),
    )


def cost_for(
    usage: ProviderUsage | None,
    *,
    reference_at: datetime = FIXED_PRICING_REFERENCE,
) -> CostAuditMetadata:
    return build_cost_audit_metadata(
        usage,
        provider=DEEPSEEK_PROVIDER_NAME,
        model=deepseek_module.DEEPSEEK_MODEL,
        pricing_reference_at=reference_at,
    )


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
    usage: ProviderUsage | None
    constructor_calls: int = 0
    generate_calls: int = 0
    cost_calls: int = 0
    last_context: object | None = None
    last_provider: object | None = None
    pricing_references: list[datetime] | None = None

    def set_outcome(self, outcome: object) -> None:
        self.outcome = outcome


@pytest.fixture
def ai_harness(monkeypatch: pytest.MonkeyPatch) -> AiHarness:
    harness = AiHarness(
        insight_output(),
        ProviderUsage(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=100,
            reasoning_tokens=0,
        ),
    )
    harness.pricing_references = []
    real_cost_builder = cost_audit_module.build_cost_audit_metadata

    class FakeDeepSeekProvider:
        def __init__(self) -> None:
            harness.constructor_calls += 1

    def fake_generate_insight_with_metadata(
        context: object,
        *,
        provider: object,
    ) -> InsightGenerationResult:
        harness.generate_calls += 1
        harness.last_context = context
        harness.last_provider = provider
        if isinstance(harness.outcome, BaseException):
            raise harness.outcome
        return InsightGenerationResult(
            output=harness.outcome,  # type: ignore[arg-type]
            usage=harness.usage,
        )

    def tracking_cost_builder(
        usage: ProviderUsage | None,
        **kwargs: object,
    ) -> CostAuditMetadata:
        harness.cost_calls += 1
        reference_at = kwargs["pricing_reference_at"]
        assert isinstance(reference_at, datetime)
        assert harness.pricing_references is not None
        harness.pricing_references.append(reference_at)
        return real_cost_builder(usage, **kwargs)  # type: ignore[arg-type]

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        deepseek_module,
        "DeepSeekInsightProvider",
        FakeDeepSeekProvider,
    )
    monkeypatch.setattr(
        provider_module,
        "generate_insight_with_metadata",
        fake_generate_insight_with_metadata,
    )
    monkeypatch.setattr(
        cost_audit_module,
        "build_cost_audit_metadata",
        tracking_cost_builder,
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
    assert ai_harness.cost_calls == 1
    assert at.session_state["ai_output"] == expected
    receipt = at.session_state["ai_receipt"]
    context = ai_harness.last_context
    assert isinstance(receipt, InsightGenerationReceipt)
    assert receipt.analysis_signature == at.session_state["analysis_signature"]
    assert receipt.group_by == ("sku",)
    assert receipt.metric_record_count == len(context.metric_records)
    assert receipt.diagnostic_signal_count == len(context.diagnostic_signals)
    assert receipt.priority_insight_count == len(expected.priority_insights)
    assert receipt.version == INSIGHT_RECEIPT_VERSION == "3"
    assert receipt.usage == ai_harness.usage
    assert receipt.cost.status == AVAILABLE
    assert receipt.cost.estimate is not None
    assert "ai_usage" not in at.session_state.filtered_state
    assert at.session_state["ai_error_code"] is None
    assert at.session_state["ai_error_message"] is None
    assert at.session_state["ai_signature"] == app.build_ai_signature(
        at.session_state["analysis_signature"]
    )
    assert "Regenerate AI Insights" in [button.label for button in at.button]


def test_request_start_timestamp_is_captured_once_before_provider_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_at = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
    usage = ProviderUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        prompt_cache_hit_tokens=40,
        prompt_cache_miss_tokens=60,
    )
    events: list[str] = []
    real_cost_builder = app.build_cost_audit_metadata

    class FakeProvider:
        pass

    def fake_clock() -> datetime:
        events.append("clock")
        return reference_at

    def fake_generate(
        _context: object,
        *,
        provider: object,
    ) -> InsightGenerationResult:
        assert isinstance(provider, FakeProvider)
        events.append("provider")
        return InsightGenerationResult(output=insight_output(), usage=usage)

    def recording_cost_builder(
        provider_usage: ProviderUsage | None,
        **kwargs: object,
    ) -> CostAuditMetadata:
        events.append("cost")
        assert kwargs["pricing_reference_at"] is reference_at
        return real_cost_builder(provider_usage, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app, "_utc_now", fake_clock)
    monkeypatch.setattr(app, "DeepSeekInsightProvider", FakeProvider)
    monkeypatch.setattr(app, "generate_insight_with_metadata", fake_generate)
    monkeypatch.setattr(app, "build_cost_audit_metadata", recording_cost_builder)
    result = run_pipeline(SAMPLE_PATH, group_by="sku")

    artifacts = app._generate_ai_artifacts(
        result,
        analysis_signature="a" * 64,
        group_by=["sku"],
    )

    assert events == ["clock", "provider", "cost"]
    assert artifacts.receipt.cost.pricing_reference_at == (
        "2026-08-17T01:00:00+00:00"
    )
    assert artifacts.receipt.cost.estimate is not None
    assert artifacts.receipt.cost.estimate.pricing_tier == PEAK


def test_app_generation_uses_default_catalog_without_explicit_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_b = synthetic_policy_b()
    catalog = PricingPolicyCatalog(
        (policy_b, DEEPSEEK_FLASH_PRICING_POLICY)
    )
    usage = ProviderUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=100,
    )
    provider_calls = 0
    real_cost_builder = cost_audit_module.build_cost_audit_metadata

    class FakeProvider:
        pass

    def fake_generate(
        _context: object,
        *,
        provider: object,
    ) -> InsightGenerationResult:
        nonlocal provider_calls
        assert isinstance(provider, FakeProvider)
        provider_calls += 1
        return InsightGenerationResult(output=insight_output(), usage=usage)

    def recording_cost_builder(
        provider_usage: ProviderUsage | None,
        **kwargs: object,
    ) -> CostAuditMetadata:
        assert "policy" not in kwargs
        assert "catalog" not in kwargs
        return real_cost_builder(provider_usage, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cost_audit_module,
        "DEFAULT_PRICING_POLICY_CATALOG",
        catalog,
    )
    monkeypatch.setattr(app, "_utc_now", lambda: POLICY_B_EFFECTIVE)
    monkeypatch.setattr(app, "DeepSeekInsightProvider", FakeProvider)
    monkeypatch.setattr(app, "generate_insight_with_metadata", fake_generate)
    monkeypatch.setattr(app, "build_cost_audit_metadata", recording_cost_builder)
    result = run_pipeline(SAMPLE_PATH, group_by="sku")

    artifacts = app._generate_ai_artifacts(
        result,
        analysis_signature="a" * 64,
        group_by=["sku"],
    )

    assert provider_calls == 1
    assert artifacts.receipt.cost.status == AVAILABLE
    assert artifacts.receipt.cost.pricing_policy_version == policy_b.version
    assert artifacts.receipt.cost.estimate is not None
    assert artifacts.receipt.cost.estimate.total_estimated_cost == Decimal(
        "0.000048"
    )


def test_generation_without_usage_succeeds_with_neutral_empty_state(
    ai_harness: AiHarness,
) -> None:
    ai_harness.usage = None
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1
    assert at.session_state["ai_output"] == ai_harness.outcome
    receipt = at.session_state["ai_receipt"]
    assert receipt.version == "3"
    assert receipt.usage is None
    assert receipt.cost.status == UNAVAILABLE
    assert receipt.cost.unavailable_reason == USAGE_UNAVAILABLE
    assert receipt.to_dict()["usage"] is None
    assert any(
        "Token usage unavailable for this generation." in caption.value
        for caption in at.caption
    )
    assert all("Token usage" not in error.value for error in at.error)
    assert all("Token usage" not in warning.value for warning in at.warning)
    assert "ai_usage" not in at.session_state.filtered_state


def test_missing_cache_breakdown_succeeds_with_neutral_cost_unavailable_ui(
    ai_harness: AiHarness,
) -> None:
    ai_harness.usage = ProviderUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    receipt = at.session_state["ai_receipt"]
    assert receipt.cost.status == UNAVAILABLE
    assert receipt.cost.unavailable_reason == CACHE_BREAKDOWN_UNAVAILABLE
    text = " ".join(rendered_text(at))
    assert "Estimated API cost unavailable for this generation." in text
    assert "Cache hit/miss breakdown unavailable." in text
    assert all("cost unavailable" not in error.value.lower() for error in at.error)
    assert all("cost unavailable" not in warning.value.lower() for warning in at.warning)


def test_512_digit_usage_builds_renders_and_downloads_safely(
    ai_harness: AiHarness,
) -> None:
    accepted = 10**MAX_RECEIPT_TOKEN_DECIMAL_DIGITS - 1
    ai_harness.usage = ProviderUsage(
        prompt_tokens=accepted,
        completion_tokens=0,
        total_tokens=accepted,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_receipt"].usage == ai_harness.usage
    assert f"Prompt tokens: {accepted:,}" in " ".join(rendered_text(at))
    payload = json.loads(
        app.build_receipt_json_bytes(at.session_state["ai_receipt"])
    )
    assert payload["usage"]["prompt_tokens"] == accepted
    assert "Download AI Receipt" in [
        button.label for button in at.download_button
    ]


def test_same_signature_rerender_and_download_render_do_not_generate_again(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    expected_receipt = at.session_state["ai_receipt"]
    assert len(at.download_button) == 2

    at.run()

    assert ai_harness.constructor_calls == 1
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1
    assert at.session_state["ai_output"] is not None
    assert at.session_state["ai_receipt"] is not None
    assert at.session_state["ai_receipt"] == expected_receipt


def test_passive_rerender_and_receipt_download_never_recalculate_cost(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    selector_calls = 0
    real_selector = cost_audit_module.select_pricing_policy

    def tracking_selector(**kwargs: object) -> PricingPolicy:
        nonlocal selector_calls
        selector_calls += 1
        return real_selector(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cost_audit_module,
        "select_pricing_policy",
        tracking_selector,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    reference_at = receipt.cost.pricing_reference_at
    cost_calls = ai_harness.cost_calls

    app.build_receipt_json_bytes(receipt)
    at.run()

    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == cost_calls == 1
    assert selector_calls == 1
    assert at.session_state["ai_receipt"] is receipt
    assert at.session_state["ai_receipt"].cost.pricing_reference_at == reference_at


def test_explicit_regenerate_is_one_new_request_and_replaces_output(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    timestamps = iter(
        (
            datetime(2026, 8, 28, 6, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 6, 31, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr(receipt_module, "_utc_now", lambda: next(timestamps))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    first_receipt = at.session_state["ai_receipt"]
    replacement = insight_output(summary="Replacement summary.")
    ai_harness.set_outcome(replacement)

    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.constructor_calls == 2
    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == replacement
    assert at.session_state["ai_receipt"] != first_receipt
    assert at.session_state["ai_receipt"].generated_at == (
        "2026-08-28T06:31:00+00:00"
    )
    assert at.session_state["ai_error_message"] is None


def test_regenerate_atomically_replaces_output_receipt_and_usage(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    first_output = at.session_state["ai_output"]
    first_receipt = at.session_state["ai_receipt"]
    assert first_receipt.usage.prompt_tokens == 100

    replacement = insight_output(summary="Generation B.")
    replacement_usage = ProviderUsage(
        prompt_tokens=200,
        completion_tokens=30,
        total_tokens=230,
        prompt_cache_hit_tokens=50,
        prompt_cache_miss_tokens=150,
        reasoning_tokens=10,
    )
    ai_harness.set_outcome(replacement)
    ai_harness.usage = replacement_usage

    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == replacement
    assert at.session_state["ai_output"] != first_output
    assert at.session_state["ai_receipt"] != first_receipt
    assert at.session_state["ai_receipt"].usage == replacement_usage
    text = " ".join(rendered_text(at))
    assert "Prompt tokens: 200" in text
    assert "Prompt tokens: 100" not in text


def test_regenerate_peak_to_off_peak_atomically_replaces_historical_cost(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    references = iter(
        (
            datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc),
        )
    )
    real_cost_builder = build_cost_audit_metadata

    def sequenced_cost_builder(
        usage: ProviderUsage | None,
        **kwargs: object,
    ) -> CostAuditMetadata:
        ai_harness.cost_calls += 1
        kwargs["pricing_reference_at"] = next(references)
        return real_cost_builder(usage, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cost_audit_module,
        "build_cost_audit_metadata",
        sequenced_cost_builder,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    first_receipt = at.session_state["ai_receipt"]
    assert first_receipt.cost.estimate.pricing_tier == PEAK
    ai_harness.set_outcome(insight_output(summary="Off-peak replacement."))
    ai_harness.usage = ProviderUsage(
        prompt_tokens=200,
        completion_tokens=40,
        total_tokens=240,
        prompt_cache_hit_tokens=100,
        prompt_cache_miss_tokens=100,
    )

    at = click_named_button(at, "Regenerate AI Insights")

    second_receipt = at.session_state["ai_receipt"]
    assert ai_harness.generate_calls == 2
    assert ai_harness.cost_calls == 2
    assert second_receipt is not first_receipt
    assert second_receipt.cost.estimate.pricing_tier == OFF_PEAK
    assert second_receipt.cost.pricing_reference_at == (
        "2026-08-17T04:00:00+00:00"
    )
    assert second_receipt.cost.to_dict() != first_receipt.cost.to_dict()
    assert "Off-peak replacement." in " ".join(rendered_text(at))


def test_new_catalog_preserves_old_receipt_until_explicit_regenerate(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    policy_b = synthetic_policy_b()
    catalog_with_b = PricingPolicyCatalog(
        (policy_b, DEEPSEEK_FLASH_PRICING_POLICY)
    )
    references = iter((FIXED_PRICING_REFERENCE, POLICY_B_EFFECTIVE))
    tracked_cost_builder = cost_audit_module.build_cost_audit_metadata
    tracked_selector = cost_audit_module.select_pricing_policy
    selector_calls = 0

    def counting_selector(**kwargs: object) -> PricingPolicy:
        nonlocal selector_calls
        selector_calls += 1
        return tracked_selector(**kwargs)  # type: ignore[arg-type]

    def sequenced_cost_builder(
        usage: ProviderUsage | None,
        **kwargs: object,
    ) -> CostAuditMetadata:
        kwargs["pricing_reference_at"] = next(references)
        return tracked_cost_builder(usage, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cost_audit_module,
        "build_cost_audit_metadata",
        sequenced_cost_builder,
    )
    monkeypatch.setattr(
        cost_audit_module,
        "select_pricing_policy",
        counting_selector,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt_a = at.session_state["ai_receipt"]
    cost_a = receipt_a.cost
    assert cost_a.pricing_policy_version == (
        DEEPSEEK_FLASH_PRICING_POLICY.version
    )
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1
    assert selector_calls == 1

    monkeypatch.setattr(
        cost_audit_module,
        "DEFAULT_PRICING_POLICY_CATALOG",
        catalog_with_b,
    )
    at.run()
    downloaded_a = json.loads(app.build_receipt_json_bytes(receipt_a))

    assert at.session_state["ai_receipt"] is receipt_a
    assert at.session_state["ai_receipt"].cost is cost_a
    assert downloaded_a["cost"]["pricing_policy_version"] == (
        DEEPSEEK_FLASH_PRICING_POLICY.version
    )
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1
    assert selector_calls == 1

    at = click_named_button(at, "Regenerate AI Insights")
    receipt_b = at.session_state["ai_receipt"]

    assert ai_harness.generate_calls == 2
    assert ai_harness.cost_calls == 2
    assert selector_calls == 2
    assert receipt_b is not receipt_a
    assert receipt_b.cost is not cost_a
    assert receipt_b.cost.pricing_policy_version == policy_b.version
    assert receipt_b.cost.pricing_reference_at == (
        "2026-09-15T00:00:00+00:00"
    )


def test_new_catalog_does_not_backfill_historical_unavailable_cost(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    ai_harness.usage = None
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    assert receipt.cost.status == UNAVAILABLE
    assert receipt.cost.unavailable_reason == USAGE_UNAVAILABLE

    monkeypatch.setattr(
        cost_audit_module,
        "DEFAULT_PRICING_POLICY_CATALOG",
        PricingPolicyCatalog(
            (synthetic_policy_b(), DEEPSEEK_FLASH_PRICING_POLICY)
        ),
    )
    at.run()

    assert at.session_state["ai_receipt"] is receipt
    assert at.session_state["ai_receipt"].cost is receipt.cost
    assert at.session_state["ai_receipt"].cost.status == UNAVAILABLE
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1


def test_new_applicable_catalog_does_not_backfill_historical_no_policy_receipt(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    selector_calls = 0
    real_selector = cost_audit_module.select_pricing_policy

    def tracking_selector(**kwargs: object) -> PricingPolicy:
        nonlocal selector_calls
        selector_calls += 1
        return real_selector(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cost_audit_module,
        "select_pricing_policy",
        tracking_selector,
    )
    monkeypatch.setattr(
        cost_audit_module,
        "DEFAULT_PRICING_POLICY_CATALOG",
        PricingPolicyCatalog(()),
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]

    assert receipt.cost.status == UNAVAILABLE
    assert receipt.cost.unavailable_reason == POLICY_NOT_APPLICABLE
    assert (
        receipt.cost.pricing_policy_version
        == UNSELECTED_PRICING_POLICY_VERSION
    )
    assert selector_calls == 1

    monkeypatch.setattr(
        cost_audit_module,
        "DEFAULT_PRICING_POLICY_CATALOG",
        DEFAULT_PRICING_POLICY_CATALOG,
    )
    downloaded = json.loads(app.build_receipt_json_bytes(receipt))
    at.run()

    assert at.session_state["ai_receipt"] is receipt
    assert downloaded["cost"]["unavailable_reason"] == POLICY_NOT_APPLICABLE
    assert downloaded["cost"]["pricing_policy_version"] == (
        UNSELECTED_PRICING_POLICY_VERSION
    )
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1
    assert selector_calls == 1


def test_regenerate_with_unavailable_cost_replaces_old_available_cost(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    first_receipt = at.session_state["ai_receipt"]
    assert first_receipt.cost.status == AVAILABLE
    ai_harness.set_outcome(insight_output(summary="No cache metadata B."))
    ai_harness.usage = ProviderUsage(
        prompt_tokens=200,
        completion_tokens=40,
        total_tokens=240,
    )

    at = click_named_button(at, "Regenerate AI Insights")

    second_receipt = at.session_state["ai_receipt"]
    assert ai_harness.generate_calls == 2
    assert second_receipt is not first_receipt
    assert second_receipt.usage == ai_harness.usage
    assert second_receipt.cost.status == UNAVAILABLE
    assert second_receipt.cost.estimate is None
    assert second_receipt.cost.unavailable_reason == (
        CACHE_BREAKDOWN_UNAVAILABLE
    )
    assert "No cache metadata B." in " ".join(rendered_text(at))
    assert "Estimated API cost unavailable for this generation." in (
        " ".join(rendered_text(at))
    )


def test_same_analysis_rerun_preserves_paid_output_without_ai_call(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    expected = at.session_state["ai_output"]
    expected_receipt = at.session_state["ai_receipt"]

    at.button[0].click().run(timeout=20)

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] == expected
    assert at.session_state["ai_receipt"] == expected_receipt
    assert "Regenerate AI Insights" in [button.label for button in at.button]


@pytest.mark.parametrize("change", ["filename", "bytes", "group"])
def test_upstream_change_immediately_invalidates_ai_state(
    change: str,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    assert at.session_state["ai_output"] is not None
    assert at.session_state["ai_receipt"] is not None

    if change == "filename":
        at.file_uploader[0].upload(
            "changed.csv",
            SAMPLE_PATH.read_bytes(),
            "text/csv",
        ).run()
    elif change == "bytes":
        at.file_uploader[0].upload(
            SAMPLE_PATH.name,
            csv_content(make_row(sku="SKU-CHANGED")),
            "text/csv",
        ).run()
    else:
        at.selectbox[0].select("Overall").run()

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
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
    assert isinstance(
        at.session_state["ai_receipt"], InsightGenerationReceipt
    )


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
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == PROVIDER_CONFIGURATION_ERROR
    assert "not configured" in at.session_state["ai_error_message"]
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS
    assert at.session_state["excel_bytes"] == excel_bytes
    assert len(at.download_button) == 1
    assert all(
        secret not in " ".join(rendered_text(at))
        for secret in ("SECRET_API_KEY", "SECRET_PROMPT", "SECRET_RESPONSE")
    )


def test_first_generation_invalid_usage_creates_no_partial_pair(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_outcome(
        InsightProviderError(
            INVALID_PROVIDER_USAGE,
            "SECRET_SDK_USAGE token arithmetic details",
        )
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == INVALID_PROVIDER_USAGE
    assert at.session_state["ai_error_message"] == (
        "AI service returned metadata that could not be safely accepted."
    )
    assert "SECRET_SDK_USAGE" not in " ".join(rendered_text(at))


def test_regenerate_failure_retains_previous_success_and_labels_it(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    ai_harness.set_outcome(
        InsightProviderError(PROVIDER_TIMEOUT, "SECRET_TIMEOUT_DETAIL")
    )

    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == previous
    assert at.session_state["ai_receipt"] == previous_receipt
    assert at.session_state["ai_error_code"] == PROVIDER_TIMEOUT
    assert any(
        "Showing the previous successful result" in warning.value
        for warning in at.warning
    )
    assert "Overall observation." in " ".join(rendered_text(at))
    assert "SECRET_TIMEOUT_DETAIL" not in " ".join(rendered_text(at))


def test_regenerate_invalid_usage_retains_previous_pair_and_safe_message(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    ai_harness.set_outcome(
        InsightProviderError(
            INVALID_PROVIDER_USAGE,
            "SECRET_SDK_USAGE token arithmetic details",
        )
    )

    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == previous_output
    assert at.session_state["ai_receipt"] == previous_receipt
    assert at.session_state["ai_error_code"] == INVALID_PROVIDER_USAGE
    assert at.session_state["ai_error_message"] == (
        "AI service returned metadata that could not be safely accepted."
    )
    assert "SECRET_SDK_USAGE" not in " ".join(rendered_text(at))


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
    assert isinstance(
        at.session_state["ai_receipt"], InsightGenerationReceipt
    )
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
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == "UNEXPECTED_AI_ERROR"
    assert "SECRET_INTERNAL_DETAIL" not in at.session_state["ai_error_message"]
    assert "SECRET_INTERNAL_DETAIL" not in " ".join(rendered_text(at))
    assert at.session_state["pipeline_result"].status is PipelineStatus.SUCCESS
    assert len(at.download_button) == 1


def test_first_cost_audit_hard_failure_creates_no_pair_and_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    def fail_cost(*_: object, **__: object) -> CostAuditMetadata:
        raise RuntimeError("SECRET_COST_INTERNAL")

    monkeypatch.setattr(
        cost_audit_module,
        "build_cost_audit_metadata",
        fail_cost,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    pipeline_result = at.session_state["pipeline_result"]
    excel_bytes = at.session_state["excel_bytes"]

    at = click_named_button(at, "Generate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == "UNEXPECTED_AI_ERROR"
    assert "SECRET_COST_INTERNAL" not in " ".join(rendered_text(at))
    assert all(
        "SECRET_COST_INTERNAL" not in value
        for value in at.session_state.filtered_state.values()
        if isinstance(value, str)
    )
    assert at.session_state["pipeline_result"] is pipeline_result
    assert at.session_state["excel_bytes"] == excel_bytes
    at.run()
    assert ai_harness.generate_calls == 1


def test_invalid_pricing_input_creates_no_pair_and_uses_safe_stable_error(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    def fail_cost(*_: object, **__: object) -> CostAuditMetadata:
        raise PricingError(
            INVALID_PRICING_INPUT,
            "SECRET_POLICY_USAGE_DECIMAL",
        )

    monkeypatch.setattr(
        cost_audit_module,
        "build_cost_audit_metadata",
        fail_cost,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == INVALID_PRICING_INPUT
    assert at.session_state["ai_error_message"] == (
        "AI cost details could not be safely prepared for this generation."
    )
    assert "SECRET_POLICY_USAGE_DECIMAL" not in " ".join(rendered_text(at))
    at.run()
    assert ai_harness.generate_calls == 1


def test_invalid_default_catalog_is_a_hard_atomic_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    monkeypatch.setattr(
        cost_audit_module,
        "DEFAULT_PRICING_POLICY_CATALOG",
        object(),
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    pipeline_result = at.session_state["pipeline_result"]
    excel_bytes = at.session_state["excel_bytes"]

    at = click_named_button(at, "Generate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == INVALID_PRICING_CATALOG
    assert at.session_state["ai_error_message"] == (
        "AI cost details could not be safely prepared for this generation."
    )
    assert at.session_state["pipeline_result"] is pipeline_result
    assert at.session_state["excel_bytes"] == excel_bytes
    at.run()
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1


def test_unexpected_selector_failure_is_sanitized_without_retry_or_partial_pair(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    def fail_selector(**_: object) -> PricingPolicy:
        raise RuntimeError("SECRET_CATALOG_INTERNAL")

    monkeypatch.setattr(
        cost_audit_module,
        "select_pricing_policy",
        fail_selector,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == "UNEXPECTED_AI_ERROR"
    assert "SECRET_CATALOG_INTERNAL" not in " ".join(rendered_text(at))
    assert all(
        "SECRET_CATALOG_INTERNAL" not in value
        for value in at.session_state.filtered_state.values()
        if isinstance(value, str)
    )
    at.run()
    assert ai_harness.generate_calls == 1
    assert ai_harness.cost_calls == 1


def test_invalid_catalog_regenerate_preserves_previous_atomic_pair(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    monkeypatch.setattr(
        cost_audit_module,
        "DEFAULT_PRICING_POLICY_CATALOG",
        object(),
    )

    at = click_named_button(at, "Regenerate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 2
    assert ai_harness.cost_calls == 2
    assert at.session_state["ai_output"] is previous_output
    assert at.session_state["ai_receipt"] is previous_receipt
    assert at.session_state["ai_error_code"] == INVALID_PRICING_CATALOG
    assert any(
        "Showing the previous successful result" in warning.value
        for warning in at.warning
    )
    at.run()
    assert ai_harness.generate_calls == 2
    assert ai_harness.cost_calls == 2


def test_regenerate_cost_audit_hard_failure_preserves_previous_atomic_pair(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    previous_cost = previous_receipt.cost
    ai_harness.set_outcome(insight_output(summary="Uncommitted output B."))

    def fail_cost(*_: object, **__: object) -> CostAuditMetadata:
        raise RuntimeError("SECRET_COST_REGENERATION")

    monkeypatch.setattr(
        cost_audit_module,
        "build_cost_audit_metadata",
        fail_cost,
    )

    at = click_named_button(at, "Regenerate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] is previous_output
    assert at.session_state["ai_receipt"] is previous_receipt
    assert at.session_state["ai_receipt"].cost is previous_cost
    assert at.session_state["ai_error_code"] == "UNEXPECTED_AI_ERROR"
    assert any(
        "Showing the previous successful result" in warning.value
        for warning in at.warning
    )
    assert "SECRET_COST_REGENERATION" not in " ".join(rendered_text(at))
    at.run()
    assert ai_harness.generate_calls == 2


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
        ("INVALID_COST_AUDIT", "cost details could not be safely prepared"),
        ("INVALID_PRICING_INPUT", "cost details could not be safely prepared"),
        ("INVALID_PRICING_CATALOG", "cost details could not be safely prepared"),
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
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    monkeypatch.setattr(
        receipt_module,
        "_utc_now",
        lambda: datetime(2026, 8, 28, 6, 30, 12, tzinfo=timezone.utc),
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    text = " ".join(rendered_text(at))
    receipt = at.session_state["ai_receipt"]

    assert "Validated AI summary." in text
    assert [expander.label for expander in at.expander] == [
        "Overall",
        "SKU: SKU-A",
        "Marketplace: Amazon · Country: US",
        "Generation Details",
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
    assert "Generated at 2026-08-28 06:30 UTC" in text
    assert "Provider:** DeepSeek" in text
    assert f"Model:** `{deepseek_module.DEEPSEEK_MODEL}`" in text
    assert "Analysis scope:** SKU" in text
    assert "Context v1 · Prompt v1 · Output v1 · Receipt v3" in text
    assert "Metric groups:" in text
    assert "Diagnostic signals:" in text
    assert "Priority insights: 3" in text
    assert "Token Usage" in text
    assert "Prompt tokens: 100" in text
    assert "Completion tokens: 20" in text
    assert "Total tokens: 120" in text
    assert "Prompt cache hit tokens: 0" in text
    assert "Prompt cache miss tokens: 100" in text
    assert "Reasoning tokens: 0" in text
    assert receipt.analysis_signature[:12] in text
    assert receipt.analysis_signature not in text


def test_generation_details_displays_exact_estimated_cost_and_disclaimer(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    estimate = receipt.cost.estimate
    assert estimate is not None
    text = " ".join(rendered_text(at))

    assert "Estimated Cost" in text
    assert (
        "Estimated total API cost (USD): "
        f"${format(estimate.total_estimated_cost, 'f')}"
    ) in text
    assert f"Pricing tier: {estimate.pricing_tier}" in text
    assert f"Pricing reference: {receipt.cost.pricing_reference_at}" in text
    assert f"Pricing policy: {receipt.cost.pricing_policy_version}" in text
    assert (
        f"Cache-hit input cost: ${format(estimate.prompt_cache_hit_cost, 'f')}"
    ) in text
    assert (
        f"Cache-miss input cost: ${format(estimate.prompt_cache_miss_cost, 'f')}"
    ) in text
    assert f"Completion cost: ${format(estimate.completion_cost, 'f')}" in text
    assert "not the provider's final billed amount" in text
    assert "Actual cost" not in text
    assert "Billed amount" not in text

    payload = json.loads(app.build_receipt_json_bytes(receipt))
    cost_payload = payload["cost"]
    estimate_payload = cost_payload["estimate"]
    assert all(
        isinstance(estimate_payload[field], str)
        for field in (
            "prompt_cache_hit_cost",
            "prompt_cache_miss_cost",
            "completion_cost",
            "total_estimated_cost",
        )
    )
    assert isinstance(payload["usage"]["prompt_tokens"], int)


def test_generation_details_provider_label_comes_from_receipt(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]

    assert receipt.provider == "deepseek"
    assert "Provider:** DeepSeek" in " ".join(rendered_text(at))


def test_generation_details_hides_absent_optional_usage_fields(
    ai_harness: AiHarness,
) -> None:
    ai_harness.usage = ProviderUsage(
        prompt_tokens=7,
        completion_tokens=3,
        total_tokens=10,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    text = " ".join(rendered_text(at))

    assert "Prompt tokens: 7" in text
    assert "Completion tokens: 3" in text
    assert "Total tokens: 10" in text
    assert "Prompt cache hit tokens" not in text
    assert "Prompt cache miss tokens" not in text
    assert "Reasoning tokens" not in text


def test_generation_details_runtime_error_is_sanitized_and_preserves_state(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    previous_signature = at.session_state["ai_signature"]
    pipeline_result = at.session_state["pipeline_result"]
    metrics = pipeline_result.metrics
    diagnostics = pipeline_result.diagnostics
    report_data = at.session_state["report_data"]
    excel_bytes = at.session_state["excel_bytes"]
    calls_before_rerun = ai_harness.generate_calls

    def fail_token_formatting(_usage: ProviderUsage) -> int:
        raise RuntimeError("SECRET_USAGE_DETAIL")

    monkeypatch.setattr(
        ProviderUsage,
        "prompt_tokens",
        property(fail_token_formatting),
        raising=False,
    )
    at.run()

    text = " ".join(rendered_text(at))
    assert list(at.exception) == []
    assert ai_harness.generate_calls == calls_before_rerun
    assert at.session_state["ai_output"] is previous_output
    assert at.session_state["ai_receipt"] is previous_receipt
    assert at.session_state["ai_signature"] == previous_signature
    assert at.session_state["ai_error_code"] is None
    assert at.session_state["ai_error_message"] is None
    assert at.session_state["pipeline_result"] is pipeline_result
    assert at.session_state["pipeline_result"].metrics is metrics
    assert at.session_state["pipeline_result"].diagnostics is diagnostics
    assert at.session_state["report_data"] is report_data
    assert at.session_state["excel_bytes"] == excel_bytes
    assert "Validated AI summary." in text
    assert "AI generation details are temporarily unavailable." in text
    assert "SECRET_USAGE_DETAIL" not in text
    assert all(
        "SECRET_USAGE_DETAIL" not in value
        for value in at.session_state.filtered_state.values()
        if isinstance(value, str)
    )
    assert "Download AI Receipt" not in [
        button.label for button in at.download_button
    ]


def test_receipt_serialization_runtime_error_is_sanitized_without_state_loss(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    previous_signature = at.session_state["ai_signature"]
    ai_harness.set_outcome(
        InsightProviderError(PROVIDER_TIMEOUT, "SECRET_PROVIDER_TIMEOUT")
    )
    at = click_named_button(at, "Regenerate AI Insights")
    previous_error_code = at.session_state["ai_error_code"]
    previous_error_message = at.session_state["ai_error_message"]
    pipeline_result = at.session_state["pipeline_result"]
    metrics = pipeline_result.metrics
    diagnostics = pipeline_result.diagnostics
    report_data = at.session_state["report_data"]
    excel_bytes = at.session_state["excel_bytes"]
    calls_before_rerun = ai_harness.generate_calls

    def fail_serialization(_receipt: InsightGenerationReceipt) -> dict[str, Any]:
        raise RuntimeError("SECRET_RECEIPT_JSON")

    monkeypatch.setattr(InsightGenerationReceipt, "to_dict", fail_serialization)
    at.run()

    text = " ".join(rendered_text(at))
    assert list(at.exception) == []
    assert ai_harness.generate_calls == calls_before_rerun
    assert at.session_state["ai_output"] is previous_output
    assert at.session_state["ai_receipt"] is previous_receipt
    assert at.session_state["ai_signature"] == previous_signature
    assert at.session_state["ai_error_code"] == previous_error_code
    assert at.session_state["ai_error_message"] == previous_error_message
    assert at.session_state["pipeline_result"] is pipeline_result
    assert at.session_state["pipeline_result"].metrics is metrics
    assert at.session_state["pipeline_result"].diagnostics is diagnostics
    assert at.session_state["report_data"] is report_data
    assert at.session_state["excel_bytes"] == excel_bytes
    assert "Validated AI summary." in text
    assert "AI generation details are temporarily unavailable." in text
    assert "SECRET_RECEIPT_JSON" not in text
    assert all(
        "SECRET_RECEIPT_JSON" not in value
        for value in at.session_state.filtered_state.values()
        if isinstance(value, str)
    )
    assert "Download AI Receipt" not in [
        button.label for button in at.download_button
    ]


def test_nested_cost_serialization_error_is_sanitized_without_state_loss(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    calls_before = ai_harness.generate_calls

    def fail_cost_serialization(_cost: CostAuditMetadata) -> dict[str, Any]:
        raise RuntimeError("SECRET_COST_SERIALIZATION")

    monkeypatch.setattr(CostAuditMetadata, "to_dict", fail_cost_serialization)
    at.run()

    text = " ".join(rendered_text(at))
    assert list(at.exception) == []
    assert ai_harness.generate_calls == calls_before
    assert at.session_state["ai_output"] is previous_output
    assert at.session_state["ai_receipt"] is previous_receipt
    assert "Validated AI summary." in text
    assert "AI generation details are temporarily unavailable." in text
    assert "SECRET_COST_SERIALIZATION" not in text
    assert "Download AI Receipt" not in [
        button.label for button in at.download_button
    ]


def test_cost_rendering_error_is_sanitized_and_ai_output_remains_visible(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    calls_before = ai_harness.generate_calls

    def fail_cost_rendering(_estimate: GenerationCostEstimate) -> object:
        raise RuntimeError("SECRET_COST_RENDERING")

    monkeypatch.setattr(
        GenerationCostEstimate,
        "total_estimated_cost",
        property(fail_cost_rendering),
        raising=False,
    )
    at.run()

    text = " ".join(rendered_text(at))
    assert list(at.exception) == []
    assert ai_harness.generate_calls == calls_before
    assert at.session_state["ai_output"] is previous_output
    assert at.session_state["ai_receipt"] is previous_receipt
    assert "Validated AI summary." in text
    assert "AI generation details are temporarily unavailable." in text
    assert "SECRET_COST_RENDERING" not in text
    assert "Download AI Receipt" not in [
        button.label for button in at.download_button
    ]


def test_generation_details_safety_boundary_does_not_swallow_baseexception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(_receipt: InsightGenerationReceipt) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "_render_generation_details", interrupt)

    with pytest.raises(KeyboardInterrupt):
        app._render_generation_details_safely(object())  # type: ignore[arg-type]


def test_generation_details_unknown_provider_uses_raw_receipt_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = InsightGenerationReceipt(
        version=INSIGHT_RECEIPT_VERSION,
        generated_at="2026-08-28T06:30:12+00:00",
        analysis_signature="abcdef0123456789",
        group_by=("sku",),
        context_version="1",
        prompt_version="1",
        output_version="1",
        provider=DEEPSEEK_PROVIDER_NAME,
        model=deepseek_module.DEEPSEEK_MODEL,
        metric_record_count=1,
        diagnostic_signal_count=1,
        priority_insight_count=1,
        cost=cost_for(None),
    )
    object.__setattr__(receipt, "provider", "future-provider")
    markdown_calls: list[str] = []

    class ExpanderContext:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(app.st, "expander", lambda *_args, **_kwargs: ExpanderContext())
    monkeypatch.setattr(app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app.st, "markdown", markdown_calls.append)
    monkeypatch.setattr(app.st, "download_button", lambda *_args, **_kwargs: None)

    app._render_generation_details(receipt)

    provider_line = next(line for line in markdown_calls if "**Provider:**" in line)
    assert "**Provider:** future-provider" in provider_line
    assert "**Provider:** DeepSeek" not in provider_line
    assert f"**Model:** `{receipt.model}`" in provider_line


def test_empty_priority_insights_render_as_a_valid_empty_state(
    ai_harness: AiHarness,
) -> None:
    ai_harness.set_outcome(insight_output(priorities=()))
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())

    at = click_named_button(at, "Generate AI Insights")

    assert at.session_state["ai_output"].priority_insights == ()
    assert at.session_state["ai_receipt"].priority_insight_count == 0
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
    assert "ai_usage" not in state_keys
    assert "ai_cost" not in state_keys
    assert "ai_cost_estimate" not in state_keys
    assert "ai_pricing" not in state_keys
    assert "ai_pricing_reference_at" not in state_keys
    assert {
        "ai_output",
        "ai_receipt",
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


def test_receipt_builder_failure_rejects_orphan_output_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    def fail_receipt(**_: object) -> InsightGenerationReceipt:
        raise InsightReceiptError(INVALID_RECEIPT_INPUT, "SECRET_RECEIPT_DETAIL")

    monkeypatch.setattr(
        receipt_module,
        "build_insight_generation_receipt",
        fail_receipt,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    excel_bytes = at.session_state["excel_bytes"]

    at = click_named_button(at, "Generate AI Insights")

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == INVALID_RECEIPT_INPUT
    assert "SECRET_RECEIPT_DETAIL" not in " ".join(rendered_text(at))
    assert at.session_state["excel_bytes"] == excel_bytes
    at.run()
    assert ai_harness.generate_calls == 1


def test_oversized_usage_is_rejected_before_first_session_commit(
    ai_harness: AiHarness,
) -> None:
    huge = 10**5000
    ai_harness.usage = ProviderUsage(
        prompt_tokens=huge,
        completion_tokens=0,
        total_tokens=huge,
    )
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    pipeline_result = at.session_state["pipeline_result"]
    excel_bytes = at.session_state["excel_bytes"]

    at = click_named_button(at, "Generate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] == INVALID_RECEIPT_INPUT
    assert at.session_state["ai_error_message"] == (
        "AI insights could not be saved with valid generation details."
    )
    assert at.session_state["pipeline_result"] is pipeline_result
    assert at.session_state["excel_bytes"] == excel_bytes
    at.run()
    assert ai_harness.generate_calls == 1


def test_oversized_usage_regeneration_preserves_previous_atomic_pair(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    previous_signature = at.session_state["ai_signature"]
    pipeline_result = at.session_state["pipeline_result"]
    excel_bytes = at.session_state["excel_bytes"]
    huge = 10**5000
    ai_harness.set_outcome(insight_output(summary="Uncommitted generation."))
    ai_harness.usage = ProviderUsage(
        prompt_tokens=huge,
        completion_tokens=0,
        total_tokens=huge,
    )

    at = click_named_button(at, "Regenerate AI Insights")

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == previous_output
    assert at.session_state["ai_receipt"] == previous_receipt
    assert at.session_state["ai_signature"] == previous_signature
    assert at.session_state["ai_error_code"] == INVALID_RECEIPT_INPUT
    assert at.session_state["pipeline_result"] is pipeline_result
    assert at.session_state["excel_bytes"] == excel_bytes
    assert any(
        "Showing the previous successful result" in warning.value
        for warning in at.warning
    )
    at.run()
    assert ai_harness.generate_calls == 2


def test_receipt_builder_failure_during_regeneration_preserves_pair(
    monkeypatch: pytest.MonkeyPatch,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    previous_output = at.session_state["ai_output"]
    previous_receipt = at.session_state["ai_receipt"]
    ai_harness.usage = ProviderUsage(
        prompt_tokens=200,
        completion_tokens=20,
        total_tokens=220,
    )

    def fail_receipt(**_: object) -> InsightGenerationReceipt:
        raise InsightReceiptError(INVALID_RECEIPT_INPUT, "internal detail")

    monkeypatch.setattr(
        receipt_module,
        "build_insight_generation_receipt",
        fail_receipt,
    )
    at = click_named_button(at, "Regenerate AI Insights")

    assert ai_harness.generate_calls == 2
    assert at.session_state["ai_output"] == previous_output
    assert at.session_state["ai_receipt"] == previous_receipt
    assert at.session_state["ai_receipt"].usage.prompt_tokens == 100
    assert at.session_state["ai_error_code"] == INVALID_RECEIPT_INPUT
    assert any(
        "Showing the previous successful result" in warning.value
        for warning in at.warning
    )


@pytest.mark.parametrize("missing", ["output", "receipt"])
def test_incomplete_legacy_ai_pair_is_cleared_without_fabricating_receipt(
    missing: str,
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    if missing == "output":
        at.session_state["ai_output"] = None
    else:
        at.session_state["ai_receipt"] = None

    at.run()

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_error_code"] is None
    assert at.session_state["ai_signature"] is None
    assert "Generate AI Insights" in [button.label for button in at.button]
    assert "Download AI Receipt" not in [
        button.label for button in at.download_button
    ]


def test_legacy_v2_receipt_is_cleared_without_in_place_upgrade(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    legacy_receipt = at.session_state["ai_receipt"]
    object.__setattr__(legacy_receipt, "version", "2")
    deterministic_result = at.session_state["pipeline_result"]
    excel_bytes = at.session_state["excel_bytes"]

    at.run()

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["pipeline_result"] is deterministic_result
    assert at.session_state["excel_bytes"] == excel_bytes
    assert "Generate AI Insights" in [button.label for button in at.button]


def test_receipt_json_download_contract_privacy_and_no_ai_side_effect(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    calls_before_render = ai_harness.generate_calls

    payload_bytes = app.build_receipt_json_bytes(receipt)
    payload = json.loads(payload_bytes)
    assert "Download AI Receipt" in [
        button.label for button in at.download_button
    ]
    assert set(payload) == {
        "version",
        "generated_at",
        "analysis_signature",
        "group_by",
        "context_version",
        "prompt_version",
        "output_version",
        "provider",
        "model",
        "metric_record_count",
        "diagnostic_signal_count",
        "priority_insight_count",
        "usage",
        "cost",
    }
    assert payload == receipt.to_dict()
    assert payload["analysis_signature"] == at.session_state["analysis_signature"]
    assert payload["provider"] == DEEPSEEK_PROVIDER_NAME
    assert payload["version"] == "3"
    assert payload["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 100,
        "reasoning_tokens": 0,
    }
    assert app.build_receipt_download_filename(receipt) == (
        f"crossborder_ops_ai_receipt_{receipt.analysis_signature[:12]}.json"
    )
    assert app.RECEIPT_DOWNLOAD_MIME == "application/json"
    assert all(
        secret not in payload_bytes
        for secret in (b"SECRET_API_KEY", b"SECRET_PROMPT", b"SECRET_RESPONSE")
    )

    at.run()
    assert ai_harness.generate_calls == calls_before_render


def test_receipt_download_filename_uses_only_safe_short_analysis_identity() -> None:
    receipt = InsightGenerationReceipt(
        version=INSIGHT_RECEIPT_VERSION,
        generated_at="2026-08-28T06:30:12+00:00",
        analysis_signature="../../恶意文件名:abcDEF0123456789",
        group_by=(),
        context_version="1",
        prompt_version="1",
        output_version="1",
        provider=DEEPSEEK_PROVIDER_NAME,
        model=deepseek_module.DEEPSEEK_MODEL,
        metric_record_count=0,
        diagnostic_signal_count=0,
        priority_insight_count=0,
        cost=cost_for(None),
    )

    assert app.build_receipt_download_filename(receipt) == (
        "crossborder_ops_ai_receipt_abcDEF012345.json"
    )


def test_group_change_then_return_does_not_restore_cached_ai_pair(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    original_receipt = at.session_state["ai_receipt"]

    at.selectbox[0].select("Overall").run()
    at.selectbox[0].select("SKU").run()

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert original_receipt is not None


def test_receipt_mismatched_with_current_output_is_cleared(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    at.session_state["ai_receipt"] = InsightGenerationReceipt(
        version=receipt.version,
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
        priority_insight_count=receipt.priority_insight_count + 1,
        usage=receipt.usage,
        cost=receipt.cost,
    )

    at.run()

    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None


def test_receipt_with_invalid_cost_type_is_cleared_without_provider_call(
    ai_harness: AiHarness,
) -> None:
    at = upload_and_run(SAMPLE_PATH.name, SAMPLE_PATH.read_bytes())
    at = click_named_button(at, "Generate AI Insights")
    receipt = at.session_state["ai_receipt"]
    deterministic_result = at.session_state["pipeline_result"]
    excel_bytes = at.session_state["excel_bytes"]
    object.__setattr__(receipt, "cost", object())

    at.run()

    assert list(at.exception) == []
    assert ai_harness.generate_calls == 1
    assert at.session_state["ai_output"] is None
    assert at.session_state["ai_receipt"] is None
    assert at.session_state["ai_signature"] is None
    assert at.session_state["pipeline_result"] is deterministic_result
    assert at.session_state["excel_bytes"] == excel_bytes
    assert "Generate AI Insights" in [button.label for button in at.button]
