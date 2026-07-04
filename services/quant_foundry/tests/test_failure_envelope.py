"""Tests for quant_foundry.failure_envelope — signed failure envelopes (T-5.3).

Covers:
- FailureStage / FailureCode enums
- FailureContext / FailureEnvelope construction + validation
- FailureEnvelopeBuilder.build (with/without secret)
- compute_context_hash determinism
- compute_envelope_hash determinism + order-independence
- sign_envelope (with/without secret)
- validate_envelope (valid, hash mismatch, context hash mismatch)
- verify_signature (valid, invalid, no signature)
- is_retryable (retryable + non-retryable codes)
- serialize/deserialize round-trip
- distinguish_missing_callback_vs_signed_failure (None/valid/invalid)
- fail-closed behavior on tampering
- edge cases: minimal, all fields, no secret
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from pydantic import ValidationError
from quant_foundry.failure_envelope import (
    FailureCode,
    FailureContext,
    FailureEnvelope,
    FailureEnvelopeBuilder,
    FailureStage,
    deserialize_envelope,
    distinguish_missing_callback_vs_signed_failure,
    is_retryable,
    serialize_envelope,
    validate_envelope,
    verify_signature,
)

SECRET = "test-callback-secret-1234"


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestFailureStage:
    def test_all_stages_present(self) -> None:
        expected = {
            "dataset_fetch",
            "data_validation",
            "quality_gate",
            "model_load",
            "training",
            "inference",
            "artifact_write",
            "callback_send",
            "security_preflight",
            "unknown",
        }
        actual = {s.value for s in FailureStage}
        assert actual == expected

    def test_stage_is_str_enum(self) -> None:
        assert isinstance(FailureStage.TRAINING, str)
        assert FailureStage.TRAINING == "training"

    def test_stage_lookup_by_value(self) -> None:
        assert FailureStage("quality_gate") is FailureStage.QUALITY_GATE


class TestFailureCode:
    def test_all_codes_present(self) -> None:
        expected = {
            "dataset_not_found",
            "dataset_checksum_mismatch",
            "dataset_format_error",
            "manifest_mismatch",
            "quality_gate_failed",
            "model_load_error",
            "training_error",
            "training_oom",
            "inference_error",
            "artifact_write_error",
            "artifact_hash_mismatch",
            "callback_error",
            "security_violation",
            "env_var_forbidden",
            "gpu_unavailable",
            "unknown_error",
        }
        actual = {c.value for c in FailureCode}
        assert actual == expected

    def test_code_is_str_enum(self) -> None:
        assert isinstance(FailureCode.TRAINING_OOM, str)
        assert FailureCode.TRAINING_OOM == "training_oom"

    def test_code_lookup_by_value(self) -> None:
        assert FailureCode("security_violation") is FailureCode.SECURITY_VIOLATION


# ---------------------------------------------------------------------------
# FailureContext tests
# ---------------------------------------------------------------------------


def _make_context_hash(builder: FailureEnvelopeBuilder, **overrides) -> tuple[FailureContext, str]:
    """Helper: build a FailureContext with a valid computed context_hash."""
    base = dict(
        job_id="job-1",
        dataset_id="ds-1",
        model_family="gbm",
        stage=FailureStage.TRAINING,
        timestamp="2026-01-01T00:00:00+00:00",
        container_user="nonroot",
        git_sha="abc123",
        image_digest="sha256:deadbeef",
        context_hash="0" * 64,
    )
    base.update(overrides)
    ctx = FailureContext(**base)
    chash = builder.compute_context_hash(ctx)
    return ctx.model_copy(update={"context_hash": chash}), chash


class TestFailureContext:
    def test_construct_minimal(self) -> None:
        ctx = FailureContext(
            job_id="job-1",
            stage=FailureStage.UNKNOWN,
            timestamp="2026-01-01T00:00:00+00:00",
            context_hash="a" * 64,
        )
        assert ctx.job_id == "job-1"
        assert ctx.dataset_id is None
        assert ctx.model_family is None
        assert ctx.stage is FailureStage.UNKNOWN
        assert ctx.context_hash == "a" * 64

    def test_construct_all_fields(self) -> None:
        ctx = FailureContext(
            job_id="job-1",
            dataset_id="ds-1",
            model_family="gbm",
            stage=FailureStage.TRAINING,
            timestamp="2026-01-01T00:00:00+00:00",
            container_user="nonroot",
            git_sha="abc123",
            image_digest="sha256:deadbeef",
            context_hash="b" * 64,
        )
        assert ctx.dataset_id == "ds-1"
        assert ctx.git_sha == "abc123"

    def test_frozen(self) -> None:
        ctx = FailureContext(
            job_id="job-1",
            stage=FailureStage.UNKNOWN,
            timestamp="t",
            context_hash="c" * 64,
        )
        with pytest.raises(ValidationError):
            ctx.job_id = "changed"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            FailureContext(
                job_id="job-1",
                stage=FailureStage.UNKNOWN,
                timestamp="t",
                context_hash="d" * 64,
                unexpected_field="bad",  # type: ignore[call-arg]
            )

    def test_context_hash_accepts_any_64_hex(self) -> None:
        """FailureContext itself does not validate context_hash format (the
        envelope-level validator enforces 64-char hex). It accepts any string."""
        ctx = FailureContext(
            job_id="job-1",
            stage=FailureStage.UNKNOWN,
            timestamp="t",
            context_hash="a" * 64,
        )
        assert ctx.context_hash == "a" * 64


# ---------------------------------------------------------------------------
# FailureEnvelope construction tests
# ---------------------------------------------------------------------------


class TestFailureEnvelopeConstruction:
    def test_construct_valid(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.DATASET_NOT_FOUND,
            failure_message="missing",
            retryable=False,
            stage=FailureStage.DATASET_FETCH,
            job_id="job-1",
            dataset_id="ds-1",
        )
        assert env.failure_code is FailureCode.DATASET_NOT_FOUND
        assert env.failure_message == "missing"
        assert env.retryable is False
        assert env.stage is FailureStage.DATASET_FETCH
        assert env.signature is not None
        assert len(env.envelope_hash) == 64
        assert len(env.context_hash) == 64
        assert len(env.envelope_id) == 64

    def test_frozen(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        with pytest.raises(ValidationError):
            env.failure_message = "changed"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            FailureEnvelope(
                envelope_id="e" * 64,
                failure_code=FailureCode.UNKNOWN_ERROR,
                failure_message="x",
                retryable=False,
                stage=FailureStage.UNKNOWN,
                context=FailureContext(
                    job_id="job-1",
                    stage=FailureStage.UNKNOWN,
                    timestamp="t",
                    context_hash="a" * 64,
                ),
                context_hash="a" * 64,
                envelope_hash="b" * 64,
                created_at="t",
                extra="bad",  # type: ignore[call-arg]
            )

    def test_empty_envelope_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FailureEnvelope(
                envelope_id="",
                failure_code=FailureCode.UNKNOWN_ERROR,
                failure_message="x",
                retryable=False,
                stage=FailureStage.UNKNOWN,
                context=FailureContext(
                    job_id="job-1",
                    stage=FailureStage.UNKNOWN,
                    timestamp="t",
                    context_hash="a" * 64,
                ),
                context_hash="a" * 64,
                envelope_hash="b" * 64,
                created_at="t",
            )

    def test_bad_envelope_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FailureEnvelope(
                envelope_id="e" * 64,
                failure_code=FailureCode.UNKNOWN_ERROR,
                failure_message="x",
                retryable=False,
                stage=FailureStage.UNKNOWN,
                context=FailureContext(
                    job_id="job-1",
                    stage=FailureStage.UNKNOWN,
                    timestamp="t",
                    context_hash="a" * 64,
                ),
                context_hash="a" * 64,
                envelope_hash="not64",
                created_at="t",
            )

    def test_bad_signature_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FailureEnvelope(
                envelope_id="e" * 64,
                failure_code=FailureCode.UNKNOWN_ERROR,
                failure_message="x",
                retryable=False,
                stage=FailureStage.UNKNOWN,
                context=FailureContext(
                    job_id="job-1",
                    stage=FailureStage.UNKNOWN,
                    timestamp="t",
                    context_hash="a" * 64,
                ),
                context_hash="a" * 64,
                signature="not64",
                envelope_hash="b" * 64,
                created_at="t",
            )

    def test_none_signature_allowed(self) -> None:
        builder = FailureEnvelopeBuilder()  # no secret
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        assert env.signature is None


# ---------------------------------------------------------------------------
# Builder tests
# ---------------------------------------------------------------------------


class TestFailureEnvelopeBuilder:
    def test_build_with_secret_signs(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.QUALITY_GATE_FAILED,
            failure_message="gate failed",
            retryable=False,
            stage=FailureStage.QUALITY_GATE,
            job_id="job-2",
        )
        assert env.signature is not None
        assert len(env.signature) == 64

    def test_build_without_secret_no_signature(self) -> None:
        builder = FailureEnvelopeBuilder()
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        assert env.signature is None

    def test_build_deterministic_envelope_id(self) -> None:
        """Same job_id + timestamp -> same envelope_id (deterministic)."""
        from quant_foundry.failure_envelope import _envelope_id

        eid1 = _envelope_id("job-1", "2026-01-01T00:00:00+00:00")
        eid2 = _envelope_id("job-1", "2026-01-01T00:00:00+00:00")
        assert eid1 == eid2
        assert len(eid1) == 64

    def test_build_all_fields(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.ARTIFACT_WRITE_ERROR,
            failure_message="disk full",
            retryable=False,
            stage=FailureStage.ARTIFACT_WRITE,
            job_id="job-3",
            dataset_id="ds-3",
            model_family="xgb",
            container_user="nonroot",
            git_sha="def456",
            image_digest="sha256:cafe",
        )
        assert env.context.dataset_id == "ds-3"
        assert env.context.model_family == "xgb"
        assert env.context.git_sha == "def456"
        assert env.context.image_digest == "sha256:cafe"
        assert env.context.container_user == "nonroot"


# ---------------------------------------------------------------------------
# compute_context_hash tests
# ---------------------------------------------------------------------------


class TestComputeContextHash:
    def test_determinism_same_context(self) -> None:
        builder = FailureEnvelopeBuilder()
        ctx1, _ = _make_context_hash(builder)
        # Recompute from a fresh identical context.
        ctx2 = ctx1.model_copy(update={"context_hash": "0" * 64})
        h1 = builder.compute_context_hash(ctx1)
        h2 = builder.compute_context_hash(ctx2)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_context_different_hash(self) -> None:
        builder = FailureEnvelopeBuilder()
        ctx1, _ = _make_context_hash(builder, job_id="job-A")
        ctx2, _ = _make_context_hash(builder, job_id="job-B")
        h1 = builder.compute_context_hash(ctx1.model_copy(update={"context_hash": "0" * 64}))
        h2 = builder.compute_context_hash(ctx2.model_copy(update={"context_hash": "0" * 64}))
        assert h1 != h2

    def test_excludes_context_hash_field(self) -> None:
        """Changing only context_hash must not change the computed hash."""
        builder = FailureEnvelopeBuilder()
        ctx = FailureContext(
            job_id="job-1",
            stage=FailureStage.UNKNOWN,
            timestamp="t",
            context_hash="0" * 64,
        )
        ctx_a = ctx.model_copy(update={"context_hash": "a" * 64})
        ctx_b = ctx.model_copy(update={"context_hash": "b" * 64})
        assert builder.compute_context_hash(ctx_a) == builder.compute_context_hash(ctx_b)


# ---------------------------------------------------------------------------
# compute_envelope_hash tests
# ---------------------------------------------------------------------------


class TestComputeEnvelopeHash:
    def test_determinism(self) -> None:
        builder = FailureEnvelopeBuilder()
        data = {
            "envelope_id": "e" * 64,
            "failure_code": "unknown_error",
            "failure_message": "x",
            "retryable": False,
            "stage": "unknown",
            "context": {
                "job_id": "j",
                "stage": "unknown",
                "timestamp": "t",
                "context_hash": "a" * 64,
            },
            "context_hash": "a" * 64,
            "created_at": "t",
        }
        h1 = builder.compute_envelope_hash(data)
        h2 = builder.compute_envelope_hash(data)
        assert h1 == h2
        assert len(h1) == 64

    def test_order_independence(self) -> None:
        """Key insertion order must not affect the hash (sort_keys=True)."""
        builder = FailureEnvelopeBuilder()
        data_a = {"a": 1, "b": 2, "c": 3}
        data_b = {"c": 3, "a": 1, "b": 2}
        assert builder.compute_envelope_hash(data_a) == builder.compute_envelope_hash(data_b)

    def test_excludes_signature_and_envelope_hash(self) -> None:
        """signature and envelope_hash must not affect the computed envelope hash."""
        builder = FailureEnvelopeBuilder()
        base = {"envelope_id": "e" * 64, "created_at": "t"}
        h1 = builder.compute_envelope_hash({**base, "signature": "x", "envelope_hash": "y"})
        h2 = builder.compute_envelope_hash({**base, "signature": "z", "envelope_hash": "w"})
        assert h1 == h2

    def test_content_change_changes_hash(self) -> None:
        builder = FailureEnvelopeBuilder()
        h1 = builder.compute_envelope_hash({"a": 1})
        h2 = builder.compute_envelope_hash({"a": 2})
        assert h1 != h2


# ---------------------------------------------------------------------------
# sign_envelope tests
# ---------------------------------------------------------------------------


class TestSignEnvelope:
    def test_with_secret(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        h = "a" * 64
        sig = builder.sign_envelope(h)
        assert sig is not None
        expected = hmac.new(SECRET.encode(), h.encode(), hashlib.sha256).hexdigest()
        assert sig == expected

    def test_without_secret_returns_none(self) -> None:
        builder = FailureEnvelopeBuilder()
        sig = builder.sign_envelope("a" * 64)
        assert sig is None

    def test_empty_secret_returns_none(self) -> None:
        builder = FailureEnvelopeBuilder("")
        sig = builder.sign_envelope("a" * 64)
        assert sig is None

    def test_invalid_hash_raises(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        with pytest.raises(ValueError):
            builder.sign_envelope("not64")


# ---------------------------------------------------------------------------
# validate_envelope tests
# ---------------------------------------------------------------------------


class TestValidateEnvelope:
    def test_valid_envelope(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.DATASET_NOT_FOUND,
            failure_message="missing",
            retryable=False,
            stage=FailureStage.DATASET_FETCH,
            job_id="job-1",
        )
        validate_envelope(env)  # should not raise

    def test_context_hash_mismatch_raises(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        tampered_context = env.context.model_copy(update={"context_hash": "0" * 64})
        tampered = env.model_copy(update={"context": tampered_context})
        with pytest.raises(ValueError, match="context_hash"):
            validate_envelope(tampered)

    def test_envelope_hash_mismatch_raises(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        tampered = env.model_copy(update={"envelope_hash": "0" * 64})
        with pytest.raises(ValueError, match="envelope_hash"):
            validate_envelope(tampered)

    def test_tampered_message_detected(self) -> None:
        """Changing failure_message breaks envelope_hash validation."""
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="original",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        tampered = env.model_copy(update={"failure_message": "tampered"})
        with pytest.raises(ValueError):
            validate_envelope(tampered)


# ---------------------------------------------------------------------------
# verify_signature tests
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_valid_signature(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        assert verify_signature(env, SECRET) is True

    def test_invalid_signature_raises(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        bad_sig = "0" * 64
        tampered = env.model_copy(update={"signature": bad_sig})
        with pytest.raises(ValueError, match="signature"):
            verify_signature(tampered, SECRET)

    def test_no_signature_returns_false(self) -> None:
        builder = FailureEnvelopeBuilder()  # no secret
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        assert verify_signature(env, SECRET) is False

    def test_wrong_secret_raises(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        with pytest.raises(ValueError):
            verify_signature(env, "wrong-secret")

    def test_empty_secret_raises(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        with pytest.raises(ValueError):
            verify_signature(env, "")


# ---------------------------------------------------------------------------
# is_retryable tests
# ---------------------------------------------------------------------------


class TestIsRetryable:
    @pytest.mark.parametrize(
        "code",
        [
            FailureCode.TRAINING_OOM,
            FailureCode.GPU_UNAVAILABLE,
            FailureCode.CALLBACK_ERROR,
        ],
    )
    def test_retryable_codes(self, code: FailureCode) -> None:
        assert is_retryable(code) is True

    @pytest.mark.parametrize(
        "code",
        [
            FailureCode.SECURITY_VIOLATION,
            FailureCode.DATASET_NOT_FOUND,
            FailureCode.QUALITY_GATE_FAILED,
        ],
    )
    def test_non_retryable_codes(self, code: FailureCode) -> None:
        assert is_retryable(code) is False

    def test_other_codes_default_non_retryable(self) -> None:
        assert is_retryable(FailureCode.UNKNOWN_ERROR) is False
        assert is_retryable(FailureCode.TRAINING_ERROR) is False
        assert is_retryable(FailureCode.ARTIFACT_WRITE_ERROR) is False


# ---------------------------------------------------------------------------
# serialize / deserialize tests
# ---------------------------------------------------------------------------


class TestSerializeDeserialize:
    def test_round_trip(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.ARTIFACT_WRITE_ERROR,
            failure_message="disk full",
            retryable=False,
            stage=FailureStage.ARTIFACT_WRITE,
            job_id="job-1",
            dataset_id="ds-1",
            git_sha="abc",
        )
        s = serialize_envelope(env)
        assert isinstance(s, str)
        env2 = deserialize_envelope(s)
        assert env2 == env

    def test_round_trip_unsigned(self) -> None:
        builder = FailureEnvelopeBuilder()
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        s = serialize_envelope(env)
        env2 = deserialize_envelope(s)
        assert env2 == env
        assert env2.signature is None

    def test_deserialize_validates_after_roundtrip(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.QUALITY_GATE_FAILED,
            failure_message="gate",
            retryable=False,
            stage=FailureStage.QUALITY_GATE,
            job_id="job-1",
        )
        s = serialize_envelope(env)
        env2 = deserialize_envelope(s)
        validate_envelope(env2)  # should not raise

    def test_deserialize_malformed_raises(self) -> None:
        with pytest.raises(Exception):
            deserialize_envelope("{not json}")

    def test_serialize_is_json(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        s = serialize_envelope(env)
        parsed = json.loads(s)
        assert parsed["failure_code"] == "unknown_error"


# ---------------------------------------------------------------------------
# distinguish_missing_callback_vs_signed_failure tests
# ---------------------------------------------------------------------------


class TestDistinguish:
    def test_none_is_missing_callback(self) -> None:
        assert (
            distinguish_missing_callback_vs_signed_failure(None, secret=SECRET)
            == "MISSING_CALLBACK"
        )

    def test_valid_signature_is_signed_failure(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.DATASET_NOT_FOUND,
            failure_message="missing",
            retryable=False,
            stage=FailureStage.DATASET_FETCH,
            job_id="job-1",
        )
        assert (
            distinguish_missing_callback_vs_signed_failure(env, secret=SECRET) == "SIGNED_FAILURE"
        )

    def test_invalid_signature_is_tampered(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.DATASET_NOT_FOUND,
            failure_message="missing",
            retryable=False,
            stage=FailureStage.DATASET_FETCH,
            job_id="job-1",
        )
        tampered = env.model_copy(update={"signature": "0" * 64})
        assert (
            distinguish_missing_callback_vs_signed_failure(tampered, secret=SECRET)
            == "TAMPERED_FAILURE"
        )

    def test_no_secret_is_tampered(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        assert (
            distinguish_missing_callback_vs_signed_failure(env, secret=None) == "TAMPERED_FAILURE"
        )

    def test_unsigned_envelope_is_tampered(self) -> None:
        builder = FailureEnvelopeBuilder()  # no secret
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        assert (
            distinguish_missing_callback_vs_signed_failure(env, secret=SECRET) == "TAMPERED_FAILURE"
        )


# ---------------------------------------------------------------------------
# Acceptance criteria: stage-specific signing
# ---------------------------------------------------------------------------


class TestAcceptanceStageSigning:
    def test_dataset_fetch_failure_signs(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.DATASET_NOT_FOUND,
            failure_message="dataset missing",
            retryable=False,
            stage=FailureStage.DATASET_FETCH,
            job_id="job-ds",
            dataset_id="ds-missing",
        )
        assert env.signature is not None
        assert verify_signature(env, SECRET) is True
        validate_envelope(env)
        assert env.stage is FailureStage.DATASET_FETCH

    def test_quality_gate_failure_signs(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.QUALITY_GATE_FAILED,
            failure_message="PBO too high",
            retryable=False,
            stage=FailureStage.QUALITY_GATE,
            job_id="job-qg",
            dataset_id="ds-1",
            model_family="gbm",
        )
        assert env.signature is not None
        assert verify_signature(env, SECRET) is True
        validate_envelope(env)
        assert env.stage is FailureStage.QUALITY_GATE

    def test_artifact_write_failure_signs(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.ARTIFACT_WRITE_ERROR,
            failure_message="disk full",
            retryable=False,
            stage=FailureStage.ARTIFACT_WRITE,
            job_id="job-aw",
            model_family="xgb",
        )
        assert env.signature is not None
        assert verify_signature(env, SECRET) is True
        validate_envelope(env)
        assert env.stage is FailureStage.ARTIFACT_WRITE

    def test_trusted_side_distinguishes_missing_vs_signed(self) -> None:
        """Core acceptance: trusted side can tell missing callback from signed failure."""
        builder = FailureEnvelopeBuilder(SECRET)
        signed_env = builder.build(
            failure_code=FailureCode.TRAINING_ERROR,
            failure_message="crashed",
            retryable=False,
            stage=FailureStage.TRAINING,
            job_id="job-1",
        )
        # Missing callback (no envelope).
        assert (
            distinguish_missing_callback_vs_signed_failure(None, secret=SECRET)
            == "MISSING_CALLBACK"
        )
        # Signed worker failure.
        assert (
            distinguish_missing_callback_vs_signed_failure(signed_env, secret=SECRET)
            == "SIGNED_FAILURE"
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_minimal_envelope(self) -> None:
        builder = FailureEnvelopeBuilder()
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="j",
        )
        validate_envelope(env)
        assert env.context.dataset_id is None
        assert env.context.model_family is None

    def test_all_fields_envelope(self) -> None:
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.SECURITY_VIOLATION,
            failure_message="forbidden env var",
            retryable=False,
            stage=FailureStage.SECURITY_PREFLIGHT,
            job_id="job-sec",
            dataset_id="ds-1",
            model_family="gbm",
            container_user="nonroot",
            git_sha="abc123def456",
            image_digest="sha256:abcdef",
        )
        validate_envelope(env)
        assert verify_signature(env, SECRET) is True
        assert env.context.container_user == "nonroot"

    def test_no_secret_envelope_validates(self) -> None:
        builder = FailureEnvelopeBuilder()
        env = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-1",
        )
        validate_envelope(env)  # hash validation works without signature
        assert env.signature is None

    def test_retryable_flag_independent_of_is_retryable(self) -> None:
        """The retryable flag on the envelope is caller-controlled; is_retryable
        is the policy function. They may differ (caller can override)."""
        builder = FailureEnvelopeBuilder(SECRET)
        env = builder.build(
            failure_code=FailureCode.TRAINING_OOM,
            failure_message="oom",
            retryable=True,
            stage=FailureStage.TRAINING,
            job_id="job-1",
        )
        assert env.retryable is True
        assert is_retryable(FailureCode.TRAINING_OOM) is True

    def test_two_envelopes_different_ids(self) -> None:
        """Two builds for different jobs produce different envelope_ids."""
        builder = FailureEnvelopeBuilder(SECRET)
        env1 = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-A",
        )
        env2 = builder.build(
            failure_code=FailureCode.UNKNOWN_ERROR,
            failure_message="x",
            retryable=False,
            stage=FailureStage.UNKNOWN,
            job_id="job-B",
        )
        assert env1.envelope_id != env2.envelope_id
