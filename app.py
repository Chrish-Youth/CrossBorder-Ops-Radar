"""Streamlit application layer for CrossBorder Ops Radar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

import pandas as pd
import streamlit as st

from src.deepseek_provider import DEEPSEEK_MODEL, DeepSeekInsightProvider
from src.diagnostics import DiagnosticsError
from src.insight_cost_audit import (
    AVAILABLE,
    UNAVAILABLE,
    CostAuditError,
    CostAuditMetadata,
    build_cost_audit_metadata,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    INVALID_PRICING_INPUT,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    USAGE_UNAVAILABLE,
    PricingError,
)
from src.insight_pricing_catalog import (
    INVALID_PRICING_CATALOG,
    PricingCatalogError,
)
from src.insight_prompt import (
    INSIGHT_OUTPUT_VERSION,
    INSIGHT_PROMPT_VERSION,
    InsightOutput,
    InsightOutputError,
    InsightPromptError,
)
from src.insight_provider import (
    InsightGenerationResult,
    InsightProviderError,
    generate_insight_with_metadata,
)
from src.insight_receipt import (
    DEEPSEEK_PROVIDER_NAME,
    INSIGHT_RECEIPT_VERSION,
    InsightGenerationReceipt,
    InsightReceiptError,
    build_insight_generation_receipt,
)
from src.insights import (
    INSIGHT_CONTEXT_VERSION,
    InsightContextError,
    build_insight_context,
)
from src.loader import DataLoadError
from src.metrics import MetricsCalculationError
from src.pipeline import PipelineError, PipelineResult, PipelineStatus, run_pipeline
from src.report import ReportData, ReportError, build_report_data, generate_excel_report

logger = logging.getLogger(__name__)

APP_TITLE = "CrossBorder Ops Radar"
APP_SUBTITLE = (
    "Cross-border e-commerce operations data validation, metrics and "
    "diagnostic radar."
)
DEFAULT_GROUP_BY_LABEL = "SKU"
DOWNLOAD_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
DEFAULT_DOWNLOAD_FILENAME = "crossborder_ops_radar_report.xlsx"
DOWNLOAD_FILENAME_SUFFIX = "_crossborder_ops_radar.xlsx"
MAX_DOWNLOAD_FILENAME_BYTES = 180
RECEIPT_DOWNLOAD_MIME = "application/json"
RECEIPT_DOWNLOAD_PREFIX = "crossborder_ops_ai_receipt_"
RECEIPT_DOWNLOAD_SUFFIX = ".json"

_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "deepseek": "DeepSeek",
}

GROUP_BY_OPTIONS: dict[str, list[str] | None] = {
    "Overall": None,
    "SKU": ["sku"],
    "Marketplace": ["marketplace"],
    "Country": ["country"],
    "Marketplace + Country": ["marketplace", "country"],
    "Marketplace + Country + SKU": ["marketplace", "country", "sku"],
    "Date + Marketplace + Country + SKU": [
        "date",
        "marketplace",
        "country",
        "sku",
    ],
}

_ANALYSIS_SESSION_DEFAULTS: dict[str, Any] = {
    "analysis_signature": None,
    "pipeline_result": None,
    "report_data": None,
    "excel_bytes": None,
    "report_error": None,
    "analysis_error": None,
    "download_filename": None,
}

_AI_SESSION_DEFAULTS: dict[str, Any] = {
    "ai_output": None,
    "ai_receipt": None,
    "ai_error_code": None,
    "ai_error_message": None,
    "ai_signature": None,
}

_SESSION_DEFAULTS: dict[str, Any] = {
    **_ANALYSIS_SESSION_DEFAULTS,
    **_AI_SESSION_DEFAULTS,
}

_AI_RESULT_REJECTION_CODES: frozenset[str] = frozenset(
    {
        "INVALID_PROVIDER",
        "INVALID_PROVIDER_RESPONSE",
        "PROVIDER_RESPONSE_TOO_LARGE",
        "INVALID_PROVIDER_JSON",
        "INVALID_INSIGHT_OUTPUT",
        "OUTPUT_TOO_LARGE",
    }
)

_AI_ERROR_MESSAGES: dict[str, str] = {
    "PROVIDER_CONFIGURATION_ERROR": (
        "AI service is not configured. Set DEEPSEEK_API_KEY in the runtime "
        "environment, then try again."
    ),
    "PROVIDER_AUTH_FAILED": "AI service authentication failed.",
    "PROVIDER_ACCOUNT_ERROR": (
        "The AI service account cannot currently complete requests."
    ),
    "PROVIDER_TIMEOUT": "The AI service request timed out.",
    "PROVIDER_RATE_LIMITED": (
        "The AI service is temporarily rate limited. Try again later."
    ),
    "PROVIDER_CONNECTION_FAILED": "Could not connect to the AI service.",
    "PROVIDER_UNAVAILABLE": (
        "The AI service is temporarily unavailable. Try again later."
    ),
    "PROVIDER_REQUEST_REJECTED": "The AI service rejected the request.",
    "PROVIDER_FAILURE": "The AI service could not complete the request.",
    "INVALID_PROVIDER_USAGE": (
        "AI service returned metadata that could not be safely accepted."
    ),
    "INVALID_INSIGHT_INPUT": (
        "The current analysis could not be prepared for AI interpretation."
    ),
    "PIPELINE_NOT_ANALYZABLE": (
        "Run a successful deterministic analysis before generating AI insights."
    ),
    "INSIGHT_CONTEXT_TOO_LARGE": (
        "The current analysis is too large for AI interpretation."
    ),
    "NON_FINITE_INSIGHT_VALUE": (
        "The current analysis contains a value that cannot be safely interpreted."
    ),
    "INVALID_PROMPT_INPUT": (
        "The current analysis could not be prepared for AI interpretation."
    ),
    "PROMPT_TOO_LARGE": (
        "The current analysis is too large for AI interpretation."
    ),
    "INVALID_RECEIPT_INPUT": (
        "AI insights could not be saved with valid generation details."
    ),
    "INVALID_COST_AUDIT": (
        "AI cost details could not be safely prepared for this generation."
    ),
    INVALID_PRICING_INPUT: (
        "AI cost details could not be safely prepared for this generation."
    ),
    INVALID_PRICING_CATALOG: (
        "AI cost details could not be safely prepared for this generation."
    ),
}

_AI_GENERIC_REJECTION_MESSAGE = (
    "The AI service returned a result that could not be safely accepted."
)
_AI_UNEXPECTED_ERROR_MESSAGE = (
    "AI insights could not be generated because of an unexpected error."
)
_AI_AUDIT_PRESENTATION_ERROR_MESSAGE = (
    "AI generation details are temporarily unavailable."
)

_COST_UNAVAILABLE_MESSAGES: dict[str, str] = {
    USAGE_UNAVAILABLE: "Token usage unavailable.",
    CACHE_BREAKDOWN_UNAVAILABLE: (
        "Cache hit/miss breakdown unavailable."
    ),
    POLICY_NOT_EFFECTIVE: (
        "Pricing snapshot not applicable to this reference time."
    ),
    POLICY_NOT_APPLICABLE: (
        "Pricing snapshot not applicable to this provider/model."
    ),
}

_COST_ESTIMATE_DISCLAIMER = (
    "Estimated from recorded token usage and the stored pricing policy "
    "snapshot; this is not the provider's final billed amount."
)

_SCOPE_LABELS: dict[str, str] = {
    "date": "Date",
    "marketplace": "Marketplace",
    "country": "Country",
    "sku": "SKU",
}

_PERCENTAGE_COLUMNS: frozenset[str] = frozenset(
    {"ctr", "cvr", "refund_rate"}
)
_USD_COLUMNS: frozenset[str] = frozenset(
    {"sales", "ad_spend", "gmv", "aov", "cpc", "cpa"}
)
_COUNT_METRICS: frozenset[str] = frozenset(
    {"impressions", "clicks", "orders", "units_sold", "refunds", "inventory"}
)

_APP_STYLES = """
<style>
:root {
    --radar-primary: oklch(0.36 0.219 270);
    --radar-bg: oklch(0.985 0.005 270);
    --radar-surface: oklch(1 0 0);
    --radar-ink: oklch(0.22 0.03 270);
    --radar-muted: oklch(0.43 0.03 270);
}
[data-testid="stAppViewContainer"] {
    background: var(--radar-bg);
    color: var(--radar-ink);
}
[data-testid="stMainBlockContainer"] {
    max-width: 1180px;
    padding-top: 2.5rem;
    padding-bottom: 4rem;
}
h1, h2, h3 {
    color: var(--radar-ink);
    letter-spacing: -0.02em;
    text-wrap: balance;
}
p, [data-testid="stCaptionContainer"] {
    text-wrap: pretty;
}
div.stButton > button[kind="primary"],
a[data-testid="stBaseButton-primary"] {
    background: var(--radar-primary);
    color: white;
    border: 0;
}
div.stButton > button[kind="primary"]:focus-visible,
a[data-testid="stBaseButton-primary"]:focus-visible {
    outline: 3px solid oklch(0.78 0.10 270);
    outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        scroll-behavior: auto !important;
        transition-duration: 0.01ms !important;
        animation-duration: 0.01ms !important;
    }
}
</style>
"""


@dataclass(frozen=True)
class ErrorPresentation:
    """Safe, structured error content suitable for the application UI."""

    title: str
    code: str
    message: str
    stage: str | None = None


@dataclass(frozen=True)
class AnalysisArtifacts:
    """One completed Pipeline run and its independently generated report."""

    pipeline_result: PipelineResult
    report_data: ReportData | None
    excel_bytes: bytes | None
    report_error: ErrorPresentation | None


@dataclass(frozen=True)
class AiGenerationArtifacts:
    """One validated AI output and its matching immutable receipt."""

    output: InsightOutput
    receipt: InsightGenerationReceipt


def resolve_group_by(label: str) -> list[str] | None:
    """Resolve one fixed UI label to the Metrics API parameter."""

    if label not in GROUP_BY_OPTIONS:
        raise ValueError(f"Unknown analysis level: {label}")
    dimensions = GROUP_BY_OPTIONS[label]
    return None if dimensions is None else dimensions.copy()


def build_analysis_signature(
    file_bytes: bytes,
    filename: str,
    group_by: list[str] | None,
) -> str:
    """Build a deterministic signature for the active upload and grain."""

    digest = hashlib.sha256()
    digest.update(file_bytes)
    digest.update(b"\x00")
    digest.update(filename.encode("utf-8", errors="surrogatepass"))
    digest.update(b"\x00")
    digest.update(
        json.dumps(group_by, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    return digest.hexdigest()


def build_ai_signature(analysis_signature: str) -> str:
    """Bind cached AI output to one analysis and the frozen AI contract."""

    components = (
        analysis_signature,
        INSIGHT_CONTEXT_VERSION,
        INSIGHT_PROMPT_VERSION,
        INSIGHT_OUTPUT_VERSION,
        DEEPSEEK_MODEL,
    )
    payload = json.dumps(
        components,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_receipt_download_filename(receipt: InsightGenerationReceipt) -> str:
    """Return a short, path-safe filename derived only from analysis identity."""

    safe_id = re.sub(r"[^A-Za-z0-9]", "", receipt.analysis_signature)[:12]
    if not safe_id:
        safe_id = "unknown"
    return f"{RECEIPT_DOWNLOAD_PREFIX}{safe_id}{RECEIPT_DOWNLOAD_SUFFIX}"


def build_receipt_json_bytes(receipt: InsightGenerationReceipt) -> bytes:
    """Serialize only the explicit public Receipt contract as UTF-8 JSON."""

    if not isinstance(receipt, InsightGenerationReceipt):
        raise TypeError("receipt must be InsightGenerationReceipt")
    return json.dumps(
        receipt.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate text within a UTF-8 byte budget without splitting a character."""

    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def build_download_filename(filename: str | None) -> str:
    """Return a deterministic, path-safe XLSX download filename."""

    if not filename:
        return DEFAULT_DOWNLOAD_FILENAME
    basename = str(filename).replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem = Path(basename).stem.strip().strip("._-")
    safe_stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE).strip("._-")
    stem_byte_budget = MAX_DOWNLOAD_FILENAME_BYTES - len(
        DOWNLOAD_FILENAME_SUFFIX.encode("utf-8")
    )
    safe_stem = _truncate_utf8(safe_stem, stem_byte_budget).strip("._-")
    if not safe_stem:
        return DEFAULT_DOWNLOAD_FILENAME
    return f"{safe_stem}{DOWNLOAD_FILENAME_SUFFIX}"


def error_presentation(error: BaseException) -> ErrorPresentation:
    """Map frozen backend exceptions to safe, non-traceback UI content."""

    if isinstance(error, DataLoadError):
        return ErrorPresentation(
            title="File could not be loaded.",
            code=error.code,
            message=error.message,
        )
    if isinstance(error, PipelineError):
        return ErrorPresentation(
            title="Internal pipeline contract error.",
            code=error.code,
            message=error.message,
            stage=error.stage,
        )
    if isinstance(error, MetricsCalculationError):
        return ErrorPresentation(
            title="Metrics calculation failed.",
            code=error.code,
            message=error.message,
        )
    if isinstance(error, DiagnosticsError):
        return ErrorPresentation(
            title="Diagnostics failed.",
            code=error.code,
            message=error.message,
        )
    if isinstance(error, ReportError):
        return ErrorPresentation(
            title="Excel report could not be generated.",
            code=error.code,
            message=error.message,
        )
    return ErrorPresentation(
        title="Unexpected application error.",
        code="UNEXPECTED_APPLICATION_ERROR",
        message="The analysis could not be completed.",
    )


def execute_analysis(
    file_bytes: bytes,
    *,
    filename: str,
    group_by: list[str] | None,
) -> AnalysisArtifacts:
    """Run the frozen business pipeline and generate its report once."""

    pipeline_result = run_pipeline(
        file_bytes,
        filename=filename,
        group_by=group_by,
    )
    report_data: ReportData | None = None
    try:
        report_data = build_report_data(pipeline_result)
        excel_bytes = generate_excel_report(report_data)
        report_error = None
    except ReportError as error:
        excel_bytes = None
        report_error = error_presentation(error)
    except Exception:
        logger.exception("Unexpected error during Excel report generation")
        excel_bytes = None
        report_error = ErrorPresentation(
            title="Excel report could not be generated.",
            code="UNEXPECTED_REPORT_ERROR",
            message="An unexpected error occurred while generating the Excel report.",
        )
    return AnalysisArtifacts(
        pipeline_result=pipeline_result,
        report_data=report_data,
        excel_bytes=excel_bytes,
        report_error=report_error,
    )


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, bytes, bytearray, date, datetime)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _format_metric_value(metric: str, value: Any) -> str:
    if _is_missing(value):
        return "—"
    if metric in _PERCENTAGE_COLUMNS:
        return f"{float(value):.2%}"
    if metric in _USD_COLUMNS:
        return f"${float(value):,.2f}"
    if metric == "roas":
        return f"{float(value):.2f}x"
    if metric in _COUNT_METRICS:
        return f"{int(value):,}"
    return str(value)


def build_metrics_display(metrics: pd.DataFrame) -> pd.DataFrame:
    """Create a display-only copy with explicit NaN and metric formatting."""

    display = metrics.copy(deep=True)
    for column in display.columns:
        if column in _PERCENTAGE_COLUMNS | _USD_COLUMNS | {"roas"}:
            display[column] = display[column].map(
                lambda value, metric=column: _format_metric_value(metric, value)
            )
    return display


def _evidence_text(value: Any) -> str:
    if _is_missing(value):
        return "—"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def build_diagnostics_display(diagnostics: pd.DataFrame) -> pd.DataFrame:
    """Create a display-only copy without changing diagnostic row order."""

    display = diagnostics.copy(deep=True)
    if "evidence" in display.columns:
        display["evidence"] = display["evidence"].map(_evidence_text)
    if {
        "metric",
        "actual_value",
        "threshold",
    }.issubset(display.columns):
        metrics = [str(value) for value in display["metric"].tolist()]
        for column in ("actual_value", "threshold"):
            display[column] = pd.Series(
                [
                    _format_metric_value(metric, value)
                    for metric, value in zip(metrics, display[column].tolist())
                ],
                index=display.index,
                dtype="object",
            )
    return display


def _initialize_session_state() -> None:
    for key, value in _SESSION_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _clear_analysis_state() -> None:
    for key, value in _ANALYSIS_SESSION_DEFAULTS.items():
        st.session_state[key] = value


def _clear_ai_state() -> None:
    for key, value in _AI_SESSION_DEFAULTS.items():
        st.session_state[key] = value


def _sync_analysis_signature(current_signature: str | None) -> None:
    previous_signature = st.session_state.analysis_signature
    if previous_signature is not None and previous_signature != current_signature:
        _clear_analysis_state()
        _clear_ai_state()


def _ai_error_message(code: str) -> str:
    if code in _AI_RESULT_REJECTION_CODES:
        return _AI_GENERIC_REJECTION_MESSAGE
    return _AI_ERROR_MESSAGES.get(code, _AI_UNEXPECTED_ERROR_MESSAGE)


def _store_ai_error(code: str, *, signature: str) -> None:
    st.session_state.ai_error_code = code
    st.session_state.ai_error_message = _ai_error_message(code)
    st.session_state.ai_signature = signature


def _utc_now() -> datetime:
    """Return the request-start clock value through a private test seam."""

    return datetime.now(timezone.utc)


def _generate_ai_artifacts(
    result: PipelineResult,
    *,
    analysis_signature: str,
    group_by: list[str] | None,
) -> AiGenerationArtifacts:
    """Generate one validated output, then create its matching Receipt."""

    context = build_insight_context(result)
    provider = DeepSeekInsightProvider()
    pricing_reference_at = _utc_now()
    generation = generate_insight_with_metadata(context, provider=provider)
    if not isinstance(generation, InsightGenerationResult) or not isinstance(
        generation.output,
        InsightOutput,
    ):
        raise TypeError(
            "generate_insight_with_metadata() returned an invalid result type"
        )
    cost = build_cost_audit_metadata(
        generation.usage,
        provider=DEEPSEEK_PROVIDER_NAME,
        model=DEEPSEEK_MODEL,
        pricing_reference_at=pricing_reference_at,
    )
    receipt = build_insight_generation_receipt(
        analysis_signature=analysis_signature,
        group_by=group_by,
        context=context,
        output=generation.output,
        usage=generation.usage,
        cost=cost,
    )
    return AiGenerationArtifacts(output=generation.output, receipt=receipt)


def _run_ai_generation(
    result: PipelineResult,
    *,
    signature: str,
    analysis_signature: str,
    group_by: list[str] | None,
) -> None:
    try:
        artifacts = _generate_ai_artifacts(
            result,
            analysis_signature=analysis_signature,
            group_by=group_by,
        )
    except (
        InsightContextError,
        InsightPromptError,
        InsightProviderError,
        InsightOutputError,
        CostAuditError,
        PricingCatalogError,
        PricingError,
        InsightReceiptError,
    ) as error:
        code = error.code if isinstance(error.code, str) else "UNEXPECTED_AI_ERROR"
        _store_ai_error(code, signature=signature)
    except Exception:
        logger.exception("Unexpected error during AI insight generation")
        _store_ai_error("UNEXPECTED_AI_ERROR", signature=signature)
    else:
        st.session_state.update(
            {
                "ai_output": artifacts.output,
                "ai_receipt": artifacts.receipt,
                "ai_error_code": None,
                "ai_error_message": None,
                "ai_signature": signature,
            }
        )


def _format_ai_scope(scope: dict[str, Any]) -> str:
    if not scope:
        return "Overall"
    return " · ".join(
        f"{_SCOPE_LABELS.get(dimension, dimension.replace('_', ' ').title())}: "
        f"{value}"
        for dimension, value in scope.items()
    )


def _render_ai_output(output: InsightOutput) -> None:
    st.markdown("#### Executive Summary")
    st.write(output.executive_summary)

    st.markdown("#### Priority Insights")
    if not output.priority_insights:
        st.info("No priority insight was produced for this analysis.")
    for insight in output.priority_insights:
        with st.expander(_format_ai_scope(insight.scope)):
            st.caption(f"Confidence: {insight.confidence.title()}")
            st.markdown("**Observation**")
            st.write(insight.observation)
            st.markdown("**Evidence codes**")
            st.markdown(
                " · ".join(f"`{code}`" for code in insight.evidence_codes)
            )
            if insight.possible_explanations:
                st.markdown("**Possible explanations (hypotheses)**")
                for explanation in insight.possible_explanations:
                    st.markdown(f"- {explanation}")
            if insight.recommended_checks:
                st.markdown("**Recommended checks (investigations)**")
                for check in insight.recommended_checks:
                    st.markdown(f"- {check}")

    if output.overall_limitations:
        st.markdown("#### Limitations")
        for limitation in output.overall_limitations:
            st.markdown(f"- {limitation}")


def _format_receipt_time(generated_at: str) -> str:
    parsed = datetime.fromisoformat(generated_at)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_receipt_group_by(group_by: tuple[str, ...]) -> str:
    if not group_by:
        return "Overall"
    return " · ".join(
        _SCOPE_LABELS.get(dimension, dimension.replace("_", " ").title())
        for dimension in group_by
    )


def _provider_display_name(provider: str) -> str:
    return _PROVIDER_DISPLAY_NAMES.get(provider, provider)


def _format_cost_amount(value: Any) -> str:
    """Render one Receipt Decimal exactly without float conversion or rounding."""

    return format(value, "f")


def _render_cost_details(cost: CostAuditMetadata) -> None:
    """Render only the historical cost audit stored inside the Receipt."""

    st.markdown("**Estimated Cost**")
    st.caption(
        f"Pricing reference: {cost.pricing_reference_at} · "
        f"Pricing policy: {cost.pricing_policy_version}"
    )
    if cost.status == UNAVAILABLE:
        st.caption("Estimated API cost unavailable for this generation.")
        reason_message = _COST_UNAVAILABLE_MESSAGES.get(
            cost.unavailable_reason or "",
            "Cost estimate unavailable.",
        )
        st.caption(reason_message)
        st.caption(_COST_ESTIMATE_DISCLAIMER)
        return
    if cost.status != AVAILABLE or cost.estimate is None:
        raise ValueError("Invalid Cost Audit presentation state")

    estimate = cost.estimate
    st.caption(
        f"Estimated total API cost ({estimate.currency}): "
        f"${_format_cost_amount(estimate.total_estimated_cost)}"
    )
    st.caption(f"Pricing tier: {estimate.pricing_tier}")
    st.caption(
        "Cache-hit input cost: "
        f"${_format_cost_amount(estimate.prompt_cache_hit_cost)} · "
        "Cache-miss input cost: "
        f"${_format_cost_amount(estimate.prompt_cache_miss_cost)} · "
        "Completion cost: "
        f"${_format_cost_amount(estimate.completion_cost)}"
    )
    st.caption(_COST_ESTIMATE_DISCLAIMER)


def _current_ai_pair(
    *,
    ai_signature: str,
    analysis_signature: str,
    group_by: list[str] | None,
) -> tuple[InsightOutput | None, InsightGenerationReceipt | None]:
    output = st.session_state.ai_output
    receipt = st.session_state.ai_receipt
    if output is None and receipt is None:
        return None, None
    expected_group_by = () if group_by is None else tuple(group_by)
    if (
        not isinstance(output, InsightOutput)
        or not isinstance(receipt, InsightGenerationReceipt)
        or st.session_state.ai_signature != ai_signature
        or receipt.analysis_signature != analysis_signature
        or receipt.group_by != expected_group_by
        or receipt.version != INSIGHT_RECEIPT_VERSION
        or receipt.context_version != INSIGHT_CONTEXT_VERSION
        or receipt.prompt_version != INSIGHT_PROMPT_VERSION
        or receipt.output_version != INSIGHT_OUTPUT_VERSION
        or receipt.provider != DEEPSEEK_PROVIDER_NAME
        or receipt.model != DEEPSEEK_MODEL
        or receipt.priority_insight_count != len(output.priority_insights)
        or not isinstance(receipt.cost, CostAuditMetadata)
    ):
        _clear_ai_state()
        return None, None
    return output, receipt


def _render_generation_details(receipt: InsightGenerationReceipt) -> None:
    with st.expander("Generation Details"):
        st.caption(
            f"Generated at {_format_receipt_time(receipt.generated_at)} · "
            f"Analysis ID {receipt.analysis_signature[:12]}"
        )
        provider_name = _provider_display_name(receipt.provider)
        st.markdown(f"**Provider:** {provider_name} · **Model:** `{receipt.model}`")
        st.markdown(f"**Analysis scope:** {_format_receipt_group_by(receipt.group_by)}")
        st.caption(
            f"Context v{receipt.context_version} · Prompt v{receipt.prompt_version} "
            f"· Output v{receipt.output_version} · Receipt v{receipt.version}"
        )
        st.caption(
            f"Metric groups: {receipt.metric_record_count:,} · "
            f"Diagnostic signals: {receipt.diagnostic_signal_count:,} · "
            f"Priority insights: {receipt.priority_insight_count:,}"
        )
        st.markdown("**Token Usage**")
        usage = receipt.usage
        if usage is None:
            st.caption("Token usage unavailable for this generation.")
        else:
            st.caption(
                f"Prompt tokens: {usage.prompt_tokens:,} · "
                f"Completion tokens: {usage.completion_tokens:,} · "
                f"Total tokens: {usage.total_tokens:,}"
            )
            optional_usage: list[str] = []
            if usage.prompt_cache_hit_tokens is not None:
                optional_usage.append(
                    "Prompt cache hit tokens: "
                    f"{usage.prompt_cache_hit_tokens:,}"
                )
            if usage.prompt_cache_miss_tokens is not None:
                optional_usage.append(
                    "Prompt cache miss tokens: "
                    f"{usage.prompt_cache_miss_tokens:,}"
                )
            if usage.reasoning_tokens is not None:
                optional_usage.append(
                    f"Reasoning tokens: {usage.reasoning_tokens:,}"
                )
            if optional_usage:
                st.caption(" · ".join(optional_usage))
        _render_cost_details(receipt.cost)
        st.download_button(
            "Download AI Receipt",
            data=build_receipt_json_bytes(receipt),
            file_name=build_receipt_download_filename(receipt),
            mime=RECEIPT_DOWNLOAD_MIME,
            key="download_ai_receipt",
        )


def _render_generation_details_safely(
    receipt: InsightGenerationReceipt,
) -> None:
    """Render passive audit UI without exposing unexpected exception details."""

    try:
        _render_generation_details(receipt)
    except Exception:
        logger.exception("Unexpected error while rendering AI generation details")
        st.error(_AI_AUDIT_PRESENTATION_ERROR_MESSAGE)


def _render_ai_section(
    result: PipelineResult,
    current_signature: str,
    group_by: list[str] | None,
) -> None:
    if result.status is not PipelineStatus.SUCCESS:
        return
    if st.session_state.analysis_signature != current_signature:
        return

    ai_signature = build_ai_signature(current_signature)
    has_ai_state = any(
        st.session_state[key] is not None for key in _AI_SESSION_DEFAULTS
    )
    if has_ai_state and st.session_state.ai_signature != ai_signature:
        _clear_ai_state()

    current_output, current_receipt = _current_ai_pair(
        ai_signature=ai_signature,
        analysis_signature=current_signature,
        group_by=group_by,
    )

    st.subheader("AI Insights")
    st.caption(
        "Optional interpretation powered by DeepSeek. Generation occurs only "
        "when you explicitly click the button below."
    )
    st.info(
        "AI interprets existing deterministic metrics and diagnostic signals. "
        "Diagnostic signals are observations; possible explanations are "
        "hypotheses; recommended checks are investigations—not proven root "
        "causes or guaranteed actions."
    )

    button_label = (
        "Regenerate AI Insights"
        if current_output is not None
        else "Generate AI Insights"
    )
    generate_clicked = st.button(button_label, key="generate_ai_insights")
    if generate_clicked:
        with st.spinner("Generating AI insights..."):
            _run_ai_generation(
                result,
                signature=ai_signature,
                analysis_signature=current_signature,
                group_by=group_by,
            )
        if st.session_state.ai_error_message is None:
            st.rerun()

    ai_error_message = st.session_state.ai_error_message
    current_output, current_receipt = _current_ai_pair(
        ai_signature=ai_signature,
        analysis_signature=current_signature,
        group_by=group_by,
    )
    if ai_error_message is not None:
        if current_output is not None:
            st.warning(
                "AI regeneration failed. Showing the previous successful result. "
                f"{ai_error_message}"
            )
        else:
            st.error(ai_error_message)
    if current_output is not None:
        _render_ai_output(current_output)
        if current_receipt is not None:
            _render_generation_details_safely(current_receipt)


def _render_error(error: ErrorPresentation) -> None:
    st.error(error.title)
    details = f"Code: `{error.code}`"
    if error.stage is not None:
        details += f" · Stage: `{error.stage}`"
    st.markdown(details)
    st.caption(error.message)


def _validation_issues_dataframe(
    result: PipelineResult,
    report_data: ReportData | None,
) -> pd.DataFrame:
    if report_data is not None:
        return report_data.validation_issues.copy(deep=True)
    return pd.DataFrame.from_records(
        [issue.to_dict() for issue in result.validation.report.issues],
        columns=("level", "code", "row", "field", "message"),
    )


def _render_validation(
    result: PipelineResult,
    report_data: ReportData | None,
) -> None:
    report = result.validation.report
    st.subheader("Validation")
    row_columns = st.columns(4)
    for column, label, value in zip(
        row_columns,
        ("Raw Rows", "Valid Rows", "Excluded Rows", "Warning Rows"),
        (
            report.total_rows,
            report.valid_rows,
            report.excluded_rows,
            report.warning_rows,
        ),
    ):
        column.metric(label, value)

    issue_columns = st.columns(3)
    issue_columns[0].metric("Fatal Issues", len(report.fatal_errors))
    issue_columns[1].metric("Error Issues", len(report.errors))
    issue_columns[2].metric("Warning Issues", len(report.warnings))

    if report.fatal_errors:
        st.error(
            "File validation failed. Fix the fatal data contract issues before "
            "metrics can be generated."
        )
    else:
        if report.errors:
            st.error(
                f"{len(report.errors)} validation error issue(s) were excluded "
                "from analysis."
            )
        if report.warnings:
            st.warning(
                f"{len(report.warnings)} validation warning issue(s) were retained "
                "or handled by the frozen validation rules."
            )
        if not report.errors and not report.warnings:
            st.success("No validation issues were found.")

    issues = _validation_issues_dataframe(result, report_data)
    if not issues.empty:
        st.dataframe(
            issues,
            hide_index=True,
            width="stretch",
            height=min(420, 38 * (len(issues) + 1)),
        )


def _render_metrics(result: PipelineResult, group_label: str) -> None:
    if result.status is not PipelineStatus.SUCCESS or result.metrics is None:
        return
    st.subheader("Metrics")
    st.caption(f"{len(result.metrics):,} aggregated group(s) · {group_label}")
    if result.metrics.empty:
        st.info("No valid rows remained for metrics calculation.")
        return
    st.dataframe(
        build_metrics_display(result.metrics),
        hide_index=True,
        width="stretch",
        height=min(520, 38 * (len(result.metrics) + 1)),
    )


def _render_diagnostics(result: PipelineResult) -> None:
    if result.status is not PipelineStatus.SUCCESS or result.diagnostics is None:
        return
    st.subheader("Diagnostic Signals")
    st.caption(
        "Diagnostic signals use Demo Default Thresholds and are not industry "
        "standards. They are observations, not root-cause conclusions."
    )
    if result.diagnostics.empty:
        st.info(
            "No diagnostic signals were triggered by the current Demo Default "
            "Thresholds."
        )
        return
    st.dataframe(
        build_diagnostics_display(result.diagnostics),
        hide_index=True,
        width="stretch",
        height=min(520, 44 * (len(result.diagnostics) + 1)),
    )


def _render_download() -> None:
    st.subheader("Excel Report")
    report_error = st.session_state.report_error
    if report_error is not None:
        _render_error(report_error)
        return
    excel_bytes = st.session_state.excel_bytes
    if excel_bytes is None:
        st.info("Excel report is not available for this analysis.")
        return
    st.download_button(
        "Download Excel Report",
        data=excel_bytes,
        file_name=st.session_state.download_filename,
        mime=DOWNLOAD_MIME,
        type="primary",
    )


def _store_artifacts(artifacts: AnalysisArtifacts, filename: str) -> None:
    st.session_state.pipeline_result = artifacts.pipeline_result
    st.session_state.report_data = artifacts.report_data
    st.session_state.excel_bytes = artifacts.excel_bytes
    st.session_state.report_error = artifacts.report_error
    st.session_state.analysis_error = None
    st.session_state.download_filename = build_download_filename(filename)


def main() -> None:
    """Render the single-page Streamlit application."""

    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="🌐",
        layout="wide",
    )
    st.markdown(_APP_STYLES, unsafe_allow_html=True)
    _initialize_session_state()

    st.title(APP_TITLE)
    st.caption(APP_SUBTITLE)
    st.write(
        "Upload daily marketplace data, choose an analysis level, then run the "
        "deterministic validation, metrics, diagnostics and Excel report flow."
    )

    st.subheader("Upload and configuration")
    uploaded_file = st.file_uploader(
        "Operations data",
        type=["csv", "xlsx"],
        accept_multiple_files=False,
        help="Supported formats: CSV and XLSX.",
    )
    group_label = st.selectbox(
        "Analysis level",
        options=tuple(GROUP_BY_OPTIONS),
        index=tuple(GROUP_BY_OPTIONS).index(DEFAULT_GROUP_BY_LABEL),
    )
    group_by = resolve_group_by(group_label)

    file_bytes: bytes | None = None
    current_signature: str | None = None
    if uploaded_file is not None:
        file_bytes = uploaded_file.getvalue()
        current_signature = build_analysis_signature(
            file_bytes,
            uploaded_file.name,
            group_by,
        )
    _sync_analysis_signature(current_signature)

    run_clicked = st.button(
        "Run Analysis",
        type="primary",
        disabled=uploaded_file is None,
    )
    if run_clicked and uploaded_file is not None and file_bytes is not None:
        _clear_analysis_state()
        st.session_state.analysis_signature = current_signature
        with st.spinner("Analyzing data..."):
            try:
                artifacts = execute_analysis(
                    file_bytes,
                    filename=uploaded_file.name,
                    group_by=group_by,
                )
                _store_artifacts(artifacts, uploaded_file.name)
                if artifacts.pipeline_result.status is not PipelineStatus.SUCCESS:
                    _clear_ai_state()
            except (
                DataLoadError,
                PipelineError,
                MetricsCalculationError,
                DiagnosticsError,
            ) as error:
                st.session_state.analysis_error = error_presentation(error)
            except Exception as error:
                logger.exception("Unexpected application error during analysis")
                st.session_state.analysis_error = error_presentation(error)

    if uploaded_file is None:
        st.info("Upload a CSV or XLSX file to begin.")
        return

    analysis_error = st.session_state.analysis_error
    result = st.session_state.pipeline_result
    if analysis_error is not None:
        _render_error(analysis_error)
        return
    if result is None:
        st.info("Ready to analyze.")
        return

    _render_validation(result, st.session_state.report_data)
    _render_metrics(result, group_label)
    _render_diagnostics(result)
    _render_download()
    if current_signature is not None:
        _render_ai_section(result, current_signature, group_by)


if __name__ == "__main__":
    main()
