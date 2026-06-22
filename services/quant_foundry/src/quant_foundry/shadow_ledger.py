"""
quant_foundry.shadow_ledger — Shadow Prediction Ledger Storage (TASK-0402).

Stores Quant Foundry shadow predictions **separately** from the existing
trading prediction streams. Shadow output must NEVER feed the orchestrator's
trading-prediction stream until a paper bridge is explicitly approved
(TASK-0704).

Design (NEXT_STEPS_PLAN TASK-0402 + cross-cutting rigor):
- Local storage first (JSONL, restart-durable). The ``qf.shadow.predictions``
  Redis stream is deferred to a later task per spec ("later, if adding").
- Idempotency by ``(prediction_id, batch_hash)``: a duplicate batch (same
  prediction_id + same batch_hash) is a no-op; a diff-hash (same prediction_id
  + different batch_hash) is a **security event** — rejected, mirroring
  TASK-0304's inbox invariant.
- Order-like fields are rejected (defense in depth on top of
  ``ShadowPrediction``'s ``extra="forbid"``): shadow predictions must never
  carry trading authority (quantity, side, broker, order_type).
- ``authority`` is always ``shadow-only`` (enforced at store time via the real
  ``ShadowPrediction`` schema).
- Read API by ``model_id`` / ``symbol`` / time window.
- Batch hashing reuses ``ids.hash_payload`` (deterministic SHA-256).
- **Structural no-trading-stream / no-bus guard**: this module contains no
  bus producer, no stream writer, no reference to the orchestrator's trading
  stream or to the bus library. This is defense-in-depth — shadow output
  stays local until the promotion gate wiring lands.

File-disjoint from all active builders (see BUILDER4.md):
- ``schemas.py`` NOT modified (``ShadowPrediction`` + ``Authority`` consumed
  read-only).
- ``callbacks.py`` (Builder 2) NOT modified — the ``ShadowLedgerStub`` in
  callbacks.py is an in-process stub; this real ledger matches its
  ``store``/``list`` surface so the mock dispatcher can swap stub → real ledger
  by injection. No import of callbacks.py here (keeps ownership clean).
- ``libs/fincept-bus/streams.py`` NOT modified (local storage MVP).
"""

from __future__ import annotations

import builtins
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from quant_foundry.ids import hash_payload
from quant_foundry.schemas import Authority, ShadowPrediction

# ---------------------------------------------------------------------------
# Order-like fields — shadow predictions must NEVER carry trading authority.
# These are rejected explicitly (defense in depth on top of ShadowPrediction's
# extra="forbid") so the rejection carries a clear security message.
# ---------------------------------------------------------------------------

ORDER_LIKE_FIELDS: frozenset[str] = frozenset(
    {
        "quantity",
        "size",
        "side",
        "broker",
        "order_type",
        "order_id",
        "client_order_id",
        "time_in_force",
        "leverage",
        "margin_type",
        "account_id",
    }
)


# ---------------------------------------------------------------------------
# Batch hashing
# ---------------------------------------------------------------------------


def compute_batch_hash(predictions: list[dict[str, Any]]) -> str:
    """Deterministic SHA-256 over the canonical JSON of a prediction batch.

    Reuses ``ids.hash_payload`` so batch hashing is consistent with the rest of
    the Quant Foundry idempotency infrastructure (outbox/inbox use the same
    hash). Order matters: the list is serialized as-is, so a different ordering
    produces a different hash (callers must sort if order-independence is
    desired).
    """
    payload = json.dumps(predictions, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hash_payload(payload)


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------


class ShadowLedgerRecord(BaseModel):
    """One stored shadow prediction, enriched with ledger metadata.

    Frozen + extra='forbid' (audit integrity). The ``authority`` field defaults
    to ``shadow-only`` and is always enforced at store time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    prediction_id: str
    model_id: str
    symbol: str
    ts_event: int
    horizon_ns: int
    direction: float
    magnitude: float | None = None
    confidence: float
    authority: Authority = Authority.SHADOW_ONLY
    expected_return: float | None = None
    p_up: float | None = None
    feature_availability: dict[str, bool] | None = None
    latency_ms: float | None = None
    regime: str | None = None
    model_version: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    # Ledger-managed:
    batch_hash: str
    stored_at_ns: int

    def to_json(self) -> str:
        """Serialize to a JSONL line (stable, sorted keys)."""
        return self.model_dump_json()


# ---------------------------------------------------------------------------
# Store receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreReceipt:
    """Result of a ``store_batch`` call."""

    stored: int
    duplicates: int
    batch_hash: str


# ---------------------------------------------------------------------------
# ShadowLedger
# ---------------------------------------------------------------------------


class ShadowLedger:
    """Durable, idempotent, shadow-only ledger for Quant Foundry predictions.

    Storage is a JSONL file at ``<base_dir>/shadow_predictions.jsonl``.
    Restart-safe: the ledger reloads prior records on construction. Idempotent
    by ``(prediction_id, batch_hash)``: a duplicate batch is a no-op; a
    diff-hash is a security event (rejected).

    This ledger has NO bus producer, NO trading-stream writer, NO bus
    reference. Shadow output stays local until TASK-0704.
    """

    def __init__(self, base_dir: pathlib.Path | str | None = None) -> None:
        self._base_dir = pathlib.Path(base_dir) if base_dir is not None else None
        self._records: list[ShadowLedgerRecord] = []
        # Index by prediction_id -> batch_hash for O(1) idempotency + diff-hash check.
        self._index: dict[str, str] = {}
        if self._base_dir is not None:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            self._reload()

    # --- persistence -----------------------------------------------------

    @property
    def _path(self) -> pathlib.Path:
        if self._base_dir is None:
            raise RuntimeError("ShadowLedger has no base_dir; persistence unavailable")
        return self._base_dir / "shadow_predictions.jsonl"

    def _reload(self) -> None:
        """Replay the JSONL log on restart (last record per prediction_id wins)."""
        path = self._path
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = ShadowLedgerRecord.model_validate_json(line)
                self._records.append(record)
                self._index[record.prediction_id] = record.batch_hash

    def _append(self, record: ShadowLedgerRecord) -> None:
        """Append a record to the in-memory list + JSONL file (fsync for durability)."""
        self._records.append(record)
        self._index[record.prediction_id] = record.batch_hash
        if self._base_dir is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(record.to_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    # --- validation ------------------------------------------------------

    def _validate_prediction(self, p: dict[str, Any]) -> ShadowPrediction:
        """Validate a prediction dict against the real schema + reject order-like fields.

        ``ShadowPrediction`` already has ``extra="forbid"``, so order-like fields
        would be rejected by Pydantic. We add an explicit check first so the
        rejection carries a clear security message (not a generic
        "extra fields not permitted").
        """
        order_fields_present = ORDER_LIKE_FIELDS & set(p.keys())
        if order_fields_present:
            raise ValueError(
                "order-like fields in shadow prediction are forbidden (shadow "
                f"predictions must never carry trading authority): "
                f"{sorted(order_fields_present)}"
            )
        sp = ShadowPrediction.model_validate(p)
        if sp.authority != Authority.SHADOW_ONLY:
            raise ValueError(
                f"non-shadow authority in shadow ledger: {sp.authority} "
                "(security invariant violation — only shadow-only allowed)"
            )
        return sp

    # --- public API ------------------------------------------------------

    def store_batch(
        self,
        predictions: list[dict[str, Any]],
        batch_hash: str,
        *,
        stored_at_ns: int | None = None,
    ) -> StoreReceipt:
        """Store a batch of shadow predictions idempotently.

        - Validates each prediction against ``ShadowPrediction`` (extra='forbid')
          and rejects order-like fields + non-shadow authority.
        - Verifies the caller-supplied ``batch_hash`` matches the computed hash
          (tamper check).
        - Idempotent: a prediction_id already stored with the same batch_hash is
          a duplicate (skipped). A prediction_id already stored with a DIFFERENT
          batch_hash is a security event (rejected).
        """
        # Tamper check: caller-supplied hash must match the computed content hash.
        computed = compute_batch_hash(predictions)
        if computed != batch_hash:
            raise ValueError(
                f"batch hash mismatch: caller supplied {batch_hash[:12]}... "
                f"but computed {computed[:12]}... (tamper / serialization mismatch)"
            )

        ts = stored_at_ns if stored_at_ns is not None else _now_ns()
        stored = 0
        duplicates = 0
        for p in predictions:
            sp = self._validate_prediction(p)
            pid = sp.prediction_id
            existing_hash = self._index.get(pid)
            if existing_hash is not None:
                if existing_hash == batch_hash:
                    duplicates += 1
                    continue
                # Same prediction_id + DIFFERENT batch_hash → security event.
                raise ValueError(
                    f"security: prediction_id {pid!r} already stored with a "
                    f"different batch_hash (existing={existing_hash[:12]}..., "
                    f"new={batch_hash[:12]}...) — tamper / replay attempt rejected"
                )
            record = ShadowLedgerRecord(
                prediction_id=sp.prediction_id,
                model_id=sp.model_id,
                symbol=sp.symbol,
                ts_event=sp.ts_event,
                horizon_ns=sp.horizon_ns,
                direction=sp.direction,
                magnitude=sp.magnitude,
                confidence=sp.confidence,
                authority=sp.authority,
                expected_return=sp.expected_return,
                p_up=sp.p_up,
                feature_availability=sp.feature_availability,
                latency_ms=sp.latency_ms,
                regime=sp.regime,
                model_version=sp.model_version,
                metadata=dict(sp.metadata),
                batch_hash=batch_hash,
                stored_at_ns=ts,
            )
            self._append(record)
            stored += 1
        return StoreReceipt(stored=stored, duplicates=duplicates, batch_hash=batch_hash)

    def list(self) -> builtins.list[ShadowLedgerRecord]:
        """Return all stored shadow prediction records."""
        return list(self._records)

    def read_by_model(self, model_id: str) -> builtins.list[ShadowLedgerRecord]:
        """Return all shadow predictions for a given model_id."""
        return [r for r in self._records if r.model_id == model_id]

    def read_by_symbol(self, symbol: str) -> builtins.list[ShadowLedgerRecord]:
        """Return all shadow predictions for a given symbol."""
        return [r for r in self._records if r.symbol == symbol]

    def read_by_window(self, start_ns: int, end_ns: int) -> builtins.list[ShadowLedgerRecord]:
        """Return shadow predictions whose ts_event falls within [start_ns, end_ns]."""
        return [r for r in self._records if start_ns <= r.ts_event <= end_ns]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ns() -> int:
    """Current monotonic-ish time in nanoseconds (for stored_at_ns stamps)."""
    import time

    return time.time_ns()
