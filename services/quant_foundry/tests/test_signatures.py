"""
TDD skeleton tests for quant_foundry.signatures and ids.

These pin the expected helper functions for future idempotency + HMAC.
For skeleton they only require import + basic determinism.
"""

from __future__ import annotations

# Imports will fail until the modules are created.
from quant_foundry.ids import make_idempotency_key
from quant_foundry.signatures import sign_callback, verify_callback


def test_quant_foundry_ids_and_signatures_import() -> None:
    """Both ids and signatures submodules must import cleanly."""
    assert callable(make_idempotency_key)
    assert callable(sign_callback)
    assert callable(verify_callback)


def test_idempotency_key_is_deterministic() -> None:
    """Same inputs must produce identical idempotency key (critical for retry safety)."""
    key1 = make_idempotency_key("training", "ds-123", "v1", "cfg-abc", "1")
    key2 = make_idempotency_key("training", "ds-123", "v1", "cfg-abc", "1")
    assert key1 == key2
    assert isinstance(key1, str) and len(key1) > 0


def test_signature_roundtrip_placeholder() -> None:
    """Placeholder sign/verify must accept good payload and at least exist."""
    payload = b"test-payload"
    sig = sign_callback(payload, secret="dev-secret", ts=1234567890, job_id="job-1")
    assert verify_callback(payload, sig, secret="dev-secret", ts=1234567890, job_id="job-1") is True
    # Tamper should ideally fail later; for skeleton we just exercise the API.
    assert isinstance(sig, (str, bytes))
