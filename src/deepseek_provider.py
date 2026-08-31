"""DeepSeek OpenAI-compatible adapter for the generic Insight Provider boundary."""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from src.insight_prompt import InsightPrompt
from src.insight_provider import (
    INVALID_PROVIDER_RESPONSE,
    INVALID_PROVIDER_USAGE,
    PROVIDER_ACCOUNT_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_CONNECTION_FAILED,
    PROVIDER_FAILURE,
    PROVIDER_RATE_LIMITED,
    PROVIDER_REQUEST_REJECTED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    ProviderGeneration,
    ProviderUsage,
    InsightProviderError,
)

DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 60.0
DEFAULT_DEEPSEEK_MAX_TOKENS = 16_384

_DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"

_ERROR_MESSAGES = {
    PROVIDER_CONFIGURATION_ERROR: "DeepSeek Provider 配置无效。",
    PROVIDER_TIMEOUT: "Provider 请求超时。",
    PROVIDER_AUTH_FAILED: "Provider 身份验证失败。",
    PROVIDER_ACCOUNT_ERROR: "Provider 账户状态无法完成请求。",
    PROVIDER_RATE_LIMITED: "Provider 请求受到速率限制。",
    PROVIDER_CONNECTION_FAILED: "无法连接 Provider。",
    PROVIDER_REQUEST_REJECTED: "Provider 拒绝了请求。",
    PROVIDER_UNAVAILABLE: "Provider 当前不可用。",
    PROVIDER_FAILURE: "Provider 调用失败。",
    INVALID_PROVIDER_USAGE: "Provider 返回的使用量信息无效。",
}

_MISSING = object()


def _provider_error(code: str) -> InsightProviderError:
    return InsightProviderError(code, _ERROR_MESSAGES[code])


def _initialize_client(
    api_key: str,
) -> tuple[Any | None, InsightProviderError | None]:
    try:
        client = OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
            max_retries=0,
        )
    except Exception:
        # SDK constructor details can retain credentials or transport metadata.
        return None, _provider_error(PROVIDER_CONFIGURATION_ERROR)
    return client, None


def _status_error_code(status_code: object) -> str:
    if status_code in {401, 403}:
        return PROVIDER_AUTH_FAILED
    if status_code == 402:
        return PROVIDER_ACCOUNT_ERROR
    if status_code == 429:
        return PROVIDER_RATE_LIMITED
    if status_code in {400, 404, 422}:
        return PROVIDER_REQUEST_REJECTED
    if isinstance(status_code, int) and status_code >= 500:
        return PROVIDER_UNAVAILABLE
    return PROVIDER_FAILURE


def _completion_request(
    client: Any,
    prompt: InsightPrompt,
) -> tuple[Any | None, InsightProviderError | None]:
    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": prompt.system_prompt},
                {"role": "user", "content": prompt.user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=DEFAULT_DEEPSEEK_MAX_TOKENS,
            temperature=0.0,
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except APITimeoutError:
        return None, _provider_error(PROVIDER_TIMEOUT)
    except AuthenticationError:
        return None, _provider_error(PROVIDER_AUTH_FAILED)
    except PermissionDeniedError:
        return None, _provider_error(PROVIDER_AUTH_FAILED)
    except RateLimitError:
        return None, _provider_error(PROVIDER_RATE_LIMITED)
    except APIConnectionError:
        return None, _provider_error(PROVIDER_CONNECTION_FAILED)
    except APIStatusError as exc:
        try:
            status_code = exc.status_code
        except Exception:
            status_code = None
        return None, _provider_error(_status_error_code(status_code))
    except Exception:
        # Do not retain raw SDK/runtime exceptions: they may carry request data.
        return None, _provider_error(PROVIDER_FAILURE)
    return response, None


def _invalid_response(message: str) -> InsightProviderError:
    return InsightProviderError(INVALID_PROVIDER_RESPONSE, message)


def _invalid_usage() -> InsightProviderError:
    return InsightProviderError(
        INVALID_PROVIDER_USAGE,
        _ERROR_MESSAGES[INVALID_PROVIDER_USAGE],
    )


def _sdk_field(source: object, field_name: str) -> object:
    """Read one typed SDK field, including Pydantic-preserved extensions."""

    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    try:
        direct = getattr(source, field_name, _MISSING)
    except Exception:
        raise _invalid_usage() from None
    if direct is not _MISSING:
        return direct
    try:
        model_extra = getattr(source, "model_extra", _MISSING)
    except Exception:
        raise _invalid_usage() from None
    if isinstance(model_extra, Mapping):
        return model_extra.get(field_name, _MISSING)
    if model_extra is not _MISSING and model_extra is not None:
        raise _invalid_usage() from None
    return _MISSING


def _response_usage(response: object) -> ProviderUsage | None:
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        prompt_tokens = _sdk_field(usage, "prompt_tokens")
        completion_tokens = _sdk_field(usage, "completion_tokens")
        total_tokens = _sdk_field(usage, "total_tokens")
        cache_hit = _sdk_field(usage, "prompt_cache_hit_tokens")
        cache_miss = _sdk_field(usage, "prompt_cache_miss_tokens")
        details = _sdk_field(usage, "completion_tokens_details")
        reasoning_tokens = (
            _MISSING
            if details is _MISSING or details is None
            else _sdk_field(details, "reasoning_tokens")
        )
        normalized_usage = ProviderUsage(
            prompt_tokens=prompt_tokens,  # type: ignore[arg-type]
            completion_tokens=completion_tokens,  # type: ignore[arg-type]
            total_tokens=total_tokens,  # type: ignore[arg-type]
            prompt_cache_hit_tokens=(
                None if cache_hit is _MISSING else cache_hit  # type: ignore[arg-type]
            ),
            prompt_cache_miss_tokens=(
                None if cache_miss is _MISSING else cache_miss  # type: ignore[arg-type]
            ),
            reasoning_tokens=(
                None if reasoning_tokens is _MISSING else reasoning_tokens  # type: ignore[arg-type]
            ),
        )
    except Exception:
        error = _invalid_usage()
    else:
        error = None
    if error is not None:
        # Raise after leaving the SDK/domain exception handler so malformed
        # usage objects and their values are not retained on the public error.
        raise error from None
    return normalized_usage


def _response_content(response: object) -> str:
    try:
        choice = response.choices[0]  # type: ignore[attr-defined]
    except Exception:
        error = _invalid_response(
            "DeepSeek response 缺少可用的 completion choice。"
        )
    else:
        error = None
    if error is not None:
        raise error from None

    try:
        finish_reason = choice.finish_reason
    except Exception:
        finish_reason = None
    if finish_reason == "insufficient_system_resource":
        raise _provider_error(PROVIDER_UNAVAILABLE) from None
    if finish_reason != "stop":
        raise _invalid_response("DeepSeek response 未正常完成。") from None

    try:
        content = choice.message.content
    except Exception:
        content = None
    if not isinstance(content, str):
        raise _invalid_response(
            "DeepSeek response 缺少非空文本 content。"
        ) from None
    try:
        stripped_content = content.strip()
        is_blank = not isinstance(stripped_content, str) or not stripped_content
    except Exception:
        blank_check_error = _invalid_response(
            "DeepSeek response 缺少非空文本 content。"
        )
    else:
        blank_check_error = None
    if blank_check_error is not None:
        raise blank_check_error from None
    if is_blank:
        raise _invalid_response(
            "DeepSeek response 缺少非空文本 content。"
        ) from None
    return content


class DeepSeekInsightProvider:
    """One reusable DeepSeek client implementing the InsightProvider Protocol."""

    def __init__(self) -> None:
        api_key = os.environ.get(_DEEPSEEK_API_KEY_ENV)
        if not isinstance(api_key, str) or not api_key.strip():
            raise _provider_error(PROVIDER_CONFIGURATION_ERROR) from None

        client, error = _initialize_client(api_key)
        if error is not None:
            raise error from None
        self._client = client

    def generate(self, prompt: InsightPrompt) -> ProviderGeneration:
        """Return content and normalized usage from one completed response."""

        response, error = _completion_request(self._client, prompt)
        if error is not None:
            raise error from None
        content = _response_content(response)
        usage = _response_usage(response)
        return ProviderGeneration(raw_text=content, usage=usage)
