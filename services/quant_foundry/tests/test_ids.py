"""
TDD tests for quant_foundry.ids (TASK-0303).

Full spec per NEXT_STEPS_PLAN:
- Idempotency key format: qf:<job_type>:<dataset_id>:<model_family>:<config_hash>:<attempt_group>
- Helper to hash request payloads (for signatures + dedup)
- Duplicate idempotency keys must be deterministic (stable for retries)

All tests are strict on format and determinism. No side effects.
"""

from __future__ import annotations

from quant_foundry.ids import hash_payload, make_idempotency_key


def test_make_idempotency_key_produces_exact_qf_format() -> None:
    """Primary format contract. Used in QuantFoundryJob.idempotency_key and outbox."""
    key = make_idempotency_key("training", "ds-123", "gbm", "cfg-abc", "1")
    assert key == "qf:training:ds-123:gbm:cfg-abc:1"
    assert key.startswith("qf:")
    parts = key.split(":")
    assert len(parts) == 6
    assert parts[0] == "qf"
    assert parts[1] == "training"
    assert parts[2] == "ds-123"
    assert parts[3] == "gbm"
    assert parts[4] == "cfg-abc"
    assert parts[5] == "1"


def test_make_idempotency_key_is_deterministic_for_duplicates() -> None:
    """Same logical parts must always yield identical key (retry safety)."""
    key1 = make_idempotency_key("inference", "ds-xyz", "rf", "h456def", "2")
    key2 = make_idempotency_key("inference", "ds-xyz", "rf", "h456def", "2")
    assert key1 == key2
    assert isinstance(key1, str)
    assert key1.startswith("qf:")


def test_make_idempotency_key_differs_for_diff_parts() -> None:
    """Different inputs produce different keys."""
    k1 = make_idempotency_key("training", "ds-1", "m", "c1", "1")
    k2 = make_idempotency_key("training", "ds-1", "m", "c2", "1")
    assert k1 != k2


def test_hash_payload_is_deterministic() -> None:
    """Payload hash helper must be stable for identical bytes (used in HMAC)."""
    p = b'{"result": {"sharpe": 1.23}, "model": "gbm"}'
    h1 = hash_payload(p)
    h2 = hash_payload(p)
    assert h1 == h2
    assert isinstance(h1, str)
    assert len(h1) == 64  # sha256 hex digest


def test_hash_payload_diff_content_produces_diff_hash() -> None:
    """Different payloads must hash differently."""
    h1 = hash_payload(b"payload-v1")
    h2 = hash_payload(b"payload-v2")
    assert h1 != h2
    # Full hex, lowercase hex chars
    assert all(c in "0123456789abcdef" for c in h1)


def test_hash_payload_sha256_length_and_format() -> None:
    """Enforce full SHA256 (not truncated like blake id keys)."""
    h = hash_payload(b"short")
    assert len(h) == 64
    assert h != make_idempotency_key("a", "b", "c", "d", "e")  # different algo/len intent
