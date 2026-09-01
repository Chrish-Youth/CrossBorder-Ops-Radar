"""Immutable provenance for completed retry-delay transitions."""

from __future__ import annotations

from dataclasses import dataclass

from src.insight_retry_delay import RetryDelayDecision

RETRY_DELAY_EXECUTION_VERSION = "1"
INVALID_RETRY_DELAY_EXECUTION_CONTRACT = (
    "INVALID_RETRY_DELAY_EXECUTION_CONTRACT"
)


class RetryDelayExecutionContractError(ValueError):
    """A stable failure at the delay-execution provenance boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_delay_execution(
    message: str,
) -> RetryDelayExecutionContractError:
    return RetryDelayExecutionContractError(
        INVALID_RETRY_DELAY_EXECUTION_CONTRACT,
        message,
    )


def _validate_positive_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid_delay_execution(
            f"{field_name} must be an integer greater than or equal to 1."
        )
    return value


def _validate_nonblank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_delay_execution(
            f"{field_name} must be a nonblank string."
        )
    return value


@dataclass(frozen=True)
class RetryDelayExecutionRecord:
    """A V2-emitted transition created after its synchronous sleeper returns."""

    version: str
    after_attempt_number: int
    delay_decision: RetryDelayDecision

    def __post_init__(self) -> None:
        if self.version != RETRY_DELAY_EXECUTION_VERSION:
            raise _invalid_delay_execution(
                "Delay execution record version does not match the current contract."
            )
        attempt_number = _validate_positive_integer(
            self.after_attempt_number,
            field_name="after_attempt_number",
        )
        if not isinstance(self.delay_decision, RetryDelayDecision):
            raise _invalid_delay_execution(
                "delay_decision must be RetryDelayDecision."
            )
        if attempt_number != self.delay_decision.attempts_completed:
            raise _invalid_delay_execution(
                "after_attempt_number must match delay_decision attempts_completed."
            )


@dataclass(frozen=True)
class RetryDelayExecutionAudit:
    """The governing delay policy and all completed delay transitions."""

    version: str
    policy_version: str
    records: tuple[RetryDelayExecutionRecord, ...]

    def __post_init__(self) -> None:
        if self.version != RETRY_DELAY_EXECUTION_VERSION:
            raise _invalid_delay_execution(
                "Delay execution audit version does not match the current contract."
            )
        policy_version = _validate_nonblank_string(
            self.policy_version,
            field_name="policy_version",
        )
        if not isinstance(self.records, tuple):
            raise _invalid_delay_execution("records must be a tuple.")

        for position, record in enumerate(self.records, start=1):
            if not isinstance(record, RetryDelayExecutionRecord):
                raise _invalid_delay_execution(
                    "Each records member must be RetryDelayExecutionRecord."
                )
            if record.after_attempt_number != position:
                raise _invalid_delay_execution(
                    "Delay execution records must be ordered consecutively from 1."
                )
            if record.delay_decision.policy_version != policy_version:
                raise _invalid_delay_execution(
                    "Each delay decision must match the audit policy version."
                )
