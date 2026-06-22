"""
quant_foundry.ids — ID and idempotency key generation.

Idempotency key format (enforced):
  qf:<job_type>:<dataset_id>:<model_family>:<config_hash>:<attempt_group>

A payload hash helper is also provided for use in HMAC signatures and dedup.

Design:
- make_idempotency_key joins to the exact qf: string (parts are pre-hashed where needed, e.g. config_hash).
- hash_payload uses full SHA256 (stable, collision resistant) for request/callback payloads.
- Deterministic by construction for at-least-once + exactly-once semantics.
"""

from __future__ import annotations

import hashlib


def make_idempotency_key(*parts: str) -> str:
    """Build the canonical qf: idempotency key from ordered parts.

    Example:
        make_idempotency_key("training", "ds-123", "gbm", "abc123cfg", "1")
        -> "qf:training:ds-123:gbm:abc123cfg:1"

    Must be 100% stable for identical inputs (critical for outbox/inbox dedup and retries).
    The 4th part (config_hash) is expected to be a content hash of the request config.
    """
    if not parts:
        # Fail fast on misuse (no silent bad keys)
        raise ValueError("make_idempotency_key requires at least one part")
    # Structured qf: format as specified (not opaque hash). Callers supply hashed sub-parts.
    return "qf:" + ":".join(parts)


def hash_payload(payload: bytes) -> str:
    """Compute a stable hex SHA256 hash of the (serialized) payload bytes.

    Used by signatures: HMAC incorporates timestamp + job_id + payload_hash (not raw bytes).
    Also suitable for request_payload_hash in outbox/inbox.

    Always full 64-char hex (sha256); different from short blake keys used for IDs.
    """
    if not isinstance(payload, (bytes, bytearray)):
        # Enforce type at boundary (no silent str encode surprises in security code)
        raise TypeError(f"hash_payload expects bytes, got {type(payload)}")
    return hashlib.sha256(payload).hexdigest()
