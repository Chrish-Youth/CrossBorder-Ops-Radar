from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import src.insight_provider as insight_provider
import src.insight_retry as retry_module
from src.insight_provider import (
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_TIMEOUT,
)
from src.insight_retry import (
    ATTEMPT_LIMIT_REACHED,
    DEFAULT_RETRY_POLICY,
    DEFAULT_RETRYABLE_ERROR_CODES,
    DO_NOT_RETRY,
    ERROR_NOT_RETRYABLE,
    INVALID_RETRY_CONTRACT,
    PERMANENT_NON_RETRYABLE_ERROR_CODES,
    RETRY,
    RETRY_POLICY_VERSION,
    RETRYABLE_TRANSIENT_ERROR,
    RetryContractError,
    RetryDecision,
    RetryPolicy,
    evaluate_retry,
)


RETRYABLE_CODES = DEFAULT_RETRYABLE_ERROR_CODES
NONRETRYABLE_CODES = PERMANENT_NON_RETRYABLE_ERROR_CODES
ALL_PROVIDER_ERROR_CODES = RETRYABLE_CODES + NONRETRYABLE_CODES


def _discover_provider_error_codes() -> set[str]:
    return {
        value
        for name, value in vars(insight_provider).items()
        if isinstance(value, str)
        and value == name
        and (
            name == "INVALID_PROVIDER"
            or name.startswith("INVALID_PROVIDER_")
            or name.startswith("PROVIDER_")
        )
    }


def test_default_policy_is_frozen_and_uses_the_explicit_transient_allowlist() -> None:
    assert DEFAULT_RETRY_POLICY == RetryPolicy(
        version=RETRY_POLICY_VERSION,
        max_attempts=2,
        retryable_error_codes=RETRYABLE_CODES,
    )
    assert isinstance(DEFAULT_RETRY_POLICY.retryable_error_codes, tuple)
    with pytest.raises(FrozenInstanceError):
        DEFAULT_RETRY_POLICY.max_attempts = 3  # type: ignore[misc]


@pytest.mark.parametrize("version", [None, "", " ", b"1"])
def test_policy_rejects_invalid_version(version: object) -> None:
    with pytest.raises(RetryContractError) as captured:
        RetryPolicy(
            version=version,  # type: ignore[arg-type]
            max_attempts=2,
            retryable_error_codes=RETRYABLE_CODES,
        )

    assert captured.value.code == INVALID_RETRY_CONTRACT


@pytest.mark.parametrize("max_attempts", [True, False, 0, -1, 1.0, None])
def test_policy_rejects_invalid_max_attempts(max_attempts: object) -> None:
    with pytest.raises(RetryContractError):
        RetryPolicy(
            version=RETRY_POLICY_VERSION,
            max_attempts=max_attempts,  # type: ignore[arg-type]
            retryable_error_codes=RETRYABLE_CODES,
        )


@pytest.mark.parametrize("max_attempts", [1, 2, 10**100])
def test_policy_accepts_positive_unbounded_python_integer_attempts(
    max_attempts: int,
) -> None:
    policy = RetryPolicy(
        version="custom-policy",
        max_attempts=max_attempts,
        retryable_error_codes=(),
    )

    assert policy.max_attempts == max_attempts


def test_normal_int_subclass_is_accepted_but_bool_remains_invalid() -> None:
    class AttemptCount(int):
        pass

    policy = RetryPolicy(
        version="int-subclass",
        max_attempts=AttemptCount(2),
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    decision = evaluate_retry(
        error_code=PROVIDER_TIMEOUT,
        attempts_completed=AttemptCount(1),
        policy=policy,
    )

    assert decision.action == RETRY
    with pytest.raises(RetryContractError):
        RetryPolicy(
            version="bool-is-not-a-count",
            max_attempts=True,
            retryable_error_codes=(),
        )


@pytest.mark.parametrize("codes", [[], {}, None, set()])
def test_policy_requires_retryable_codes_tuple(codes: object) -> None:
    with pytest.raises(RetryContractError):
        RetryPolicy(
            version=RETRY_POLICY_VERSION,
            max_attempts=2,
            retryable_error_codes=codes,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("member", [None, "", " ", b"CODE", 1])
def test_policy_rejects_invalid_retryable_code_members(member: object) -> None:
    with pytest.raises(RetryContractError):
        RetryPolicy(
            version=RETRY_POLICY_VERSION,
            max_attempts=2,
            retryable_error_codes=(member,),  # type: ignore[arg-type]
        )


def test_policy_rejects_duplicate_retryable_codes() -> None:
    with pytest.raises(RetryContractError) as captured:
        RetryPolicy(
            version=RETRY_POLICY_VERSION,
            max_attempts=2,
            retryable_error_codes=(PROVIDER_TIMEOUT, PROVIDER_TIMEOUT),
        )

    assert captured.value.code == INVALID_RETRY_CONTRACT


@pytest.mark.parametrize("error_code", PERMANENT_NON_RETRYABLE_ERROR_CODES)
def test_policy_rejects_every_permanent_terminal_code(
    error_code: str,
) -> None:
    with pytest.raises(RetryContractError) as captured:
        RetryPolicy(
            version="unsafe",
            max_attempts=2,
            retryable_error_codes=(error_code,),
        )

    assert captured.value.code == INVALID_RETRY_CONTRACT
    assert error_code not in captured.value.message


@pytest.mark.parametrize(
    "codes",
    [
        (PROVIDER_TIMEOUT, PROVIDER_AUTH_FAILED),
        ("FUTURE_PROVIDER_ERROR", PROVIDER_AUTH_FAILED),
    ],
)
def test_policy_rejects_entire_mixed_terminal_allowlist(
    codes: tuple[str, ...],
) -> None:
    with pytest.raises(RetryContractError) as captured:
        RetryPolicy(
            version="mixed-unsafe",
            max_attempts=2,
            retryable_error_codes=codes,
        )

    assert captured.value.code == INVALID_RETRY_CONTRACT


def test_custom_policy_may_disable_default_transient_and_allow_future_code() -> None:
    policy = RetryPolicy(
        version="custom-conservative",
        max_attempts=2,
        retryable_error_codes=(PROVIDER_TIMEOUT, "FUTURE_PROVIDER_ERROR"),
    )

    disabled = evaluate_retry(
        error_code=PROVIDER_CONNECTION_FAILED,
        attempts_completed=1,
        policy=policy,
    )
    future = evaluate_retry(
        error_code="FUTURE_PROVIDER_ERROR",
        attempts_completed=1,
        policy=policy,
    )

    assert disabled.action == DO_NOT_RETRY
    assert disabled.reason == ERROR_NOT_RETRYABLE
    assert future.action == RETRY
    assert future.reason == RETRYABLE_TRANSIENT_ERROR


def test_valid_retry_and_do_not_retry_decisions_are_frozen() -> None:
    retry = RetryDecision(
        policy_version="1",
        error_code=PROVIDER_TIMEOUT,
        action=RETRY,
        reason=RETRYABLE_TRANSIENT_ERROR,
        attempts_completed=1,
        max_attempts=2,
    )
    stop = RetryDecision(
        policy_version="1",
        error_code=PROVIDER_AUTH_FAILED,
        action=DO_NOT_RETRY,
        reason=ERROR_NOT_RETRYABLE,
        attempts_completed=1,
        max_attempts=2,
    )

    assert retry.action == RETRY
    assert stop.action == DO_NOT_RETRY
    with pytest.raises(FrozenInstanceError):
        retry.action = DO_NOT_RETRY  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("policy_version", None),
        ("policy_version", ""),
        ("policy_version", " "),
        ("error_code", None),
        ("error_code", ""),
        ("error_code", " "),
    ],
)
def test_decision_rejects_invalid_identity_fields(
    field_name: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "policy_version": "1",
        "error_code": PROVIDER_TIMEOUT,
        "action": RETRY,
        "reason": RETRYABLE_TRANSIENT_ERROR,
        "attempts_completed": 1,
        "max_attempts": 2,
    }
    values[field_name] = value

    with pytest.raises(RetryContractError):
        RetryDecision(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("action", ["", "maybe", None, 1, [], {}])
def test_decision_rejects_invalid_action(action: object) -> None:
    with pytest.raises(RetryContractError):
        RetryDecision(
            policy_version="1",
            error_code=PROVIDER_TIMEOUT,
            action=action,  # type: ignore[arg-type]
            reason=RETRYABLE_TRANSIENT_ERROR,
            attempts_completed=1,
            max_attempts=2,
        )


@pytest.mark.parametrize("reason", ["", "UNKNOWN", None, 1, [], {}])
def test_decision_rejects_invalid_reason(reason: object) -> None:
    with pytest.raises(RetryContractError):
        RetryDecision(
            policy_version="1",
            error_code=PROVIDER_AUTH_FAILED,
            action=DO_NOT_RETRY,
            reason=reason,  # type: ignore[arg-type]
            attempts_completed=1,
            max_attempts=2,
        )


@pytest.mark.parametrize("attempts", [True, False, 0, -1, 1.5, None])
def test_decision_rejects_invalid_attempts_completed(attempts: object) -> None:
    with pytest.raises(RetryContractError):
        RetryDecision(
            policy_version="1",
            error_code=PROVIDER_TIMEOUT,
            action=RETRY,
            reason=RETRYABLE_TRANSIENT_ERROR,
            attempts_completed=attempts,  # type: ignore[arg-type]
            max_attempts=2,
        )


@pytest.mark.parametrize("max_attempts", [True, False, 0, -1, 1.5, None])
def test_decision_rejects_invalid_max_attempts(max_attempts: object) -> None:
    with pytest.raises(RetryContractError):
        RetryDecision(
            policy_version="1",
            error_code=PROVIDER_TIMEOUT,
            action=RETRY,
            reason=RETRYABLE_TRANSIENT_ERROR,
            attempts_completed=1,
            max_attempts=max_attempts,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("action", "reason", "attempts", "maximum"),
    [
        (RETRY, ATTEMPT_LIMIT_REACHED, 1, 2),
        (RETRY, RETRYABLE_TRANSIENT_ERROR, 2, 2),
        (DO_NOT_RETRY, RETRYABLE_TRANSIENT_ERROR, 1, 2),
        (DO_NOT_RETRY, ATTEMPT_LIMIT_REACHED, 1, 2),
        (DO_NOT_RETRY, ERROR_NOT_RETRYABLE, 2, 2),
    ],
)
def test_decision_rejects_contradictory_fields(
    action: str,
    reason: str,
    attempts: int,
    maximum: int,
) -> None:
    with pytest.raises(RetryContractError):
        RetryDecision(
            policy_version="1",
            error_code=PROVIDER_TIMEOUT,
            action=action,
            reason=reason,
            attempts_completed=attempts,
            max_attempts=maximum,
        )


@pytest.mark.parametrize("error_code", PERMANENT_NON_RETRYABLE_ERROR_CODES)
def test_decision_rejects_direct_retry_for_every_permanent_terminal_code(
    error_code: str,
) -> None:
    with pytest.raises(RetryContractError) as captured:
        RetryDecision(
            policy_version="unsafe",
            error_code=error_code,
            action=RETRY,
            reason=RETRYABLE_TRANSIENT_ERROR,
            attempts_completed=1,
            max_attempts=2,
        )

    assert captured.value.code == INVALID_RETRY_CONTRACT
    assert error_code not in captured.value.message


@pytest.mark.parametrize("error_code", PERMANENT_NON_RETRYABLE_ERROR_CODES)
def test_terminal_decisions_allow_stop_before_and_at_attempt_limit(
    error_code: str,
) -> None:
    before_limit = RetryDecision(
        policy_version="1",
        error_code=error_code,
        action=DO_NOT_RETRY,
        reason=ERROR_NOT_RETRYABLE,
        attempts_completed=1,
        max_attempts=2,
    )
    at_limit = RetryDecision(
        policy_version="1",
        error_code=error_code,
        action=DO_NOT_RETRY,
        reason=ATTEMPT_LIMIT_REACHED,
        attempts_completed=2,
        max_attempts=2,
    )

    assert before_limit.reason == ERROR_NOT_RETRYABLE
    assert at_limit.reason == ATTEMPT_LIMIT_REACHED


@pytest.mark.parametrize("error_code", RETRYABLE_CODES)
def test_every_transient_provider_code_is_retryable_on_first_attempt(
    error_code: str,
) -> None:
    decision = evaluate_retry(error_code=error_code, attempts_completed=1)

    assert decision == RetryDecision(
        policy_version=RETRY_POLICY_VERSION,
        error_code=error_code,
        action=RETRY,
        reason=RETRYABLE_TRANSIENT_ERROR,
        attempts_completed=1,
        max_attempts=2,
    )


@pytest.mark.parametrize("error_code", NONRETRYABLE_CODES)
def test_every_nonretryable_provider_code_fails_closed_on_first_attempt(
    error_code: str,
) -> None:
    decision = evaluate_retry(error_code=error_code, attempts_completed=1)

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ERROR_NOT_RETRYABLE
    assert decision.error_code == error_code


def test_classification_matrix_contains_every_current_provider_error_code() -> None:
    discovered_codes = _discover_provider_error_codes()
    retryable_codes = set(DEFAULT_RETRYABLE_ERROR_CODES)
    terminal_codes = set(PERMANENT_NON_RETRYABLE_ERROR_CODES)

    assert len(discovered_codes) == 14
    assert len(retryable_codes) == 4
    assert len(terminal_codes) == 10
    assert retryable_codes.isdisjoint(terminal_codes)
    assert discovered_codes == retryable_codes | terminal_codes
    assert DEFAULT_RETRY_POLICY.retryable_error_codes == (
        DEFAULT_RETRYABLE_ERROR_CODES
    )


@pytest.mark.parametrize("error_code", ALL_PROVIDER_ERROR_CODES)
def test_every_current_provider_code_stops_at_exact_attempt_limit(
    error_code: str,
) -> None:
    decision = evaluate_retry(
        error_code=error_code,
        attempts_completed=2,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ATTEMPT_LIMIT_REACHED


@pytest.mark.parametrize("attempts_completed", [2, 3, 10**100])
def test_attempt_limit_takes_priority_for_retryable_error(
    attempts_completed: int,
) -> None:
    decision = evaluate_retry(
        error_code=PROVIDER_TIMEOUT,
        attempts_completed=attempts_completed,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ATTEMPT_LIMIT_REACHED


def test_attempt_limit_takes_priority_for_nonretryable_error() -> None:
    decision = evaluate_retry(
        error_code=PROVIDER_AUTH_FAILED,
        attempts_completed=2,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ATTEMPT_LIMIT_REACHED


@pytest.mark.parametrize("error_code", [PROVIDER_TIMEOUT, PROVIDER_AUTH_FAILED])
def test_max_attempts_one_never_allows_retry(error_code: str) -> None:
    policy = RetryPolicy(
        version="single-attempt",
        max_attempts=1,
        retryable_error_codes=RETRYABLE_CODES,
    )

    decision = evaluate_retry(
        error_code=error_code,
        attempts_completed=1,
        policy=policy,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ATTEMPT_LIMIT_REACHED
    assert decision.policy_version == policy.version
    assert decision.max_attempts == 1


def test_max_attempts_one_with_empty_allowlist_stops_on_first_failure() -> None:
    policy = RetryPolicy(
        version="none",
        max_attempts=1,
        retryable_error_codes=(),
    )

    decision = evaluate_retry(
        error_code="FUTURE_PROVIDER_ERROR",
        attempts_completed=1,
        policy=policy,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ATTEMPT_LIMIT_REACHED


def test_unknown_future_error_fails_closed() -> None:
    decision = evaluate_retry(
        error_code="FUTURE_PROVIDER_ERROR",
        attempts_completed=1,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ERROR_NOT_RETRYABLE


@pytest.mark.parametrize(
    "error_code",
    ["provider_timeout", "PROVIDER_TIMEOUT ", "Provider_TIMEOUT"],
)
def test_default_policy_uses_exact_error_code_identity(error_code: str) -> None:
    decision = evaluate_retry(
        error_code=error_code,
        attempts_completed=1,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ERROR_NOT_RETRYABLE


@pytest.mark.parametrize("error_code", [None, "", " ", b"CODE"])
def test_evaluator_rejects_invalid_error_code(error_code: object) -> None:
    with pytest.raises(RetryContractError):
        evaluate_retry(
            error_code=error_code,  # type: ignore[arg-type]
            attempts_completed=1,
        )


@pytest.mark.parametrize("attempts", [True, False, 0, -1, 1.5, None])
def test_evaluator_rejects_invalid_attempt_state(attempts: object) -> None:
    with pytest.raises(RetryContractError):
        evaluate_retry(
            error_code=PROVIDER_TIMEOUT,
            attempts_completed=attempts,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("policy", [object(), {}, []])
def test_evaluator_rejects_wrong_policy_type(policy: object) -> None:
    with pytest.raises(RetryContractError):
        evaluate_retry(
            error_code=PROVIDER_TIMEOUT,
            attempts_completed=1,
            policy=policy,  # type: ignore[arg-type]
        )


def test_custom_policy_can_explicitly_allowlist_an_exact_future_code() -> None:
    policy = RetryPolicy(
        version="future-test",
        max_attempts=2,
        retryable_error_codes=("FUTURE_PROVIDER_ERROR",),
    )

    decision = evaluate_retry(
        error_code="FUTURE_PROVIDER_ERROR",
        attempts_completed=1,
        policy=policy,
    )

    assert decision.action == RETRY
    assert decision.reason == RETRYABLE_TRANSIENT_ERROR
    assert decision.policy_version == policy.version


@pytest.mark.parametrize("attempts_completed", [2, 3])
def test_custom_future_code_cannot_bypass_attempt_limit(
    attempts_completed: int,
) -> None:
    policy = RetryPolicy(
        version="future-test",
        max_attempts=2,
        retryable_error_codes=("FUTURE_PROVIDER_ERROR",),
    )

    decision = evaluate_retry(
        error_code="FUTURE_PROVIDER_ERROR",
        attempts_completed=attempts_completed,
        policy=policy,
    )

    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ATTEMPT_LIMIT_REACHED


def test_default_policy_is_resolved_at_evaluation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = RetryPolicy(
        version="replacement",
        max_attempts=1,
        retryable_error_codes=RETRYABLE_CODES,
    )
    monkeypatch.setattr(retry_module, "DEFAULT_RETRY_POLICY", replacement)

    decision = evaluate_retry(
        error_code=PROVIDER_TIMEOUT,
        attempts_completed=1,
    )

    assert decision.policy_version == replacement.version
    assert decision.max_attempts == 1
    assert decision.reason == ATTEMPT_LIMIT_REACHED


def test_evaluator_is_deterministic_and_does_not_mutate_policy() -> None:
    policy = RetryPolicy(
        version="deterministic",
        max_attempts=2,
        retryable_error_codes=RETRYABLE_CODES,
    )
    original_codes = policy.retryable_error_codes

    decisions = tuple(
        evaluate_retry(
            error_code=PROVIDER_CONNECTION_FAILED,
            attempts_completed=1,
            policy=policy,
        )
        for _ in range(100)
    )

    assert all(decision == decisions[0] for decision in decisions)
    assert policy.retryable_error_codes is original_codes


def test_retry_core_has_no_execution_or_provider_specific_imports() -> None:
    source_path = Path(retry_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert "openai" not in imports
    assert "src.deepseek_provider" not in imports
    assert "time" not in imports
    assert "asyncio" not in imports
    assert "random" not in imports
