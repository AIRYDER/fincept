"""
quant_foundry.shadow_settlement — store and settle shadow predictions (TASK-0603).

Connects RunPod shadow predictions to the settlement ledger. This module is
the **orchestration layer** that:

1. **Stores** signed prediction batches via ``ShadowLedger.store_batch()``
   (Builder 1's ``shadow_ledger.py`` — read-only call).
2. **Rejects** bad signatures and schemas (invalid callbacks are stored as
   rejected, not silently discarded).
3. **Settles** predictions via ``SettlementLedger.settle()`` (Builder 1's
   ``settlement.py`` — read-only call) after the horizon expires.
4. **Emits** a receipt for settled batches with settlement lag, settled count,
   and pending count.

Key invariants:
- **No prediction reaches ``sig.predict``.** All predictions have
  ``authority: shadow_only`` (enforced by ``ShadowPrediction`` schema).
- **Settlement lag is visible.** The receipt includes ``settlement_lag_ns``.
- **Invalid callbacks are stored as rejected.** Bad signatures, bad schemas,
  and bad hashes are recorded with a reason, not silently discarded.

File-disjoint from Builder 1's ``shadow_ledger.py`` + ``settlement.py``
(read-only imports). Does NOT modify them.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.metrics import PriceTick
from quant_foundry.outcomes import CostModel, SettlementRecord
from quant_foundry.schemas import ShadowPrediction
from quant_foundry.settlement import SettlementLedger
from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


class CallbackRejectionReason(StrEnum):
    """Reason why a callback was rejected."""

    BAD_SIGNATURE = "bad_signature"
    BAD_SCHEMA = "bad_schema"
    BAD_HASH = "bad_hash"


class RejectedCallback(BaseModel):
    """A rejected callback (bad signature, schema, or hash).

    Frozen + extra='forbid'. Stored for audit — invalid callbacks are not
    silently discarded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: CallbackRejectionReason
    message: str
    raw_payload: dict[str, Any]
    rejected_at_ns: int


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class SettlementReceipt(BaseModel):
    """Receipt for a settled batch of shadow predictions.

    Frozen + extra='forbid'. Carries the settled records, rejected callbacks,
    settlement lag, settled count, and pending count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: list[SettlementRecord] = []
    rejected: list[RejectedCallback] = []
    stored: int = 0
    duplicates: int = 0
    settled_count: int = 0
    pending_count: int = 0
    settlement_lag_ns: int = 0
    batch_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "records": [
                {
                    "prediction_id": r.prediction_id,
                    "model_id": r.model_id,
                    "symbol": r.symbol,
                    "ts_event": r.ts_event,
                    "horizon_ns": r.horizon_ns,
                    "status": r.status.value,
                    "settled_at_ns": r.settled_at_ns,
                    "realized_return_gross": r.realized_return_gross,
                    "realized_return_net": r.realized_return_net,
                    "abnormal_return": r.abnormal_return,
                    "brier": r.brier,
                    "calibration_bucket": r.calibration_bucket,
                    "cost_model_version": r.cost_model_version,
                }
                for r in self.records
            ],
            "rejected": [r.model_dump() for r in self.rejected],
            "stored": self.stored,
            "duplicates": self.duplicates,
            "settled_count": self.settled_count,
            "pending_count": self.pending_count,
            "settlement_lag_ns": self.settlement_lag_ns,
            "batch_hash": self.batch_hash,
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ===========================================================================


class ShadowSettlementOrchestrator:
    """Orchestrates storing and settling shadow prediction batches.

    Wraps Builder 1's ``ShadowLedger`` (store) and ``SettlementLedger``
    (settle) with signature verification and rejection tracking. Does NOT
    modify those classes — uses them read-only.
    """

    def __init__(
        self,
        shadow_ledger: ShadowLedger,
        settlement_ledger: SettlementLedger | None = None,
        secret: bytes = b"",
    ) -> None:
        self.shadow_ledger = shadow_ledger
        self.settlement_ledger = settlement_ledger
        self.secret = secret

    def _verify_signature(self, batch_hash: str, signature: str) -> bool:
        """Verify the HMAC signature of the batch hash."""
        expected = hmac.new(self.secret, batch_hash.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def store_batch(
        self,
        predictions: list[dict[str, Any]],
        batch_hash: str,
        signature: str,
        stored_at_ns: int | None = None,
    ) -> SettlementReceipt:
        """Store a signed prediction batch.

        Verifies the signature, validates the schema, and stores the batch
        via ``ShadowLedger.store_batch()``. Invalid callbacks are recorded
        as rejected, not silently discarded.
        """
        now_ns = stored_at_ns or time.time_ns()

        # Verify signature.
        if not self._verify_signature(batch_hash, signature):
            return SettlementReceipt(
                rejected=[
                    RejectedCallback(
                        reason=CallbackRejectionReason.BAD_SIGNATURE,
                        message="HMAC signature mismatch",
                        raw_payload={"batch_hash": batch_hash, "n_predictions": len(predictions)},
                        rejected_at_ns=now_ns,
                    )
                ],
                batch_hash=batch_hash,
            )

        # Verify hash (tamper check).
        computed = compute_batch_hash(predictions)
        if computed != batch_hash:
            return SettlementReceipt(
                rejected=[
                    RejectedCallback(
                        reason=CallbackRejectionReason.BAD_HASH,
                        message="batch hash mismatch (tamper / serialization mismatch)",
                        raw_payload={"expected": batch_hash, "computed": computed},
                        rejected_at_ns=now_ns,
                    )
                ],
                batch_hash=batch_hash,
            )

        # Validate schema (each prediction must be a valid ShadowPrediction).
        for p in predictions:
            try:
                ShadowPrediction(**p)
            except Exception as e:
                return SettlementReceipt(
                    rejected=[
                        RejectedCallback(
                            reason=CallbackRejectionReason.BAD_SCHEMA,
                            message=f"schema validation failed: {e}",
                            raw_payload=p,
                            rejected_at_ns=now_ns,
                        )
                    ],
                    batch_hash=batch_hash,
                )

        # Store the batch.
        store_receipt = self.shadow_ledger.store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
            stored_at_ns=now_ns,
        )

        return SettlementReceipt(
            stored=store_receipt.stored,
            duplicates=store_receipt.duplicates,
            batch_hash=batch_hash,
        )

    def settle_prediction(
        self,
        prediction: dict[str, Any],
        prices: list[PriceTick],
        cost_model: CostModel,
        now_ns: int,
        benchmark_prices: list[PriceTick] | None = None,
    ) -> SettlementRecord:
        """Settle a single prediction via the settlement ledger.

        Delegates to ``SettlementLedger.settle()`` (Builder 1's code).
        If no settlement ledger is configured, raises ``RuntimeError``.
        """
        if self.settlement_ledger is None:
            raise RuntimeError("no settlement ledger configured")
        return self.settlement_ledger.settle(
            prediction=prediction,
            prices=prices,
            benchmark_prices=benchmark_prices,
            cost_model=cost_model,
            now_ns=now_ns,
        )

    def settle_batch(
        self,
        predictions: list[dict[str, Any]],
        prices_by_symbol: dict[str, list[PriceTick]],
        cost_model: CostModel,
        now_ns: int,
        benchmark_prices_by_symbol: dict[str, list[PriceTick]] | None = None,
    ) -> SettlementReceipt:
        """Settle a batch of predictions after their horizons expire.

        For each prediction, delegates to ``SettlementLedger.settle()``.
        Records the settlement lag, settled count, and pending count.
        """
        if self.settlement_ledger is None:
            raise RuntimeError("no settlement ledger configured")

        records: list[SettlementRecord] = []
        settled_count = 0
        pending_count = 0
        min_settled_at_ns = now_ns
        max_ts_event = 0

        for pred in predictions:
            symbol = pred.get("symbol", "")
            prices = prices_by_symbol.get(symbol, [])
            benchmark = None
            if benchmark_prices_by_symbol is not None:
                benchmark = benchmark_prices_by_symbol.get(symbol)

            record = self.settlement_ledger.settle(
                prediction=pred,
                prices=prices,
                benchmark_prices=benchmark,
                cost_model=cost_model,
                now_ns=now_ns,
            )
            records.append(record)

            if record.status.value == "settled":
                settled_count += 1
                if record.settled_at_ns is not None:
                    min_settled_at_ns = min(min_settled_at_ns, record.settled_at_ns)
            else:
                pending_count += 1

            max_ts_event = max(max_ts_event, pred.get("ts_event", 0))

        # Settlement lag = time between the latest decision time and now.
        settlement_lag_ns = max(0, now_ns - max_ts_event) if max_ts_event > 0 else 0

        return SettlementReceipt(
            records=records,
            settled_count=settled_count,
            pending_count=pending_count,
            settlement_lag_ns=settlement_lag_ns,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def store_and_settle_batch(
    predictions: list[dict[str, Any]],
    batch_hash: str,
    signature: str,
    prices_by_symbol: dict[str, list[PriceTick]],
    cost_model: CostModel,
    now_ns: int,
    secret: bytes,
    shadow_ledger_dir: str,
    settlement_ledger_dir: str,
    benchmark_prices_by_symbol: dict[str, list[PriceTick]] | None = None,
) -> SettlementReceipt:
    """Store and settle a batch of shadow predictions end-to-end.

    Convenience entry point for TASK-0603. Creates a
    ``ShadowSettlementOrchestrator`` with the given ledgers, stores the
    batch, and settles it.
    """
    import pathlib

    orchestrator = ShadowSettlementOrchestrator(
        shadow_ledger=ShadowLedger(base_dir=shadow_ledger_dir),
        settlement_ledger=SettlementLedger(root=pathlib.Path(settlement_ledger_dir)),
        secret=secret,
    )

    # Store the batch.
    store_receipt = orchestrator.store_batch(
        predictions=predictions,
        batch_hash=batch_hash,
        signature=signature,
    )

    # If storage failed (rejected), return the store receipt.
    if store_receipt.stored == 0 and len(store_receipt.rejected) > 0:
        return store_receipt

    # Settle the batch.
    settle_receipt = orchestrator.settle_batch(
        predictions=predictions,
        prices_by_symbol=prices_by_symbol,
        cost_model=cost_model,
        now_ns=now_ns,
        benchmark_prices_by_symbol=benchmark_prices_by_symbol,
    )

    # Merge the store + settle receipts.
    return SettlementReceipt(
        records=settle_receipt.records,
        rejected=store_receipt.rejected,
        stored=store_receipt.stored,
        duplicates=store_receipt.duplicates,
        settled_count=settle_receipt.settled_count,
        pending_count=settle_receipt.pending_count,
        settlement_lag_ns=settle_receipt.settlement_lag_ns,
        batch_hash=store_receipt.batch_hash,
    )
