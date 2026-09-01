"""Execute audited Provider retries without changing the current App path."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from src.insight_attempt_audit import (
    ATTEMPT_AUDIT_VERSION,
    FAILED,
    MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS,
    SUCCEEDED,
    AttemptAuditTrail,
    ProviderAttemptAudit,
    build_failed_attempt_audit,
    build_succeeded_attempt_audit,
)
from src.insight_cost_audit import CostAuditMetadata, build_cost_audit_metadata
from src.insight_prompt import InsightOutput, InsightOutputError
from src.insight_provider import (
    InsightProvider,
    InsightProviderError,
    ProviderUsage,
    generate_insight_with_metadata,
)
from src.insight_retry import (
    DEFAULT_RETRY_POLICY,
    RETRY,
    RetryPolicy,
    evaluate_retry,
)
from src.insight_retry_delay import (
    DEFAULT_RETRY_DELAY_POLICY,
    RetryDelayPolicy,
    resolve_retry_delay,
)
from src.insight_retry_delay_execution import (
    RETRY_DELAY_EXECUTION_VERSION,
    RetryDelayExecutionAudit,
    RetryDelayExecutionRecord,
)
from src.insights import InsightContext

RETRY_EXECUTION_VERSION = "2"
INVALID_RETRY_EXECUTION = "INVALID_RETRY_EXECUTION"


class RetryExecutionError(RuntimeError):
    """A stable hard failure at the retry-execution contract boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_execution(message: str) -> RetryExecutionError:
    return RetryExecutionError(INVALID_RETRY_EXECUTION, message)


def _validate_optional_result_types(
    *,
    output: object,
    final_usage: object,
    final_cost: object,
) -> None:
    if output is not None and not isinstance(output, InsightOutput):
        raise _invalid_execution("output must be InsightOutput or None.")
    if final_usage is not None and not isinstance(final_usage, ProviderUsage):
        raise _invalid_execution("final_usage must be ProviderUsage or None.")
    if final_cost is not None and not isinstance(final_cost, CostAuditMetadata):
        raise _invalid_execution(
            "final_cost must be CostAuditMetadata or None."
        )


@dataclass(frozen=True)
class RetryExecutionResult:
    """One completed logical generation and its ordered attempt audit trail."""

    version: str
    status: str
    output: InsightOutput | None
    final_usage: ProviderUsage | None
    final_cost: CostAuditMetadata | None
    attempt_audit: AttemptAuditTrail
    delay_audit: RetryDelayExecutionAudit
    error_code: str | None

    def __post_init__(self) -> None:
        if self.version != RETRY_EXECUTION_VERSION:
            raise _invalid_execution(
                "Retry execution version does not match the current contract."
            )
        if self.status not in {SUCCEEDED, FAILED}:
            raise _invalid_execution("status must be succeeded or failed.")
        _validate_optional_result_types(
            output=self.output,
            final_usage=self.final_usage,
            final_cost=self.final_cost,
        )
        if not isinstance(self.attempt_audit, AttemptAuditTrail):
            raise _invalid_execution(
                "attempt_audit must be AttemptAuditTrail."
            )
        if not isinstance(self.delay_audit, RetryDelayExecutionAudit):
            raise _invalid_execution(
                "delay_audit must be RetryDelayExecutionAudit."
            )

        attempts = self.attempt_audit.attempts
        delay_records = self.delay_audit.records
        if len(delay_records) != len(attempts) - 1:
            raise _invalid_execution(
                "delay_audit must contain one record per completed retry transition."
            )
        for position, delay_record in enumerate(delay_records, start=1):
            attempt = attempts[position - 1]
            retry_decision = attempt.retry_decision
            if attempt.status != FAILED:
                raise _invalid_execution(
                    "A delay transition must follow a failed attempt."
                )
            if retry_decision is None or retry_decision.action != RETRY:
                raise _invalid_execution(
                    "A delay transition requires a retry decision."
                )
            if delay_record.after_attempt_number != position:
                raise _invalid_execution(
                    "Delay transition attempt numbers must match the attempt trail."
                )
            if delay_record.delay_decision.error_code != attempt.error_code:
                raise _invalid_execution(
                    "Delay and attempt error codes must match."
                )
            if (
                delay_record.delay_decision.error_code
                != retry_decision.error_code
            ):
                raise _invalid_execution(
                    "Delay and retry decision error codes must match."
                )

        final_attempt = attempts[-1]
        if self.status == SUCCEEDED:
            if self.attempt_audit.outcome != SUCCEEDED:
                raise _invalid_execution(
                    "A succeeded result requires a succeeded attempt trail."
                )
            if not isinstance(self.output, InsightOutput):
                raise _invalid_execution(
                    "A succeeded result requires InsightOutput."
                )
            if not isinstance(self.final_cost, CostAuditMetadata):
                raise _invalid_execution(
                    "A succeeded result requires final CostAuditMetadata."
                )
            if self.error_code is not None:
                raise _invalid_execution(
                    "A succeeded result cannot contain error_code."
                )
            if final_attempt.status != SUCCEEDED:
                raise _invalid_execution(
                    "A succeeded result must end with a succeeded attempt."
                )
            if self.final_usage != final_attempt.usage:
                raise _invalid_execution(
                    "final_usage must match the final succeeded attempt."
                )
            if self.final_cost != final_attempt.cost:
                raise _invalid_execution(
                    "final_cost must match the final succeeded attempt."
                )
            return

        if self.attempt_audit.outcome != FAILED:
            raise _invalid_execution(
                "A failed result requires a failed attempt trail."
            )
        if self.output is not None:
            raise _invalid_execution("A failed result cannot contain output.")
        if self.final_usage is not None or self.final_cost is not None:
            raise _invalid_execution(
                "A failed result cannot contain final usage or cost."
            )
        if not isinstance(self.error_code, str) or not self.error_code.strip():
            raise _invalid_execution(
                "A failed result requires a nonblank error_code."
            )
        if final_attempt.status != FAILED:
            raise _invalid_execution(
                "A failed result must end with a failed attempt."
            )
        if self.error_code != final_attempt.error_code:
            raise _invalid_execution(
                "error_code must match the final failed attempt."
            )


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_identity(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_execution(f"{field_name} must be a nonblank string.")
    return value


def _resolve_provider_identity(provider: object) -> tuple[str, str]:
    """Resolve one auditable adapter identity before any attempt begins."""

    try:
        generate = getattr(provider, "generate", None)
    except Exception:
        raise _invalid_execution(
            "provider must expose a callable generate method."
        ) from None
    if not callable(generate):
        raise _invalid_execution(
            "provider must expose a callable generate method."
        )

    try:
        provider_name = getattr(provider, "provider_name", None)
        model = getattr(provider, "model", None)
    except Exception:
        raise _invalid_execution(
            "provider identity could not be resolved."
        ) from None

    provider_name = _validate_identity(
        provider_name,
        field_name="provider.provider_name",
    )
    model = _validate_identity(model, field_name="provider.model")
    return provider_name, model


def _validate_reference_at(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise _invalid_execution(
            "utc_now must return a timezone-aware datetime."
        )
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError, OverflowError):
        raise _invalid_execution(
            "utc_now must return a timezone-aware datetime."
        ) from None
    if value.tzinfo is None or offset is None:
        raise _invalid_execution(
            "utc_now must return a timezone-aware datetime."
        )
    return value


def _active_policy(policy: RetryPolicy | None) -> RetryPolicy:
    if policy is None:
        return DEFAULT_RETRY_POLICY
    if not isinstance(policy, RetryPolicy):
        raise _invalid_execution("retry_policy must be RetryPolicy or None.")
    return policy


def _active_delay_policy(
    policy: RetryDelayPolicy | None,
) -> RetryDelayPolicy:
    if policy is None:
        return DEFAULT_RETRY_DELAY_POLICY
    if not isinstance(policy, RetryDelayPolicy):
        raise _invalid_execution(
            "retry_delay_policy must be RetryDelayPolicy or None."
        )
    return policy


def _sleep_ms(delay_ms: int) -> None:
    """Execute the runtime wait adapter in seconds."""

    time.sleep(delay_ms / 1_000.0)


def _active_sleeper(
    sleeper: Callable[[int], None] | None,
) -> Callable[[int], None]:
    if sleeper is None:
        return _sleep_ms
    if not callable(sleeper):
        raise _invalid_execution("sleeper must be callable or None.")
    return sleeper


def _validate_audit_compatibility(policy: RetryPolicy) -> None:
    max_audit_integer = (
        10**MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS - 1
    )
    if policy.max_attempts > max_audit_integer:
        raise _invalid_execution(
            "retry_policy.max_attempts exceeds the Attempt Audit boundary."
        )


def _attempt_trail(
    *,
    policy: RetryPolicy,
    outcome: str,
    attempts: list[ProviderAttemptAudit],
) -> AttemptAuditTrail:
    try:
        return AttemptAuditTrail(
            version=ATTEMPT_AUDIT_VERSION,
            retry_policy_version=policy.version,
            max_attempts=policy.max_attempts,
            outcome=outcome,
            attempts=tuple(attempts),
        )
    except Exception:
        raise _invalid_execution(
            "Retry execution could not build a valid attempt trail."
        ) from None


def _delay_execution_audit(
    *,
    policy: RetryDelayPolicy,
    records: list[RetryDelayExecutionRecord],
) -> RetryDelayExecutionAudit:
    try:
        return RetryDelayExecutionAudit(
            version=RETRY_DELAY_EXECUTION_VERSION,
            policy_version=policy.version,
            records=tuple(records),
        )
    except Exception:
        raise _invalid_execution(
            "Retry execution could not build a valid delay audit."
        ) from None


def execute_insight_generation_with_retry(
    context: InsightContext,
    *,
    provider: InsightProvider,
    retry_policy: RetryPolicy | None = None,
    retry_delay_policy: RetryDelayPolicy | None = None,
    utc_now: Callable[[], datetime] | None = None,
    sleeper: Callable[[int], None] | None = None,
) -> RetryExecutionResult:
    """Execute one logical generation under an explicit retry policy.

    This standalone core waits between approved retry attempts. It does not
    select providers, update receipts, or participate in the current
    Streamlit generation path.
    """

    active_policy = _active_policy(retry_policy)
    _validate_audit_compatibility(active_policy)
    active_delay_policy = _active_delay_policy(retry_delay_policy)
    if utc_now is not None and not callable(utc_now):
        raise _invalid_execution("utc_now must be callable or None.")
    active_sleeper = _active_sleeper(sleeper)
    provider_name, model = _resolve_provider_identity(provider)

    clock = _default_utc_now if utc_now is None else utc_now
    attempts: list[ProviderAttemptAudit] = []
    delay_records: list[RetryDelayExecutionRecord] = []
    attempt_number = 1

    while True:
        try:
            pricing_reference_at = _validate_reference_at(clock())
        except RetryExecutionError:
            raise
        except Exception:
            raise _invalid_execution(
                "Retry execution could not capture a valid UTC reference."
            ) from None

        try:
            generation = generate_insight_with_metadata(
                context,
                provider=provider,
            )
        except (InsightProviderError, InsightOutputError) as error:
            try:
                decision = evaluate_retry(
                    error_code=error.code,
                    attempts_completed=attempt_number,
                    policy=active_policy,
                )
                failed_attempt = build_failed_attempt_audit(
                    attempt_number=attempt_number,
                    provider=provider_name,
                    model=model,
                    pricing_reference_at=pricing_reference_at,
                    error_code=error.code,
                    retry_decision=decision,
                )
            except Exception:
                raise _invalid_execution(
                    "Retry execution could not audit a failed attempt."
                ) from None

            attempts.append(failed_attempt)
            if decision.action == RETRY:
                try:
                    delay_decision = resolve_retry_delay(
                        retry_decision=decision,
                        policy=active_delay_policy,
                    )
                except Exception:
                    raise _invalid_execution(
                        "Retry execution could not resolve a retry delay."
                    ) from None
                try:
                    active_sleeper(delay_decision.delay_ms)
                except Exception:
                    raise _invalid_execution(
                        "Retry execution sleeper failed."
                    ) from None
                try:
                    delay_record = RetryDelayExecutionRecord(
                        version=RETRY_DELAY_EXECUTION_VERSION,
                        after_attempt_number=attempt_number,
                        delay_decision=delay_decision,
                    )
                except Exception:
                    raise _invalid_execution(
                        "Retry execution could not record a completed delay."
                    ) from None
                delay_records.append(delay_record)
                attempt_number += 1
                continue

            trail = _attempt_trail(
                policy=active_policy,
                outcome=FAILED,
                attempts=attempts,
            )
            delay_audit = _delay_execution_audit(
                policy=active_delay_policy,
                records=delay_records,
            )
            return RetryExecutionResult(
                version=RETRY_EXECUTION_VERSION,
                status=FAILED,
                output=None,
                final_usage=None,
                final_cost=None,
                attempt_audit=trail,
                delay_audit=delay_audit,
                error_code=error.code,
            )
        except Exception:
            raise _invalid_execution(
                "Retry execution encountered an unexpected generation failure."
            ) from None

        try:
            final_cost = build_cost_audit_metadata(
                generation.usage,
                provider=provider_name,
                model=model,
                pricing_reference_at=pricing_reference_at,
            )
            succeeded_attempt = build_succeeded_attempt_audit(
                attempt_number=attempt_number,
                provider=provider_name,
                model=model,
                pricing_reference_at=pricing_reference_at,
                usage=generation.usage,
                cost=final_cost,
            )
        except Exception:
            raise _invalid_execution(
                "Retry execution could not audit a successful attempt."
            ) from None

        attempts.append(succeeded_attempt)
        trail = _attempt_trail(
            policy=active_policy,
            outcome=SUCCEEDED,
            attempts=attempts,
        )
        delay_audit = _delay_execution_audit(
            policy=active_delay_policy,
            records=delay_records,
        )
        return RetryExecutionResult(
            version=RETRY_EXECUTION_VERSION,
            status=SUCCEEDED,
            output=generation.output,
            final_usage=generation.usage,
            final_cost=final_cost,
            attempt_audit=trail,
            delay_audit=delay_audit,
            error_code=None,
        )
