"""Tests for quant_foundry.llm_feature_agent (T-13.4 LLM Feature Agent).

Covers:
- LLMModelSpec construction + validation
- PromptSpec construction + validation (hash matches template)
- LLMFeature construction + validation (provenance, no trade signal)
- LLMFeatureManifest construction + validation (no duplicate ids)
- compute_prompt_hash (deterministic, hex)
- validate_no_trade_signal (allowed, rejected)
- validate_source_hash_present (present, missing)
- LLMFeatureAgent.extract (mocked LLM, schema valid/invalid, fail-closed)
- LLMFeatureAgent.validate_feature (valid, invalid)
- LLMFeatureAgent.batch_extract
- LLMFeatureAgent.build_manifest
- Fail-closed behaviors: missing source hash, trade signal, invalid schema
- Edge cases: empty text, single feature, multiple prompts
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError
from quant_foundry.llm_feature_agent import (
    LLMFeature,
    LLMFeatureAgent,
    LLMFeatureManifest,
    LLMModelSpec,
    PromptSpec,
    compute_prompt_hash,
    validate_no_trade_signal,
    validate_source_hash_present,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

ZERO_HASH = hashlib.sha256(b"").hexdigest()
ALT_HASH = hashlib.sha256(b"different").hexdigest()
ISO_TS = "2026-01-01T00:00:00+00:00"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _model_spec(
    model_id: str = "gpt-4",
    model_hash: str = ZERO_HASH,
    provider: str = "openai",
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> LLMModelSpec:
    return LLMModelSpec(
        model_id=model_id,
        model_hash=model_hash,
        provider=provider,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _prompt_spec(
    prompt_id: str = "event_sentiment",
    prompt_template: str = "Classify the sentiment of: {source_text}",
    output_schema: dict | None = None,
) -> PromptSpec:
    if output_schema is None:
        output_schema = {"type": "string", "enum": ["positive", "negative", "neutral"]}
    return PromptSpec(
        prompt_id=prompt_id,
        prompt_template=prompt_template,
        prompt_hash=compute_prompt_hash(prompt_template),
        output_schema=output_schema,
    )


def _feature(
    feature_id: str = "event_sentiment_" + ZERO_HASH + "_" + ZERO_HASH,
    prompt_id: str = "event_sentiment",
    prompt_hash: str = ZERO_HASH,
    model_id: str = "gpt-4",
    model_hash: str = ZERO_HASH,
    source_hash: str = ZERO_HASH,
    feature_name: str = "event_sentiment",
    feature_value: Any = "positive",
    availability_time: str = ISO_TS,
    created_at: str = ISO_TS,
    validated: bool = True,
) -> LLMFeature:
    return LLMFeature(
        feature_id=feature_id,
        prompt_id=prompt_id,
        prompt_hash=prompt_hash,
        model_id=model_id,
        model_hash=model_hash,
        source_hash=source_hash,
        feature_name=feature_name,
        feature_value=feature_value,
        availability_time=availability_time,
        created_at=created_at,
        validated=validated,
    )


def _mock_llm_client(return_value: Any = "positive"):
    """Return a mock LLM client callable returning ``return_value``."""

    def _client(model_spec: LLMModelSpec, prompt_text: str) -> Any:
        return return_value

    return _client


# ---------------------------------------------------------------------------
# compute_prompt_hash
# ---------------------------------------------------------------------------


class TestComputePromptHash:
    def test_deterministic(self):
        h1 = compute_prompt_hash("hello world")
        h2 = compute_prompt_hash("hello world")
        assert h1 == h2

    def test_is_64_char_hex(self):
        h = compute_prompt_hash("abc")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_matches_sha256(self):
        assert compute_prompt_hash("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_different_inputs_different_hashes(self):
        assert compute_prompt_hash("a") != compute_prompt_hash("b")

    def test_empty_string_hash(self):
        # Empty string is allowed (hash of b"").
        h = compute_prompt_hash("")
        assert h == hashlib.sha256(b"").hexdigest()

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            compute_prompt_hash(123)  # type: ignore[arg-type]

    def test_unicode(self):
        h = compute_prompt_hash("héllo wörld 日本語")
        assert h == hashlib.sha256("héllo wörld 日本語".encode()).hexdigest()


# ---------------------------------------------------------------------------
# LLMModelSpec
# ---------------------------------------------------------------------------


class TestLLMModelSpec:
    def test_defaults(self):
        spec = _model_spec()
        assert spec.model_id == "gpt-4"
        assert spec.model_hash == ZERO_HASH
        assert spec.provider == "openai"
        assert spec.max_tokens == 4096
        assert spec.temperature == 0.0

    def test_frozen(self):
        spec = _model_spec()
        with pytest.raises(ValidationError):
            spec.model_id = "claude"  # type: ignore[misc]

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            LLMModelSpec(
                model_id="gpt-4",
                model_hash=ZERO_HASH,
                provider="openai",
                unexpected="x",  # type: ignore[call-arg]
            )

    def test_empty_model_id_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(model_id="")

    def test_whitespace_model_id_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(model_id="   ")

    def test_invalid_model_hash_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(model_hash="not-a-hash")

    def test_short_model_hash_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(model_hash="abc")

    def test_uppercase_model_hash_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(model_hash=ZERO_HASH.upper())

    def test_invalid_provider_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(provider="google")

    def test_all_providers_allowed(self):
        for p in ["openai", "anthropic", "local", "azure"]:
            assert _model_spec(provider=p).provider == p

    def test_zero_max_tokens_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(max_tokens=0)

    def test_negative_max_tokens_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(max_tokens=-1)

    def test_temperature_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(temperature=-0.1)

    def test_temperature_above_two_rejected(self):
        with pytest.raises(ValidationError):
            _model_spec(temperature=2.1)

    def test_temperature_boundaries_allowed(self):
        assert _model_spec(temperature=0.0).temperature == 0.0
        assert _model_spec(temperature=2.0).temperature == 2.0


# ---------------------------------------------------------------------------
# PromptSpec
# ---------------------------------------------------------------------------


class TestPromptSpec:
    def test_valid(self):
        ps = _prompt_spec()
        assert ps.prompt_id == "event_sentiment"
        assert ps.prompt_hash == compute_prompt_hash(ps.prompt_template)

    def test_frozen(self):
        ps = _prompt_spec()
        with pytest.raises(ValidationError):
            ps.prompt_id = "x"  # type: ignore[misc]

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            PromptSpec(
                prompt_id="x",
                prompt_template="t",
                prompt_hash=compute_prompt_hash("t"),
                output_schema={"type": "string"},
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_empty_prompt_id_rejected(self):
        with pytest.raises(ValidationError):
            PromptSpec(
                prompt_id="",
                prompt_template="t",
                prompt_hash=compute_prompt_hash("t"),
                output_schema={"type": "string"},
            )

    def test_invalid_prompt_hash_rejected(self):
        with pytest.raises(ValidationError):
            PromptSpec(
                prompt_id="x",
                prompt_template="t",
                prompt_hash="nothex",
                output_schema={"type": "string"},
            )

    def test_hash_mismatch_rejected(self):
        with pytest.raises(ValidationError):
            PromptSpec(
                prompt_id="x",
                prompt_template="t",
                prompt_hash=ZERO_HASH,  # wrong hash
                output_schema={"type": "string"},
            )

    def test_empty_template_rejected(self):
        with pytest.raises(ValidationError):
            PromptSpec(
                prompt_id="x",
                prompt_template="",
                prompt_hash=compute_prompt_hash(""),
                output_schema={"type": "string"},
            )

    def test_output_schema_must_be_dict(self):
        with pytest.raises(ValidationError):
            PromptSpec(
                prompt_id="x",
                prompt_template="t",
                prompt_hash=compute_prompt_hash("t"),
                output_schema=["not", "a", "dict"],  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# LLMFeature
# ---------------------------------------------------------------------------


class TestLLMFeature:
    def test_valid(self):
        f = _feature()
        assert f.feature_name == "event_sentiment"
        assert f.validated is True
        assert f.feature_value == "positive"

    def test_frozen(self):
        f = _feature()
        with pytest.raises(ValidationError):
            f.feature_value = "negative"  # type: ignore[misc]

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            LLMFeature(
                feature_id="x",
                prompt_id="event_sentiment",
                prompt_hash=ZERO_HASH,
                model_id="gpt-4",
                model_hash=ZERO_HASH,
                source_hash=ZERO_HASH,
                feature_name="event_sentiment",
                feature_value="positive",
                availability_time=ISO_TS,
                created_at=ISO_TS,
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_trade_signal_name_rejected(self):
        with pytest.raises(ValidationError):
            _feature(feature_name="buy")

    def test_trade_signal_target_weight_rejected(self):
        with pytest.raises(ValidationError):
            _feature(feature_name="target_weight")

    def test_trade_signal_case_insensitive(self):
        with pytest.raises(ValidationError):
            _feature(feature_name="BUY")

    def test_invalid_prompt_hash_rejected(self):
        with pytest.raises(ValidationError):
            _feature(prompt_hash="nothex")

    def test_invalid_model_hash_rejected(self):
        with pytest.raises(ValidationError):
            _feature(model_hash="nothex")

    def test_invalid_source_hash_rejected(self):
        with pytest.raises(ValidationError):
            _feature(source_hash="nothex")

    def test_empty_feature_name_rejected(self):
        with pytest.raises(ValidationError):
            _feature(feature_name="")

    def test_empty_feature_id_rejected(self):
        with pytest.raises(ValidationError):
            _feature(feature_id="")

    def test_invalid_availability_time_rejected(self):
        with pytest.raises(ValidationError):
            _feature(availability_time="not-a-date")

    def test_invalid_created_at_rejected(self):
        with pytest.raises(ValidationError):
            _feature(created_at="not-a-date")

    def test_feature_value_list(self):
        f = _feature(feature_value=["a", "b"], feature_name="event_tags")
        assert f.feature_value == ["a", "b"]

    def test_feature_value_dict(self):
        f = _feature(feature_value={"k": "v"}, feature_name="explanation")
        assert f.feature_value == {"k": "v"}

    def test_feature_value_float(self):
        f = _feature(feature_value=0.75, feature_name="risk_flag")
        assert f.feature_value == 0.75


# ---------------------------------------------------------------------------
# LLMFeatureManifest
# ---------------------------------------------------------------------------


class TestLLMFeatureManifest:
    def test_valid(self):
        m = LLMFeatureManifest(
            manifest_id="m1",
            model_spec=_model_spec(),
            prompt_specs=[_prompt_spec()],
            features=[_feature()],
            created_at=ISO_TS,
        )
        assert m.manifest_id == "m1"
        assert len(m.features) == 1

    def test_frozen(self):
        m = LLMFeatureManifest(
            manifest_id="m1",
            model_spec=_model_spec(),
            prompt_specs=[_prompt_spec()],
            features=[_feature()],
            created_at=ISO_TS,
        )
        with pytest.raises(ValidationError):
            m.manifest_id = "x"  # type: ignore[misc]

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            LLMFeatureManifest(
                manifest_id="m1",
                model_spec=_model_spec(),
                prompt_specs=[_prompt_spec()],
                features=[_feature()],
                created_at=ISO_TS,
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_duplicate_feature_ids_rejected(self):
        f = _feature()
        with pytest.raises(ValidationError):
            LLMFeatureManifest(
                manifest_id="m1",
                model_spec=_model_spec(),
                prompt_specs=[_prompt_spec()],
                features=[f, f],
                created_at=ISO_TS,
            )

    def test_duplicate_prompt_ids_rejected(self):
        ps = _prompt_spec()
        with pytest.raises(ValidationError):
            LLMFeatureManifest(
                manifest_id="m1",
                model_spec=_model_spec(),
                prompt_specs=[ps, ps],
                features=[_feature()],
                created_at=ISO_TS,
            )

    def test_empty_manifest_id_rejected(self):
        with pytest.raises(ValidationError):
            LLMFeatureManifest(
                manifest_id="",
                model_spec=_model_spec(),
                prompt_specs=[_prompt_spec()],
                features=[_feature()],
                created_at=ISO_TS,
            )

    def test_empty_features_allowed(self):
        m = LLMFeatureManifest(
            manifest_id="m1",
            model_spec=_model_spec(),
            prompt_specs=[_prompt_spec()],
            features=[],
            created_at=ISO_TS,
        )
        assert m.features == []


# ---------------------------------------------------------------------------
# validate_no_trade_signal
# ---------------------------------------------------------------------------


class TestValidateNoTradeSignal:
    def test_allowed_feature(self):
        f = _feature(feature_name="event_sentiment")
        assert validate_no_trade_signal(f) is True

    def test_allowed_tags(self):
        f = _feature(feature_name="event_tags", feature_value=["x"])
        assert validate_no_trade_signal(f) is True

    def test_buy_rejected(self):
        # LLMFeature construction already rejects trade signals, so we
        # build a feature with an allowed name and then monkeypatch to
        # test the validator directly.
        f = _feature(feature_name="event_sentiment")
        object.__setattr__(f, "feature_name", "buy")
        with pytest.raises(ValueError):
            validate_no_trade_signal(f)

    def test_sell_rejected(self):
        f = _feature(feature_name="event_sentiment")
        object.__setattr__(f, "feature_name", "sell")
        with pytest.raises(ValueError):
            validate_no_trade_signal(f)

    def test_target_weight_rejected(self):
        f = _feature(feature_name="event_sentiment")
        object.__setattr__(f, "feature_name", "target_weight")
        with pytest.raises(ValueError):
            validate_no_trade_signal(f)

    def test_non_llmfeature_rejected(self):
        with pytest.raises(ValueError):
            validate_no_trade_signal("not a feature")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_source_hash_present
# ---------------------------------------------------------------------------


class TestValidateSourceHashPresent:
    def test_present(self):
        f = _feature(source_hash=ZERO_HASH)
        assert validate_source_hash_present(f) is True

    def test_missing_raises(self):
        f = _feature(source_hash=ZERO_HASH)
        object.__setattr__(f, "source_hash", "")
        with pytest.raises(ValueError):
            validate_source_hash_present(f)

    def test_non_llmfeature_rejected(self):
        with pytest.raises(ValueError):
            validate_source_hash_present(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LLMFeatureAgent.extract
# ---------------------------------------------------------------------------


class TestLLMFeatureAgentExtract:
    def test_extract_valid_schema(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        f = agent.extract(
            source_text="Company X beat earnings.",
            source_hash=ZERO_HASH,
            prompt_id="event_sentiment",
            availability_time=ISO_TS,
        )
        assert f.feature_name == "event_sentiment"
        assert f.feature_value == "positive"
        assert f.validated is True
        assert f.prompt_id == "event_sentiment"
        assert f.prompt_hash == compute_prompt_hash("Classify the sentiment of: {source_text}")
        assert f.model_id == "gpt-4"
        assert f.model_hash == ZERO_HASH
        assert f.source_hash == ZERO_HASH
        assert f.availability_time == ISO_TS

    def test_extract_invalid_schema_marks_unvalidated(self):
        # Schema expects a string enum, but the mock returns a number.
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client(42),
        )
        f = agent.extract(
            source_text="text",
            source_hash=ZERO_HASH,
            prompt_id="event_sentiment",
            availability_time=ISO_TS,
        )
        assert f.validated is False
        assert f.feature_value == 42

    def test_extract_missing_source_hash_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.extract(
                source_text="text",
                source_hash="",
                prompt_id="event_sentiment",
                availability_time=ISO_TS,
            )

    def test_extract_none_source_hash_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.extract(
                source_text="text",
                source_hash=None,  # type: ignore[arg-type]
                prompt_id="event_sentiment",
                availability_time=ISO_TS,
            )

    def test_extract_invalid_source_hash_format_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.extract(
                source_text="text",
                source_hash="not-a-hash",
                prompt_id="event_sentiment",
                availability_time=ISO_TS,
            )

    def test_extract_unknown_prompt_id_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.extract(
                source_text="text",
                source_hash=ZERO_HASH,
                prompt_id="nonexistent",
                availability_time=ISO_TS,
            )

    def test_extract_empty_prompt_id_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.extract(
                source_text="text",
                source_hash=ZERO_HASH,
                prompt_id="",
                availability_time=ISO_TS,
            )

    def test_extract_trade_signal_prompt_id_raises(self):
        # A prompt whose id is a trade-signal name is rejected.
        bad_prompt = PromptSpec(
            prompt_id="buy",
            prompt_template="Should I buy? {source_text}",
            prompt_hash=compute_prompt_hash("Should I buy? {source_text}"),
            output_schema={"type": "string"},
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec(), bad_prompt],
            llm_client=_mock_llm_client("yes"),
        )
        with pytest.raises(ValueError):
            agent.extract(
                source_text="text",
                source_hash=ZERO_HASH,
                prompt_id="buy",
                availability_time=ISO_TS,
            )

    def test_extract_empty_source_text_allowed(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("neutral"),
        )
        f = agent.extract(
            source_text="",
            source_hash=ZERO_HASH,
            prompt_id="event_sentiment",
            availability_time=ISO_TS,
        )
        assert f.feature_value == "neutral"
        assert f.validated is True

    def test_extract_deterministic_feature_id(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        f1 = agent.extract("text", ZERO_HASH, "event_sentiment", ISO_TS)
        f2 = agent.extract("text", ZERO_HASH, "event_sentiment", ISO_TS)
        assert f1.feature_id == f2.feature_id
        assert f1.feature_id == f"event_sentiment_{ZERO_HASH}_{ZERO_HASH}"

    def test_extract_different_source_hash_different_id(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        f1 = agent.extract("text", ZERO_HASH, "event_sentiment", ISO_TS)
        f2 = agent.extract("text", ALT_HASH, "event_sentiment", ISO_TS)
        assert f1.feature_id != f2.feature_id

    def test_extract_invalid_availability_time_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.extract(
                source_text="text",
                source_hash=ZERO_HASH,
                prompt_id="event_sentiment",
                availability_time="not-a-date",
            )

    def test_extract_provenance_complete(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        f = agent.extract("text", ZERO_HASH, "event_sentiment", ISO_TS)
        # Every feature must have prompt hash, model id, source hash,
        # availability time.
        assert f.prompt_hash and len(f.prompt_hash) == 64
        assert f.model_id
        assert f.model_hash and len(f.model_hash) == 64
        assert f.source_hash and len(f.source_hash) == 64
        assert f.availability_time


# ---------------------------------------------------------------------------
# LLMFeatureAgent.validate_feature
# ---------------------------------------------------------------------------


class TestValidateFeature:
    def test_valid(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        f = _feature(feature_value="positive")
        assert agent.validate_feature(f, _prompt_spec()) is True

    def test_invalid_schema_raises(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        f = _feature(feature_value=42)
        with pytest.raises(ValueError):
            agent.validate_feature(f, _prompt_spec())

    def test_invalid_enum_raises(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        f = _feature(feature_value="not-in-enum")
        with pytest.raises(ValueError):
            agent.validate_feature(f, _prompt_spec())

    def test_non_feature_rejected(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        with pytest.raises(ValueError):
            agent.validate_feature("not a feature", _prompt_spec())  # type: ignore[arg-type]

    def test_non_prompt_spec_rejected(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        f = _feature()
        with pytest.raises(ValueError):
            agent.validate_feature(f, "not a spec")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LLMFeatureAgent.batch_extract
# ---------------------------------------------------------------------------


class TestBatchExtract:
    def test_batch(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        feats = agent.batch_extract(
            source_texts=["a", "b", "c"],
            source_hashes=[ZERO_HASH, ALT_HASH, _hash("c")],
            prompt_id="event_sentiment",
            availability_time=ISO_TS,
        )
        assert len(feats) == 3
        assert all(f.validated for f in feats)
        assert feats[0].source_hash == ZERO_HASH
        assert feats[1].source_hash == ALT_HASH

    def test_batch_length_mismatch_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.batch_extract(
                source_texts=["a", "b"],
                source_hashes=[ZERO_HASH],
                prompt_id="event_sentiment",
                availability_time=ISO_TS,
            )

    def test_batch_empty_lists(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        feats = agent.batch_extract(
            source_texts=[],
            source_hashes=[],
            prompt_id="event_sentiment",
            availability_time=ISO_TS,
        )
        assert feats == []

    def test_batch_fail_closed_on_missing_hash(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.batch_extract(
                source_texts=["a", "b"],
                source_hashes=[ZERO_HASH, ""],
                prompt_id="event_sentiment",
                availability_time=ISO_TS,
            )

    def test_batch_non_list_rejected(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        with pytest.raises(ValueError):
            agent.batch_extract(
                source_texts="not a list",  # type: ignore[arg-type]
                source_hashes=[ZERO_HASH],
                prompt_id="event_sentiment",
                availability_time=ISO_TS,
            )


# ---------------------------------------------------------------------------
# LLMFeatureAgent.build_manifest
# ---------------------------------------------------------------------------


class TestBuildManifest:
    def test_build_manifest(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        f1 = agent.extract("a", ZERO_HASH, "event_sentiment", ISO_TS)
        f2 = agent.extract("b", ALT_HASH, "event_sentiment", ISO_TS)
        m = agent.build_manifest([f1, f2], manifest_id="m1")
        assert m.manifest_id == "m1"
        assert len(m.features) == 2
        assert m.model_spec.model_id == "gpt-4"
        assert len(m.prompt_specs) == 1

    def test_build_manifest_default_id(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        f = agent.extract("a", ZERO_HASH, "event_sentiment", ISO_TS)
        m = agent.build_manifest([f])
        assert m.manifest_id.startswith("manifest_")

    def test_build_manifest_duplicate_feature_ids_raises(self):
        agent = LLMFeatureAgent(
            _model_spec(),
            [_prompt_spec()],
            llm_client=_mock_llm_client("positive"),
        )
        f = agent.extract("a", ZERO_HASH, "event_sentiment", ISO_TS)
        with pytest.raises(ValidationError):
            agent.build_manifest([f, f])

    def test_build_manifest_empty_features(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        m = agent.build_manifest([], manifest_id="m1")
        assert m.features == []

    def test_build_manifest_non_list_rejected(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        with pytest.raises(ValueError):
            agent.build_manifest("not a list")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LLMFeatureAgent construction
# ---------------------------------------------------------------------------


class TestAgentConstruction:
    def test_empty_prompt_specs_rejected(self):
        with pytest.raises(ValueError):
            LLMFeatureAgent(_model_spec(), [])

    def test_duplicate_prompt_ids_rejected(self):
        ps = _prompt_spec()
        with pytest.raises(ValueError):
            LLMFeatureAgent(_model_spec(), [ps, ps])

    def test_non_model_spec_rejected(self):
        with pytest.raises(ValueError):
            LLMFeatureAgent("not a spec", [_prompt_spec()])  # type: ignore[arg-type]

    def test_non_prompt_spec_in_list_rejected(self):
        with pytest.raises(ValueError):
            LLMFeatureAgent(_model_spec(), ["not a spec"])  # type: ignore[list-item]

    def test_properties(self):
        agent = LLMFeatureAgent(_model_spec(), [_prompt_spec()])
        assert agent.model_spec.model_id == "gpt-4"
        assert len(agent.prompt_specs) == 1

    def test_multiple_prompts(self):
        ps1 = _prompt_spec(prompt_id="event_sentiment")
        ps2 = _prompt_spec(
            prompt_id="event_tags",
            prompt_template="Extract tags from: {source_text}",
            output_schema={"type": "array", "items": {"type": "string"}},
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [ps1, ps2],
            llm_client=_mock_llm_client(["earnings", "beat"]),
        )
        f = agent.extract("text", ZERO_HASH, "event_tags", ISO_TS)
        assert f.feature_name == "event_tags"
        assert f.feature_value == ["earnings", "beat"]
        assert f.validated is True


# ---------------------------------------------------------------------------
# Schema validation edge cases
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_array_schema_valid(self):
        ps = _prompt_spec(
            prompt_id="event_tags",
            prompt_template="Tags: {source_text}",
            output_schema={"type": "array", "items": {"type": "string"}},
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [ps],
            llm_client=_mock_llm_client(["a", "b"]),
        )
        f = agent.extract("text", ZERO_HASH, "event_tags", ISO_TS)
        assert f.validated is True

    def test_array_schema_invalid_item(self):
        ps = _prompt_spec(
            prompt_id="event_tags",
            prompt_template="Tags: {source_text}",
            output_schema={"type": "array", "items": {"type": "string"}},
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [ps],
            llm_client=_mock_llm_client(["a", 42]),
        )
        f = agent.extract("text", ZERO_HASH, "event_tags", ISO_TS)
        assert f.validated is False

    def test_object_schema_valid(self):
        ps = _prompt_spec(
            prompt_id="explanation",
            prompt_template="Explain: {source_text}",
            output_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [ps],
            llm_client=_mock_llm_client({"summary": "good news"}),
        )
        f = agent.extract("text", ZERO_HASH, "explanation", ISO_TS)
        assert f.validated is True

    def test_object_schema_missing_required(self):
        ps = _prompt_spec(
            prompt_id="explanation",
            prompt_template="Explain: {source_text}",
            output_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [ps],
            llm_client=_mock_llm_client({"other": "x"}),
        )
        f = agent.extract("text", ZERO_HASH, "explanation", ISO_TS)
        assert f.validated is False

    def test_number_schema_valid(self):
        ps = _prompt_spec(
            prompt_id="risk_flag",
            prompt_template="Risk score: {source_text}",
            output_schema={"type": "number"},
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [ps],
            llm_client=_mock_llm_client(0.85),
        )
        f = agent.extract("text", ZERO_HASH, "risk_flag", ISO_TS)
        assert f.validated is True

    def test_number_schema_invalid_type(self):
        ps = _prompt_spec(
            prompt_id="risk_flag",
            prompt_template="Risk score: {source_text}",
            output_schema={"type": "number"},
        )
        agent = LLMFeatureAgent(
            _model_spec(),
            [ps],
            llm_client=_mock_llm_client("high"),
        )
        f = agent.extract("text", ZERO_HASH, "risk_flag", ISO_TS)
        assert f.validated is False
