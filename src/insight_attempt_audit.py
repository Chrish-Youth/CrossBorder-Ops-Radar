"""Immutable attempt-level provenance for completed Provider invocations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from src.insight_cost_audit import (
    CostAuditMetadata,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    USAGE_UNAVAILABLE as COST_REASON_USAGE_UNAVAILABLE,
)
from src.insight_provider import ProviderUsage
from src.insight_retry import DO_NOT_RETRY, RETRY, RetryDecision

ATTEMPT_AUDIT_VERSION = "1"
INVALID_ATTEMPT_AUDIT = "INVALID_ATTEMPT_AUDIT"
MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS = 512

SUCCEEDED = "succeeded"
FAILED = "failed"

USAGE_RECORDED = "recorded"
USAGE_UNAVAILABLE = "unavailable"
USAGE_UNKNOWN = "unknown"

COST_AVAILABLE = "available"
COST_UNAVAILABLE = "unavailable"
COST_UNKNOWN = "unknown"

_ATTEMPT_STATUSES = frozenset({SUCCEEDED, FAILED})
_USAGE_STATUSES = frozenset(
    {USAGE_RECORDED, USAGE_UNAVAILABLE, USAGE_UNKNOWN}
)
_COST_STATUSES = frozenset(
    {COST_AVAILABLE, COST_UNAVAILABLE, COST_UNKNOWN}
)
_STABLE_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MAX_ATTEMPT_AUDIT_JSON_INTEGER = (
    10**MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS - 1
)


class AttemptAuditError(ValueError):
    """A stable failure at the attempt-audit contract boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_attempt_audit(message: str) -> AttemptAuditError:
    return AttemptAuditError(INVALID_ATTEMPT_AUDIT, message)


def _validate_nonblank_string(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_attempt_audit(
            f"{field_name} must be a nonblank string."
        )


def _validate_positive_integer(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid_attempt_audit(
            f"{field_name} must be an integer greater than or equal to 1."
        )


def _validate_json_integer_representability(
    value: object,
    *,
    field_name: str,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid_attempt_audit(
            f"{field_name} must be an integer."
        )
    if value > _MAX_ATTEMPT_AUDIT_JSON_INTEGER:
        raise _invalid_attempt_audit(
            f"{field_name} exceeds the Attempt Audit JSON integer boundary."
        )


def _validate_usage_json_integers(usage: ProviderUsage) -> None:
    fields = (
        (
            "usage.prompt_cache_hit_tokens",
            usage.prompt_cache_hit_tokens,
        ),
        (
            "usage.prompt_cache_miss_tokens",
            usage.prompt_cache_miss_tokens,
        ),
        ("usage.reasoning_tokens", usage.reasoning_tokens),
        ("usage.prompt_tokens", usage.prompt_tokens),
        ("usage.completion_tokens", usage.completion_tokens),
        ("usage.total_tokens", usage.total_tokens),
    )
    for field_name, value in fields:
        if value is not None:
            _validate_json_integer_representability(
                value,
                field_name=field_name,
            )


def _validate_stable_error_code(value: object) -> None:
    _validate_nonblank_string(value, field_name="error_code")
    if _STABLE_ERROR_CODE_PATTERN.fullmatch(value) is None:
        raise _invalid_attempt_audit(
            "error_code must be a stable application error identifier."
        )


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_attempt_audit(
            "pricing_reference_at must be a timezone-aware UTC ISO timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_attempt_audit(
            "pricing_reference_at must be a timezone-aware UTC ISO timestamp."
        ) from exc
    if parsed.tzinfo is None or offset != timedelta(0):
        raise _invalid_attempt_audit(
            "pricing_reference_at must be a timezone-aware UTC ISO timestamp."
        )
    return parsed


def _normalize_reference_at(value: object) -> str:
    if not isinstance(value, datetime):
        raise _invalid_attempt_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        )
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_attempt_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        ) from exc
    if value.tzinfo is None or offset is None:
        raise _invalid_attempt_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        )
    try:
        return value.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_attempt_audit(
            "pricing_reference_at must be a timezone-aware datetime."
        ) from exc


def _usage_to_dict(
    usage: ProviderUsage | None,
) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "prompt_cache_hit_tokens": usage.prompt_cache_hit_tokens,
        "prompt_cache_miss_tokens": usage.prompt_cache_miss_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


def _retry_decision_to_dict(
    decision: RetryDecision | None,
) -> dict[str, str | int] | None:
    if decision is None:
        return None
    return {
        "policy_version": decision.policy_version,
        "error_code": decision.error_code,
        "action": decision.action,
        "reason": decision.reason,
        "attempts_completed": decision.attempts_completed,
        "max_attempts": decision.max_attempts,
    }


@dataclass(frozen=True)
class ProviderAttemptAudit:
    """Audit facts for one completed Provider invocation."""

    version: str
    attempt_number: int
    provider: str
    model: str
    pricing_reference_at: str
    status: str
    error_code: str | None
    retry_decision: RetryDecision | None
    usage_status: str
    usage: ProviderUsage | None
    cost_status: str
    cost: CostAuditMetadata | None

    def __post_init__(self) -> None:
        if self.version != ATTEMPT_AUDIT_VERSION:
            raise _invalid_attempt_audit(
                "Attempt audit version does not match the current contract."
            )
        _validate_positive_integer(
            self.attempt_number,
            field_name="attempt_number",
        )
        _validate_json_integer_representability(
            self.attempt_number,
            field_name="attempt_number",
        )
        _validate_nonblank_string(self.provider, field_name="provider")
        _validate_nonblank_string(self.model, field_name="model")
        _parse_utc_timestamp(self.pricing_reference_at)
        if (
            not isinstance(self.status, str)
            or self.status not in _ATTEMPT_STATUSES
        ):
            raise _invalid_attempt_audit(
                "status must be succeeded or failed."
            )
        if (
            not isinstance(self.usage_status, str)
            or self.usage_status not in _USAGE_STATUSES
        ):
            raise _invalid_attempt_audit(
                "usage_status is not supported."
            )
        if (
            not isinstance(self.cost_status, str)
            or self.cost_status not in _COST_STATUSES
        ):
            raise _invalid_attempt_audit(
                "cost_status is not supported."
            )
        if self.usage is not None and not isinstance(
            self.usage,
            ProviderUsage,
        ):
            raise _invalid_attempt_audit(
                "usage must be ProviderUsage or None."
            )
        if self.usage is not None:
            _validate_usage_json_integers(self.usage)
        if self.cost is not None and not isinstance(
            self.cost,
            CostAuditMetadata,
        ):
            raise _invalid_attempt_audit(
                "cost must be CostAuditMetadata or None."
            )

        if self.status == SUCCEEDED:
            self._validate_succeeded_attempt()
        else:
            self._validate_failed_attempt()

    def _validate_succeeded_attempt(self) -> None:
        if self.error_code is not None or self.retry_decision is not None:
            raise _invalid_attempt_audit(
                "A succeeded attempt cannot contain failure metadata."
            )
        if self.usage_status == USAGE_RECORDED:
            if not isinstance(self.usage, ProviderUsage):
                raise _invalid_attempt_audit(
                    "Recorded usage requires ProviderUsage."
                )
        elif self.usage_status == USAGE_UNAVAILABLE:
            if self.usage is not None:
                raise _invalid_attempt_audit(
                    "Unavailable usage requires usage=None."
                )
        else:
            raise _invalid_attempt_audit(
                "A succeeded attempt cannot have unknown usage."
            )

        if self.cost_status not in {COST_AVAILABLE, COST_UNAVAILABLE}:
            raise _invalid_attempt_audit(
                "A succeeded attempt cannot have unknown cost."
            )
        if not isinstance(self.cost, CostAuditMetadata):
            raise _invalid_attempt_audit(
                "A succeeded attempt requires CostAuditMetadata."
            )
        if self.cost_status != self.cost.status:
            raise _invalid_attempt_audit(
                "Attempt cost status and CostAuditMetadata status differ."
            )
        if self.pricing_reference_at != self.cost.pricing_reference_at:
            raise _invalid_attempt_audit(
                "Attempt and cost pricing reference times differ."
            )

        if self.cost_status == COST_AVAILABLE:
            if self.usage_status != USAGE_RECORDED:
                raise _invalid_attempt_audit(
                    "Available cost requires recorded ProviderUsage."
                )
            estimate = self.cost.estimate
            if estimate is None:
                raise _invalid_attempt_audit(
                    "Available cost requires GenerationCostEstimate."
                )
            if estimate.provider != self.provider or estimate.model != self.model:
                raise _invalid_attempt_audit(
                    "Cost provider/model and attempt provenance differ."
                )
            return

        reason = self.cost.unavailable_reason
        if reason == COST_REASON_USAGE_UNAVAILABLE:
            if self.usage_status != USAGE_UNAVAILABLE:
                raise _invalid_attempt_audit(
                    "USAGE_UNAVAILABLE requires unavailable attempt usage."
                )
        elif reason == CACHE_BREAKDOWN_UNAVAILABLE:
            if self.usage_status != USAGE_RECORDED:
                raise _invalid_attempt_audit(
                    "CACHE_BREAKDOWN_UNAVAILABLE requires recorded usage."
                )
        elif reason not in {POLICY_NOT_EFFECTIVE, POLICY_NOT_APPLICABLE}:
            raise _invalid_attempt_audit(
                "Cost unavailable reason is not supported by attempt audit."
            )

    def _validate_failed_attempt(self) -> None:
        _validate_stable_error_code(self.error_code)
        if not isinstance(self.retry_decision, RetryDecision):
            raise _invalid_attempt_audit(
                "A failed attempt requires RetryDecision."
            )
        _validate_json_integer_representability(
            self.retry_decision.attempts_completed,
            field_name="retry_decision.attempts_completed",
        )
        _validate_json_integer_representability(
            self.retry_decision.max_attempts,
            field_name="retry_decision.max_attempts",
        )
        if self.retry_decision.error_code != self.error_code:
            raise _invalid_attempt_audit(
                "Attempt and RetryDecision error codes differ."
            )
        if self.retry_decision.attempts_completed != self.attempt_number:
            raise _invalid_attempt_audit(
                "RetryDecision attempt count and attempt number differ."
            )
        if self.usage_status != USAGE_UNKNOWN or self.usage is not None:
            raise _invalid_attempt_audit(
                "A failed attempt requires unknown usage and usage=None."
            )
        if self.cost_status != COST_UNKNOWN or self.cost is not None:
            raise _invalid_attempt_audit(
                "A failed attempt requires unknown cost and cost=None."
            )

    def to_dict(self) -> dict[str, Any]:
        """Return an explicit, fresh, JSON-safe public representation."""

        return {
            "version": self.version,
            "attempt_number": self.attempt_number,
            "provider": self.provider,
            "model": self.model,
            "pricing_reference_at": self.pricing_reference_at,
            "status": self.status,
            "error_code": self.error_code,
            "retry_decision": _retry_decision_to_dict(
                self.retry_decision
            ),
            "usage_status": self.usage_status,
            "usage": _usage_to_dict(self.usage),
            "cost_status": self.cost_status,
            "cost": None if self.cost is None else self.cost.to_dict(),
        }


def build_succeeded_attempt_audit(
    *,
    attempt_number: int,
    provider: str,
    model: str,
    pricing_reference_at: datetime,
    usage: ProviderUsage | None,
    cost: CostAuditMetadata,
) -> ProviderAttemptAudit:
    """Build audit facts from one already-completed successful invocation."""

    if usage is not None and not isinstance(usage, ProviderUsage):
        raise _invalid_attempt_audit(
            "usage must be ProviderUsage or None."
        )
    if not isinstance(cost, CostAuditMetadata):
        raise _invalid_attempt_audit(
            "cost must be CostAuditMetadata."
        )
    return ProviderAttemptAudit(
        version=ATTEMPT_AUDIT_VERSION,
        attempt_number=attempt_number,
        provider=provider,
        model=model,
        pricing_reference_at=_normalize_reference_at(
            pricing_reference_at
        ),
        status=SUCCEEDED,
        error_code=None,
        retry_decision=None,
        usage_status=(
            USAGE_UNAVAILABLE if usage is None else USAGE_RECORDED
        ),
        usage=usage,
        cost_status=cost.status,
        cost=cost,
    )


def build_failed_attempt_audit(
    *,
    attempt_number: int,
    provider: str,
    model: str,
    pricing_reference_at: datetime,
    error_code: str,
    retry_decision: RetryDecision,
) -> ProviderAttemptAudit:
    """Build audit facts from one already-completed failed invocation."""

    return ProviderAttemptAudit(
        version=ATTEMPT_AUDIT_VERSION,
        attempt_number=attempt_number,
        provider=provider,
        model=model,
        pricing_reference_at=_normalize_reference_at(
            pricing_reference_at
        ),
        status=FAILED,
        error_code=error_code,
        retry_decision=retry_decision,
        usage_status=USAGE_UNKNOWN,
        usage=None,
        cost_status=COST_UNKNOWN,
        cost=None,
    )


@dataclass(frozen=True)
class AttemptAuditTrail:
    """A completed, ordered sequence of Provider attempt audit facts."""

    version: str
    retry_policy_version: str
    max_attempts: int
    outcome: str
    attempts: tuple[ProviderAttemptAudit, ...]

    def __post_init__(self) -> None:
        if self.version != ATTEMPT_AUDIT_VERSION:
            raise _invalid_attempt_audit(
                "Attempt trail version does not match the current contract."
            )
        _validate_nonblank_string(
            self.retry_policy_version,
            field_name="retry_policy_version",
        )
        _validate_positive_integer(
            self.max_attempts,
            field_name="max_attempts",
        )
        _validate_json_integer_representability(
            self.max_attempts,
            field_name="max_attempts",
        )
        if (
            not isinstance(self.outcome, str)
            or self.outcome not in _ATTEMPT_STATUSES
        ):
            raise _invalid_attempt_audit(
                "outcome must be succeeded or failed."
            )
        if not isinstance(self.attempts, tuple):
            raise _invalid_attempt_audit("attempts must be a tuple.")
        if not self.attempts:
            raise _invalid_attempt_audit(
                "Attempt trail requires at least one attempt."
            )
        if len(self.attempts) > self.max_attempts:
            raise _invalid_attempt_audit(
                "Attempt count exceeds max_attempts."
            )

        for expected_number, attempt in enumerate(self.attempts, start=1):
            if not isinstance(attempt, ProviderAttemptAudit):
                raise _invalid_attempt_audit(
                    "attempts must contain ProviderAttemptAudit objects."
                )
            if attempt.attempt_number != expected_number:
                raise _invalid_attempt_audit(
                    "Attempt numbering must be contiguous and ordered from 1."
                )
            if attempt.status == FAILED:
                decision = attempt.retry_decision
                if decision is None:
                    raise _invalid_attempt_audit(
                        "A failed attempt requires RetryDecision."
                    )
                if decision.policy_version != self.retry_policy_version:
                    raise _invalid_attempt_audit(
                        "Retry policy versions differ within attempt trail."
                    )
                if decision.max_attempts != self.max_attempts:
                    raise _invalid_attempt_audit(
                        "RetryDecision and trail max_attempts differ."
                    )

        for attempt in self.attempts[:-1]:
            if (
                attempt.status != FAILED
                or attempt.retry_decision is None
                or attempt.retry_decision.action != RETRY
            ):
                raise _invalid_attempt_audit(
                    "Every non-final attempt must fail with a retry decision."
                )

        final_attempt = self.attempts[-1]
        if self.outcome == SUCCEEDED:
            if final_attempt.status != SUCCEEDED:
                raise _invalid_attempt_audit(
                    "A succeeded trail must end with a succeeded attempt."
                )
            return

        if (
            final_attempt.status != FAILED
            or final_attempt.retry_decision is None
            or final_attempt.retry_decision.action != DO_NOT_RETRY
        ):
            raise _invalid_attempt_audit(
                "A failed trail must end with do_not_retry."
            )

    def to_dict(self) -> dict[str, Any]:
        """Return an explicit, fresh, JSON-safe public representation."""

        return {
            "version": self.version,
            "retry_policy_version": self.retry_policy_version,
            "max_attempts": self.max_attempts,
            "outcome": self.outcome,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }
