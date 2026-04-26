from __future__ import annotations

import hashlib

from ulid import ULID


def new_id() -> str:
    return str(ULID())


def idempotency_key(*parts: str) -> str:
    digest = hashlib.blake2b(digest_size=16)
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()
