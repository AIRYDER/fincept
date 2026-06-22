"""
quant_foundry — safe bridge to external quant ML workers (initially local mock, later RunPod).

This package owns:
- Strict cross-boundary contracts (schemas.py)
- Deterministic ID and idempotency key generation (ids.py)
- HMAC callback signing / verification (signatures.py)

Design invariants (enforced from skeleton onward):
- All external payloads use Pydantic `extra="forbid"`.
- Shadow predictions NEVER contain trading fields (quantity, side, broker, etc.).
- Idempotency + signatures guarantee at-least-once transport with exactly-once effects.
- No direct trading side effects; only signals and dossiers until promotion gates.

Public surface (stubs for TASK-0301; expanded in 0302+):
  - get_placeholder_schema
  - make_idempotency_key
  - sign_callback, verify_callback
"""

from __future__ import annotations

__version__ = "0.1.0"

# Stubs re-exported for convenience in tests and early consumers.
from quant_foundry.ids import make_idempotency_key
from quant_foundry.schemas import get_placeholder_schema
from quant_foundry.signatures import sign_callback, verify_callback

__all__ = [
    "get_placeholder_schema",
    "make_idempotency_key",
    "sign_callback",
    "verify_callback",
]
