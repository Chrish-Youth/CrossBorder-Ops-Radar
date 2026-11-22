"""Offline regressions for the pinned reference encoding, not hosted usage.

The upstream chat case 4 is copied byte-for-byte from the pinned repository's
encoding/tests directory, under its MIT license in assets/.../LICENSE. Its
broader upstream message shape verifies the artifact only, not our public scope.
Small-corpus counts were captured independently with the upstream encoder and
Tokenizer.from_file, before exercising this wrapper. The seed 113 boundary and
production sample counts reproduce the retained feasibility calibration.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from hashlib import sha256
import json
from pathlib import Path
import random
import shutil
import socket
import string
import time
from types import SimpleNamespace

import pytest

import src.insight_token_measurement as measurement
from src.insight_prompt import (
    InsightPrompt,
    InsightPromptError,
    MAX_PROMPT_BYTES,
    build_insight_prompt,
    build_insight_prompt_v1,
)
from src.insight_provider import ProviderGeneration, ProviderUsage
from src.insights import build_insight_context
from src.pipeline import run_pipeline

ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "assets" / "deepseek_v4_flash_0731"
FIXTURES = ROOT / "tests" / "fixtures" / "deepseek_v4_flash_0731"
SECRET = "SECRET_TOKEN_MEASUREMENT_CONTENT"
HASHES = {
    "tokenizer.json": "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf",
    "tokenizer_config.json": "6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547",
    "encoding_dsv4.py": "abc0d26120250dda0ae077dc64aa28836026e61e970854aaeb792445e6a0dde6",
    "LICENSE": "f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040",
}
NUMERIC_FIELDS = (
    "local_encoded_input_tokens", "prompt_body_utf8_bytes", "encoded_input_utf8_bytes",
)
CORPUS = [
    ("Hello, merchant!", 10, 89),
    ("商家运营库存订单", 10, 97),
    ("SKU-009 库存 42 units", 15, 96),
    ("😀🧪🚀👩\u200d💻", 17, 96),
    ("e\u0301a\u0327\u0308Z\u0338", 15, 84),
    ('{"sku":"A\\B","n":123,"text":"\\u4e2d"}', 26, 110),
    ('"quoted" \\ path \\\\', 13, 91),
    ("line1\nline2\r\n\t literal \\n", 17, 98),
    ("  leading and trailing  ", 11, 97),
    ("<｜User｜></think><image>", 11, 100),
    ("\u0085\u2028\u2029", 12, 81),
    ("", 6, 73),
]


def fail_if_called(*args, **kwargs):
    raise AssertionError("Unexpected external operation")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", fail_if_called)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_if_called)
    monkeypatch.setattr(socket, "create_connection", fail_if_called)
    monkeypatch.setattr(time, "sleep", fail_if_called)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(scope="module")
def sample_context():
    return build_insight_context(run_pipeline(ROOT / "data/sample_ecommerce_data.csv", group_by="sku"))


@pytest.fixture(scope="module")
def valid_result():
    return measurement.measure_insight_prompt_tokens(InsightPrompt("1", "System.", "Hello, merchant!"))


@pytest.fixture
def fresh_loader():
    loader = measurement._load_verified_resources
    loader.cache_clear()
    yield
    loader.cache_clear()


@pytest.fixture
def copied_assets(tmp_path, monkeypatch, fresh_loader):
    target = tmp_path / "pinned-assets"
    shutil.copytree(ASSETS, target)
    monkeypatch.setattr(measurement, "_ASSET_DIRECTORY", target)
    return target


def assert_error(call, code, caplog=None):
    with pytest.raises(measurement.TokenMeasurementError) as caught:
        call()
    error = caught.value
    assert error.code == code
    assert str(error) == f"{code}: {error.message}"
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    if caplog is not None:
        assert SECRET not in caplog.text
    return error


def secret_prompt():
    return InsightPrompt("1", SECRET, SECRET)


def test_result_identity_and_result_are_frozen(valid_result):
    with pytest.raises(FrozenInstanceError):
        valid_result.local_encoded_input_tokens = 0
    with pytest.raises(FrozenInstanceError):
        valid_result.asset_identity.publisher = "another"
    assert valid_result.version == measurement.TOKEN_MEASUREMENT_VERSION == "1"
    assert valid_result.provider == "deepseek"
    assert valid_result.model == "deepseek-v4-flash"
    assert valid_result.encoding_mode == "chat"
    assert valid_result.runtime_name == "tokenizers"
    assert valid_result.runtime_version == "0.22.2"
    assert not hasattr(valid_result, "to_dict")


@pytest.mark.parametrize("name", NUMERIC_FIELDS)
@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "1", None])
def test_numeric_fields_reject_inexact_or_negative_values(valid_result, name, bad):
    assert_error(lambda: replace(valid_result, **{name: bad}), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


@pytest.mark.parametrize("name", NUMERIC_FIELDS)
def test_integer_subclasses_rejected(valid_result, name):
    class Integer(int):
        pass
    assert_error(lambda: replace(valid_result, **{name: Integer(1)}), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


@pytest.mark.parametrize("name", NUMERIC_FIELDS)
def test_arbitrary_size_domain_integers_without_persistence_cap(valid_result, name):
    huge = 10 ** 6000
    assert getattr(replace(valid_result, **{name: huge}), name) == huge
    assert getattr(replace(valid_result, **{name: 0}), name) == 0


@pytest.mark.parametrize("name", ["version", "provider", "model", "encoding_mode", "runtime_name", "runtime_version"])
@pytest.mark.parametrize("bad", [None, True, "", "unsupported"])
def test_result_rejects_unsupported_target_metadata(valid_result, name, bad):
    assert_error(lambda: replace(valid_result, **{name: bad}), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


@pytest.mark.parametrize("name", [field.name for field in fields(measurement.TokenMeasurementAssetIdentity)])
@pytest.mark.parametrize("bad", [None, "", " \t\n"])
def test_identity_requires_exact_nonblank_strings(valid_result, name, bad):
    assert_error(lambda: replace(valid_result.asset_identity, **{name: bad}), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


@pytest.mark.parametrize("name", ["tokenizer_sha256", "tokenizer_config_sha256", "encoder_sha256", "license_sha256"])
@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "g" * 64])
def test_identity_rejects_invalid_digest_without_normalizing(valid_result, name, bad):
    assert_error(lambda: replace(valid_result.asset_identity, **{name: bad}), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


def test_identity_and_result_do_not_coerce(valid_result):
    identity = replace(valid_result.asset_identity, publisher=" DeepSeek-AI ")
    assert identity.publisher == " DeepSeek-AI "
    for bad in ["main", "A" * 40, "a" * 39]:
        assert_error(lambda: replace(identity, revision=bad), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)
    assert_error(lambda: replace(valid_result, asset_identity={}), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


@pytest.mark.parametrize("prompt", [
    None, {}, [], "prompt", SimpleNamespace(version="1", system_prompt="x", user_prompt="y"),
    InsightPrompt("2", "system", "user"), InsightPrompt(1, "system", "user"),
    InsightPrompt("1", None, "user"), InsightPrompt("1", "system", 1),
    InsightPrompt("1", "\ud800", "user"), InsightPrompt("1", "system", "\udfff"),
])
def test_invalid_prompt_fails_before_loading_assets(prompt, monkeypatch):
    monkeypatch.setattr(measurement, "_load_verified_resources", fail_if_called)
    assert_error(lambda: measurement.measure_insight_prompt_tokens(prompt), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


def test_context_is_not_a_prompt(sample_context):
    assert_error(lambda: measurement.measure_insight_prompt_tokens(sample_context), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


def test_current_production_prompt_v2_is_rejected_before_asset_loading(
    sample_context,
    monkeypatch,
):
    prompt = build_insight_prompt(sample_context)
    monkeypatch.setattr(measurement, "_load_verified_resources", fail_if_called)

    assert prompt.version == "2"
    assert_error(
        lambda: measurement.measure_insight_prompt_tokens(prompt),
        measurement.INVALID_TOKEN_MEASUREMENT_INPUT,
    )


def test_string_subclass_cannot_run_custom_encoding():
    class Text(str):
        def encode(self, *args, **kwargs):
            raise AssertionError(SECRET)
    assert_error(lambda: measurement.measure_insight_prompt_tokens(InsightPrompt("1", Text(SECRET), "user")), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


@pytest.mark.parametrize("content,tokens,encoded_bytes", CORPUS)
def test_independent_reference_corpus(content, tokens, encoded_bytes):
    prompt = InsightPrompt("1", "System.", content)
    before = deepcopy(prompt)
    result = measurement.measure_insight_prompt_tokens(prompt)
    assert result == measurement.measure_insight_prompt_tokens(prompt)
    assert prompt == before
    assert result.local_encoded_input_tokens == tokens
    assert result.encoded_input_utf8_bytes == encoded_bytes
    assert result.prompt_body_utf8_bytes == len(prompt.system_prompt.encode()) + len(content.encode())
    assert result.local_encoded_input_tokens <= result.encoded_input_utf8_bytes


def test_upstream_chat_vector():
    input_bytes = (FIXTURES / "test_input_4.json").read_bytes()
    output_bytes = (FIXTURES / "test_output_4.txt").read_bytes()
    assert sha256(input_bytes).hexdigest() == "c45bbd0a1b7a2f75033d8db4ba74ee5b7653bd01114045a52f79d3b387663465"
    assert sha256(output_bytes).hexdigest() == "60e1643840ba9e4aeede450feb7b0498fa66ee24e4a939d48855ce04ec6fc375"
    resources = measurement._load_verified_resources()
    encoded = resources.encode_messages(json.loads(input_bytes), thinking_mode="chat")
    # The vendored module has no future-annotations import. Do not inherit
    # compiler flags from our host module when executing the upstream source.
    assert resources.encode_messages.__annotations__["return"] is str
    assert encoded.encode("utf-8") == output_bytes
    assert len(resources.tokenizer.encode(encoded, add_special_tokens=False).ids) == 582
    # Upstream vector shape is NOT added to the public InsightPrompt API.
    assert_error(lambda: measurement.measure_insight_prompt_tokens(json.loads(input_bytes)), measurement.INVALID_TOKEN_MEASUREMENT_INPUT)


def test_known_framing_token_ids_and_empty_input():
    resources = measurement._load_verified_resources()
    encoded = resources.encode_messages([
        {"role": "system", "content": ""}, {"role": "user", "content": ""},
    ], thinking_mode="chat")
    assert encoded == "<｜begin▁of▁sentence｜><｜User｜><｜Assistant｜></think>"
    assert resources.tokenizer.encode(encoded, add_special_tokens=False).ids == [0, 128803, 128804, 128822]
    result = measurement.measure_insight_prompt_tokens(InsightPrompt("1", "", ""))
    assert (result.prompt_body_utf8_bytes, result.encoded_input_utf8_bytes, result.local_encoded_input_tokens) == (0, 66, 4)


def test_exact_production_messages_pass_to_official_encoder(sample_context, monkeypatch):
    from src.deepseek_provider import _completion_request

    prompt = build_insight_prompt_v1(sample_context)
    resources = measurement._load_verified_resources()
    captured = []

    def encode(messages, **kwargs):
        captured.append((deepcopy(messages), kwargs))
        return resources.encode_messages(messages, **kwargs)

    monkeypatch.setattr(measurement, "_load_verified_resources", lambda: replace(resources, encode_messages=encode))
    result = measurement.measure_insight_prompt_tokens(prompt)
    requests = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: requests.append(kwargs))))
    _, error = _completion_request(client, prompt)
    assert error is None
    assert captured == [(requests[0]["messages"], {"thinking_mode": "chat"})]
    assert requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert captured[0][0] == [
        {"role": "system", "content": prompt.system_prompt},
        {"role": "user", "content": prompt.user_prompt},
    ]
    assert result.local_encoded_input_tokens == 3494


@pytest.mark.parametrize("group_by,body_bytes,tokens", [
    (None, 5347, 1227), ("sku", 11406, 3494),
    (["date", "marketplace", "country", "sku"], 13473, 4211),
])
def test_production_builder_reference_counts(group_by, body_bytes, tokens):
    context = build_insight_context(run_pipeline(ROOT / "data/sample_ecommerce_data.csv", group_by=group_by))
    prompt = build_insight_prompt_v1(context)
    result = measurement.measure_insight_prompt_tokens(prompt)
    assert result.prompt_body_utf8_bytes == len(prompt.system_prompt.encode()) + len(prompt.user_prompt.encode()) == body_bytes
    assert result.local_encoded_input_tokens == tokens
    assert result.encoded_input_utf8_bytes == body_bytes + 66


def boundary_prompt(context, pattern):
    def build(text):
        modified = deepcopy(context)
        modified.diagnostic_signals[0]["message"] = text
        return build_insight_prompt_v1(modified)

    lo, hi = 0, MAX_PROMPT_BYTES // len(pattern.encode("utf-8")) + 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        try:
            build(pattern * mid)
        except InsightPromptError as error:
            assert error.code == "PROMPT_TOO_LARGE"
            hi = mid
        else:
            lo = mid
    prompt = build(pattern * lo)
    remaining = MAX_PROMPT_BYTES - len(prompt.system_prompt.encode()) - len(prompt.user_prompt.encode())
    return build(pattern * lo + "x" * remaining)


@pytest.mark.parametrize("seed", [113, 66])
def test_reproduced_seeded_100kb_boundary(sample_context, seed):
    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits + string.punctuation
    motif = "".join(rng.choice(alphabet) for _ in range(rng.randrange(2, 33)))
    prompt = boundary_prompt(sample_context, motif)
    result = measurement.measure_insight_prompt_tokens(prompt)
    assert result.prompt_body_utf8_bytes == MAX_PROMPT_BYTES == 100000
    assert result.encoded_input_utf8_bytes == 100066
    assert result.local_encoded_input_tokens == 92147
    assert result.local_encoded_input_tokens <= result.encoded_input_utf8_bytes
    assert result == measurement.measure_insight_prompt_tokens(prompt)
    # The full trailing content reaches the tokenizer; no hidden clipping.
    extended = measurement.measure_insight_prompt_tokens(replace(prompt, user_prompt=prompt.user_prompt + " q7Z3"))
    assert extended.prompt_body_utf8_bytes == 100005
    assert extended.encoded_input_utf8_bytes == 100071
    assert extended.local_encoded_input_tokens > result.local_encoded_input_tokens


@pytest.mark.parametrize("pattern", ["商家库存", "😀e\u0301", '{"sku":"A\\B","n":123}\n'])
def test_unicode_and_escaped_production_boundary(sample_context, pattern):
    prompt = boundary_prompt(sample_context, pattern)
    result = measurement.measure_insight_prompt_tokens(prompt)
    assert result.prompt_body_utf8_bytes == 100000
    assert result.encoded_input_utf8_bytes == 100066
    assert result.local_encoded_input_tokens <= result.encoded_input_utf8_bytes
    assert result == measurement.measure_insight_prompt_tokens(prompt)


def test_pinned_bytelevel_configuration_supports_local_bound():
    from tokenizers.pre_tokenizers import ByteLevel

    config = json.loads((ASSETS / "tokenizer.json").read_bytes())
    assert config["normalizer"] == {"type": "Sequence", "normalizers": []}
    assert config["truncation"] is None and config["padding"] is None
    model = config["model"]
    assert model["type"] == "BPE" and model["dropout"] is None
    assert model["continuing_subword_prefix"] is None and model["end_of_word_suffix"] is None
    assert all(symbol in model["vocab"] for symbol in ByteLevel.alphabet())
    assert all(token["content"] for token in config["added_tokens"])
    stages = config["pre_tokenizer"]["pretokenizers"]
    assert all(stage["type"] == "Split" and stage["behavior"] == "Isolated" for stage in stages[:-1])
    assert stages[-1] == {"type": "ByteLevel", "add_prefix_space": False, "trim_offsets": True, "use_regex": False}
    assert config["post_processor"]["type"] == "ByteLevel"
    resources = measurement._load_verified_resources()
    assert resources.tokenizer.truncation is None and resources.tokenizer.padding is None


def test_usage_is_independent_and_missing_usage_stays_missing(valid_result):
    usage = ProviderUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    assert usage.prompt_tokens != valid_result.local_encoded_input_tokens
    generation = ProviderGeneration(raw_text="{}", usage=usage)
    missing = ProviderGeneration(raw_text="{}", usage=None)
    measurement.measure_insight_prompt_tokens(secret_prompt())
    assert generation.usage is usage and usage.prompt_tokens == 1
    assert missing.usage is None
    assert "usage" not in {field.name for field in fields(valid_result)}


def test_deterministic_cold_warm_loads_keys_time_and_randomness(monkeypatch, fresh_loader):
    prompt = secret_prompt()
    first = measurement.measure_insight_prompt_tokens(prompt)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "FAKE_KEY_NOT_USED")
    monkeypatch.setenv("HF_TOKEN", "FAKE_KEY_NOT_USED")
    monkeypatch.setattr(time, "time", fail_if_called)
    monkeypatch.setattr(random, "random", fail_if_called)
    second = measurement.measure_insight_prompt_tokens(prompt)
    measurement._load_verified_resources.cache_clear()
    assert measurement.measure_insight_prompt_tokens(prompt) == second == first


@pytest.mark.parametrize("filename", HASHES)
def test_vendored_hashes_match_reviewed_pin(filename):
    assert sha256((ASSETS / filename).read_bytes()).hexdigest() == HASHES[filename]
    manifest = json.loads((ASSETS / "manifest.json").read_bytes())
    assert manifest["files"][filename] == HASHES[filename]
    assert manifest["revision"] == "7872f01b1d1fe23eabc4c98b48bffcef5a386062"


@pytest.mark.parametrize("filename", HASHES)
@pytest.mark.parametrize("kind", ["tamper", "missing", "tamper_and_rehash_manifest"])
def test_asset_failure_is_safe_and_does_not_load_runtime(copied_assets, filename, kind, monkeypatch, caplog):
    path = copied_assets / filename
    if kind == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + SECRET.encode())
    if kind == "tamper_and_rehash_manifest":
        manifest_path = copied_assets / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"][filename] = sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime_calls = []
    monkeypatch.setattr(measurement, "_load_tokenizer_runtime", lambda: runtime_calls.append(True))
    assert_error(lambda: measurement.measure_insight_prompt_tokens(secret_prompt()), measurement.TOKEN_MEASUREMENT_UNAVAILABLE, caplog)
    assert runtime_calls == []
    assert measurement._load_verified_resources.cache_info().currsize == 0


@pytest.mark.parametrize("kind", ["missing", "malformed", "list", "revision", "runtime", "duplicate", "wrong_hash"])
def test_manifest_failures(copied_assets, kind):
    path = copied_assets / "manifest.json"
    original = path.read_text(encoding="utf-8")
    manifest = json.loads(original)
    if kind == "missing":
        path.unlink()
    elif kind == "malformed":
        path.write_text(SECRET, encoding="utf-8")
    elif kind == "list":
        path.write_text("[]", encoding="utf-8")
    elif kind == "duplicate":
        path.write_text(original.replace('"asset_schema_version": "1",', '"asset_schema_version": "1", "asset_schema_version": "1",'), encoding="utf-8")
    else:
        if kind == "revision":
            manifest["revision"] = "main"
        elif kind == "runtime":
            manifest["runtime_version"] = "0.22.1"
        else:
            manifest["files"]["tokenizer.json"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
    assert_error(lambda: measurement.measure_insight_prompt_tokens(secret_prompt()), measurement.TOKEN_MEASUREMENT_UNAVAILABLE)


@pytest.mark.parametrize("version", [None, 0.22, "0.22.1", "0.22.2 "])
def test_incompatible_runtime_version(monkeypatch, fresh_loader, version):
    runtime = SimpleNamespace(__version__=version, Tokenizer=SimpleNamespace(from_str=fail_if_called))
    monkeypatch.setattr(measurement, "_load_tokenizer_runtime", lambda: runtime)
    assert_error(lambda: measurement.measure_insight_prompt_tokens(secret_prompt()), measurement.TOKEN_MEASUREMENT_UNAVAILABLE)
    assert measurement._load_verified_resources.cache_info().currsize == 0


@pytest.mark.parametrize("stage", ["runtime_import", "tokenizer_load", "encoder", "tokenizer_encode"])
def test_exception_privacy(monkeypatch, fresh_loader, stage, caplog, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET + " private/path secret asset contents")

    if stage == "runtime_import":
        monkeypatch.setattr(measurement, "_load_tokenizer_runtime", fail)
    elif stage == "tokenizer_load":
        monkeypatch.setattr(measurement, "_load_tokenizer_runtime", lambda: SimpleNamespace(
            __version__="0.22.2", Tokenizer=SimpleNamespace(from_str=fail),
        ))
    else:
        resources = measurement._load_verified_resources()
        changed = replace(resources, **({"encode_messages": fail} if stage == "encoder" else {"tokenizer": SimpleNamespace(encode=fail)}))
        monkeypatch.setattr(measurement, "_load_verified_resources", lambda: changed)
    assert_error(lambda: measurement.measure_insight_prompt_tokens(secret_prompt()), measurement.TOKEN_MEASUREMENT_UNAVAILABLE, caplog)
    assert SECRET not in str(capsys.readouterr())


@pytest.mark.parametrize("setting", ["padding", "truncation"])
def test_runtime_cannot_silently_pad_or_truncate(monkeypatch, fresh_loader, setting):
    tokenizer = SimpleNamespace(padding=None, truncation=None)
    setattr(tokenizer, setting, {"enabled": True})
    runtime = SimpleNamespace(__version__="0.22.2", Tokenizer=SimpleNamespace(from_str=lambda value: tokenizer))
    monkeypatch.setattr(measurement, "_load_tokenizer_runtime", lambda: runtime)
    assert_error(lambda: measurement.measure_insight_prompt_tokens(secret_prompt()), measurement.TOKEN_MEASUREMENT_UNAVAILABLE)
    assert measurement._load_verified_resources.cache_info().currsize == 0


def test_verified_cache_keeps_snapshot_and_failed_load_can_recover(copied_assets):
    path = copied_assets / "tokenizer_config.json"
    original = path.read_bytes()
    first = measurement.measure_insight_prompt_tokens(secret_prompt())
    path.write_bytes(b"changed after verification")
    assert measurement.measure_insight_prompt_tokens(secret_prompt()) == first
    measurement._load_verified_resources.cache_clear()
    assert_error(lambda: measurement.measure_insight_prompt_tokens(secret_prompt()), measurement.TOKEN_MEASUREMENT_UNAVAILABLE)
    assert measurement._load_verified_resources.cache_info().currsize == 0
    path.write_bytes(original)
    assert measurement.measure_insight_prompt_tokens(secret_prompt()) == first


def test_unknown_error_code_cannot_echo_arbitrary_text():
    error = measurement.TokenMeasurementError(SECRET)
    assert error.code == measurement.INVALID_TOKEN_MEASUREMENT_INPUT
    assert SECRET not in str(error)
