"""Truthful cost status for one completed logical AI generation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.insight_attempt_audit import SUCCEEDED
from src.insight_cost_audit import AVAILABLE, CostAuditMetadata
from src.insight_retry_execution import RetryExecutionResult

LOGICAL_GENERATION_COST_SUMMARY_VERSION = "1"
INVALID_LOGICAL_GENERATION_COST = "INVALID_LOGICAL_GENERATION_COST"

FULLY_ESTIMATED = "fully_estimated"
UNKNOWN_TOTAL = "unknown_total"
UNAVAILABLE = "unavailable"

PRIOR_FAILED_ATTEMPT_COST_UNKNOWN = (
    "PRIOR_FAILED_ATTEMPT_COST_UNKNOWN"
)
FINAL_ATTEMPT_COST_UNAVAILABLE = "FINAL_ATTEMPT_COST_UNAVAILABLE"


class LogicalGenerationCostError(ValueError):
    """A stable failure at the logical-generation cost boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_logical_cost(message: str) -> LogicalGenerationCostError:
    return LogicalGenerationCostError(
        INVALID_LOGICAL_GENERATION_COST,
        message,
    )


def _validate_amount(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise _invalid_logical_cost(
            "estimated_total_cost_usd must be Decimal or None."
        )
    if not value.is_finite() or value < 0:
        raise _invalid_logical_cost(
            "estimated_total_cost_usd must be finite and non-negative."
        )
    return value


@dataclass(frozen=True)
class LogicalGenerationCostSummary:
    """Whether every represented Provider attempt has an estimated cost."""

    version: str
    status: str
    estimated_total_cost_usd: Decimal | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.version != LOGICAL_GENERATION_COST_SUMMARY_VERSION:
            raise _invalid_logical_cost(
                "Logical-generation cost version does not match the current contract."
            )
        if self.status == FULLY_ESTIMATED:
            _validate_amount(self.estimated_total_cost_usd)
            if self.reason is not None:
                raise _invalid_logical_cost(
                    "fully_estimated cannot contain a reason."
                )
            return
        if self.status == UNKNOWN_TOTAL:
            if self.estimated_total_cost_usd is not None:
                raise _invalid_logical_cost(
                    "unknown_total cannot contain an estimated amount."
                )
            if self.reason != PRIOR_FAILED_ATTEMPT_COST_UNKNOWN:
                raise _invalid_logical_cost(
                    "unknown_total requires the prior-failed-attempt reason."
                )
            return
        if self.status == UNAVAILABLE:
            if self.estimated_total_cost_usd is not None:
                raise _invalid_logical_cost(
                    "unavailable cannot contain an estimated amount."
                )
            if self.reason != FINAL_ATTEMPT_COST_UNAVAILABLE:
                raise _invalid_logical_cost(
                    "unavailable requires the final-attempt-unavailable reason."
                )
            return
        raise _invalid_logical_cost(
            "status must be fully_estimated, unknown_total, or unavailable."
        )

    def to_dict(self) -> dict[str, Any]:
        """Return an explicit, fresh, JSON-safe public representation."""

        return {
            "version": self.version,
            "status": self.status,
            "estimated_total_cost_usd": (
                None
                if self.estimated_total_cost_usd is None
                else format(self.estimated_total_cost_usd, "f")
            ),
            "reason": self.reason,
        }


def _summary_from_execution_facts(
    *,
    attempt_count: int,
    final_cost: CostAuditMetadata,
) -> LogicalGenerationCostSummary:
    """Derive the summary without estimating or aggregating attempt costs."""

    if attempt_count > 1:
        return LogicalGenerationCostSummary(
            version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
            status=UNKNOWN_TOTAL,
            estimated_total_cost_usd=None,
            reason=PRIOR_FAILED_ATTEMPT_COST_UNKNOWN,
        )
    if final_cost.status == AVAILABLE:
        estimate = final_cost.estimate
        if estimate is None:
            raise _invalid_logical_cost(
                "An available final cost must contain an estimate."
            )
        return LogicalGenerationCostSummary(
            version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
            status=FULLY_ESTIMATED,
            estimated_total_cost_usd=estimate.total_estimated_cost,
            reason=None,
        )
    return LogicalGenerationCostSummary(
        version=LOGICAL_GENERATION_COST_SUMMARY_VERSION,
        status=UNAVAILABLE,
        estimated_total_cost_usd=None,
        reason=FINAL_ATTEMPT_COST_UNAVAILABLE,
    )


def build_logical_generation_cost_summary(
    execution_result: RetryExecutionResult,
) -> LogicalGenerationCostSummary:
    """Summarize existing execution facts without recomputing any cost."""

    if not isinstance(execution_result, RetryExecutionResult):
        raise _invalid_logical_cost(
            "execution_result must be RetryExecutionResult."
        )
    if execution_result.status != SUCCEEDED:
        raise _invalid_logical_cost(
            "A logical-generation cost summary requires a succeeded execution."
        )
    final_cost = execution_result.final_cost
    if not isinstance(final_cost, CostAuditMetadata):
        raise _invalid_logical_cost(
            "A succeeded execution requires final CostAuditMetadata."
        )
    return _summary_from_execution_facts(
        attempt_count=len(execution_result.attempt_audit.attempts),
        final_cost=final_cost,
    )
