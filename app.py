"""Streamlit application layer for CrossBorder Ops Radar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

import pandas as pd
import streamlit as st

from src import ui_text as ui

from src.deepseek_provider import DeepSeekInsightProvider
from src.diagnostics import DiagnosticsError
from src.insight_prompt import InsightOutput, InsightOutputError, InsightPromptError
from src.insight_provider import InsightProviderError, generate_insight_with_metadata
from src.insights import InsightContextError, build_insight_context
from src.loader import DataLoadError
from src.metrics import MetricsCalculationError
from src.pipeline import PipelineError, PipelineResult, PipelineStatus, run_pipeline
from src.report import ReportData, ReportError, build_report_data, generate_excel_report

logger = logging.getLogger(__name__)

APP_TITLE = "CrossBorder Ops Radar"
APP_SUBTITLE = (
    ui.TEXT["subtitle"]
)
DEFAULT_GROUP_BY_LABEL = "SKU"
DOWNLOAD_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
DEFAULT_DOWNLOAD_FILENAME = "crossborder_ops_radar_report.xlsx"
DOWNLOAD_FILENAME_SUFFIX = "_crossborder_ops_radar.xlsx"
MAX_DOWNLOAD_FILENAME_BYTES = 180

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

_SESSION_DEFAULTS: dict[str, Any] = {
    **_ANALYSIS_SESSION_DEFAULTS,
    "ai_generation": None,
    "ai_error": None,
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
    st.session_state.ai_generation = None
    st.session_state.ai_error = None


def _sync_analysis_signature(current_signature: str | None) -> None:
    previous_signature = st.session_state.analysis_signature
    if previous_signature is not None and previous_signature != current_signature:
        _clear_analysis_state()
        _clear_ai_state()


def _ai_error_message(code: object) -> str:
    if isinstance(code, str):
        return ui.AI_ERROR_MESSAGES.get(code, ui.AI_ERROR_FALLBACK)
    return ui.AI_ERROR_FALLBACK


def _run_ai_generation(result: PipelineResult) -> None:
    try:
        provider = DeepSeekInsightProvider()
        context = build_insight_context(result)
        generation = generate_insight_with_metadata(context, provider=provider)
    except (InsightContextError, InsightPromptError, InsightOutputError, InsightProviderError) as error:
        st.session_state.ai_error = _ai_error_message(error.code)
    except Exception:
        # Provider exceptions can contain request data or credentials.
        logger.error("Unexpected failure during AI insight generation")
        st.session_state.ai_error = ui.AI_ERROR_FALLBACK
    else:
        st.session_state.ai_generation = generation
        st.session_state.ai_error = None


def _format_ai_scope(scope: dict[str, Any]) -> str:
    if not scope:
        return ui.GROUP_LABELS["Overall"]
    return " · ".join(
        f"{ui.SCOPE_LABELS.get(dimension, ui.TEXT['unknown_field'])}: "
        f"{value}"
        for dimension, value in scope.items()
    )


def _render_ai_output(output: InsightOutput) -> None:
    st.markdown('#### ' + ui.TEXT['summary'])
    st.write(output.executive_summary)

    st.markdown('#### ' + ui.TEXT['priority'])
    if not output.priority_insights:
        st.info(ui.TEXT['no_priority'])
    for insight in output.priority_insights:
        with st.expander(_format_ai_scope(insight.scope)):
            st.caption(ui.TEXT["confidence"].format(value=ui.CONFIDENCE_LABELS[insight.confidence]))
            st.markdown('**' + ui.TEXT['observation'] + "**")
            st.write(insight.observation)
            st.markdown('**' + ui.TEXT['evidence_codes'] + "**")
            st.markdown(
                " · ".join(f"`{code}`" for code in insight.evidence_codes)
            )
            if insight.possible_explanations:
                st.markdown('**' + ui.TEXT['explanations'] + "**")
                for explanation in insight.possible_explanations:
                    st.markdown(f"- {explanation}")
            if insight.recommended_checks:
                st.markdown('**' + ui.TEXT['checks'] + "**")
                for check in insight.recommended_checks:
                    st.markdown(f"- {check}")

    if output.overall_limitations:
        st.markdown('#### ' + ui.TEXT['limitations'])
        for limitation in output.overall_limitations:
            st.markdown(f"- {limitation}")


def _render_ai_section(result: PipelineResult) -> None:
    if result.status is not PipelineStatus.SUCCESS:
        return

    st.subheader(ui.TEXT["ai"])
    st.caption(ui.TEXT["ai_help"])
    st.info(ui.TEXT["ai_limits"])

    generation = st.session_state.ai_generation
    label = ui.TEXT["generate"] if generation is None else ui.TEXT["regenerate"]
    if st.button(label, key="generate_ai_insights"):
        with st.spinner(ui.TEXT["generating"]):
            _run_ai_generation(result)
        if st.session_state.ai_error is None:
            st.rerun()

    error_message = st.session_state.ai_error
    if error_message:
        if generation is None:
            st.error(error_message)
        else:
            st.warning(ui.TEXT["regeneration_failed"].format(message=error_message))
    if generation is not None:
        _render_ai_output(generation.output)
        usage = generation.usage
        if usage is not None:
            st.caption(ui.TEXT["usage_counts"].format(
                prompt=usage.prompt_tokens,
                completion=usage.completion_tokens,
                total=usage.total_tokens,
            ))


def _render_error(error: ErrorPresentation) -> None:
    # Only controlled titles, stable codes and stages may reach the UI.
    # ErrorPresentation remains unchanged; its raw message is never rendered.
    title = error.title if type(error.title) is str else ""
    st.error(ui.ERROR_TITLES.get(title, ui.ERROR_TITLES["Unexpected application error."]))
    code = error.code
    valid_code = type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]*", code) is not None
    if valid_code:
        details = ui.TEXT["error_code"].format(code=code)
        if type(error.stage) is str and error.stage in {
            "loader", "validator", "validation", "metrics", "diagnostics", "report"
        }:
            details += ui.TEXT["error_stage"].format(stage=error.stage)
        st.markdown(details)
    st.caption(ui.ERROR_MESSAGES.get(code, ui.ERROR_FALLBACK) if valid_code else ui.ERROR_FALLBACK)


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


def _table_column_config(columns: object, table_kind: str) -> dict[str, Any]:
    """Keep Arrow/CSV names intact; labels only affect the visible header."""
    return {
        column: st.column_config.Column(
            label=(
                ui.TABLE_CODE_LABELS[table_kind]
                if column == "code" and table_kind in ui.TABLE_CODE_LABELS
                else ui.FIELD_LABELS.get(column, column)
            ),
            help=ui.METRIC_HELP.get(column),
        )
        for column in columns
    }


def _table_cell_display(frame: pd.DataFrame, table_kind: str) -> Any:
    """Style controlled text without changing the data used by built-in CSV.

    Never raise a Styler size limit or allow its row/column trimming to hide data.
    Metrics retain their existing number formatting and need only header labels.
    """
    if table_kind == "metrics" or frame.empty:
        return frame
    max_rows = pd.get_option("styler.render.max_rows")
    max_columns = pd.get_option("styler.render.max_columns")
    if (
        frame.size > pd.get_option("styler.render.max_elements")
        or (max_rows is not None and len(frame) > max_rows)
        or (max_columns is not None and len(frame.columns) > max_columns)
        or not frame.index.is_unique
    ):
        return frame
    style = frame.style.set_uuid("radar_" + table_kind)
    for column, labels in (
        ("level", ui.LEVEL_LABELS), ("severity", ui.LEVEL_LABELS),
        ("field", ui.FIELD_LABELS), ("metric", ui.FIELD_LABELS),
    ):
        if column in frame:
            style.format(
                lambda value, labels=labels: (
                    labels.get(value, value) if isinstance(value, str) else value
                ),
                subset=[column], na_rep="—",
            )
    if "message" in frame and "code" in frame:
        # Group by controlled code/field facts, never by the original prose.
        groups: dict[str, list[Any]] = {}
        for index, row in frame.iterrows():
            code = row["code"]
            if table_kind == "validation":
                field = row.get("field")
                label = ui.FIELD_LABELS.get(field, ui.TEXT["unknown_field"])
                template = ui.VALIDATION_MESSAGES.get(code, ui.TEXT["unknown_issue"])
                message = template.format(field=label)
            else:
                message = ui.DIAGNOSTIC_MESSAGES.get(code, ui.TEXT["unknown_issue"])
            groups.setdefault(message, []).append(index)
        for message, indexes in groups.items():
            style.format(
                lambda _value, message=message: message,
                subset=pd.IndexSlice[indexes, ["message"]],
            )
    return style


def _render_table(frame: pd.DataFrame, table_kind: str, *, height: int) -> None:
    display = _table_cell_display(frame, table_kind)
    if display is frame and table_kind != "metrics" and not frame.empty:
        st.caption(ui.TEXT["large_table"])
        with st.expander(ui.TEXT["legend"]):
            labels = (
                ui.VALIDATION_MESSAGES if table_kind == "validation"
                else ui.DIAGNOSTIC_LABELS
            )
            st.text("\n".join(
                f"{code}：{label.format(field=ui.TEXT['unknown_field'])}"
                for code, label in labels.items()
            ))
            st.text(" · ".join(f"{key}：{value}" for key, value in ui.LEVEL_LABELS.items()))
    st.dataframe(
        display,
        column_config=_table_column_config(frame.columns, table_kind),
        hide_index=True,
        width="stretch",
        height=height,
    )


def _render_validation(
    result: PipelineResult,
    report_data: ReportData | None,
) -> None:
    report = result.validation.report
    st.subheader(ui.TEXT['validation'])
    row_columns = st.columns(4)
    for column, label, value in zip(
        row_columns,
        (ui.TEXT['raw_rows'], ui.TEXT['valid_rows'], ui.TEXT['excluded_rows'], ui.TEXT['warning_rows']),
        (
            report.total_rows,
            report.valid_rows,
            report.excluded_rows,
            report.warning_rows,
        ),
    ):
        column.metric(label, value)

    issue_columns = st.columns(3)
    issue_columns[0].metric(ui.TEXT['fatal_issues'], len(report.fatal_errors))
    issue_columns[1].metric(ui.TEXT['error_issues'], len(report.errors))
    issue_columns[2].metric(ui.TEXT['warning_issues'], len(report.warnings))

    if report.fatal_errors:
        st.error(
            ui.TEXT['validation_failed']
        )
    else:
        if report.errors:
            st.error(
                ui.TEXT["validation_errors"].format(count=len(report.errors))
            )
        if report.warnings:
            st.warning(
                ui.TEXT["validation_warnings"].format(count=len(report.warnings))
            )
        if not report.errors and not report.warnings:
            st.success(ui.TEXT['validation_clean'])

    issues = _validation_issues_dataframe(result, report_data)
    if not issues.empty:
        _render_table(
            issues, "validation",
            height=min(420, 38 * (len(issues) + 1)),
        )


def _render_metrics(result: PipelineResult, group_label: str) -> None:
    if result.status is not PipelineStatus.SUCCESS or result.metrics is None:
        return
    st.subheader(ui.TEXT['metrics'])
    st.caption(ui.TEXT["metric_groups"].format(
        count=len(result.metrics), scope=ui.GROUP_LABELS[group_label]
    ))
    if result.metrics.empty:
        st.info(ui.TEXT['no_metrics'])
        return
    _render_table(
        build_metrics_display(result.metrics), "metrics",
        height=min(520, 38 * (len(result.metrics) + 1)),
    )


def _render_diagnostics(result: PipelineResult) -> None:
    if result.status is not PipelineStatus.SUCCESS or result.diagnostics is None:
        return
    st.subheader(ui.TEXT['diagnostics'])
    st.caption(
        ui.TEXT['diagnostics_help']
    )
    if result.diagnostics.empty:
        st.info(
            ui.TEXT['no_diagnostics']
        )
        return
    _render_table(
        build_diagnostics_display(result.diagnostics), "diagnostics",
        height=min(520, 44 * (len(result.diagnostics) + 1)),
    )


def _render_download() -> None:
    st.subheader(ui.TEXT['excel'])
    report_error = st.session_state.report_error
    if report_error is not None:
        _render_error(report_error)
        return
    excel_bytes = st.session_state.excel_bytes
    if excel_bytes is None:
        st.info(ui.TEXT['no_excel'])
        return
    st.download_button(
        ui.TEXT['download_excel'],
        data=excel_bytes,
        file_name=st.session_state.download_filename,
        mime=DOWNLOAD_MIME,
        key="download_excel_report",
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
        ui.TEXT['intro']
    )

    st.subheader(ui.TEXT['upload_section'])
    uploaded_file = st.file_uploader(
        ui.TEXT['upload'],
        type=["csv", "xlsx"],
        accept_multiple_files=False,
        help=ui.TEXT['upload_help'],
        key="operations_data_upload",
    )
    group_label = st.selectbox(
        ui.TEXT['analysis_level'],
        options=tuple(GROUP_BY_OPTIONS),
        format_func=ui.GROUP_LABELS.__getitem__,
        key="analysis_level",
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
        ui.TEXT['run'],
        type="primary",
        disabled=uploaded_file is None,
        key="run_analysis",
    )
    if run_clicked and uploaded_file is not None and file_bytes is not None:
        _clear_analysis_state()
        st.session_state.analysis_signature = current_signature
        with st.spinner(ui.TEXT['analyzing']):
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
        st.info(ui.TEXT['no_upload'])
        return

    analysis_error = st.session_state.analysis_error
    result = st.session_state.pipeline_result
    if analysis_error is not None:
        _render_error(analysis_error)
        return
    if result is None:
        st.info(ui.TEXT['ready'])
        return

    _render_validation(result, st.session_state.report_data)
    _render_metrics(result, group_label)
    _render_diagnostics(result)
    _render_download()
    _render_ai_section(result)


if __name__ == "__main__":
    main()
