"""
quant_foundry.signatures — HMAC callback signing (stub).

Spec (full in TASK-0303):
  HMAC_SHA256(callback_secret, timestamp + "." + job_id + "." + payload_hash)

For skeleton: simple HMAC + verify that works for the TDD tests.
Security: always constant-time compare in real impl; include ts skew checks.
"""

from __future__ import annotations

import hashlib
import hmac


def sign_callback(payload: bytes, *, secret: str, ts: int, job_id: str) -> str:
    """Return hex signature for the given payload + metadata.

    Placeholder only — real version will include strict timestamp validation.
    """
    msg = f"{ts}.{job_id}.".encode() + payload
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return sig


def verify_callback(payload: bytes, signature: str, *, secret: str, ts: int, job_id: str) -> bool:
    """Verify signature. Returns False on mismatch.

    In production this must reject old timestamps and wrong job_id.
    """
    expected = sign_callback(payload, secret=secret, ts=ts, job_id=job_id)
    # Use compare_digest for timing safety even in stub.
    return hmac.compare_digest(expected, signature)
