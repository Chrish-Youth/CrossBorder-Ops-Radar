from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import src.insight_retry_execution as execution_module
from src.insight_attempt_audit import (
    FAILED,
    MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS,
    SUCCEEDED,
    AttemptAuditTrail,
)
from src.insight_cost_audit import (
    AVAILABLE,
    UNAVAILABLE,
    build_cost_audit_metadata,
)
from src.insight_pricing import (
    CACHE_BREAKDOWN_UNAVAILABLE,
    OFF_PEAK,
    PEAK,
    POLICY_NOT_APPLICABLE,
    POLICY_NOT_EFFECTIVE,
    USAGE_UNAVAILABLE,
)
from src.insight_prompt import (
    INVALID_INSIGHT_OUTPUT,
    InsightOutputError,
    InsightPromptError,
)
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
    InsightProviderError,
    ProviderGeneration,
    ProviderUsage,
)
from src.insight_retry import (
    ATTEMPT_LIMIT_REACHED,
    DEFAULT_RETRY_POLICY,
    DO_NOT_RETRY,
    ERROR_NOT_RETRYABLE,
    RETRY,
    RETRYABLE_TRANSIENT_ERROR,
    RetryPolicy,
)
from src.insight_retry_delay import RetryDelayDecision, RetryDelayPolicy
from src.insight_retry_delay_execution import (
    RETRY_DELAY_EXECUTION_VERSION,
    RetryDelayExecutionAudit,
    RetryDelayExecutionRecord,
)
from src.insight_retry_execution import (
    INVALID_RETRY_EXECUTION,
    RETRY_EXECUTION_VERSION,
    RetryExecutionError,
    RetryExecutionResult,
    execute_insight_generation_with_retry,
)
from src.insights import InsightContext, build_insight_context
from src.pipeline import run_pipeline


UTC = timezone.utc
PROVIDER = "deepseek"
MODEL = "deepseek-v4-flash"
SAMPLE_PATH = Path(__file__).parents[1] / "data" / "sample_ecommerce_data.csv"
MONDAY_PEAK = datetime(2026, 8, 17, 1, 0, tzinfo=UTC)

PERMANENT_CODES = (
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
TRANSIENT_CODES = (
    PROVIDER_TIMEOUT,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_RATE_LIMITED,
    PROVIDER_UNAVAILABLE,
)


@pytest.fixture(scope="module")
def sample_context() -> InsightContext:
    return build_insight_context(run_pipeline(SAMPLE_PATH, group_by="sku"))


def complete_usage() -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=400,
        reasoning_tokens=100,
    )


def incomplete_cache_usage() -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
    )


def valid_raw_response(context: InsightContext) -> str:
    signal = next(
        item
        for item in context.diagnostic_signals
        if item["group"] == {"sku": "SKU-LOW-CTR"}
        and item["code"] == "HIGH_IMPRESSIONS_LOW_CTR"
    )
    return json.dumps(
        {
            "version": "1",
            "executive_summary": "One diagnostic pattern warrants review.",
            "priority_insights": [
                {
                    "scope": signal["group"],
                    "observation": "The supplied context contains this signal.",
                    "evidence_codes": [signal["code"]],
                    "possible_explanations": [
                        "A possible association may warrant investigation."
                    ],
                    "recommended_checks": [
                        "Review the supporting operational inputs."
                    ],
                    "confidence": "medium",
                }
            ],
            "overall_limitations": [],
        },
        ensure_ascii=False,
        allow_nan=False,
    )


class ScriptedProvider:
    """Offline DeepSeek adapter double with canonical owned identity."""

    provider_name = PROVIDER
    model = MODEL

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = outcomes
        self.call_count = 0

    def generate(self, prompt: object) -> ProviderGeneration:
        del prompt
        outcome = self._outcomes[self.call_count]
        self.call_count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, ProviderGeneration)
        return outcome


class GenericScriptedProvider(ScriptedProvider):
    """Auditable non-DeepSeek adapter double for unavailable pricing tests."""

    provider_name = "scripted"
    model = "scripted-model"


class IdentityScriptedProvider(ScriptedProvider):
    def __init__(
        self,
        *outcomes: object,
        provider_name: object,
        model: object,
    ) -> None:
        super().__init__(*outcomes)
        self.provider_name = provider_name  # type: ignore[assignment]
        self.model = model  # type: ignore[assignment]


class CountingIdentityProvider(ScriptedProvider):
    def __init__(self, *outcomes: object) -> None:
        super().__init__(*outcomes)
        self.provider_name_reads = 0
        self.model_reads = 0

    @property
    def provider_name(self) -> str:
        self.provider_name_reads += 1
        return PROVIDER

    @property
    def model(self) -> str:
        self.model_reads += 1
        return MODEL


class ScriptedClock:
    def __init__(self, *values: object) -> None:
        self._values = values
        self.call_count = 0

    def __call__(self) -> object:
        value = self._values[self.call_count]
        self.call_count += 1
        if isinstance(value, BaseException):
            raise value
        return value


class RecordingSleeper:
    def __init__(self, events: list[object] | None = None) -> None:
        self.calls: list[int] = []
        self._events = events

    def __call__(self, delay_ms: int) -> None:
        self.calls.append(delay_ms)
        if self._events is not None:
            self._events.append(("sleep", delay_ms))


def success_generation(
    context: InsightContext,
    *,
    usage: ProviderUsage | None = None,
) -> ProviderGeneration:
    return ProviderGeneration(
        raw_text=valid_raw_response(context),
        usage=usage,
    )


def execute(
    context: InsightContext,
    provider: object,
    *,
    clock: object = None,
    policy: RetryPolicy | None = None,
    delay_policy: RetryDelayPolicy | None = None,
    sleeper: object = None,
) -> RetryExecutionResult:
    active_sleeper = RecordingSleeper() if sleeper is None else sleeper
    return execute_insight_generation_with_retry(
        context,
        provider=provider,  # type: ignore[arg-type]
        retry_policy=policy,
        retry_delay_policy=delay_policy,
        utc_now=(lambda: MONDAY_PEAK) if clock is None else clock,  # type: ignore[arg-type]
        sleeper=active_sleeper,  # type: ignore[arg-type]
    )


def test_first_attempt_success_returns_linked_immutable_result(
    sample_context: InsightContext,
) -> None:
    usage = complete_usage()
    provider = ScriptedProvider(success_generation(sample_context, usage=usage))
    clock = ScriptedClock(MONDAY_PEAK)

    result = execute(sample_context, provider, clock=clock)

    assert result.version == RETRY_EXECUTION_VERSION == "2"
    assert result.status == SUCCEEDED
    assert result.output is not None
    assert result.final_usage is usage
    assert result.final_cost is result.attempt_audit.attempts[-1].cost
    assert result.final_cost is not None
    assert result.final_cost.status == AVAILABLE
    assert result.error_code is None
    assert result.attempt_audit.outcome == SUCCEEDED
    assert len(result.attempt_audit.attempts) == 1
    assert result.delay_audit == RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version="1",
        records=(),
    )
    assert provider.call_count == clock.call_count == 1
    with pytest.raises(FrozenInstanceError):
        result.status = FAILED  # type: ignore[misc]


@pytest.mark.parametrize(
    ("error_code", "expected_delay"),
    [
        (PROVIDER_TIMEOUT, 1_000),
        (PROVIDER_CONNECTION_FAILED, 1_000),
        (PROVIDER_RATE_LIMITED, 5_000),
        (PROVIDER_UNAVAILABLE, 2_000),
    ],
)
def test_each_default_transient_retries_once_then_succeeds(
    sample_context: InsightContext,
    error_code: str,
    expected_delay: int,
) -> None:
    provider = ScriptedProvider(
        InsightProviderError(error_code, "SECRET_TRANSIENT"),
        success_generation(sample_context, usage=complete_usage()),
    )
    clock = ScriptedClock(
        MONDAY_PEAK,
        datetime(2026, 8, 17, 1, 0, 1, tzinfo=UTC),
    )
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        provider,
        clock=clock,
        sleeper=sleeper,
    )

    first, second = result.attempt_audit.attempts
    assert result.status == SUCCEEDED
    assert first.error_code == error_code
    assert first.retry_decision is not None
    assert first.retry_decision.action == RETRY
    assert first.retry_decision.reason == RETRYABLE_TRANSIENT_ERROR
    assert second.status == SUCCEEDED
    assert provider.call_count == clock.call_count == 2
    assert sleeper.calls == [expected_delay]
    assert len(result.delay_audit.records) == 1
    delay_record = result.delay_audit.records[0]
    assert delay_record.after_attempt_number == 1
    assert delay_record.delay_decision.error_code == error_code
    assert delay_record.delay_decision.delay_ms == expected_delay
    assert "SECRET_TRANSIENT" not in repr(result.attempt_audit.to_dict())


@pytest.mark.parametrize("error_code", PERMANENT_CODES)
def test_every_permanent_code_is_terminal_after_one_attempt(
    sample_context: InsightContext,
    error_code: str,
) -> None:
    provider = ScriptedProvider(
        InsightProviderError(error_code, "SECRET_TERMINAL")
    )
    clock = ScriptedClock(MONDAY_PEAK)
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        provider,
        clock=clock,
        sleeper=sleeper,
    )

    attempt = result.attempt_audit.attempts[0]
    assert result.status == FAILED
    assert result.output is None
    assert result.final_usage is None
    assert result.final_cost is None
    assert result.error_code == error_code == attempt.error_code
    assert attempt.retry_decision is not None
    assert attempt.retry_decision.action == DO_NOT_RETRY
    assert attempt.retry_decision.reason == ERROR_NOT_RETRYABLE
    assert provider.call_count == clock.call_count == 1
    assert sleeper.calls == []
    assert result.delay_audit.records == ()
    assert "SECRET_TERMINAL" not in repr(result.attempt_audit.to_dict())


def test_retry_exhaustion_uses_limit_priority(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        InsightProviderError(PROVIDER_CONNECTION_FAILED, "second"),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK)

    result = execute(sample_context, provider, clock=clock)

    assert result.status == FAILED
    assert result.error_code == PROVIDER_CONNECTION_FAILED
    assert len(result.attempt_audit.attempts) == 2
    final_decision = result.attempt_audit.attempts[-1].retry_decision
    assert final_decision is not None
    assert final_decision.action == DO_NOT_RETRY
    assert final_decision.reason == ATTEMPT_LIMIT_REACHED
    assert provider.call_count == clock.call_count == 2


def test_retryable_then_terminal_stops_without_third_call(
    sample_context: InsightContext,
) -> None:
    policy = RetryPolicy(
        version="three-attempt-policy",
        max_attempts=3,
        retryable_error_codes=TRANSIENT_CODES,
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        InsightProviderError(PROVIDER_AUTH_FAILED, "second"),
        success_generation(sample_context),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK, MONDAY_PEAK)
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        provider,
        clock=clock,
        policy=policy,
        sleeper=sleeper,
    )

    assert result.status == FAILED
    assert result.error_code == PROVIDER_AUTH_FAILED
    assert provider.call_count == clock.call_count == 2
    assert sleeper.calls == [1_000]
    assert len(result.delay_audit.records) == 1
    decision = result.attempt_audit.attempts[-1].retry_decision
    assert decision is not None
    assert decision.reason == ERROR_NOT_RETRYABLE


def test_custom_max_one_disables_second_attempt(
    sample_context: InsightContext,
) -> None:
    policy = RetryPolicy(
        version="one-attempt",
        max_attempts=1,
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "timeout"),
        success_generation(sample_context),
    )

    result = execute(sample_context, provider, policy=policy)

    assert result.status == FAILED
    assert provider.call_count == 1
    decision = result.attempt_audit.attempts[0].retry_decision
    assert decision is not None
    assert decision.reason == ATTEMPT_LIMIT_REACHED


def test_custom_max_three_executes_three_and_succeeds(
    sample_context: InsightContext,
) -> None:
    policy = RetryPolicy(
        version="three-attempts",
        max_attempts=3,
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        InsightProviderError(PROVIDER_TIMEOUT, "second"),
        success_generation(sample_context, usage=complete_usage()),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK, MONDAY_PEAK)
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        provider,
        clock=clock,
        policy=policy,
        sleeper=sleeper,
    )

    assert result.status == SUCCEEDED
    assert len(result.attempt_audit.attempts) == 3
    assert provider.call_count == clock.call_count == 3
    assert sleeper.calls == [1_000, 2_000]
    assert [
        item.delay_decision.delay_ms
        for item in result.delay_audit.records
    ] == [1_000, 2_000]


def test_custom_max_three_full_failure_has_exactly_two_delay_records(
    sample_context: InsightContext,
) -> None:
    policy = RetryPolicy(
        version="three-failed-attempts",
        max_attempts=3,
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        InsightProviderError(PROVIDER_TIMEOUT, "second"),
        InsightProviderError(PROVIDER_TIMEOUT, "third"),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK, MONDAY_PEAK)
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        provider,
        clock=clock,
        policy=policy,
        sleeper=sleeper,
    )

    assert result.status == FAILED
    assert len(result.attempt_audit.attempts) == 3
    assert len(result.delay_audit.records) == 2
    assert sleeper.calls == [1_000, 2_000]


def test_custom_policy_can_disable_timeout_retry(
    sample_context: InsightContext,
) -> None:
    policy = RetryPolicy(
        version="no-timeout-retry",
        max_attempts=3,
        retryable_error_codes=(),
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "timeout")
    )

    result = execute(sample_context, provider, policy=policy)

    assert result.status == FAILED
    assert provider.call_count == 1
    decision = result.attempt_audit.attempts[0].retry_decision
    assert decision is not None
    assert decision.reason == ERROR_NOT_RETRYABLE


def test_custom_future_code_retries_but_default_fails_closed(
    sample_context: InsightContext,
) -> None:
    future_code = "FUTURE_PROVIDER_ERROR"
    default_provider = ScriptedProvider(
        InsightProviderError(future_code, "future")
    )

    default_result = execute(sample_context, default_provider)

    assert default_result.status == FAILED
    assert default_provider.call_count == 1

    custom = RetryPolicy(
        version="future-enabled",
        max_attempts=2,
        retryable_error_codes=(future_code,),
    )
    custom_provider = ScriptedProvider(
        InsightProviderError(future_code, "future"),
        success_generation(sample_context),
    )
    custom_result = execute(sample_context, custom_provider, policy=custom)

    assert custom_result.status == SUCCEEDED
    assert custom_provider.call_count == 2


@pytest.mark.parametrize("provider", [None, object()])
def test_noninvokable_provider_is_a_pre_attempt_hard_failure(
    sample_context: InsightContext,
    provider: object,
) -> None:
    clock = ScriptedClock(MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(sample_context, provider, clock=clock)

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert clock.call_count == 0


def test_callable_provider_raising_invalid_provider_is_an_audited_attempt(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(
        InsightProviderError(INVALID_PROVIDER, "SECRET_PROVIDER_FAILURE")
    )
    clock = ScriptedClock(MONDAY_PEAK)

    result = execute(sample_context, provider, clock=clock)

    assert result.status == FAILED
    assert result.error_code == INVALID_PROVIDER
    assert len(result.attempt_audit.attempts) == 1
    assert provider.call_count == clock.call_count == 1
    assert "SECRET_PROVIDER_FAILURE" not in repr(result.attempt_audit.to_dict())


def test_ordinary_provider_exception_is_mapped_by_sealed_single_attempt(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(RuntimeError("SECRET_INTERNAL"))

    result = execute(sample_context, provider)

    assert result.status == FAILED
    assert result.error_code == PROVIDER_FAILURE
    assert provider.call_count == 1
    assert "SECRET_INTERNAL" not in repr(result.attempt_audit.to_dict())


def test_output_contract_failure_is_a_terminal_handled_attempt(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(
        ProviderGeneration(
            raw_text=json.dumps(
                {
                    "version": "1",
                    "executive_summary": "invalid output",
                    "priority_insights": "not-an-array",
                    "overall_limitations": [],
                }
            )
        )
    )

    result = execute(sample_context, provider)

    assert result.status == FAILED
    assert result.error_code == INVALID_INSIGHT_OUTPUT
    assert provider.call_count == 1
    decision = result.attempt_audit.attempts[0].retry_decision
    assert decision is not None
    assert decision.action == DO_NOT_RETRY
    assert decision.reason == ERROR_NOT_RETRYABLE


def test_prompt_contract_exception_is_an_internal_hard_failure(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_generation(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise InsightPromptError("INVALID_PROMPT_INPUT", "SECRET_PROMPT")

    monkeypatch.setattr(
        execution_module,
        "generate_insight_with_metadata",
        fail_generation,
    )
    provider = ScriptedProvider()

    with pytest.raises(RetryExecutionError) as captured:
        execute(sample_context, provider)

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_PROMPT" not in str(captured.value)


def test_real_prompt_failure_occurs_after_clock_but_before_provider_invocation(
    sample_context: InsightContext,
) -> None:
    invalid_context = replace(sample_context, version="unsupported")
    provider = CountingIdentityProvider(success_generation(sample_context))
    clock = ScriptedClock(MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(invalid_context, provider, clock=clock)

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert provider.call_count == 0
    assert clock.call_count == 1


def test_each_attempt_uses_a_fresh_reference_and_final_cost_uses_success_time(
    sample_context: InsightContext,
) -> None:
    first = datetime(2026, 8, 17, 3, 59, 59, tzinfo=UTC)
    second = datetime(2026, 8, 17, 4, 0, 5, tzinfo=UTC)
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "timeout"),
        success_generation(sample_context, usage=complete_usage()),
    )

    result = execute(
        sample_context,
        provider,
        clock=ScriptedClock(first, second),
    )

    assert [
        attempt.pricing_reference_at
        for attempt in result.attempt_audit.attempts
    ] == [first.isoformat(), second.isoformat()]
    assert result.final_cost is not None
    assert result.final_cost.pricing_reference_at == second.isoformat()
    assert result.final_cost.estimate is not None
    assert result.final_cost.estimate.pricing_tier == OFF_PEAK


def test_off_peak_to_peak_boundary_uses_second_attempt_reference(
    sample_context: InsightContext,
) -> None:
    first = datetime(2026, 8, 17, 5, 59, 59, tzinfo=UTC)
    second = datetime(2026, 8, 17, 6, 0, 5, tzinfo=UTC)
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "timeout"),
        success_generation(sample_context, usage=complete_usage()),
    )

    result = execute(
        sample_context,
        provider,
        clock=ScriptedClock(first, second),
    )

    assert result.final_cost is not None
    assert result.final_cost.estimate is not None
    assert result.final_cost.estimate.pricing_tier == PEAK
    assert result.final_cost.pricing_reference_at == second.isoformat()


@pytest.mark.parametrize(
    ("usage", "provider_type", "reference", "reason"),
    [
        (None, ScriptedProvider, MONDAY_PEAK, USAGE_UNAVAILABLE),
        (
            incomplete_cache_usage(),
            ScriptedProvider,
            MONDAY_PEAK,
            CACHE_BREAKDOWN_UNAVAILABLE,
        ),
        (
            complete_usage(),
            GenericScriptedProvider,
            MONDAY_PEAK,
            POLICY_NOT_APPLICABLE,
        ),
        (
            complete_usage(),
            ScriptedProvider,
            datetime(2026, 8, 16, 15, 59, 59, tzinfo=UTC),
            POLICY_NOT_EFFECTIVE,
        ),
    ],
)
def test_known_cost_unavailable_states_remain_success(
    sample_context: InsightContext,
    usage: ProviderUsage | None,
    provider_type: type[ScriptedProvider],
    reference: datetime,
    reason: str,
) -> None:
    provider = provider_type(success_generation(sample_context, usage=usage))

    result = execute(
        sample_context,
        provider,
        clock=ScriptedClock(reference),
    )

    assert result.status == SUCCEEDED
    assert result.final_cost is not None
    assert result.final_cost.status == UNAVAILABLE
    assert result.final_cost.unavailable_reason == reason


def test_evaluator_runs_once_per_failed_attempt_and_cost_only_on_success(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision_calls = 0
    cost_calls = 0
    real_evaluate = execution_module.evaluate_retry
    real_cost = execution_module.build_cost_audit_metadata

    def counted_evaluate(**kwargs: object) -> object:
        nonlocal decision_calls
        decision_calls += 1
        return real_evaluate(**kwargs)  # type: ignore[arg-type]

    def counted_cost(*args: object, **kwargs: object) -> object:
        nonlocal cost_calls
        cost_calls += 1
        return real_cost(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module, "evaluate_retry", counted_evaluate)
    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        counted_cost,
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "timeout"),
        success_generation(sample_context, usage=complete_usage()),
    )

    result = execute(sample_context, provider)

    assert result.status == SUCCEEDED
    assert decision_calls == 1
    assert cost_calls == 1


def test_cost_builder_is_never_called_for_exhausted_failure(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("cost builder must not run for failed attempts")

    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        forbidden,
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "one"),
        InsightProviderError(PROVIDER_TIMEOUT, "two"),
    )

    result = execute(sample_context, provider)

    assert result.status == FAILED
    assert provider.call_count == 2


def test_old_caller_identity_arguments_are_rejected_by_the_api(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(success_generation(sample_context))
    clock = ScriptedClock(MONDAY_PEAK)

    with pytest.raises(TypeError):
        execute_insight_generation_with_retry(
            sample_context,
            provider=provider,
            provider_name=PROVIDER,  # type: ignore[call-arg]
            model=MODEL,  # type: ignore[call-arg]
            utc_now=clock,  # type: ignore[arg-type]
        )

    assert provider.call_count == clock.call_count == 0


def test_provider_identity_is_resolved_once_and_reused_for_all_attempts(
    sample_context: InsightContext,
) -> None:
    policy = RetryPolicy(
        version="three-attempts",
        max_attempts=3,
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    provider = CountingIdentityProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "one"),
        InsightProviderError(PROVIDER_TIMEOUT, "two"),
        success_generation(sample_context, usage=complete_usage()),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK, MONDAY_PEAK)

    result = execute(sample_context, provider, policy=policy, clock=clock)

    assert provider.provider_name_reads == 1
    assert provider.model_reads == 1
    assert provider.call_count == clock.call_count == 3
    assert {
        (attempt.provider, attempt.model)
        for attempt in result.attempt_audit.attempts
    } == {(PROVIDER, MODEL)}
    assert result.final_cost is not None
    assert result.final_cost.status == AVAILABLE


def test_cost_builder_receives_the_same_owned_identity_as_attempt_audit(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    real_cost = execution_module.build_cost_audit_metadata

    def capture_cost(*args: object, **kwargs: object) -> object:
        captured.append(dict(kwargs))
        return real_cost(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        capture_cost,
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context, usage=complete_usage()),
    )
    first = MONDAY_PEAK
    second = datetime(2026, 8, 17, 1, 0, 1, tzinfo=UTC)

    result = execute(
        sample_context,
        provider,
        clock=ScriptedClock(first, second),
    )

    assert captured == [
        {
            "provider": PROVIDER,
            "model": MODEL,
            "pricing_reference_at": second,
        }
    ]
    assert [
        (attempt.provider, attempt.model)
        for attempt in result.attempt_audit.attempts
    ] == [(PROVIDER, MODEL), (PROVIDER, MODEL)]


def test_success_cost_failure_is_sanitized_without_retry_or_partial_result(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator_calls = 0
    cost_calls = 0
    real_evaluator = execution_module.evaluate_retry

    def counted_evaluator(**kwargs: object) -> object:
        nonlocal evaluator_calls
        evaluator_calls += 1
        return real_evaluator(**kwargs)  # type: ignore[arg-type]

    def fail_cost(*args: object, **kwargs: object) -> object:
        nonlocal cost_calls
        del args, kwargs
        cost_calls += 1
        raise RuntimeError("SECRET_COST_FAILURE")

    monkeypatch.setattr(execution_module, "evaluate_retry", counted_evaluator)
    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        fail_cost,
    )
    provider = ScriptedProvider(
        success_generation(sample_context, usage=complete_usage()),
        success_generation(sample_context, usage=complete_usage()),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(sample_context, provider, clock=clock)

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_COST_FAILURE" not in str(captured.value)
    assert provider.call_count == clock.call_count == cost_calls == 1
    assert evaluator_calls == 0


def test_unauditable_provider_matrix_hard_fails_before_any_accounting(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingGenerate:
        provider_name = PROVIDER
        model = MODEL

    class NoncallableGenerate:
        provider_name = PROVIDER
        model = MODEL
        generate = None

    class MissingProviderName:
        model = MODEL
        call_count = 0

        def generate(self, prompt: object) -> ProviderGeneration:
            del prompt
            self.call_count += 1
            raise AssertionError("generate must not be called")

    class MissingModel:
        provider_name = PROVIDER
        call_count = 0

        def generate(self, prompt: object) -> ProviderGeneration:
            del prompt
            self.call_count += 1
            raise AssertionError("generate must not be called")

    class RaisingGenerateAccessor:
        provider_name = PROVIDER
        model = MODEL

        @property
        def generate(self) -> object:
            raise RuntimeError("SECRET_GENERATE_ACCESSOR")

    providers = (
        None,
        object(),
        MissingGenerate(),
        NoncallableGenerate(),
        MissingProviderName(),
        MissingModel(),
        RaisingGenerateAccessor(),
    )

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("preflight failure must not reach cost or retry evaluation")

    monkeypatch.setattr(execution_module, "evaluate_retry", forbidden)
    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        forbidden,
    )

    for provider in providers:
        clock = ScriptedClock(MONDAY_PEAK)
        with pytest.raises(RetryExecutionError) as captured:
            execute(sample_context, provider, clock=clock)
        assert captured.value.code == INVALID_RETRY_EXECUTION
        assert "SECRET_GENERATE_ACCESSOR" not in str(captured.value)
        assert clock.call_count == 0
        assert getattr(provider, "call_count", 0) == 0


@pytest.mark.parametrize("identity_field", ["provider_name", "model"])
def test_identity_getter_failure_is_sanitized_before_attempt_one(
    sample_context: InsightContext,
    identity_field: str,
) -> None:
    class RaisingIdentityProvider(ScriptedProvider):
        @property
        def provider_name(self) -> str:
            if identity_field == "provider_name":
                raise RuntimeError("SECRET_IDENTITY_VALUE")
            return PROVIDER

        @property
        def model(self) -> str:
            if identity_field == "model":
                raise RuntimeError("SECRET_IDENTITY_VALUE")
            return MODEL

    provider = RaisingIdentityProvider(success_generation(sample_context))
    clock = ScriptedClock(MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(sample_context, provider, clock=clock)

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_IDENTITY_VALUE" not in str(captured.value)
    assert provider.call_count == clock.call_count == 0


def test_adapter_identity_is_preserved_exactly_without_normalization(
    sample_context: InsightContext,
) -> None:
    provider = IdentityScriptedProvider(
        success_generation(sample_context),
        provider_name=" DeepSeek ",
        model="DeepSeek-V4-Flash",
    )

    result = execute(sample_context, provider)

    attempt = result.attempt_audit.attempts[0]
    assert (attempt.provider, attempt.model) == (
        " DeepSeek ",
        "DeepSeek-V4-Flash",
    )
    assert result.final_cost is not None
    assert result.final_cost.status == UNAVAILABLE
    assert result.final_cost.unavailable_reason == POLICY_NOT_APPLICABLE


@pytest.mark.parametrize(
    (
        "case",
        "expected_provider",
        "expected_clock",
        "expected_cost",
        "expected_eval",
        "expected_delay",
    ),
    [
        ("success", 1, 1, 1, 0, 0),
        ("timeout-success", 2, 2, 1, 1, 1),
        ("timeout-timeout", 2, 2, 0, 2, 1),
        ("auth", 1, 1, 0, 1, 0),
        ("max-one-timeout", 1, 1, 0, 1, 0),
        ("three-success", 3, 3, 1, 2, 2),
    ],
)
def test_completed_execution_mechanical_accounting_matrix(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_provider: int,
    expected_clock: int,
    expected_cost: int,
    expected_eval: int,
    expected_delay: int,
) -> None:
    evaluator_calls = 0
    cost_calls = 0
    resolver_calls = 0
    real_evaluator = execution_module.evaluate_retry
    real_cost = execution_module.build_cost_audit_metadata
    real_resolver = execution_module.resolve_retry_delay

    def counted_evaluator(**kwargs: object) -> object:
        nonlocal evaluator_calls
        evaluator_calls += 1
        return real_evaluator(**kwargs)  # type: ignore[arg-type]

    def counted_cost(*args: object, **kwargs: object) -> object:
        nonlocal cost_calls
        cost_calls += 1
        return real_cost(*args, **kwargs)  # type: ignore[arg-type]

    def counted_resolver(**kwargs: object) -> object:
        nonlocal resolver_calls
        resolver_calls += 1
        return real_resolver(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module, "evaluate_retry", counted_evaluator)
    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        counted_cost,
    )
    monkeypatch.setattr(
        execution_module,
        "resolve_retry_delay",
        counted_resolver,
    )

    success = success_generation(sample_context, usage=complete_usage())
    policy = None
    if case == "success":
        outcomes = (success,)
    elif case == "timeout-success":
        outcomes = (
            InsightProviderError(PROVIDER_TIMEOUT, "first"),
            success,
        )
    elif case == "timeout-timeout":
        outcomes = (
            InsightProviderError(PROVIDER_TIMEOUT, "first"),
            InsightProviderError(PROVIDER_TIMEOUT, "second"),
        )
    elif case == "auth":
        outcomes = (InsightProviderError(PROVIDER_AUTH_FAILED, "auth"),)
    elif case == "max-one-timeout":
        outcomes = (InsightProviderError(PROVIDER_TIMEOUT, "timeout"),)
        policy = RetryPolicy(
            version="one-attempt",
            max_attempts=1,
            retryable_error_codes=(PROVIDER_TIMEOUT,),
        )
    else:
        outcomes = (
            InsightProviderError(PROVIDER_TIMEOUT, "first"),
            InsightProviderError(PROVIDER_TIMEOUT, "second"),
            success,
        )
        policy = RetryPolicy(
            version="three-attempts",
            max_attempts=3,
            retryable_error_codes=(PROVIDER_TIMEOUT,),
        )

    provider = ScriptedProvider(*outcomes)
    clock = ScriptedClock(*([MONDAY_PEAK] * expected_clock))
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        provider,
        clock=clock,
        policy=policy,
        sleeper=sleeper,
    )

    assert provider.call_count == expected_provider
    assert clock.call_count == expected_clock
    assert len(result.attempt_audit.attempts) == expected_provider
    assert cost_calls == expected_cost
    assert evaluator_calls == expected_eval
    assert resolver_calls == expected_delay
    assert len(sleeper.calls) == expected_delay
    assert len(result.delay_audit.records) == expected_delay


def test_custom_delay_policy_drives_three_attempt_backoff_and_provenance(
    sample_context: InsightContext,
) -> None:
    retry_policy = RetryPolicy(
        version="three-attempts",
        max_attempts=3,
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    delay_policy = RetryDelayPolicy(
        version="custom-delay-v2",
        base_delays_ms=((PROVIDER_TIMEOUT, 2_500),),
        fallback_base_delay_ms=1_000,
        max_delay_ms=30_000,
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        InsightProviderError(PROVIDER_TIMEOUT, "second"),
        success_generation(sample_context, usage=complete_usage()),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK, MONDAY_PEAK)
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        provider,
        clock=clock,
        policy=retry_policy,
        delay_policy=delay_policy,
        sleeper=sleeper,
    )

    assert result.status == SUCCEEDED
    assert result.delay_audit.policy_version == "custom-delay-v2"
    assert sleeper.calls == [2_500, 5_000]
    assert [
        record.delay_decision.delay_ms
        for record in result.delay_audit.records
    ] == [2_500, 5_000]
    assert [
        record.after_attempt_number
        for record in result.delay_audit.records
    ] == [1, 2]


def test_custom_2501_ms_is_exact_from_resolver_to_sleeper_and_record(
    sample_context: InsightContext,
) -> None:
    delay_policy = RetryDelayPolicy(
        version="custom-delay-2501",
        base_delays_ms=((PROVIDER_TIMEOUT, 2_501),),
        fallback_base_delay_ms=1_000,
        max_delay_ms=30_000,
    )
    sleeper = RecordingSleeper()
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context),
    )

    result = execute(
        sample_context,
        provider,
        delay_policy=delay_policy,
        sleeper=sleeper,
    )

    record = result.delay_audit.records[0]
    assert sleeper.calls == [2_501]
    assert record.delay_decision.delay_ms == 2_501
    assert record.after_attempt_number == 1


def test_zero_retry_still_records_custom_delay_policy_version(
    sample_context: InsightContext,
) -> None:
    delay_policy = RetryDelayPolicy(
        version="governing-delay-policy",
        base_delays_ms=(),
        fallback_base_delay_ms=1_250,
        max_delay_ms=30_000,
    )
    sleeper = RecordingSleeper()

    result = execute(
        sample_context,
        ScriptedProvider(success_generation(sample_context)),
        delay_policy=delay_policy,
        sleeper=sleeper,
    )

    assert result.delay_audit.policy_version == "governing-delay-policy"
    assert result.delay_audit.records == ()
    assert sleeper.calls == []


def test_custom_future_error_uses_explicit_delay_override(
    sample_context: InsightContext,
) -> None:
    future_code = "FUTURE_PROVIDER_ERROR"
    retry_policy = RetryPolicy(
        version="future-enabled",
        max_attempts=2,
        retryable_error_codes=(future_code,),
    )
    delay_policy = RetryDelayPolicy(
        version="future-delay",
        base_delays_ms=((future_code, 3_000),),
        fallback_base_delay_ms=1_000,
        max_delay_ms=30_000,
    )
    sleeper = RecordingSleeper()
    provider = ScriptedProvider(
        InsightProviderError(future_code, "future"),
        success_generation(sample_context),
    )

    result = execute(
        sample_context,
        provider,
        policy=retry_policy,
        delay_policy=delay_policy,
        sleeper=sleeper,
    )

    assert result.status == SUCCEEDED
    assert sleeper.calls == [3_000]
    assert result.delay_audit.records[0].delay_decision.error_code == future_code


def test_sleeper_returns_before_next_clock_and_provider(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    first = datetime(2026, 8, 17, 5, 59, 59, tzinfo=UTC)
    second = datetime(2026, 8, 17, 6, 0, 5, tzinfo=UTC)

    class EventClock:
        def __init__(self) -> None:
            self.values = iter((first, second))

        def __call__(self) -> datetime:
            value = next(self.values)
            events.append(("clock", value))
            return value

    class EventProvider(ScriptedProvider):
        def generate(self, prompt: object) -> ProviderGeneration:
            events.append(("provider", self.call_count + 1))
            return super().generate(prompt)

    provider = EventProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context, usage=complete_usage()),
    )
    sleeper = RecordingSleeper(events)
    real_record = execution_module.RetryDelayExecutionRecord

    def observed_record(*args: object, **kwargs: object) -> object:
        events.append(("record", kwargs["after_attempt_number"]))
        return real_record(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        execution_module,
        "RetryDelayExecutionRecord",
        observed_record,
    )

    result = execute(
        sample_context,
        provider,
        clock=EventClock(),
        sleeper=sleeper,
    )

    assert events == [
        ("clock", first),
        ("provider", 1),
        ("sleep", 1_000),
        ("record", 1),
        ("clock", second),
        ("provider", 2),
    ]
    assert result.final_cost is not None
    assert result.final_cost.pricing_reference_at == second.isoformat()
    assert result.final_cost.estimate is not None
    assert result.final_cost.estimate.pricing_tier == PEAK


@pytest.mark.parametrize(
    ("error_code", "expected_seconds"),
    [
        (PROVIDER_TIMEOUT, 1.0),
        (PROVIDER_RATE_LIMITED, 5.0),
    ],
)
def test_default_sleeper_converts_integer_ms_to_seconds_without_real_wait(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_seconds: float,
) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(execution_module.time, "sleep", sleep_calls.append)
    provider = ScriptedProvider(
        InsightProviderError(error_code, "first"),
        success_generation(sample_context),
    )

    result = execute_insight_generation_with_retry(
        sample_context,
        provider=provider,
        utc_now=lambda: MONDAY_PEAK,
    )

    assert result.status == SUCCEEDED
    assert sleep_calls == [expected_seconds]


def test_default_sleeper_converts_custom_2500_ms_to_2_5_seconds(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(execution_module.time, "sleep", sleep_calls.append)
    delay_policy = RetryDelayPolicy(
        version="custom-default-adapter",
        base_delays_ms=((PROVIDER_TIMEOUT, 2_500),),
        fallback_base_delay_ms=1_000,
        max_delay_ms=30_000,
    )

    result = execute_insight_generation_with_retry(
        sample_context,
        provider=ScriptedProvider(
            InsightProviderError(PROVIDER_TIMEOUT, "first"),
            success_generation(sample_context),
        ),
        retry_delay_policy=delay_policy,
        utc_now=lambda: MONDAY_PEAK,
    )

    assert result.status == SUCCEEDED
    assert sleep_calls == [2.5]


@pytest.mark.parametrize("return_value", [123, "done", object()])
def test_non_none_sleeper_return_value_is_ignored(
    sample_context: InsightContext,
    return_value: object,
) -> None:
    calls: list[int] = []

    def sleeper(delay_ms: int) -> object:
        calls.append(delay_ms)
        return return_value

    result = execute(
        sample_context,
        ScriptedProvider(
            InsightProviderError(PROVIDER_TIMEOUT, "first"),
            success_generation(sample_context),
        ),
        sleeper=sleeper,
    )

    assert result.status == SUCCEEDED
    assert calls == [1_000]
    assert len(result.delay_audit.records) == 1


@pytest.mark.parametrize("sleeper", [123, object(), "sleep"])
def test_invalid_sleeper_fails_before_identity_clock_or_provider(
    sample_context: InsightContext,
    sleeper: object,
) -> None:
    provider = CountingIdentityProvider(success_generation(sample_context))
    clock = ScriptedClock(MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute_insight_generation_with_retry(
            sample_context,
            provider=provider,
            utc_now=clock,
            sleeper=sleeper,  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert provider.call_count == clock.call_count == 0
    assert provider.provider_name_reads == provider.model_reads == 0


def test_invalid_delay_policy_fails_before_identity_clock_or_provider(
    sample_context: InsightContext,
) -> None:
    provider = CountingIdentityProvider(success_generation(sample_context))
    clock = ScriptedClock(MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute_insight_generation_with_retry(
            sample_context,
            provider=provider,
            retry_delay_policy=object(),  # type: ignore[arg-type]
            utc_now=clock,
            sleeper=RecordingSleeper(),
        )

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert provider.call_count == clock.call_count == 0
    assert provider.provider_name_reads == provider.model_reads == 0


def test_sleeper_failure_is_sanitized_and_prevents_next_attempt(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSleeper:
        calls = 0

        def __call__(self, delay_ms: int) -> None:
            assert delay_ms == 1_000
            self.calls += 1
            raise RuntimeError("SECRET_SLEEPER")

    sleeper = FailingSleeper()

    def forbidden_record(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("failed sleeper must not create a delay record")

    monkeypatch.setattr(
        execution_module,
        "RetryDelayExecutionRecord",
        forbidden_record,
    )
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(
            sample_context,
            provider,
            clock=clock,
            sleeper=sleeper,
        )

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_SLEEPER" not in str(captured.value)
    assert sleeper.calls == 1
    assert provider.call_count == clock.call_count == 1


def test_delay_resolver_failure_is_sanitized_before_sleep_or_next_attempt(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_resolver(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("SECRET_DELAY")

    monkeypatch.setattr(
        execution_module,
        "resolve_retry_delay",
        fail_resolver,
    )
    sleeper = RecordingSleeper()
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(
            sample_context,
            provider,
            clock=clock,
            sleeper=sleeper,
        )

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_DELAY" not in str(captured.value)
    assert sleeper.calls == []
    assert provider.call_count == clock.call_count == 1


def test_delay_record_failure_occurs_after_sleep_but_before_next_attempt(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_record(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("SECRET_DELAY_RECORD")

    monkeypatch.setattr(
        execution_module,
        "RetryDelayExecutionRecord",
        fail_record,
    )
    sleeper = RecordingSleeper()
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context),
    )
    clock = ScriptedClock(MONDAY_PEAK, MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(
            sample_context,
            provider,
            clock=clock,
            sleeper=sleeper,
        )

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_DELAY_RECORD" not in str(captured.value)
    assert sleeper.calls == [1_000]
    assert provider.call_count == clock.call_count == 1


def test_second_clock_failure_occurs_after_completed_sleep(
    sample_context: InsightContext,
) -> None:
    sleeper = RecordingSleeper()
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context),
    )
    clock = ScriptedClock(MONDAY_PEAK, RuntimeError("SECRET_CLOCK"))

    with pytest.raises(RetryExecutionError) as captured:
        execute(
            sample_context,
            provider,
            clock=clock,
            sleeper=sleeper,
        )

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_CLOCK" not in str(captured.value)
    assert sleeper.calls == [1_000]
    assert clock.call_count == 2
    assert provider.call_count == 1


@pytest.mark.parametrize("provider_name", ["", " ", None, b"deepseek", 1, object()])
def test_invalid_owned_provider_identity_fails_before_clock_or_provider(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: object,
) -> None:
    provider = IdentityScriptedProvider(
        success_generation(sample_context),
        provider_name=provider_name,
        model=MODEL,
    )
    clock = ScriptedClock(MONDAY_PEAK)

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("invalid identity must not reach cost or retry evaluation")

    monkeypatch.setattr(execution_module, "evaluate_retry", forbidden)
    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        forbidden,
    )

    with pytest.raises(RetryExecutionError) as captured:
        execute_insight_generation_with_retry(
            sample_context,
            provider=provider,
            utc_now=clock,  # type: ignore[arg-type]
        )

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert provider.call_count == clock.call_count == 0


@pytest.mark.parametrize("model", ["", " ", None, b"model", 1, object()])
def test_invalid_owned_model_identity_fails_before_clock_or_provider(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
    model: object,
) -> None:
    provider = IdentityScriptedProvider(
        success_generation(sample_context),
        provider_name=PROVIDER,
        model=model,
    )
    clock = ScriptedClock(MONDAY_PEAK)

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("invalid identity must not reach cost or retry evaluation")

    monkeypatch.setattr(execution_module, "evaluate_retry", forbidden)
    monkeypatch.setattr(
        execution_module,
        "build_cost_audit_metadata",
        forbidden,
    )

    with pytest.raises(RetryExecutionError):
        execute_insight_generation_with_retry(
            sample_context,
            provider=provider,
            utc_now=clock,  # type: ignore[arg-type]
        )

    assert provider.call_count == clock.call_count == 0


def test_wrong_policy_and_noncallable_clock_fail_preflight(
    sample_context: InsightContext,
) -> None:
    provider = CountingIdentityProvider(success_generation(sample_context))

    with pytest.raises(RetryExecutionError):
        execute_insight_generation_with_retry(
            sample_context,
            provider=provider,
            retry_policy=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(RetryExecutionError):
        execute_insight_generation_with_retry(
            sample_context,
            provider=provider,
            utc_now=object(),  # type: ignore[arg-type]
        )

    assert provider.call_count == 0


@pytest.mark.parametrize(
    "clock_value",
    [None, "2026-08-17T01:00:00+00:00", datetime(2026, 8, 17, 1, 0)],
)
def test_invalid_first_clock_value_fails_before_provider(
    sample_context: InsightContext,
    clock_value: object,
) -> None:
    provider = ScriptedProvider(success_generation(sample_context))
    clock = ScriptedClock(clock_value)

    with pytest.raises(RetryExecutionError):
        execute(sample_context, provider, clock=clock)

    assert clock.call_count == 1
    assert provider.call_count == 0


def test_invalid_second_clock_value_hard_fails_without_partial_result(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        success_generation(sample_context),
    )
    clock = ScriptedClock(MONDAY_PEAK, None)

    with pytest.raises(RetryExecutionError):
        execute(sample_context, provider, clock=clock)

    assert provider.call_count == 1
    assert clock.call_count == 2


def test_unrepresentable_policy_is_rejected_before_clock_or_provider(
    sample_context: InsightContext,
) -> None:
    policy = RetryPolicy(
        version="too-large-for-audit",
        max_attempts=10**5000,
        retryable_error_codes=(),
    )
    provider = CountingIdentityProvider(success_generation(sample_context))
    clock = ScriptedClock(MONDAY_PEAK)

    with pytest.raises(RetryExecutionError) as captured:
        execute(sample_context, provider, clock=clock, policy=policy)

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert provider.call_count == clock.call_count == 0
    assert provider.provider_name_reads == provider.model_reads == 0


def test_512_digit_maximum_policy_is_auditable_on_first_success(
    sample_context: InsightContext,
) -> None:
    maximum = 10**MAX_ATTEMPT_AUDIT_INTEGER_DECIMAL_DIGITS - 1
    policy = RetryPolicy(
        version="maximum-auditable",
        max_attempts=maximum,
        retryable_error_codes=(),
    )
    provider = ScriptedProvider(success_generation(sample_context))

    result = execute(sample_context, provider, policy=policy)

    assert result.status == SUCCEEDED
    assert result.attempt_audit.max_attempts == maximum
    json.dumps(result.attempt_audit.to_dict(), allow_nan=False)
    assert provider.call_count == 1


@pytest.mark.parametrize(
    "target",
    [
        "generate_insight_with_metadata",
        "evaluate_retry",
        "build_failed_attempt_audit",
        "build_cost_audit_metadata",
        "build_succeeded_attempt_audit",
    ],
)
def test_unexpected_internal_failures_are_sanitized_hard_failures(
    sample_context: InsightContext,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("SECRET_INTERNAL")

    monkeypatch.setattr(execution_module, target, fail)
    if target in {"evaluate_retry", "build_failed_attempt_audit"}:
        provider = ScriptedProvider(
            InsightProviderError(PROVIDER_TIMEOUT, "timeout")
        )
    else:
        provider = ScriptedProvider(success_generation(sample_context))

    with pytest.raises(RetryExecutionError) as captured:
        execute(sample_context, provider)

    assert captured.value.code == INVALID_RETRY_EXECUTION
    assert "SECRET_INTERNAL" not in str(captured.value)


def test_clock_exception_is_sanitized_and_does_not_call_provider(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(success_generation(sample_context))
    clock = ScriptedClock(RuntimeError("SECRET_CLOCK"))

    with pytest.raises(RetryExecutionError) as captured:
        execute(sample_context, provider, clock=clock)

    assert "SECRET_CLOCK" not in str(captured.value)
    assert provider.call_count == 0


def test_result_direct_construction_rejects_cross_object_contradictions(
    sample_context: InsightContext,
) -> None:
    success = execute(
        sample_context,
        ScriptedProvider(
            success_generation(sample_context, usage=complete_usage())
        ),
    )
    failure = execute(
        sample_context,
        ScriptedProvider(InsightProviderError(PROVIDER_AUTH_FAILED, "auth")),
    )

    cases = (
        (success, {"status": FAILED}),
        (failure, {"status": SUCCEEDED}),
        (success, {"output": None}),
        (failure, {"output": success.output}),
        (failure, {"final_cost": success.final_cost}),
        (success, {"final_cost": None}),
        (failure, {"error_code": PROVIDER_TIMEOUT}),
        (success, {"final_usage": None}),
        (success, {"error_code": PROVIDER_TIMEOUT}),
    )
    for original, changes in cases:
        with pytest.raises(RetryExecutionError):
            replace(original, **changes)


def test_result_rejects_delay_attempt_cross_audit_contradictions(
    sample_context: InsightContext,
) -> None:
    result = execute(
        sample_context,
        ScriptedProvider(
            InsightProviderError(PROVIDER_TIMEOUT, "first"),
            success_generation(sample_context),
        ),
    )
    original_record = result.delay_audit.records[0]
    empty_audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version=result.delay_audit.policy_version,
        records=(),
    )
    wrong_error_decision = RetryDelayDecision(
        policy_version=original_record.delay_decision.policy_version,
        error_code=PROVIDER_UNAVAILABLE,
        attempts_completed=1,
        delay_ms=2_000,
    )
    wrong_error_record = RetryDelayExecutionRecord(
        version=RETRY_DELAY_EXECUTION_VERSION,
        after_attempt_number=1,
        delay_decision=wrong_error_decision,
    )
    wrong_error_audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version=result.delay_audit.policy_version,
        records=(wrong_error_record,),
    )
    second_decision = RetryDelayDecision(
        policy_version=original_record.delay_decision.policy_version,
        error_code=PROVIDER_TIMEOUT,
        attempts_completed=2,
        delay_ms=2_000,
    )
    second_record = RetryDelayExecutionRecord(
        version=RETRY_DELAY_EXECUTION_VERSION,
        after_attempt_number=2,
        delay_decision=second_decision,
    )
    extra_record_audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version=result.delay_audit.policy_version,
        records=(original_record, second_record),
    )
    three_attempt_policy = RetryPolicy(
        version="three-attempt-result",
        max_attempts=3,
        retryable_error_codes=(PROVIDER_TIMEOUT,),
    )
    three_attempt_result = execute(
        sample_context,
        ScriptedProvider(
            InsightProviderError(PROVIDER_TIMEOUT, "first"),
            InsightProviderError(PROVIDER_TIMEOUT, "second"),
            success_generation(sample_context),
        ),
        policy=three_attempt_policy,
        sleeper=RecordingSleeper(),
    )
    missing_record_audit = RetryDelayExecutionAudit(
        version=RETRY_DELAY_EXECUTION_VERSION,
        policy_version=three_attempt_result.delay_audit.policy_version,
        records=(three_attempt_result.delay_audit.records[0],),
    )

    with pytest.raises(RetryExecutionError):
        replace(result, delay_audit=empty_audit)
    with pytest.raises(RetryExecutionError):
        replace(result, delay_audit=wrong_error_audit)
    with pytest.raises(RetryExecutionError):
        replace(result, delay_audit=extra_record_audit)
    with pytest.raises(RetryExecutionError):
        replace(three_attempt_result, delay_audit=missing_record_audit)


def test_result_rejects_valid_but_unequal_final_usage_and_cost(
    sample_context: InsightContext,
) -> None:
    usage = complete_usage()
    result = execute(
        sample_context,
        ScriptedProvider(success_generation(sample_context, usage=usage)),
    )
    unequal_usage = ProviderUsage(
        prompt_tokens=1001,
        completion_tokens=200,
        total_tokens=1201,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=401,
        reasoning_tokens=100,
    )
    unequal_cost = build_cost_audit_metadata(
        usage,
        provider=PROVIDER,
        model=MODEL,
        pricing_reference_at=datetime(
            2026,
            8,
            17,
            1,
            0,
            1,
            tzinfo=UTC,
        ),
    )

    assert unequal_usage != result.final_usage
    assert unequal_cost != result.final_cost
    with pytest.raises(RetryExecutionError):
        replace(result, final_usage=unequal_usage)
    with pytest.raises(RetryExecutionError):
        replace(result, final_cost=unequal_cost)


def test_result_rejects_invalid_version_status_and_audit_type(
    sample_context: InsightContext,
) -> None:
    result = execute(
        sample_context,
        ScriptedProvider(success_generation(sample_context)),
    )

    for changes in (
        {"version": "1"},
        {"status": "pending"},
        {"attempt_audit": object()},
        {"delay_audit": object()},
    ):
        with pytest.raises(RetryExecutionError):
            replace(result, **changes)


def test_result_failure_has_exact_final_error_link(
    sample_context: InsightContext,
) -> None:
    provider = ScriptedProvider(
        InsightProviderError(PROVIDER_TIMEOUT, "first"),
        InsightProviderError(PROVIDER_UNAVAILABLE, "second"),
    )

    result = execute(sample_context, provider)

    assert result.error_code == PROVIDER_UNAVAILABLE
    assert result.error_code == result.attempt_audit.attempts[-1].error_code


def test_executor_has_delay_but_no_network_app_or_receipt_integration() -> None:
    source_path = Path(execution_module.__file__)
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

    assert "range(" not in source
    assert "time.sleep" in source
    assert "resolve_retry_delay" in source
    assert "RetryDelayExecutionRecord" in source
    assert "Retry-After" not in source
    assert "jitter" not in source.lower()
    assert "src.deepseek_provider" not in imports
    assert "src.insight_receipt" not in imports
    assert "app" not in imports
    assert "os" not in imports
    assert "random" not in imports
    assert "requests" not in imports
    assert "httpx" not in imports
    assert "openai" not in imports


def test_existing_streamlit_app_does_not_import_or_call_retry_executor() -> None:
    app_source = (Path(__file__).parents[1] / "app.py").read_text(
        encoding="utf-8"
    )

    assert "insight_retry_execution" not in app_source
    assert "execute_insight_generation_with_retry" not in app_source


def test_default_policy_budget_remains_two() -> None:
    assert DEFAULT_RETRY_POLICY.max_attempts == 2
