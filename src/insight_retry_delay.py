"""Deterministic retry-delay policy for sealed RetryDecision objects."""

from __future__ import annotations

from dataclasses import dataclass

from src.insight_retry import (
    PERMANENT_NON_RETRYABLE_ERROR_CODES,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_RATE_LIMITED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    RETRY,
    RetryDecision,
)

RETRY_DELAY_POLICY_VERSION = "1"
INVALID_RETRY_DELAY_CONTRACT = "INVALID_RETRY_DELAY_CONTRACT"


class RetryDelayContractError(ValueError):
    """A stable failure at the retry-delay contract boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_delay(message: str) -> RetryDelayContractError:
    return RetryDelayContractError(INVALID_RETRY_DELAY_CONTRACT, message)


def _validate_nonblank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_delay(f"{field_name} must be a nonblank string.")
    return value


def _validate_positive_integer(
    value: object,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid_delay(
            f"{field_name} must be an integer greater than or equal to 1."
        )
    return value


@dataclass(frozen=True)
class RetryDelayPolicy:
    """One immutable set of exact error-code delay overrides."""

    version: str
    base_delays_ms: tuple[tuple[str, int], ...]
    fallback_base_delay_ms: int
    max_delay_ms: int

    def __post_init__(self) -> None:
        _validate_nonblank_string(self.version, field_name="version")
        maximum = _validate_positive_integer(
            self.max_delay_ms,
            field_name="max_delay_ms",
        )
        fallback = _validate_positive_integer(
            self.fallback_base_delay_ms,
            field_name="fallback_base_delay_ms",
        )
        if fallback > maximum:
            raise _invalid_delay(
                "fallback_base_delay_ms must not exceed max_delay_ms."
            )
        if not isinstance(self.base_delays_ms, tuple):
            raise _invalid_delay("base_delays_ms must be a tuple.")

        seen_codes: list[str] = []
        for rule in self.base_delays_ms:
            if not isinstance(rule, tuple) or len(rule) != 2:
                raise _invalid_delay(
                    "Each base_delays_ms member must be a two-item tuple."
                )
            error_code = _validate_nonblank_string(
                rule[0],
                field_name="base_delays_ms error code",
            )
            base_delay = _validate_positive_integer(
                rule[1],
                field_name="base_delays_ms delay",
            )
            if base_delay > maximum:
                raise _invalid_delay(
                    "A base delay must not exceed max_delay_ms."
                )
            if error_code in PERMANENT_NON_RETRYABLE_ERROR_CODES:
                raise _invalid_delay(
                    "Delay rules must not contain permanent terminal codes."
                )
            if error_code in seen_codes:
                raise _invalid_delay(
                    "base_delays_ms must not contain duplicate error codes."
                )
            seen_codes.append(error_code)


DEFAULT_RETRY_DELAY_POLICY = RetryDelayPolicy(
    version=RETRY_DELAY_POLICY_VERSION,
    base_delays_ms=(
        (PROVIDER_TIMEOUT, 1_000),
        (PROVIDER_CONNECTION_FAILED, 1_000),
        (PROVIDER_UNAVAILABLE, 2_000),
        (PROVIDER_RATE_LIMITED, 5_000),
    ),
    fallback_base_delay_ms=1_000,
    max_delay_ms=30_000,
)


@dataclass(frozen=True)
class RetryDelayDecision:
    """One immutable delay decision for an already-approved retry."""

    policy_version: str
    error_code: str
    attempts_completed: int
    delay_ms: int

    def __post_init__(self) -> None:
        _validate_nonblank_string(
            self.policy_version,
            field_name="policy_version",
        )
        _validate_nonblank_string(self.error_code, field_name="error_code")
        _validate_positive_integer(
            self.attempts_completed,
            field_name="attempts_completed",
        )
        _validate_positive_integer(
            self.delay_ms,
            field_name="delay_ms",
        )


def _base_delay_for_error(
    error_code: str,
    *,
    policy: RetryDelayPolicy,
) -> int:
    for configured_code, base_delay in policy.base_delays_ms:
        if configured_code == error_code:
            return base_delay
    return policy.fallback_base_delay_ms


def _capped_linear_delay(
    *,
    base_delay_ms: int,
    attempts_completed: int,
    max_delay_ms: int,
) -> int:
    saturation_attempt = (
        max_delay_ms + base_delay_ms - 1
    ) // base_delay_ms
    if attempts_completed >= saturation_attempt:
        return max_delay_ms
    return base_delay_ms * attempts_completed


def resolve_retry_delay(
    *,
    retry_decision: RetryDecision,
    policy: RetryDelayPolicy | None = None,
) -> RetryDelayDecision:
    """Resolve when to retry; this function never waits or reevaluates retry."""

    active_policy = DEFAULT_RETRY_DELAY_POLICY if policy is None else policy
    if not isinstance(active_policy, RetryDelayPolicy):
        raise _invalid_delay("policy must be RetryDelayPolicy or None.")
    if not isinstance(retry_decision, RetryDecision):
        raise _invalid_delay("retry_decision must be RetryDecision.")
    if retry_decision.action != RETRY:
        raise _invalid_delay("retry_decision action must be retry.")

    base_delay = _base_delay_for_error(
        retry_decision.error_code,
        policy=active_policy,
    )
    delay_ms = _capped_linear_delay(
        base_delay_ms=base_delay,
        attempts_completed=retry_decision.attempts_completed,
        max_delay_ms=active_policy.max_delay_ms,
    )
    return RetryDelayDecision(
        policy_version=active_policy.version,
        error_code=retry_decision.error_code,
        attempts_completed=retry_decision.attempts_completed,
        delay_ms=delay_ms,
    )
