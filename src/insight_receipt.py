"""Build immutable, privacy-safe metadata for one successful AI generation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.deepseek_provider import DEEPSEEK_MODEL
from src.insight_cost_audit import AVAILABLE, CostAuditMetadata
from src.insight_prompt import (
    INSIGHT_OUTPUT_VERSION,
    INSIGHT_PROMPT_VERSION,
    InsightOutput,
)
from src.insight_provider import ProviderUsage
from src.insights import INSIGHT_CONTEXT_VERSION, InsightContext

INSIGHT_RECEIPT_VERSION = "3"
MAX_RECEIPT_TOKEN_DECIMAL_DIGITS = 512
DEEPSEEK_PROVIDER_NAME = "deepseek"
INVALID_RECEIPT_INPUT = "INVALID_RECEIPT_INPUT"

_MAX_RECEIPT_TOKEN_VALUE = 10**MAX_RECEIPT_TOKEN_DECIMAL_DIGITS - 1
_RECEIPT_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
)


class InsightReceiptError(ValueError):
    """A stable failure at the generation-receipt boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _invalid_receipt(message: str) -> InsightReceiptError:
    return InsightReceiptError(INVALID_RECEIPT_INPUT, message)


def _utc_now() -> datetime:
    """Return the receipt clock value through a small private test seam."""

    return datetime.now(timezone.utc)


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_receipt("generated_at 必须是非空 UTC ISO 8601 string。")
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_receipt(
            "generated_at 必须是合法 UTC ISO 8601 string。"
        ) from exc
    if parsed.tzinfo is None or offset != timedelta(0):
        raise _invalid_receipt("generated_at 必须使用 timezone-aware UTC。")


def _validate_count(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid_receipt(f"{field_name} 必须是非负整数且不能是 bool。")


def _validate_usage_representability(usage: ProviderUsage) -> None:
    """Enforce only the Receipt JSON/UI decimal representation bound."""

    for field_name in _RECEIPT_USAGE_FIELDS:
        value = getattr(usage, field_name)
        if value is not None and value > _MAX_RECEIPT_TOKEN_VALUE:
            raise _invalid_receipt(
                "usage 包含超出 Receipt 可表示范围的 token count。"
            )


@dataclass(frozen=True)
class InsightGenerationReceipt:
    """Stable audit metadata for exactly one validated InsightOutput."""

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
    usage: ProviderUsage | None = None

    def __post_init__(self) -> None:
        if self.version != INSIGHT_RECEIPT_VERSION:
            raise _invalid_receipt("Receipt version 与当前 Contract 不一致。")
        _validate_timestamp(self.generated_at)
        if (
            not isinstance(self.analysis_signature, str)
            or not self.analysis_signature.strip()
        ):
            raise _invalid_receipt("analysis_signature 必须是非空 string。")
        if not isinstance(self.group_by, tuple) or any(
            not isinstance(dimension, str) or not dimension.strip()
            for dimension in self.group_by
        ):
            raise _invalid_receipt("group_by 必须是非空字符串组成的 tuple。")
        if self.context_version != INSIGHT_CONTEXT_VERSION:
            raise _invalid_receipt("Context version 与当前 Contract 不一致。")
        if self.prompt_version != INSIGHT_PROMPT_VERSION:
            raise _invalid_receipt("Prompt version 与当前 Contract 不一致。")
        if self.output_version != INSIGHT_OUTPUT_VERSION:
            raise _invalid_receipt("Output version 与当前 Contract 不一致。")
        if self.provider != DEEPSEEK_PROVIDER_NAME:
            raise _invalid_receipt("Provider identifier 与当前 Contract 不一致。")
        if self.model != DEEPSEEK_MODEL:
            raise _invalid_receipt("Model identifier 与当前 Contract 不一致。")
        for field_name in (
            "metric_record_count",
            "diagnostic_signal_count",
            "priority_insight_count",
        ):
            _validate_count(getattr(self, field_name), field_name=field_name)
        if self.usage is not None:
            if not isinstance(self.usage, ProviderUsage):
                raise _invalid_receipt("usage 必须是 ProviderUsage 或 None。")
            _validate_usage_representability(self.usage)
        if not isinstance(self.cost, CostAuditMetadata):
            raise _invalid_receipt("cost 必须是 CostAuditMetadata。")
        if self.cost.status == AVAILABLE:
            if self.usage is None:
                raise _invalid_receipt(
                    "available cost 必须对应已保存的 ProviderUsage。"
                )
            if (
                self.usage.prompt_cache_hit_tokens is None
                or self.usage.prompt_cache_miss_tokens is None
            ):
                raise _invalid_receipt(
                    "available cost 必须对应完整的 cache token breakdown。"
                )
            estimate = self.cost.estimate
            if estimate is None:
                raise _invalid_receipt(
                    "available cost 必须包含 GenerationCostEstimate。"
                )
            if estimate.provider != self.provider or estimate.model != self.model:
                raise _invalid_receipt(
                    "Cost estimate provider/model 与 Receipt provenance 不一致。"
                )

    def to_dict(self) -> dict[str, Any]:
        """Return an explicit, fresh JSON-safe public representation."""

        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "analysis_signature": self.analysis_signature,
            "group_by": list(self.group_by),
            "context_version": self.context_version,
            "prompt_version": self.prompt_version,
            "output_version": self.output_version,
            "provider": self.provider,
            "model": self.model,
            "metric_record_count": self.metric_record_count,
            "diagnostic_signal_count": self.diagnostic_signal_count,
            "priority_insight_count": self.priority_insight_count,
            "usage": _usage_to_dict(self.usage),
            "cost": self.cost.to_dict(),
        }


def _usage_to_dict(
    usage: ProviderUsage | None,
) -> dict[str, int | None] | None:
    """Return the fixed public Usage schema without dynamic serialization."""

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


def _normalize_group_by(group_by: Sequence[str] | None) -> tuple[str, ...]:
    if group_by is None:
        return ()
    if isinstance(group_by, (str, bytes, bytearray)) or not isinstance(
        group_by,
        (list, tuple),
    ):
        raise _invalid_receipt("group_by 必须是 list、tuple 或 None。")
    normalized = tuple(group_by)
    if any(
        not isinstance(dimension, str) or not dimension.strip()
        for dimension in normalized
    ):
        raise _invalid_receipt("group_by 只能包含非空 string。")
    return normalized


def build_insight_generation_receipt(
    *,
    analysis_signature: str,
    group_by: Sequence[str] | None,
    context: InsightContext,
    output: InsightOutput,
    cost: CostAuditMetadata,
    usage: ProviderUsage | None = None,
) -> InsightGenerationReceipt:
    """Build receipt metadata after one validated output has been produced."""

    if not isinstance(analysis_signature, str) or not analysis_signature.strip():
        raise _invalid_receipt("analysis_signature 必须是非空 string。")
    normalized_group_by = _normalize_group_by(group_by)
    if not isinstance(context, InsightContext):
        raise _invalid_receipt("context 必须是 InsightContext。")
    if context.version != INSIGHT_CONTEXT_VERSION:
        raise _invalid_receipt("context 使用了不支持的 Contract version。")
    if not isinstance(output, InsightOutput):
        raise _invalid_receipt("output 必须是 validated InsightOutput。")
    if output.version != INSIGHT_OUTPUT_VERSION:
        raise _invalid_receipt("output 使用了不支持的 Contract version。")
    if usage is not None and not isinstance(usage, ProviderUsage):
        raise _invalid_receipt("usage 必须是 ProviderUsage 或 None。")
    if not isinstance(cost, CostAuditMetadata):
        raise _invalid_receipt("cost 必须是 CostAuditMetadata。")

    generated_at = _utc_now().isoformat()
    return InsightGenerationReceipt(
        version=INSIGHT_RECEIPT_VERSION,
        generated_at=generated_at,
        analysis_signature=analysis_signature,
        group_by=normalized_group_by,
        context_version=INSIGHT_CONTEXT_VERSION,
        prompt_version=INSIGHT_PROMPT_VERSION,
        output_version=INSIGHT_OUTPUT_VERSION,
        provider=DEEPSEEK_PROVIDER_NAME,
        model=DEEPSEEK_MODEL,
        metric_record_count=len(context.metric_records),
        diagnostic_signal_count=len(context.diagnostic_signals),
        priority_insight_count=len(output.priority_insights),
        cost=cost,
        usage=usage,
    )
