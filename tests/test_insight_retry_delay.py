from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import src.insight_retry_delay as delay_module
from src.insight_provider import (
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_RATE_LIMITED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
)
from src.insight_retry import (
    ATTEMPT_LIMIT_REACHED,
    DEFAULT_RETRYABLE_ERROR_CODES,
    DO_NOT_RETRY,
    ERROR_NOT_RETRYABLE,
    PERMANENT_NON_RETRYABLE_ERROR_CODES,
    RETRY,
    RETRYABLE_TRANSIENT_ERROR,
    RetryDecision,
    evaluate_retry,
)
from src.insight_retry_delay import (
    DEFAULT_RETRY_DELAY_POLICY,
    INVALID_RETRY_DELAY_CONTRACT,
    RETRY_DELAY_POLICY_VERSION,
    RetryDelayContractError,
    RetryDelayDecision,
    RetryDelayPolicy,
    resolve_retry_delay,
)


DEFAULT_RULES = (
    (PROVIDER_TIMEOUT, 1_000),
    (PROVIDER_CONNECTION_FAILED, 1_000),
    (PROVIDER_UNAVAILABLE, 2_000),
    (PROVIDER_RATE_LIMITED, 5_000),
)


def retry_decision(
    error_code: str,
    *,
    attempts_completed: int = 1,
) -> RetryDecision:
    return RetryDecision(
        policy_version="retry-policy",
        error_code=error_code,
        action=RETRY,
        reason=RETRYABLE_TRANSIENT_ERROR,
        attempts_completed=attempts_completed,
        max_attempts=attempts_completed + 1,
    )


def custom_policy(
    *,
    rules: tuple[tuple[str, int], ...] = (),
    fallback: int = 1_000,
    maximum: int = 30_000,
    version: str = "custom-delay",
) -> RetryDelayPolicy:
    return RetryDelayPolicy(
        version=version,
        base_delays_ms=rules,
        fallback_base_delay_ms=fallback,
        max_delay_ms=maximum,
    )


def test_default_policy_contract_is_exact_and_frozen() -> None:
    assert DEFAULT_RETRY_DELAY_POLICY == RetryDelayPolicy(
        version=RETRY_DELAY_POLICY_VERSION,
        base_delays_ms=DEFAULT_RULES,
        fallback_base_delay_ms=1_000,
        max_delay_ms=30_000,
    )
    assert RETRY_DELAY_POLICY_VERSION == "1"
    assert isinstance(DEFAULT_RETRY_DELAY_POLICY.base_delays_ms, tuple)
    with pytest.raises(FrozenInstanceError):
        DEFAULT_RETRY_DELAY_POLICY.max_delay_ms = 1  # type: ignore[misc]


def test_default_delay_rules_cover_current_retryable_taxonomy_exactly() -> None:
    explicit_codes = {
        error_code
        for error_code, _ in DEFAULT_RETRY_DELAY_POLICY.base_delays_ms
    }

    assert explicit_codes == set(DEFAULT_RETRYABLE_ERROR_CODES)


@pytest.mark.parametrize("version", [None, "", " ", b"1"])
def test_policy_rejects_invalid_version(version: object) -> None:
    with pytest.raises(RetryDelayContractError) as captured:
        RetryDelayPolicy(
            version=version,  # type: ignore[arg-type]
            base_delays_ms=(),
            fallback_base_delay_ms=1,
            max_delay_ms=1,
        )

    assert captured.value.code == INVALID_RETRY_DELAY_CONTRACT


@pytest.mark.parametrize("rules", [[], {}, set(), None])
def test_policy_requires_tuple_rule_container(rules: object) -> None:
    with pytest.raises(RetryDelayContractError):
        RetryDelayPolicy(
            version="1",
            base_delays_ms=rules,  # type: ignore[arg-type]
            fallback_base_delay_ms=1,
            max_delay_ms=1,
        )


@pytest.mark.parametrize(
    "rules",
    [
        ((),),
        ((PROVIDER_TIMEOUT,),),
        ((PROVIDER_TIMEOUT, 1, 2),),
        ([PROVIDER_TIMEOUT, 1],),
    ],
)
def test_policy_rejects_malformed_rule_shape(rules: object) -> None:
    with pytest.raises(RetryDelayContractError):
        RetryDelayPolicy(
            version="1",
            base_delays_ms=rules,  # type: ignore[arg-type]
            fallback_base_delay_ms=1,
            max_delay_ms=1,
        )


@pytest.mark.parametrize("error_code", [None, "", " ", b"CODE", 1])
def test_policy_rejects_invalid_rule_error_code(error_code: object) -> None:
    with pytest.raises(RetryDelayContractError):
        RetryDelayPolicy(
            version="1",
            base_delays_ms=((error_code, 1),),  # type: ignore[arg-type]
            fallback_base_delay_ms=1,
            max_delay_ms=1,
        )


@pytest.mark.parametrize("delay", [True, False, 0, -1, 1.0, None])
def test_policy_rejects_invalid_rule_delay(delay: object) -> None:
    with pytest.raises(RetryDelayContractError):
        RetryDelayPolicy(
            version="1",
            base_delays_ms=((PROVIDER_TIMEOUT, delay),),  # type: ignore[arg-type]
            fallback_base_delay_ms=1,
            max_delay_ms=1,
        )


@pytest.mark.parametrize("fallback", [True, False, 0, -1, 1.0, None])
def test_policy_rejects_invalid_fallback_delay(fallback: object) -> None:
    with pytest.raises(RetryDelayContractError):
        RetryDelayPolicy(
            version="1",
            base_delays_ms=(),
            fallback_base_delay_ms=fallback,  # type: ignore[arg-type]
            max_delay_ms=1,
        )


@pytest.mark.parametrize("maximum", [True, False, 0, -1, 1.0, None])
def test_policy_rejects_invalid_maximum_delay(maximum: object) -> None:
    with pytest.raises(RetryDelayContractError):
        RetryDelayPolicy(
            version="1",
            base_delays_ms=(),
            fallback_base_delay_ms=1,
            max_delay_ms=maximum,  # type: ignore[arg-type]
        )


def test_policy_rejects_duplicate_error_code() -> None:
    with pytest.raises(RetryDelayContractError) as captured:
        custom_policy(
            rules=((PROVIDER_TIMEOUT, 1), (PROVIDER_TIMEOUT, 2)),
        )

    assert captured.value.code == INVALID_RETRY_DELAY_CONTRACT


@pytest.mark.parametrize("error_code", PERMANENT_NON_RETRYABLE_ERROR_CODES)
def test_policy_rejects_every_permanent_terminal_code(
    error_code: str,
) -> None:
    with pytest.raises(RetryDelayContractError) as captured:
        custom_policy(rules=((error_code, 1_000),))

    assert captured.value.code == INVALID_RETRY_DELAY_CONTRACT
    assert error_code not in captured.value.message


def test_empty_rules_are_legal_and_use_fallback() -> None:
    policy = custom_policy(rules=(), fallback=1_250, maximum=5_000)

    result = resolve_retry_delay(
        retry_decision=retry_decision("FUTURE_PROVIDER_ERROR"),
        policy=policy,
    )

    assert result.delay_ms == 1_250


def test_policy_rejects_rule_base_above_cap() -> None:
    with pytest.raises(RetryDelayContractError):
        custom_policy(
            rules=((PROVIDER_TIMEOUT, 1_001),),
            fallback=1,
            maximum=1_000,
        )


def test_policy_rejects_fallback_above_cap() -> None:
    with pytest.raises(RetryDelayContractError):
        custom_policy(fallback=1_001, maximum=1_000)


def test_normal_int_subclass_is_accepted_but_bool_is_not() -> None:
    class Milliseconds(int):
        pass

    policy = custom_policy(
        rules=((PROVIDER_TIMEOUT, Milliseconds(2_500)),),
        fallback=Milliseconds(1_000),
        maximum=Milliseconds(30_000),
    )

    assert resolve_retry_delay(
        retry_decision=retry_decision(PROVIDER_TIMEOUT),
        policy=policy,
    ).delay_ms == 2_500
    with pytest.raises(RetryDelayContractError):
        custom_policy(maximum=True)


@pytest.mark.parametrize(
    ("error_code", "expected_delay"),
    [
        (PROVIDER_TIMEOUT, 1_000),
        (PROVIDER_CONNECTION_FAILED, 1_000),
        (PROVIDER_UNAVAILABLE, 2_000),
        (PROVIDER_RATE_LIMITED, 5_000),
    ],
)
def test_default_first_retry_delays(
    error_code: str,
    expected_delay: int,
) -> None:
    result = resolve_retry_delay(
        retry_decision=retry_decision(error_code),
    )

    assert result == RetryDelayDecision(
        policy_version=RETRY_DELAY_POLICY_VERSION,
        error_code=error_code,
        attempts_completed=1,
        delay_ms=expected_delay,
    )


@pytest.mark.parametrize(
    ("error_code", "expected_delay"),
    [
        (PROVIDER_TIMEOUT, 2_000),
        (PROVIDER_CONNECTION_FAILED, 2_000),
        (PROVIDER_UNAVAILABLE, 4_000),
        (PROVIDER_RATE_LIMITED, 10_000),
    ],
)
def test_default_second_retry_delays(
    error_code: str,
    expected_delay: int,
) -> None:
    result = resolve_retry_delay(
        retry_decision=retry_decision(
            error_code,
            attempts_completed=2,
        ),
    )

    assert result.delay_ms == expected_delay


@pytest.mark.parametrize(
    ("error_code", "expected_delay"),
    [
        (PROVIDER_TIMEOUT, 3_000),
        (PROVIDER_CONNECTION_FAILED, 3_000),
        (PROVIDER_UNAVAILABLE, 6_000),
        (PROVIDER_RATE_LIMITED, 15_000),
    ],
)
def test_default_third_retry_delays(
    error_code: str,
    expected_delay: int,
) -> None:
    result = resolve_retry_delay(
        retry_decision=retry_decision(
            error_code,
            attempts_completed=3,
        ),
    )

    assert result.delay_ms == expected_delay


@pytest.mark.parametrize(
    ("error_code", "attempts_completed"),
    [
        (PROVIDER_TIMEOUT, 30),
        (PROVIDER_CONNECTION_FAILED, 31),
        (PROVIDER_UNAVAILABLE, 15),
        (PROVIDER_RATE_LIMITED, 6),
        (PROVIDER_RATE_LIMITED, 7),
    ],
)
def test_default_rules_saturate_at_cap(
    error_code: str,
    attempts_completed: int,
) -> None:
    result = resolve_retry_delay(
        retry_decision=retry_decision(
            error_code,
            attempts_completed=attempts_completed,
        ),
    )

    assert result.delay_ms == 30_000


def test_non_divisible_base_saturates_at_ceiling_boundary() -> None:
    policy = custom_policy(
        rules=((PROVIDER_TIMEOUT, 7_000),),
        maximum=30_000,
    )

    assert [
        resolve_retry_delay(
            retry_decision=retry_decision(
                PROVIDER_TIMEOUT,
                attempts_completed=attempt,
            ),
            policy=policy,
        ).delay_ms
        for attempt in (1, 2, 3, 4, 5)
    ] == [7_000, 14_000, 21_000, 28_000, 30_000]


def test_base_equal_to_cap_is_always_capped() -> None:
    policy = custom_policy(
        rules=((PROVIDER_TIMEOUT, 30_000),),
        fallback=30_000,
        maximum=30_000,
    )

    assert [
        resolve_retry_delay(
            retry_decision=retry_decision(
                PROVIDER_TIMEOUT,
                attempts_completed=attempt,
            ),
            policy=policy,
        ).delay_ms
        for attempt in (1, 2, 10**100)
    ] == [30_000, 30_000, 30_000]


def test_base_one_saturates_at_exact_boundary() -> None:
    policy = custom_policy(
        rules=((PROVIDER_TIMEOUT, 1),),
        maximum=30_000,
    )

    assert [
        resolve_retry_delay(
            retry_decision=retry_decision(
                PROVIDER_TIMEOUT,
                attempts_completed=attempt,
            ),
            policy=policy,
        ).delay_ms
        for attempt in (1, 29_999, 30_000, 30_001)
    ] == [1, 29_999, 30_000, 30_000]


def test_huge_attempt_counts_saturate_without_decimal_conversion() -> None:
    for exponent in (100, 5_000):
        attempts_completed = 10**exponent
        result = resolve_retry_delay(
            retry_decision=retry_decision(
                PROVIDER_RATE_LIMITED,
                attempts_completed=attempts_completed,
            ),
        )

        assert result.delay_ms == 30_000
        assert result.attempts_completed is attempts_completed


@pytest.mark.parametrize(
    ("error_code", "expected_delay"),
    [
        ("provider_rate_limited", 1_000),
        ("PROVIDER_RATE_LIMITED ", 1_000),
        ("Provider_RATE_LIMITED", 1_000),
        (PROVIDER_RATE_LIMITED, 5_000),
    ],
)
def test_error_code_identity_is_exact(
    error_code: str,
    expected_delay: int,
) -> None:
    result = resolve_retry_delay(
        retry_decision=retry_decision(error_code),
    )

    assert result.error_code == error_code
    assert result.delay_ms == expected_delay


def test_future_error_uses_fallback_and_saturates() -> None:
    first = resolve_retry_delay(
        retry_decision=retry_decision("FUTURE_PROVIDER_ERROR"),
    )
    second = resolve_retry_delay(
        retry_decision=retry_decision(
            "FUTURE_PROVIDER_ERROR",
            attempts_completed=2,
        ),
    )
    capped = resolve_retry_delay(
        retry_decision=retry_decision(
            "FUTURE_PROVIDER_ERROR",
            attempts_completed=31,
        ),
    )

    assert (first.delay_ms, second.delay_ms, capped.delay_ms) == (
        1_000,
        2_000,
        30_000,
    )


def test_custom_future_override_replaces_fallback() -> None:
    policy = custom_policy(
        rules=(("FUTURE_PROVIDER_ERROR", 3_000),),
    )

    result = resolve_retry_delay(
        retry_decision=retry_decision(
            "FUTURE_PROVIDER_ERROR",
            attempts_completed=2,
        ),
        policy=policy,
    )

    assert result.delay_ms == 6_000


def test_custom_future_override_first_second_and_cap() -> None:
    policy = custom_policy(
        rules=(("FUTURE_PROVIDER_ERROR", 3_000),),
    )

    assert [
        resolve_retry_delay(
            retry_decision=retry_decision(
                "FUTURE_PROVIDER_ERROR",
                attempts_completed=attempt,
            ),
            policy=policy,
        ).delay_ms
        for attempt in (1, 2, 10)
    ] == [3_000, 6_000, 30_000]


def test_custom_policy_can_change_current_transient_delay() -> None:
    policy = custom_policy(rules=((PROVIDER_TIMEOUT, 2_500),))

    result = resolve_retry_delay(
        retry_decision=retry_decision(PROVIDER_TIMEOUT),
        policy=policy,
    )

    assert result.delay_ms == 2_500


def test_custom_current_transient_applies_linear_second_delay() -> None:
    policy = custom_policy(rules=((PROVIDER_TIMEOUT, 2_500),))

    assert [
        resolve_retry_delay(
            retry_decision=retry_decision(
                PROVIDER_TIMEOUT,
                attempts_completed=attempt,
            ),
            policy=policy,
        ).delay_ms
        for attempt in (1, 2)
    ] == [2_500, 5_000]


def test_rule_order_does_not_change_resolved_mapping() -> None:
    rules = (
        (PROVIDER_TIMEOUT, 1_000),
        (PROVIDER_RATE_LIMITED, 5_000),
        ("FUTURE_PROVIDER_ERROR", 3_000),
    )
    forward = custom_policy(rules=rules, fallback=1_250)
    reversed_policy = custom_policy(
        rules=tuple(reversed(rules)),
        fallback=1_250,
    )

    for error_code in (
        PROVIDER_TIMEOUT,
        PROVIDER_RATE_LIMITED,
        "FUTURE_PROVIDER_ERROR",
        "UNKNOWN_PROVIDER_ERROR",
    ):
        assert resolve_retry_delay(
            retry_decision=retry_decision(
                error_code,
                attempts_completed=2,
            ),
            policy=forward,
        ) == resolve_retry_delay(
            retry_decision=retry_decision(
                error_code,
                attempts_completed=2,
            ),
            policy=reversed_policy,
        )


@pytest.mark.parametrize(
    "decision",
    [
        RetryDecision(
            policy_version="1",
            error_code=PROVIDER_AUTH_FAILED,
            action=DO_NOT_RETRY,
            reason=ERROR_NOT_RETRYABLE,
            attempts_completed=1,
            max_attempts=2,
        ),
        RetryDecision(
            policy_version="1",
            error_code=PROVIDER_TIMEOUT,
            action=DO_NOT_RETRY,
            reason=ATTEMPT_LIMIT_REACHED,
            attempts_completed=2,
            max_attempts=2,
        ),
    ],
)
def test_do_not_retry_decision_is_rejected(decision: RetryDecision) -> None:
    with pytest.raises(RetryDelayContractError) as captured:
        resolve_retry_delay(retry_decision=decision)

    assert captured.value.code == INVALID_RETRY_DELAY_CONTRACT


@pytest.mark.parametrize("decision", [None, {}, object()])
def test_resolver_rejects_wrong_retry_decision_type(decision: object) -> None:
    with pytest.raises(RetryDelayContractError) as captured:
        resolve_retry_delay(retry_decision=decision)  # type: ignore[arg-type]

    assert captured.value.code == INVALID_RETRY_DELAY_CONTRACT


@pytest.mark.parametrize("policy", [{}, [], object()])
def test_resolver_rejects_wrong_policy_type(policy: object) -> None:
    with pytest.raises(RetryDelayContractError):
        resolve_retry_delay(
            retry_decision=retry_decision(PROVIDER_TIMEOUT),
            policy=policy,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("policy_version", None),
        ("policy_version", ""),
        ("policy_version", " "),
        ("policy_version", b"1"),
        ("policy_version", 1),
        ("error_code", None),
        ("error_code", ""),
        ("error_code", " "),
        ("error_code", b"CODE"),
        ("error_code", 1),
        ("attempts_completed", True),
        ("attempts_completed", 0),
        ("attempts_completed", -1),
        ("attempts_completed", 1.0),
        ("attempts_completed", None),
        ("delay_ms", True),
        ("delay_ms", 0),
        ("delay_ms", -1),
        ("delay_ms", 1.0),
        ("delay_ms", None),
    ],
)
def test_delay_decision_direct_contract_rejects_invalid_fields(
    field_name: str,
    value: object,
) -> None:
    fields: dict[str, object] = {
        "policy_version": "1",
        "error_code": PROVIDER_TIMEOUT,
        "attempts_completed": 1,
        "delay_ms": 1_000,
    }
    fields[field_name] = value

    with pytest.raises(RetryDelayContractError):
        RetryDelayDecision(**fields)  # type: ignore[arg-type]


def test_delay_decision_is_frozen_and_has_no_cross_policy_cap_validation() -> None:
    decision = RetryDelayDecision(
        policy_version="historical-policy",
        error_code=" Exact_Code ",
        attempts_completed=1,
        delay_ms=10**100,
    )

    assert decision.error_code == " Exact_Code "
    assert decision.delay_ms == 10**100
    with pytest.raises(FrozenInstanceError):
        decision.delay_ms = 1  # type: ignore[misc]


def test_resolver_preserves_policy_and_retry_decision_linkage_exactly() -> None:
    policy = custom_policy(
        version=" Delay-V1 ",
        rules=((" Exact_Code ", 3_000),),
    )
    source_decision = retry_decision(" Exact_Code ", attempts_completed=2)

    result = resolve_retry_delay(
        retry_decision=source_decision,
        policy=policy,
    )

    assert result == RetryDelayDecision(
        policy_version=" Delay-V1 ",
        error_code=" Exact_Code ",
        attempts_completed=2,
        delay_ms=6_000,
    )


def test_resolver_is_deterministic_and_does_not_mutate_policy() -> None:
    policy = custom_policy(
        rules=(("FUTURE_PROVIDER_ERROR", 3_000),),
    )
    source_decision = retry_decision(
        "FUTURE_PROVIDER_ERROR",
        attempts_completed=3,
    )
    original_rules = policy.base_delays_ms

    results = tuple(
        resolve_retry_delay(
            retry_decision=source_decision,
            policy=policy,
        )
        for _ in range(100)
    )

    assert all(result == results[0] for result in results)
    assert policy.base_delays_ms is original_rules
    assert source_decision.attempts_completed == 3


def test_default_policy_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = custom_policy(
        version="replacement",
        rules=((PROVIDER_TIMEOUT, 2_500),),
        fallback=2_000,
        maximum=5_000,
    )
    monkeypatch.setattr(
        delay_module,
        "DEFAULT_RETRY_DELAY_POLICY",
        replacement,
    )

    result = resolve_retry_delay(
        retry_decision=retry_decision(PROVIDER_TIMEOUT),
    )

    assert result.policy_version == "replacement"
    assert result.delay_ms == 2_500


def test_public_errors_do_not_render_rule_values_or_huge_integers() -> None:
    secret_code = "SECRET_DELAY_RULE"
    huge_delay = 10**5_000

    with pytest.raises(RetryDelayContractError) as captured:
        custom_policy(
            rules=((secret_code, huge_delay),),
            fallback=1,
            maximum=1,
        )

    assert captured.value.code == INVALID_RETRY_DELAY_CONTRACT
    assert secret_code not in str(captured.value)
    assert "10000000000000000000" not in str(captured.value)


def test_delay_module_has_no_execution_time_random_network_or_domain_imports() -> None:
    source_path = Path(delay_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
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

    assert imports == {"__future__", "dataclasses", "src.insight_retry"}
    assert "evaluate_retry" not in source
    assert "range(" not in source
    assert "time.sleep" not in source
    assert "asyncio.sleep" not in source
    assert "Retry-After" not in source
    assert "random" not in imports
    assert "secrets" not in imports
    assert "uuid" not in imports
    assert "datetime" not in imports
    assert "time" not in imports
    assert "math" not in imports
    assert "os" not in imports
    assert "src.insight_provider" not in imports
    assert "src.deepseek_provider" not in imports
    assert "src.insight_retry_execution" not in imports
    assert "src.insight_attempt_audit" not in imports
    assert "src.insight_cost_audit" not in imports
    assert "src.insight_pricing" not in imports
    assert "src.insight_receipt" not in imports
    assert "app" not in imports


def test_retry_execution_integrates_delay_but_app_remains_isolated() -> None:
    root = Path(__file__).parents[1]
    execution_source = (root / "src" / "insight_retry_execution.py").read_text(
        encoding="utf-8"
    )
    app_source = (root / "app.py").read_text(encoding="utf-8")

    assert "insight_retry_delay" in execution_source
    assert "resolve_retry_delay" in execution_source
    assert "insight_retry_delay" not in app_source
    assert "resolve_retry_delay" not in app_source


def test_retry_decision_is_consumed_without_reevaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("delay resolver must not reevaluate retry eligibility")

    monkeypatch.setattr("src.insight_retry.evaluate_retry", forbidden)
    source_decision = retry_decision("FUTURE_PROVIDER_ERROR")

    result = resolve_retry_delay(retry_decision=source_decision)

    assert result.delay_ms == 1_000


def test_existing_retry_evaluator_still_produces_compatible_retry_decision() -> None:
    decision = evaluate_retry(
        error_code=PROVIDER_TIMEOUT,
        attempts_completed=1,
    )

    result = resolve_retry_delay(retry_decision=decision)

    assert decision.action == RETRY
    assert result.delay_ms == 1_000
