"""
quant_foundry.signatures — HMAC callback signing and verification.

Spec:
  HMAC_SHA256(callback_secret, timestamp + "." + job_id + "." + payload_hash)

Security invariants (non-negotiable):
- Always use hmac.compare_digest for verification (constant time).
- Timestamp skew validation rejects old or future signatures (replay protection).
- Payload is always hashed before inclusion in the MAC (never raw bytes).
- job_id binding prevents cross-job replay.
- No secrets or keys ever logged or returned in error paths.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from quant_foundry.ids import hash_payload

# 5 minute skew window. Callers should use wall-clock seconds since epoch for ts.
# Adjust only with coordinated config change; too large weakens replay protection.
MAX_TS_SKEW_SECONDS: int = 300


def sign_callback(payload: bytes, *, secret: str, ts: int, job_id: str) -> str:
    """Return hex HMAC-SHA256 signature over (ts + "." + job_id + "." + payload_hash).

    The payload is hashed (via ids.hash_payload) before MAC to keep sig size constant
    and avoid including large bodies directly.
    ts must be int seconds (unix). No validation here — caller + verify enforce recency.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes for signing")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must be non-empty str")
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be non-empty str")
    p_hash = hash_payload(payload)
    msg = f"{ts}.{job_id}.{p_hash}".encode()
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return sig


def verify_callback(payload: bytes, signature: str, *, secret: str, ts: int, job_id: str) -> bool:
    """Verify HMAC signature. Returns False on any failure (mismatch, skew, bad inputs).

    Skew check uses local wall time at verification time.
    This makes old signed callbacks (replays) fail closed even if the MAC would match.
    Wrong job_id or tampered payload will fail MAC recompute (binding).
    """
    # Basic input hygiene (fail closed, no crash on bad data from wire)
    if not isinstance(payload, (bytes, bytearray)):
        return False
    if not isinstance(signature, str) or not signature:
        return False
    if not isinstance(secret, str) or not secret:
        return False
    if not isinstance(job_id, str) or not job_id:
        return False
    if not isinstance(ts, int):
        return False

    now = int(time.time())
    if abs(now - ts) > MAX_TS_SKEW_SECONDS:
        # Explicit skew rejection (old or clock-skewed/future)
        return False

    # Recompute using same rules (hash + format) then constant-time compare.
    expected = sign_callback(payload, secret=secret, ts=ts, job_id=job_id)
    return hmac.compare_digest(expected, signature)
