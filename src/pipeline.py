"""Sequential orchestration for the deterministic analysis stages."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

import pandas as pd

from src.diagnostics import diagnose_metrics
from src.loader import FileSource, load_file
from src.metrics import calculate_metrics
from src.validator import ValidationResult, validate_dataframe

INVALID_STAGE_RESULT = "INVALID_STAGE_RESULT"

_StageResult = TypeVar("_StageResult")


class PipelineStatus(StrEnum):
    """Stable V1 outcomes returned without replacing downstream exceptions."""

    SUCCESS = "SUCCESS"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class PipelineError(Exception):
    """A stable failure caused by a Pipeline-owned contract violation."""

    def __init__(self, code: str, stage: str, message: str) -> None:
        self.code = code
        self.stage = stage
        self.message = message
        super().__init__(f"{code} [{stage}]: {message}")


@dataclass(frozen=True)
class PipelineResult:
    """Validation and downstream results from one complete pipeline run."""

    status: PipelineStatus
    validation: ValidationResult
    metrics: pd.DataFrame | None
    diagnostics: pd.DataFrame | None


def _assert_stage_result(
    result: object,
    expected_type: type[_StageResult],
    *,
    stage: str,
    producer: str,
) -> _StageResult:
    """Return a stage result after checking only its public result type."""

    if not isinstance(result, expected_type):
        raise PipelineError(
            code=INVALID_STAGE_RESULT,
            stage=stage,
            message=(
                f"{producer} 返回了无效结果类型；预期 "
                f"{expected_type.__name__}，实际 {type(result).__name__}。"
            ),
        )
    return result


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
    validation = _assert_stage_result(
        validate_dataframe(raw_data),
        ValidationResult,
        stage="validation",
        producer="validate_dataframe",
    )

    if validation.report.has_fatal_errors:
        return PipelineResult(
            status=PipelineStatus.VALIDATION_FAILED,
            validation=validation,
            metrics=None,
            diagnostics=None,
        )

    metrics = _assert_stage_result(
        calculate_metrics(validation.clean_data, group_by=group_by),
        pd.DataFrame,
        stage="metrics",
        producer="calculate_metrics",
    )
    diagnostics = _assert_stage_result(
        diagnose_metrics(metrics),
        pd.DataFrame,
        stage="diagnostics",
        producer="diagnose_metrics",
    )
    return PipelineResult(
        status=PipelineStatus.SUCCESS,
        validation=validation,
        metrics=metrics,
        diagnostics=diagnostics,
    )
