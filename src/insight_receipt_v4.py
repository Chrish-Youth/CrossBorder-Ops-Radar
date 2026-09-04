"""Retry-aware Receipt V4 for one successful logical AI generation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from src.insight_attempt_audit import FAILED, SUCCEEDED, AttemptAuditTrail
from src.insight_cost_audit import CostAuditMetadata
from src.insight_logical_generation_cost import (
    LogicalGenerationCostSummary,
    _summary_from_execution_facts,
    build_logical_generation_cost_summary,
)
from src.insight_prompt import INSIGHT_PROMPT_VERSION
from src.insight_provider import ProviderUsage
from src.insight_receipt import (
    INSIGHT_RECEIPT_VERSION,
    MAX_RECEIPT_TOKEN_DECIMAL_DIGITS,
    InsightGenerationReceipt,
    InsightReceiptError,
    _normalize_group_by,
)
from src.insight_retry import RETRY
from src.insight_retry_delay_execution import RetryDelayExecutionAudit
from src.insight_retry_execution import (
    RETRY_EXECUTION_VERSION,
    RetryExecutionResult,
)
from src.insights import INSIGHT_CONTEXT_VERSION, InsightContext

INSIGHT_RECEIPT_V4_VERSION = "4"
INVALID_RECEIPT_V4_INPUT = "INVALID_RECEIPT_V4_INPUT"
MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS = (
    MAX_RECEIPT_TOKEN_DECIMAL_DIGITS
)

_MAX_RECEIPT_V4_JSON_INTEGER = (
    10**MAX_RECEIPT_V4_INTEGER_DECIMAL_DIGITS - 1
)


class InsightReceiptV4Error(ValueError):
    """A stable failure at the retry-aware receipt boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_receipt_v4(message: str) -> InsightReceiptV4Error:
    return InsightReceiptV4Error(INVALID_RECEIPT_V4_INPUT, message)


def _validate_delay_json_integer(
    value: object,
    *,
    field_name: str,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise _invalid_receipt_v4(
            f"{field_name} must be an integer greater than or equal to 1."
        )
    _validate_json_integer_upper_bound(
        value,
        boundary_message=(
            "Delay provenance contains an integer outside the Receipt V4 "
            "JSON boundary."
        ),
    )


def _validate_json_integer_upper_bound(
    value: int,
    *,
    boundary_message: str,
) -> None:
    """Enforce the shared V4 JSON integer bound without string conversion."""

    if value > _MAX_RECEIPT_V4_JSON_INTEGER:
        raise _invalid_receipt_v4(boundary_message)


def _validate_top_level_count_json_integer(
    value: object,
    *,
    field_name: str,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise _invalid_receipt_v4(
            f"{field_name} must be a non-negative integer."
        )
    _validate_json_integer_upper_bound(
        value,
        boundary_message=(
            "Receipt V4 top-level count exceeds the JSON integer boundary."
        ),
    )


def _validate_delay_audit_for_receipt(
    *,
    attempt_audit: AttemptAuditTrail,
    delay_audit: RetryDelayExecutionAudit,
) -> None:
    attempts = attempt_audit.attempts
    records = delay_audit.records
    if len(records) != len(attempts) - 1:
        raise _invalid_receipt_v4(
            "Delay records must equal completed attempts minus one."
        )

    for position, record in enumerate(records, start=1):
        decision = record.delay_decision
        _validate_delay_json_integer(
            record.after_attempt_number,
            field_name="delay record after_attempt_number",
        )
        _validate_delay_json_integer(
            decision.attempts_completed,
            field_name="delay decision attempts_completed",
        )
        _validate_delay_json_integer(
            decision.delay_ms,
            field_name="delay decision delay_ms",
        )

        attempt = attempts[position - 1]
        retry_decision = attempt.retry_decision
        if (
            attempt.status != FAILED
            or retry_decision is None
            or retry_decision.action != RETRY
        ):
            raise _invalid_receipt_v4(
                "Each delay must follow a failed retryable attempt."
            )
        if record.after_attempt_number != attempt.attempt_number:
            raise _invalid_receipt_v4(
                "Delay and attempt numbers must match."
            )
        if (
            decision.error_code != attempt.error_code
            or decision.error_code != retry_decision.error_code
        ):
            raise _invalid_receipt_v4(
                "Delay, attempt, and retry error codes must match."
            )


@dataclass(frozen=True)
class InsightGenerationReceiptV4:
    """Immutable receipt for one succeeded retry-aware execution."""

    version: str
    generated_at: str
    analysis_signature: str
    group_by: tuple[str, ...]
    context_version: str
    prompt_version: str
    output_version: str
    provider: str
    model: str
    metric_record_count: int
    diagnostic_signal_count: int
    priority_insight_count: int
    cost: CostAuditMetadata
    usage: ProviderUsage | None
    attempt_audit: AttemptAuditTrail
    delay_audit: RetryDelayExecutionAudit
    logical_generation_cost: LogicalGenerationCostSummary

    def __post_init__(self) -> None:
        if self.version != INSIGHT_RECEIPT_V4_VERSION:
            raise _invalid_receipt_v4(
                "Receipt V4 version does not match the current contract."
            )
        self._validated_v3_base_receipt()
        for field_name in (
            "metric_record_count",
            "diagnostic_signal_count",
            "priority_insight_count",
        ):
            _validate_top_level_count_json_integer(
                getattr(self, field_name),
                field_name=field_name,
            )
        if not isinstance(self.attempt_audit, AttemptAuditTrail):
            raise _invalid_receipt_v4(
                "attempt_audit must be AttemptAuditTrail."
            )
        if self.attempt_audit.outcome != SUCCEEDED:
            raise _invalid_receipt_v4(
                "Receipt V4 requires a succeeded AttemptAuditTrail."
            )
        if not isinstance(self.delay_audit, RetryDelayExecutionAudit):
            raise _invalid_receipt_v4(
                "delay_audit must be RetryDelayExecutionAudit."
            )
        if not isinstance(
            self.logical_generation_cost,
            LogicalGenerationCostSummary,
        ):
            raise _invalid_receipt_v4(
                "logical_generation_cost must be LogicalGenerationCostSummary."
            )

        final_attempt = self.attempt_audit.attempts[-1]
        if final_attempt.status != SUCCEEDED:
            raise _invalid_receipt_v4(
                "Receipt V4 must end with a succeeded Provider attempt."
            )
        if self.provider != final_attempt.provider:
            raise _invalid_receipt_v4(
                "Receipt provider must match the final successful attempt."
            )
        if self.model != final_attempt.model:
            raise _invalid_receipt_v4(
                "Receipt model must match the final successful attempt."
            )
        if self.usage != final_attempt.usage:
            raise _invalid_receipt_v4(
                "Receipt usage must match the final successful attempt."
            )
        if self.cost != final_attempt.cost:
            raise _invalid_receipt_v4(
                "Receipt cost must match the final successful attempt."
            )

        _validate_delay_audit_for_receipt(
            attempt_audit=self.attempt_audit,
            delay_audit=self.delay_audit,
        )
        expected_summary = _summary_from_execution_facts(
            attempt_count=len(self.attempt_audit.attempts),
            final_cost=self.cost,
        )
        if self.logical_generation_cost != expected_summary:
            raise _invalid_receipt_v4(
                "Logical-generation cost summary contradicts execution facts."
            )

    def _validated_v3_base_receipt(self) -> InsightGenerationReceipt:
        """Reuse the sealed V3 domain validator for the original fields."""

        try:
            return InsightGenerationReceipt(
                version=INSIGHT_RECEIPT_VERSION,
                generated_at=self.generated_at,
                analysis_signature=self.analysis_signature,
                group_by=self.group_by,
                context_version=self.context_version,
                prompt_version=self.prompt_version,
                output_version=self.output_version,
                provider=self.provider,
                model=self.model,
                metric_record_count=self.metric_record_count,
                diagnostic_signal_count=self.diagnostic_signal_count,
                priority_insight_count=self.priority_insight_count,
                cost=self.cost,
                usage=self.usage,
            )
        except InsightReceiptError:
            raise _invalid_receipt_v4(
                "Receipt V4 base metadata violates the sealed V3 contract."
            ) from None

    def to_dict(self) -> dict[str, Any]:
        """Return an explicit, fresh, strict-JSON-compatible mapping."""

        base = self._validated_v3_base_receipt().to_dict()
        return {
            "version": self.version,
            "generated_at": base["generated_at"],
            "analysis_signature": base["analysis_signature"],
            "group_by": base["group_by"],
            "context_version": base["context_version"],
            "prompt_version": base["prompt_version"],
            "output_version": base["output_version"],
            "provider": base["provider"],
            "model": base["model"],
            "metric_record_count": base["metric_record_count"],
            "diagnostic_signal_count": base["diagnostic_signal_count"],
            "priority_insight_count": base["priority_insight_count"],
            "usage": base["usage"],
            "cost": base["cost"],
            "attempt_audit": self.attempt_audit.to_dict(),
            "delay_audit": _delay_audit_to_dict(self.delay_audit),
            "logical_generation_cost": self.logical_generation_cost.to_dict(),
        }


def _delay_audit_to_dict(
    audit: RetryDelayExecutionAudit,
) -> dict[str, Any]:
    """Serialize the sealed non-persistent Delay Audit explicitly."""

    return {
        "version": audit.version,
        "policy_version": audit.policy_version,
        "records": [
            {
                "version": record.version,
                "after_attempt_number": record.after_attempt_number,
                "delay_decision": {
                    "policy_version": record.delay_decision.policy_version,
                    "error_code": record.delay_decision.error_code,
                    "attempts_completed": (
                        record.delay_decision.attempts_completed
                    ),
                    "delay_ms": record.delay_decision.delay_ms,
                },
            }
            for record in audit.records
        ],
    }


def build_insight_receipt_v4(
    *,
    generated_at: str,
    analysis_signature: str,
    group_by: Sequence[str] | None,
    context: InsightContext,
    execution_result: RetryExecutionResult,
) -> InsightGenerationReceiptV4:
    """Build V4 only from one succeeded Retry Execution V2 result."""

    if not isinstance(context, InsightContext):
        raise _invalid_receipt_v4("context must be InsightContext.")
    if context.version != INSIGHT_CONTEXT_VERSION:
        raise _invalid_receipt_v4(
            "context uses an unsupported contract version."
        )
    if not isinstance(execution_result, RetryExecutionResult):
        raise _invalid_receipt_v4(
            "execution_result must be RetryExecutionResult."
        )
    if execution_result.version != RETRY_EXECUTION_VERSION:
        raise _invalid_receipt_v4(
            "execution_result uses an unsupported contract version."
        )
    if execution_result.status != SUCCEEDED:
        raise _invalid_receipt_v4(
            "Receipt V4 requires a succeeded Retry Execution result."
        )
    output = execution_result.output
    final_cost = execution_result.final_cost
    if output is None or not isinstance(final_cost, CostAuditMetadata):
        raise _invalid_receipt_v4(
            "Succeeded execution is missing final output or cost."
        )
    try:
        normalized_group_by = _normalize_group_by(group_by)
    except InsightReceiptError:
        raise _invalid_receipt_v4(
            "group_by violates the sealed Receipt contract."
        ) from None

    final_attempt = execution_result.attempt_audit.attempts[-1]
    summary = build_logical_generation_cost_summary(execution_result)
    return InsightGenerationReceiptV4(
        version=INSIGHT_RECEIPT_V4_VERSION,
        generated_at=generated_at,
        analysis_signature=analysis_signature,
        group_by=normalized_group_by,
        context_version=context.version,
        prompt_version=INSIGHT_PROMPT_VERSION,
        output_version=output.version,
        provider=final_attempt.provider,
        model=final_attempt.model,
        metric_record_count=len(context.metric_records),
        diagnostic_signal_count=len(context.diagnostic_signals),
        priority_insight_count=len(output.priority_insights),
        cost=final_cost,
        usage=execution_result.final_usage,
        attempt_audit=execution_result.attempt_audit,
        delay_audit=execution_result.delay_audit,
        logical_generation_cost=summary,
    )
