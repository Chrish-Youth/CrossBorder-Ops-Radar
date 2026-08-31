"""Deterministic retry eligibility for stable Provider error codes."""

from __future__ import annotations

from dataclasses import dataclass

from src.insight_provider import (
    INVALID_PROVIDER,
    INVALID_PROVIDER_JSON,
    INVALID_PROVIDER_RESPONSE,
    INVALID_PROVIDER_USAGE,
    PROVIDER_ACCOUNT_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_FAILURE,
    PROVIDER_RATE_LIMITED,
    PROVIDER_REQUEST_REJECTED,
    PROVIDER_RESPONSE_TOO_LARGE,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
)

RETRY_POLICY_VERSION = "1"
INVALID_RETRY_CONTRACT = "INVALID_RETRY_CONTRACT"

RETRY = "retry"
DO_NOT_RETRY = "do_not_retry"

RETRYABLE_TRANSIENT_ERROR = "RETRYABLE_TRANSIENT_ERROR"
ERROR_NOT_RETRYABLE = "ERROR_NOT_RETRYABLE"
ATTEMPT_LIMIT_REACHED = "ATTEMPT_LIMIT_REACHED"

_ACTIONS = frozenset({RETRY, DO_NOT_RETRY})
_REASONS = frozenset(
    {
        RETRYABLE_TRANSIENT_ERROR,
        ERROR_NOT_RETRYABLE,
        ATTEMPT_LIMIT_REACHED,
    }
)

PERMANENT_NON_RETRYABLE_ERROR_CODES = (
    INVALID_PROVIDER,
    PROVIDER_FAILURE,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_ACCOUNT_ERROR,
    PROVIDER_REQUEST_REJECTED,
    INVALID_PROVIDER_RESPONSE,
    INVALID_PROVIDER_USAGE,
    PROVIDER_RESPONSE_TOO_LARGE,
    INVALID_PROVIDER_JSON,
)

DEFAULT_RETRYABLE_ERROR_CODES = (
    PROVIDER_TIMEOUT,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_RATE_LIMITED,
    PROVIDER_UNAVAILABLE,
)


class RetryContractError(ValueError):
    """A stable failure at the retry-policy contract boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_retry(message: str) -> RetryContractError:
    return RetryContractError(INVALID_RETRY_CONTRACT, message)


def _validate_nonblank_string(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_retry(f"{field_name} must be a nonblank string.")


def _validate_positive_integer(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid_retry(
            f"{field_name} must be an integer greater than or equal to 1."
        )


@dataclass(frozen=True)
class RetryPolicy:
    """One immutable allowlist and Provider invocation budget."""

    version: str
    max_attempts: int
    retryable_error_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_nonblank_string(self.version, field_name="version")
        _validate_positive_integer(
            self.max_attempts,
            field_name="max_attempts",
        )
        if not isinstance(self.retryable_error_codes, tuple):
            raise _invalid_retry("retryable_error_codes must be a tuple.")
        for error_code in self.retryable_error_codes:
            _validate_nonblank_string(
                error_code,
                field_name="retryable_error_codes member",
            )
        if any(
            error_code in PERMANENT_NON_RETRYABLE_ERROR_CODES
            for error_code in self.retryable_error_codes
        ):
            raise _invalid_retry(
                "retryable_error_codes must not contain permanent terminal codes."
            )
        if len(self.retryable_error_codes) != len(
            set(self.retryable_error_codes)
        ):
            raise _invalid_retry(
                "retryable_error_codes must not contain duplicates."
            )


DEFAULT_RETRY_POLICY = RetryPolicy(
    version=RETRY_POLICY_VERSION,
    max_attempts=2,
    retryable_error_codes=DEFAULT_RETRYABLE_ERROR_CODES,
)


@dataclass(frozen=True)
class RetryDecision:
    """One immutable eligibility decision after a completed failed attempt."""

    policy_version: str
    error_code: str
    action: str
    reason: str
    attempts_completed: int
    max_attempts: int

    def __post_init__(self) -> None:
        _validate_nonblank_string(
            self.policy_version,
            field_name="policy_version",
        )
        _validate_nonblank_string(self.error_code, field_name="error_code")
        if not isinstance(self.action, str) or self.action not in _ACTIONS:
            raise _invalid_retry("action must be retry or do_not_retry.")
        if not isinstance(self.reason, str) or self.reason not in _REASONS:
            raise _invalid_retry("reason is not a supported retry reason.")
        _validate_positive_integer(
            self.attempts_completed,
            field_name="attempts_completed",
        )
        _validate_positive_integer(
            self.max_attempts,
            field_name="max_attempts",
        )

        limit_reached = self.attempts_completed >= self.max_attempts
        if self.action == RETRY:
            if self.error_code in PERMANENT_NON_RETRYABLE_ERROR_CODES:
                raise _invalid_retry(
                    "Permanent terminal Provider errors cannot be retried."
                )
            if self.reason != RETRYABLE_TRANSIENT_ERROR or limit_reached:
                raise _invalid_retry(
                    "retry requires a transient reason and remaining attempts."
                )
            return

        if self.reason not in {ERROR_NOT_RETRYABLE, ATTEMPT_LIMIT_REACHED}:
            raise _invalid_retry(
                "do_not_retry requires a terminal retry reason."
            )
        if self.reason == ATTEMPT_LIMIT_REACHED and not limit_reached:
            raise _invalid_retry(
                "ATTEMPT_LIMIT_REACHED requires an exhausted attempt budget."
            )
        if self.reason == ERROR_NOT_RETRYABLE and limit_reached:
            raise _invalid_retry(
                "An exhausted attempt budget must take reason priority."
            )


def evaluate_retry(
    *,
    error_code: str,
    attempts_completed: int,
    policy: RetryPolicy | None = None,
) -> RetryDecision:
    """Return eligibility only; this function never executes another attempt."""

    active_policy = DEFAULT_RETRY_POLICY if policy is None else policy
    if not isinstance(active_policy, RetryPolicy):
        raise _invalid_retry("policy must be a RetryPolicy.")
    _validate_nonblank_string(error_code, field_name="error_code")
    _validate_positive_integer(
        attempts_completed,
        field_name="attempts_completed",
    )

    if attempts_completed >= active_policy.max_attempts:
        action = DO_NOT_RETRY
        reason = ATTEMPT_LIMIT_REACHED
    elif error_code in active_policy.retryable_error_codes:
        action = RETRY
        reason = RETRYABLE_TRANSIENT_ERROR
    else:
        action = DO_NOT_RETRY
        reason = ERROR_NOT_RETRYABLE

    return RetryDecision(
        policy_version=active_policy.version,
        error_code=error_code,
        action=action,
        reason=reason,
        attempts_completed=attempts_completed,
        max_attempts=active_policy.max_attempts,
    )
