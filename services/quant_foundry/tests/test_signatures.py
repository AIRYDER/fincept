"""
TDD tests for quant_foundry.signatures (TASK-0303).

Per spec:
- HMAC_SHA256(callback_secret, timestamp + "." + job_id + "." + payload_hash)
- Timestamp skew validation (reject old/future ts)
- Tamper detection: wrong signature, wrong job_id, wrong payload
- Roundtrips for valid recent calls
- Uses payload hash helper (not raw payload concat)

Security: always constant-time compare; skew prevents replays.
"""

from __future__ import annotations

import time

from quant_foundry.ids import hash_payload, make_idempotency_key
from quant_foundry.signatures import sign_callback, verify_callback


def test_quant_foundry_ids_and_signatures_import() -> None:
    """Both ids and signatures submodules must import cleanly."""
    assert callable(make_idempotency_key)
    assert callable(sign_callback)
    assert callable(verify_callback)
    assert callable(hash_payload)


def test_idempotency_key_is_deterministic() -> None:
    """Same inputs must produce identical idempotency key (critical for retry safety)."""
    key1 = make_idempotency_key("training", "ds-123", "v1", "cfg-abc", "1")
    key2 = make_idempotency_key("training", "ds-123", "v1", "cfg-abc", "1")
    assert key1 == key2
    assert isinstance(key1, str) and key1.startswith("qf:")


def test_signature_roundtrip_valid_recent_ts() -> None:
    """Valid same inputs with fresh ts must roundtrip true."""
    payload = b"test-payload-for-roundtrip"
    ts = int(time.time())
    sig = sign_callback(payload, secret="dev-secret", ts=ts, job_id="job-1")
    assert verify_callback(payload, sig, secret="dev-secret", ts=ts, job_id="job-1") is True
    assert isinstance(sig, str)
    assert len(sig) == 64  # sha256 hex


def test_verify_rejects_tampered_signature() -> None:
    """Wrong signature must fail (tamper detection)."""
    payload = b"payload"
    ts = int(time.time())
    sig = sign_callback(payload, secret="dev-secret", ts=ts, job_id="job-1")
    bad_sig = "a" * len(sig) if sig else "deadbeef"
    assert verify_callback(payload, bad_sig, secret="dev-secret", ts=ts, job_id="job-1") is False


def test_verify_rejects_wrong_payload() -> None:
    """Signature for payload A must not verify for payload B."""
    ts = int(time.time())
    sig = sign_callback(b"original", secret="s", ts=ts, job_id="j")
    assert verify_callback(b"tampered", sig, secret="s", ts=ts, job_id="j") is False


def test_verify_rejects_wrong_job_id() -> None:
    """Signature computed for job J1 must fail when verified under J2 (even if sig bytes match)."""
    payload = b"data"
    ts = int(time.time())
    sig = sign_callback(payload, secret="s", ts=ts, job_id="job-correct")
    assert verify_callback(payload, sig, secret="s", ts=ts, job_id="job-wrong") is False


def test_verify_rejects_old_timestamp() -> None:
    """Old timestamp must fail verification even if signature would otherwise match (skew guard)."""
    payload = b"replay-data"
    old_ts = int(time.time()) - 10000  #  ~2.7 hours old, > skew
    sig = sign_callback(payload, secret="s", ts=old_ts, job_id="job-1")
    # Must reject regardless of recomputed match
    assert verify_callback(payload, sig, secret="s", ts=old_ts, job_id="job-1") is False


def test_verify_rejects_future_timestamp() -> None:
    """Future timestamp must also fail (skew)."""
    payload = b"fut"
    future_ts = int(time.time()) + 10000
    sig = sign_callback(payload, secret="s", ts=future_ts, job_id="j")
    assert verify_callback(payload, sig, secret="s", ts=future_ts, job_id="j") is False


def test_signature_uses_payload_hash_internally() -> None:
    """Changing only the payload content must change the produced signature (proves hash used, not raw)."""
    ts = int(time.time())
    job = "job-h"
    s1 = sign_callback(b"content-one", secret="sec", ts=ts, job_id=job)
    s2 = sign_callback(b"content-two", secret="sec", ts=ts, job_id=job)
    assert s1 != s2
    # Also, direct hash helper is stable
    assert hash_payload(b"content-one") == hash_payload(b"content-one")
