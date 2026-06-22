"""
quant_foundry.ids — ID and idempotency key generation.

Idempotency key format (enforced in later tasks):
  qf:<job_type>:<dataset_id>:<model_family>:<config_hash>:<attempt_group>

For skeleton we provide a deterministic helper that later will match the spec exactly.
"""

from __future__ import annotations

import hashlib


def make_idempotency_key(*parts: str) -> str:
    """Stable, deterministic key from ordered parts.

    Uses blake2b (fast, good distribution) truncated for readability.
    Must be identical for identical inputs — required for dedup in outbox/inbox.
    """
    digest = hashlib.blake2b(digest_size=16)
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()
