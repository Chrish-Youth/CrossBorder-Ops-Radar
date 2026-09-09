"""Measure pinned local DeepSeek input encoding; never Provider/billing usage.

Version 1 covers InsightPrompt v1 system + user messages in non-thinking chat
mode only. Resources are verified once per process, then retained in memory.
No prompt, encoded text, or token IDs are retained in the resource cache.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from functools import lru_cache
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
import re
from typing import Any, Callable

from src.insight_prompt import InsightPrompt

TOKEN_MEASUREMENT_VERSION = "1"
INVALID_TOKEN_MEASUREMENT_INPUT = "INVALID_TOKEN_MEASUREMENT_INPUT"
TOKEN_MEASUREMENT_UNAVAILABLE = "TOKEN_MEASUREMENT_UNAVAILABLE"

# Do not automatically accept a future Prompt contract's version/shape.
_SUPPORTED_PROMPT_VERSION = "1"
_RUNTIME_NAME = "tokenizers"
_RUNTIME_VERSION = "0.22.2"
_ASSET_DIRECTORY = Path(__file__).resolve().parents[1] / "assets" / "deepseek_v4_flash_0731"
_ERROR_MESSAGES = {
    INVALID_TOKEN_MEASUREMENT_INPUT: "Unsupported or invalid local token measurement input.",
    TOKEN_MEASUREMENT_UNAVAILABLE: "Verified local token measurement is unavailable.",
}


class TokenMeasurementError(Exception):
    """Stable code and fixed safe text; no caller/asset/exception text accepted."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _ERROR_MESSAGES:
            code = INVALID_TOKEN_MEASUREMENT_INPUT
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(f"{code}: {self.message}")


@dataclass(frozen=True)
class TokenMeasurementAssetIdentity:
    """Exact immutable provenance, with no string normalization or aliases."""

    publisher: str
    repository: str
    revision: str
    tokenizer_sha256: str
    tokenizer_config_sha256: str
    encoder_sha256: str
    license_sha256: str

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not str or not value or value.isspace():
                raise TokenMeasurementError(INVALID_TOKEN_MEASUREMENT_INPUT)
            if field.name.endswith("_sha256") and re.fullmatch("[0-9a-f]{64}", value) is None:
                raise TokenMeasurementError(INVALID_TOKEN_MEASUREMENT_INPUT)
        if re.fullmatch("[0-9a-f]{40}", self.revision) is None:
            raise TokenMeasurementError(INVALID_TOKEN_MEASUREMENT_INPUT)


_PINNED_IDENTITY = TokenMeasurementAssetIdentity(
    publisher="deepseek-ai",
    repository="DeepSeek-V4-Flash-0731",
    revision="7872f01b1d1fe23eabc4c98b48bffcef5a386062",
    tokenizer_sha256="8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf",
    tokenizer_config_sha256="6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547",
    encoder_sha256="abc0d26120250dda0ae077dc64aa28836026e61e970854aaeb792445e6a0dde6",
    license_sha256="f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040",
)


@dataclass(frozen=True)
class LocalTokenMeasurement:
    """Local reference count and byte sizes, without a persistence contract.

    Numeric fields are arbitrary-size exact nonnegative ints. These metadata
    values do not decide admission or establish ProviderUsage equivalence.
    """

    version: str
    provider: str
    model: str
    encoding_mode: str
    local_encoded_input_tokens: int
    prompt_body_utf8_bytes: int
    encoded_input_utf8_bytes: int
    asset_identity: TokenMeasurementAssetIdentity
    runtime_name: str
    runtime_version: str

    def __post_init__(self) -> None:
        for name, expected in (
            ("version", TOKEN_MEASUREMENT_VERSION),
            ("provider", "deepseek"),
            ("model", "deepseek-v4-flash"),
            ("encoding_mode", "chat"),
            ("runtime_name", _RUNTIME_NAME),
            ("runtime_version", _RUNTIME_VERSION),
        ):
            value = getattr(self, name)
            if type(value) is not str or value != expected:
                raise TokenMeasurementError(INVALID_TOKEN_MEASUREMENT_INPUT)
        for name in (
            "local_encoded_input_tokens", "prompt_body_utf8_bytes", "encoded_input_utf8_bytes"
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TokenMeasurementError(INVALID_TOKEN_MEASUREMENT_INPUT)
        if type(self.asset_identity) is not TokenMeasurementAssetIdentity:
            raise TokenMeasurementError(INVALID_TOKEN_MEASUREMENT_INPUT)


@dataclass(frozen=True)
class _VerifiedResources:
    tokenizer: Any
    encode_messages: Callable[..., str]
    identity: TokenMeasurementAssetIdentity


def _expected_manifest() -> dict[str, Any]:
    identity = _PINNED_IDENTITY
    return {
        "asset_schema_version": "1",
        "publisher": identity.publisher,
        "repository": identity.repository,
        "revision": identity.revision,
        "files": {
            "tokenizer.json": identity.tokenizer_sha256,
            "tokenizer_config.json": identity.tokenizer_config_sha256,
            "encoding_dsv4.py": identity.encoder_sha256,
            "LICENSE": identity.license_sha256,
        },
        "runtime_name": _RUNTIME_NAME,
        "runtime_version": _RUNTIME_VERSION,
        "encoding_mode": "chat",
        "license": "MIT",
    }


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result


def _load_tokenizer_runtime() -> Any:
    """Lazy local import; separate seam for runtime compatibility tests."""
    return import_module(_RUNTIME_NAME)


@lru_cache(maxsize=1)
def _load_verified_resources() -> _VerifiedResources:
    """Cache only a completely verified load; exceptions never enter the cache.

    The manifest is also anchored to code-owned pins, so changing both a file
    and its manifest hash cannot authorize an unreviewed encoder/revision.
    Load from the bytes we hashed, avoiding a second filesystem read for use.
    Tests can reset this private cache with .cache_clear().
    """
    expected = _expected_manifest()
    manifest = json.loads(
        (_ASSET_DIRECTORY / "manifest.json").read_bytes(),
        object_pairs_hook=_unique_json_object,
    )
    if manifest != expected:
        raise ValueError("Unsupported asset manifest")
    verified_bytes: dict[str, bytes] = {}
    for filename, digest in expected["files"].items():
        payload = (_ASSET_DIRECTORY / filename).read_bytes()
        if sha256(payload).hexdigest() != digest:
            raise ValueError("Asset integrity failure")
        verified_bytes[filename] = payload

    runtime = _load_tokenizer_runtime()
    if type(runtime.__version__) is not str or runtime.__version__ != _RUNTIME_VERSION:
        raise ValueError("Unsupported tokenizer runtime")
    tokenizer = runtime.Tokenizer.from_str(verified_bytes["tokenizer.json"].decode("utf-8"))
    if tokenizer.truncation is not None or tokenizer.padding is not None:
        raise ValueError("Unsupported tokenizer configuration")

    # Exact upstream source, executed only after all four assets are verified.
    # Fixed compile name keeps local paths out of upstream exception filenames.
    namespace: dict[str, Any] = {"__name__": "_pinned_deepseek_v4_encoder"}
    code = compile(
        verified_bytes["encoding_dsv4.py"], "encoding_dsv4.py", "exec", dont_inherit=True
    )
    exec(code, namespace)
    encoder = namespace["encode_messages"]
    if not callable(encoder):
        raise ValueError("Unavailable reference encoder")
    return _VerifiedResources(tokenizer, encoder, _PINNED_IDENTITY)


def _prompt_body_bytes(prompt: InsightPrompt) -> int | None:
    # Exact types avoid executing arbitrary subclass access/encode overrides.
    if type(prompt) is not InsightPrompt:
        return None
    if type(prompt.version) is not str or prompt.version != _SUPPORTED_PROMPT_VERSION:
        return None
    if type(prompt.system_prompt) is not str or type(prompt.user_prompt) is not str:
        return None
    try:
        return len(prompt.system_prompt.encode("utf-8")) + len(prompt.user_prompt.encode("utf-8"))
    except UnicodeError:
        return None


def _try_measure(prompt: InsightPrompt, body_bytes: int) -> LocalTokenMeasurement | None:
    # Return across the exception boundary before raising the fixed public error:
    # neither __context__ nor __cause__ retains a raw runtime/prompt exception.
    try:
        resources = _load_verified_resources()
        messages = [
            {"role": "system", "content": prompt.system_prompt},
            {"role": "user", "content": prompt.user_prompt},
        ]
        encoded_input = resources.encode_messages(messages, thinking_mode="chat")
        if type(encoded_input) is not str:
            return None
        encoded_bytes = len(encoded_input.encode("utf-8"))
        tokens = resources.tokenizer.encode(encoded_input, add_special_tokens=False)
        return LocalTokenMeasurement(
            version=TOKEN_MEASUREMENT_VERSION,
            provider="deepseek",
            model="deepseek-v4-flash",
            encoding_mode="chat",
            local_encoded_input_tokens=len(tokens.ids),
            prompt_body_utf8_bytes=body_bytes,
            encoded_input_utf8_bytes=encoded_bytes,
            asset_identity=resources.identity,
            runtime_name=_RUNTIME_NAME,
            runtime_version=_RUNTIME_VERSION,
        )
    except Exception:
        return None


def measure_insight_prompt_tokens(prompt: InsightPrompt) -> LocalTokenMeasurement:
    """Measure exact Prompt v1 strings through the pinned full-message encoder.

    The existing builder owns its 100,000-byte Prompt limit. This measurement
    API adds no admission/size policy and never truncates or normalizes text.
    It does not retain the prompt or fill missing ProviderUsage.
    """
    body_bytes = _prompt_body_bytes(prompt)
    if body_bytes is None:
        raise TokenMeasurementError(INVALID_TOKEN_MEASUREMENT_INPUT)
    result = _try_measure(prompt, body_bytes)
    if result is None:
        raise TokenMeasurementError(TOKEN_MEASUREMENT_UNAVAILABLE)
    return result
