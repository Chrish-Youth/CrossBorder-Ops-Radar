from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.insight_provider import PROVIDER_TIMEOUT
from src.insight_retry_delay import RetryDelayDecision
from src.insight_retry_delay_execution import (
    INVALID_RETRY_DELAY_EXECUTION_CONTRACT,
    RETRY_DELAY_EXECUTION_VERSION,
    RetryDelayExecutionAudit,
    RetryDelayExecutionContractError,
    RetryDelayExecutionRecord,
)


def delay_decision(
    attempt: int,
    *,
    policy_version: str = "delay-policy",
    error_code: str = PROVIDER_TIMEOUT,
    delay_ms: int = 1_000,
) -> RetryDelayDecision:
    return RetryDelayDecision(
        policy_version=policy_version,
        error_code=error_code,
        attempts_completed=attempt,
        delay_ms=delay_ms,
    )


def record(
    attempt: int,
    *,
    policy_version: str = "delay-policy",
    error_code: str = PROVIDER_TIMEOUT,
    delay_ms: int = 1_000,
) -> RetryDelayExecutionRecord:
    return RetryDelayExecutionRecord(
        version=RETRY_DELAY_EXECUTION_VERSION,
        after_attempt_number=attempt,
        delay_decision=delay_decision(
            attempt,
            policy_version=policy_version,
            error_code=error_code,
            delay_ms=delay_ms,
        ),
    )


def test_record_contract_is_exact_and_frozen() -> None:
    item = record(1)

    assert item == RetryDelayExecutionRecord(
        version="1",
        after_attempt_number=1,
        delay_decision=delay_decision(1),
    )
    assert RETRY_DELAY_EXECUTION_VERSION == "1"
    with pytest.raises(FrozenInstanceError):
        item.after_attempt_number = 2  # type: ignore[misc]


@pytest.mark.parametrize("version", ["0", "2", "", " ", None, 1])
def test_record_rejects_noncurrent_version(version: object) -> None:
    with pytest.raises(RetryDelayExecutionContractError) as captured:
        RetryDelayExecutionRecord(
            version=version,  # type: ignore[arg-type]
            after_attempt_number=1,
            delay_decision=delay_decision(1),
        )

    assert captured.value.code == INVALID_RETRY_DELAY_EXECUTION_CONTRACT


@pytest.mark.parametrize("attempt", [True, False, 0, -1, 1.0, None])
def test_record_rejects_invalid_attempt_number(attempt: object) -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionRecord(
            version=RETRY_DELAY_EXECUTION_VERSION,
            after_attempt_number=attempt,  # type: ignore[arg-type]
            delay_decision=delay_decision(1),
        )


@pytest.mark.parametrize("decision", [None, {}, object()])
def test_record_requires_delay_decision(decision: object) -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionRecord(
            version=RETRY_DELAY_EXECUTION_VERSION,
            after_attempt_number=1,
            delay_decision=decision,  # type: ignore[arg-type]
        )


def test_record_requires_attempt_linkage() -> None:
    with pytest.raises(RetryDelayExecutionContractError) as captured:
        RetryDelayExecutionRecord(
            version=RETRY_DELAY_EXECUTION_VERSION,
            after_attempt_number=1,
            delay_decision=delay_decision(2),
        )

    assert captured.value.code == INVALID_RETRY_DELAY_EXECUTION_CONTRACT


def test_empty_audit_records_governing_policy_and_is_frozen() -> None:
    audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version="custom-delay-v2",
        records=(),
    )

    assert audit.records == ()
    assert audit.policy_version == "custom-delay-v2"
    with pytest.raises(FrozenInstanceError):
        audit.records = (record(1),)  # type: ignore[misc]


@pytest.mark.parametrize("version", ["0", "2", "", " ", None, 1])
def test_audit_rejects_noncurrent_version(version: object) -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionAudit(
            version=version,  # type: ignore[arg-type]
            policy_version="delay-policy",
            records=(),
        )


@pytest.mark.parametrize("policy_version", ["", " ", None, b"1", 1])
def test_audit_rejects_invalid_policy_version(
    policy_version: object,
) -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionAudit(
            version=RETRY_DELAY_EXECUTION_VERSION,
            policy_version=policy_version,  # type: ignore[arg-type]
            records=(),
        )


@pytest.mark.parametrize("records", [[], {}, set(), None])
def test_audit_requires_tuple_records(records: object) -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionAudit(
            version=RETRY_DELAY_EXECUTION_VERSION,
            policy_version="delay-policy",
            records=records,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("member", [None, {}, object()])
def test_audit_rejects_wrong_record_member(member: object) -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionAudit(
            version=RETRY_DELAY_EXECUTION_VERSION,
            policy_version="delay-policy",
            records=(member,),  # type: ignore[arg-type]
        )


def test_audit_requires_policy_linkage() -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionAudit(
            version=RETRY_DELAY_EXECUTION_VERSION,
            policy_version="audit-policy",
            records=(record(1, policy_version="decision-policy"),),
        )


@pytest.mark.parametrize(
    "records",
    [
        (record(2),),
        (record(1), record(1)),
        (record(1), record(3)),
        (record(2), record(1)),
    ],
)
def test_audit_rejects_gaps_duplicates_and_wrong_order(
    records: tuple[RetryDelayExecutionRecord, ...],
) -> None:
    with pytest.raises(RetryDelayExecutionContractError):
        RetryDelayExecutionAudit(
            version=RETRY_DELAY_EXECUTION_VERSION,
            policy_version="delay-policy",
            records=records,
        )


def test_multi_transition_audit_preserves_exact_records() -> None:
    records = (
        record(1, delay_ms=1_000),
        record(2, delay_ms=2_000),
    )

    audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version="delay-policy",
        records=records,
    )

    assert audit.records is records
    assert [
        item.delay_decision.delay_ms for item in audit.records
    ] == [1_000, 2_000]


def test_contract_intentionally_has_no_serialization_boundary() -> None:
    audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version="delay-policy",
        records=(record(1, delay_ms=10**5_000),),
    )

    assert not hasattr(audit, "to_dict")
    assert not hasattr(audit.records[0], "to_dict")
    assert audit.records[0].delay_decision.delay_ms == 10**5_000
