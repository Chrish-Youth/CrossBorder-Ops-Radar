"""Sequential orchestration for the deterministic analysis stages."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from src.diagnostics import diagnose_metrics
from src.loader import FileSource, load_file
from src.metrics import calculate_metrics
from src.validator import ValidationResult, validate_dataframe


class PipelineStatus(StrEnum):
    """Stable V1 outcomes returned without replacing downstream exceptions."""

    SUCCESS = "SUCCESS"
    VALIDATION_FAILED = "VALIDATION_FAILED"


@dataclass(frozen=True)
class PipelineResult:
    """Validation and downstream results from one complete pipeline run."""

    status: PipelineStatus
    validation: ValidationResult
    metrics: pd.DataFrame | None
    diagnostics: pd.DataFrame | None


def run_pipeline(
    source: FileSource,
    *,
    filename: str | None = None,
    group_by: str | Sequence[str] | None = None,
) -> PipelineResult:
    """Run Loader, Validator, Metrics, and Diagnostics in contract order.

    Loader, Metrics, and Diagnostics exceptions intentionally propagate without
    wrapping. A fatal Validation result is the only expected short-circuit that
    is represented as a ``PipelineResult``.
    """

    raw_data = load_file(source, filename=filename)
    validation = validate_dataframe(raw_data)

    if validation.report.has_fatal_errors:
        return PipelineResult(
            status=PipelineStatus.VALIDATION_FAILED,
            validation=validation,
            metrics=None,
            diagnostics=None,
        )

    metrics = calculate_metrics(validation.clean_data, group_by=group_by)
    diagnostics = diagnose_metrics(metrics)
    return PipelineResult(
        status=PipelineStatus.SUCCESS,
        validation=validation,
        metrics=metrics,
        diagnostics=diagnostics,
    )
