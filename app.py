"""Streamlit application layer for CrossBorder Ops Radar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import logging
from pathlib import Path
import re
from typing import Any

import pandas as pd
import streamlit as st

from src.deepseek_provider import DEEPSEEK_MODEL, DeepSeekInsightProvider
from src.diagnostics import DiagnosticsError
from src.insight_attempt_audit import FAILED, SUCCEEDED
from src.insight_cost_audit import (
    AVAILABLE,
    UNAVAILABLE,
    CostAuditMetadata,
)
from src.insight_logical_generation_cost import (
    FULLY_ESTIMATED,
    UNKNOWN_TOTAL,
    UNAVAILABLE as LOGICAL_COST_UNAVAILABLE,
    LogicalGenerationCostError,
    LogicalGenerationCostSummary,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    USAGE_UNAVAILABLE,
)
from src.insight_prompt import (
    INSIGHT_OUTPUT_VERSION,
    INSIGHT_PROMPT_VERSION,
    InsightOutput,
    InsightOutputError,
    InsightPromptError,
)
from src.insight_provider import InsightProviderError
from src.insight_receipt import DEEPSEEK_PROVIDER_NAME
from src.insight_receipt_v4 import (
    INSIGHT_RECEIPT_V4_VERSION,
    InsightGenerationReceiptV4,
    InsightReceiptV4Error,
    build_insight_receipt_v4,
)
from src.insight_retry_execution import (
    RetryExecutionError,
    RetryExecutionResult,
    execute_insight_generation_with_retry,
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
AI_SUCCESS_BINDING_VERSION = "1"

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
    "ai_signature": None,
    "ai_success_binding": None,
    "ai_failure": None,
}

_LEGACY_AI_ERROR_KEYS: tuple[str, ...] = (
    "ai_error_code",
    "ai_error_message",
)

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
    "INVALID_RECEIPT_V4_INPUT": (
        "AI insights could not be saved with valid generation details."
    ),
    "INVALID_LOGICAL_GENERATION_COST": (
        "AI cost details could not be safely recorded for this generation."
    ),
    "INVALID_RETRY_EXECUTION": (
        "AI retry execution could not be completed safely."
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

_POST_EXECUTION_AI_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "INVALID_RECEIPT_V4_INPUT",
        "INVALID_LOGICAL_GENERATION_COST",
        "UNEXPECTED_AI_ERROR",
    }
)

_AI_LOG_STAGES: frozenset[str] = frozenset(
    {
        "context_build",
        "provider_setup",
        "retry_execution",
        "receipt_build",
        "success_binding",
        "render",
    }
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
    receipt: InsightGenerationReceiptV4
    success_binding: str


@dataclass(frozen=True)
class AiGenerationFailure:
    """Failure provenance for one explicit AI generation operation."""

    signature: str
    error_code: str
    execution_result: RetryExecutionResult | None


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


def build_ai_success_binding(
    output: InsightOutput,
    receipt: InsightGenerationReceiptV4,
    ai_signature: str,
) -> str:
    """Bind one App-local success snapshot without exporting its payload."""

    if not isinstance(output, InsightOutput):
        raise TypeError("output must be InsightOutput")
    if not isinstance(receipt, InsightGenerationReceiptV4):
        raise TypeError("receipt must be InsightGenerationReceiptV4")
    if (
        not isinstance(ai_signature, str)
        or re.fullmatch(r"[0-9a-f]{64}", ai_signature) is None
    ):
        raise ValueError("ai_signature must be a lowercase SHA-256 digest")
    canonical = json.dumps(
        {
            "binding_version": AI_SUCCESS_BINDING_VERSION,
            "ai_signature": ai_signature,
            "output": output.to_dict(),
            "receipt": receipt.to_dict(),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_receipt_download_filename(
    receipt: InsightGenerationReceiptV4,
) -> str:
    """Return a short, path-safe filename derived only from analysis identity."""

    safe_id = re.sub(r"[^A-Za-z0-9]", "", receipt.analysis_signature)[:12]
    if not safe_id:
        safe_id = "unknown"
    return f"{RECEIPT_DOWNLOAD_PREFIX}{safe_id}{RECEIPT_DOWNLOAD_SUFFIX}"


def build_receipt_json_bytes(receipt: InsightGenerationReceiptV4) -> bytes:
    """Serialize only the explicit public Receipt contract as UTF-8 JSON."""

    if not isinstance(receipt, InsightGenerationReceiptV4):
        raise TypeError("receipt must be InsightGenerationReceiptV4")
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
    for key in _LEGACY_AI_ERROR_KEYS:
        if key in st.session_state:
            del st.session_state[key]


def _clear_analysis_state() -> None:
    for key, value in _ANALYSIS_SESSION_DEFAULTS.items():
        st.session_state[key] = value


def _clear_ai_success_state() -> None:
    for key in (
        "ai_output",
        "ai_receipt",
        "ai_signature",
        "ai_success_binding",
    ):
        st.session_state[key] = None


def _clear_ai_state() -> None:
    _clear_ai_success_state()
    st.session_state.ai_failure = None
    for key in _LEGACY_AI_ERROR_KEYS:
        if key in st.session_state:
            del st.session_state[key]


def _clear_ai_failure() -> None:
    st.session_state.ai_failure = None


def _sync_analysis_signature(current_signature: str | None) -> None:
    previous_signature = st.session_state.analysis_signature
    if previous_signature is not None and previous_signature != current_signature:
        _clear_analysis_state()
        _clear_ai_state()


def _ai_error_message(code: str) -> str:
    if code in _AI_RESULT_REJECTION_CODES:
        return _AI_GENERIC_REJECTION_MESSAGE
    return _AI_ERROR_MESSAGES.get(code, _AI_UNEXPECTED_ERROR_MESSAGE)


def _log_unexpected_ai_error(stage: str, error: Exception) -> None:
    """Log only fixed AI failure metadata, never exception content/traceback."""

    safe_stage = stage if stage in _AI_LOG_STAGES else "unknown"
    logger.error(
        "Unexpected AI operation failure at stage=%s; exception_type=%s",
        safe_stage,
        type(error).__name__,
    )


def _store_ai_failure(
    code: str,
    *,
    signature: str,
    execution_result: RetryExecutionResult | None,
) -> None:
    st.session_state.ai_failure = AiGenerationFailure(
        signature=signature,
        error_code=code,
        execution_result=execution_result,
    )


def _is_valid_ai_failure(value: object, *, signature: str) -> bool:
    """Validate an App-local failure across Streamlit script reruns."""

    if type(value).__name__ != AiGenerationFailure.__name__:
        return False
    failure_fields = getattr(type(value), "__dataclass_fields__", None)
    if not isinstance(failure_fields, dict) or set(failure_fields) != {
        "signature",
        "error_code",
        "execution_result",
    }:
        return False
    try:
        failure_signature = value.signature  # type: ignore[attr-defined]
        error_code = value.error_code  # type: ignore[attr-defined]
        execution_result = value.execution_result  # type: ignore[attr-defined]
    except Exception:
        return False
    if failure_signature != signature:
        return False
    if (
        not isinstance(error_code, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", error_code) is None
    ):
        return False
    if execution_result is None:
        return True
    if not isinstance(execution_result, RetryExecutionResult):
        return False
    if execution_result.status == FAILED:
        return execution_result.error_code == error_code
    if execution_result.status == SUCCEEDED:
        return error_code in _POST_EXECUTION_AI_FAILURE_CODES
    return False


def _utc_now() -> datetime:
    """Return the receipt-generation clock through a private test seam."""

    return datetime.now(timezone.utc)


def _run_ai_generation(
    result: PipelineResult,
    *,
    signature: str,
    analysis_signature: str,
    group_by: list[str] | None,
) -> None:
    execution_result: RetryExecutionResult | None = None
    stage = "context_build"
    try:
        context = build_insight_context(result)
        stage = "provider_setup"
        provider = DeepSeekInsightProvider()
        stage = "retry_execution"
        candidate = execute_insight_generation_with_retry(
            context,
            provider=provider,
        )
        if not isinstance(candidate, RetryExecutionResult):
            raise TypeError(
                "execute_insight_generation_with_retry() returned an invalid "
                "result type"
            )
        execution_result = candidate
        if execution_result.status == FAILED:
            error_code = execution_result.error_code
            if not isinstance(error_code, str) or not error_code.strip():
                raise TypeError(
                    "A failed Retry Execution result requires an error code"
                )
            _store_ai_failure(
                error_code,
                signature=signature,
                execution_result=execution_result,
            )
            return
        if execution_result.status != SUCCEEDED:
            raise TypeError("Retry Execution returned an unknown status")
        output = execution_result.output
        if not isinstance(output, InsightOutput):
            raise TypeError(
                "A succeeded Retry Execution result requires InsightOutput"
            )
        stage = "receipt_build"
        generated_at = _utc_now().isoformat()
        receipt = build_insight_receipt_v4(
            generated_at=generated_at,
            analysis_signature=analysis_signature,
            group_by=group_by,
            context=context,
            execution_result=execution_result,
        )
        stage = "success_binding"
        success_binding = build_ai_success_binding(
            output,
            receipt,
            signature,
        )
        artifacts = AiGenerationArtifacts(
            output=output,
            receipt=receipt,
            success_binding=success_binding,
        )
    except (
        InsightContextError,
        InsightPromptError,
        InsightProviderError,
        InsightOutputError,
        RetryExecutionError,
        InsightReceiptV4Error,
        LogicalGenerationCostError,
    ) as error:
        code = error.code if isinstance(error.code, str) else "UNEXPECTED_AI_ERROR"
        _store_ai_failure(
            code,
            signature=signature,
            execution_result=execution_result,
        )
    except Exception as error:
        _log_unexpected_ai_error(stage, error)
        _store_ai_failure(
            "UNEXPECTED_AI_ERROR",
            signature=signature,
            execution_result=execution_result,
        )
    else:
        st.session_state.update(
            {
                "ai_output": artifacts.output,
                "ai_receipt": artifacts.receipt,
                "ai_signature": signature,
                "ai_success_binding": artifacts.success_binding,
                "ai_failure": None,
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


def _render_cost_details(
    cost: CostAuditMetadata,
    logical_cost: LogicalGenerationCostSummary,
) -> None:
    """Render final-attempt and logical-generation cost truth separately."""

    st.markdown("**Cost Estimate**")
    st.caption(
        f"Pricing reference: {cost.pricing_reference_at} · "
        f"Pricing policy: {cost.pricing_policy_version}"
    )
    if cost.status == UNAVAILABLE:
        st.caption("Final successful attempt cost estimate unavailable.")
        reason_message = _COST_UNAVAILABLE_MESSAGES.get(
            cost.unavailable_reason or "",
            "Cost estimate unavailable.",
        )
        st.caption(reason_message)
    elif cost.status == AVAILABLE and cost.estimate is not None:
        estimate = cost.estimate
        st.caption(
            "Final successful attempt estimated API cost (USD): "
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
    else:
        raise ValueError("Invalid Cost Audit presentation state")

    if logical_cost.status == FULLY_ESTIMATED:
        amount = logical_cost.estimated_total_cost_usd
        if amount is None:
            raise ValueError("Logical-generation total is missing")
        st.caption(
            "Logical-generation estimated total (USD): "
            f"${_format_cost_amount(amount)}"
        )
    elif logical_cost.status == LOGICAL_COST_UNAVAILABLE:
        st.caption(
            "Logical-generation total estimate unavailable because the final "
            "successful attempt cost estimate is unavailable."
        )
    elif logical_cost.status == UNKNOWN_TOTAL:
        st.caption(
            "Logical-generation total spend is unknown because one or more "
            "prior failed attempts have unknown cost."
        )
    else:
        raise ValueError("Invalid logical-generation cost presentation state")
    st.caption(_COST_ESTIMATE_DISCLAIMER)


def _is_valid_ai_success_binding(
    binding: object,
    *,
    output: InsightOutput,
    receipt: InsightGenerationReceiptV4,
    ai_signature: str,
) -> bool:
    if (
        not isinstance(binding, str)
        or re.fullmatch(r"[0-9a-f]{64}", binding) is None
    ):
        return False
    try:
        expected = build_ai_success_binding(output, receipt, ai_signature)
    except Exception as error:
        _log_unexpected_ai_error("success_binding", error)
        return False
    return hmac.compare_digest(binding, expected)


def _current_ai_pair(
    *,
    ai_signature: str,
    analysis_signature: str,
    group_by: list[str] | None,
) -> tuple[InsightOutput | None, InsightGenerationReceiptV4 | None]:
    output = st.session_state.ai_output
    receipt = st.session_state.ai_receipt
    signature = st.session_state.ai_signature
    binding = st.session_state.ai_success_binding
    if (
        output is None
        and receipt is None
        and signature is None
        and binding is None
    ):
        return None, None
    expected_group_by = () if group_by is None else tuple(group_by)
    if (
        not isinstance(output, InsightOutput)
        or not isinstance(receipt, InsightGenerationReceiptV4)
        or signature != ai_signature
        or receipt.analysis_signature != analysis_signature
        or receipt.group_by != expected_group_by
        or receipt.version != INSIGHT_RECEIPT_V4_VERSION
        or receipt.context_version != INSIGHT_CONTEXT_VERSION
        or receipt.prompt_version != INSIGHT_PROMPT_VERSION
        or receipt.output_version != INSIGHT_OUTPUT_VERSION
        or receipt.provider != DEEPSEEK_PROVIDER_NAME
        or receipt.model != DEEPSEEK_MODEL
        or receipt.priority_insight_count != len(output.priority_insights)
        or not isinstance(receipt.cost, CostAuditMetadata)
    ):
        _clear_ai_success_state()
        return None, None
    if not _is_valid_ai_success_binding(
        binding,
        output=output,
        receipt=receipt,
        ai_signature=signature,
    ):
        _clear_ai_success_state()
        return None, None
    return output, receipt


def _render_retry_provenance(
    source: RetryExecutionResult | InsightGenerationReceiptV4,
) -> None:
    attempts = source.attempt_audit.attempts
    delays = source.delay_audit.records
    status = source.status if isinstance(source, RetryExecutionResult) else SUCCEEDED
    st.markdown("**Retry Provenance**")
    st.caption(
        f"Completed execution status: {status} · "
        f"Provider attempts: {len(attempts):,} · "
        f"Completed retry-delay transitions: {len(delays):,}"
    )
    st.caption(
        f"Retry policy: {source.attempt_audit.retry_policy_version} · "
        f"Delay policy: {source.delay_audit.policy_version}"
    )
    if delays:
        requested = " · ".join(
            f"after attempt {record.after_attempt_number}: "
            f"{record.delay_decision.delay_ms:,} ms requested"
            for record in delays
        )
        st.caption(f"Requested retry delays: {requested}")


def _render_failure_details(failure: AiGenerationFailure) -> None:
    with st.expander("Failed Generation Details"):
        st.caption(f"Failure code: {failure.error_code}")
        result = failure.execution_result
        if result is None:
            st.caption(
                "Attempt audit unavailable because no completed Retry Execution "
                "result was returned."
            )
            return
        _render_retry_provenance(result)
        if result.status == SUCCEEDED:
            st.caption(
                "Provider execution completed successfully, but post-execution "
                "application processing failed."
            )


def _render_generation_details(receipt: InsightGenerationReceiptV4) -> None:
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
        _render_retry_provenance(receipt)
        _render_cost_details(receipt.cost, receipt.logical_generation_cost)
        st.download_button(
            "Download AI Receipt",
            data=build_receipt_json_bytes(receipt),
            file_name=build_receipt_download_filename(receipt),
            mime=RECEIPT_DOWNLOAD_MIME,
            key="download_ai_receipt",
        )


def _render_generation_details_safely(
    receipt: InsightGenerationReceiptV4,
) -> None:
    """Render passive audit UI without exposing unexpected exception details."""

    try:
        _render_generation_details(receipt)
    except Exception as error:
        _log_unexpected_ai_error("render", error)
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
    has_ai_success_state = any(
        st.session_state[key] is not None
        for key in (
            "ai_output",
            "ai_receipt",
            "ai_signature",
            "ai_success_binding",
        )
    )
    if has_ai_success_state and st.session_state.ai_signature != ai_signature:
        _clear_ai_success_state()

    failure = st.session_state.ai_failure
    if failure is not None and not _is_valid_ai_failure(
        failure,
        signature=ai_signature,
    ):
        _clear_ai_failure()

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
        if st.session_state.ai_failure is None:
            st.rerun()

    failure = st.session_state.ai_failure
    current_output, current_receipt = _current_ai_pair(
        ai_signature=ai_signature,
        analysis_signature=current_signature,
        group_by=group_by,
    )
    if failure is not None and _is_valid_ai_failure(
        failure,
        signature=ai_signature,
    ):
        ai_error_message = _ai_error_message(failure.error_code)
        if current_output is not None:
            st.warning(
                "AI regeneration failed. Showing the previous successful result. "
                f"{ai_error_message}"
            )
        else:
            st.error(ai_error_message)
        _render_failure_details(failure)
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
