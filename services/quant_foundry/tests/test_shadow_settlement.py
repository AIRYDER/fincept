"""
Tests for TASK-0603: Store and Settle Shadow Predictions.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `shadow_settlement.py` exists.

Acceptance criteria covered:
- Shadow predictions settle into outcomes.
- Settlement lag is visible.
- No prediction reaches `sig.predict`.
- Invalid callback is stored as rejected, not silently discarded.

Additional checks from the spec:
- Store signed prediction batches.
- Reject bad signatures and schemas.
- Mark predictions pending by horizon.
- Settle after horizon expires.
- Update model live calibration metrics.
- Emit receipt for settled batches.

File-disjoint from Builder 1's `shadow_ledger.py` + `settlement.py`
(read-only imports). Does NOT modify them.
"""

from __future__ import annotations

import hashlib
import hmac
import pathlib
import tempfile
from typing import Any

import pytest
from quant_foundry.metrics import PriceTick
from quant_foundry.outcomes import CostModel, SettlementRecord, SettlementStatus
from quant_foundry.schemas import Authority
from quant_foundry.settlement import SettlementLedger
from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash
from quant_foundry.shadow_settlement import (
    CallbackRejectionReason,
    RejectedCallback,
    SettlementReceipt,
    ShadowSettlementOrchestrator,
    store_and_settle_batch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SECRET = b"test-shared-secret"


def _make_prediction(
    prediction_id: str = "pred-1",
    model_id: str = "m1",
    symbol: str = "AAPL",
    ts_event: int = 1000,
    horizon_ns: int = 3600_000_000_000,
    direction: float = 0.5,
    confidence: float = 0.7,
) -> dict[str, Any]:
    """Build a minimal shadow prediction dict for testing."""
    return {
        "prediction_id": prediction_id,
        "model_id": model_id,
        "symbol": symbol,
        "ts_event": ts_event,
        "horizon_ns": horizon_ns,
        "direction": direction,
        "confidence": confidence,
        "authority": "shadow-only",
        "p_up": 0.7,
    }


def _make_signed_batch(
    predictions: list[dict[str, Any]] | None = None,
    secret: bytes = _SECRET,
) -> tuple[list[dict[str, Any]], str, str]:
    """Build a signed prediction batch (predictions, batch_hash, signature)."""
    if predictions is None:
        predictions = [_make_prediction()]
    batch_hash = compute_batch_hash(predictions)
    signature = hmac.new(secret, batch_hash.encode(), hashlib.sha256).hexdigest()
    return predictions, batch_hash, signature


def _make_cost_model() -> CostModel:
    return CostModel(
        version="v1",
        fee_bps=1.0,
        spread_bps=2.0,
        slippage_bps=1.0,
        borrow_bps_per_day=0.5,
    )


def _make_prices(symbol: str = "AAPL", ts_event: int = 1000, horizon_ns: int = 3600_000_000_000) -> list[PriceTick]:
    """Build entry + exit prices for a prediction."""
    return [
        PriceTick(ts=ts_event, price=100.0),
        PriceTick(ts=ts_event + horizon_ns + 1, price=101.0),
    ]


# ---------------------------------------------------------------------------
# CallbackRejectionReason + RejectedCallback
# ===========================================================================


class TestRejectedCallback:
    """Invalid callback is stored as rejected, not silently discarded."""

    def test_rejection_reasons_are_defined(self) -> None:
        """CallbackRejectionReason has the expected values."""
        assert CallbackRejectionReason.BAD_SIGNATURE is not None
        assert CallbackRejectionReason.BAD_SCHEMA is not None
        assert CallbackRejectionReason.BAD_HASH is not None

    def test_rejected_callback_has_required_fields(self) -> None:
        """RejectedCallback has reason, message, raw_payload, rejected_at_ns."""
        rejected = RejectedCallback(
            reason=CallbackRejectionReason.BAD_SIGNATURE,
            message="signature mismatch",
            raw_payload={"foo": "bar"},
            rejected_at_ns=1000,
        )
        assert rejected.reason == CallbackRejectionReason.BAD_SIGNATURE
        assert rejected.message == "signature mismatch"
        assert rejected.raw_payload == {"foo": "bar"}
        assert rejected.rejected_at_ns == 1000

    def test_rejected_callback_is_frozen(self) -> None:
        """RejectedCallback is frozen (immutable for audit)."""
        rejected = RejectedCallback(
            reason=CallbackRejectionReason.BAD_SIGNATURE,
            message="test",
            raw_payload={},
            rejected_at_ns=1000,
        )
        with pytest.raises((TypeError, ValueError)):
            rejected.reason = CallbackRejectionReason.BAD_SCHEMA  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Store signed prediction batches
# ===========================================================================


class TestStoreSignedBatch:
    """Store signed prediction batches."""

    def test_store_valid_batch_succeeds(self) -> None:
        """A valid signed batch is stored successfully."""
        predictions, batch_hash, signature = _make_signed_batch()
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            secret=_SECRET,
        )
        receipt = orchestrator.store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
            signature=signature,
        )
        assert receipt.stored > 0

    def test_store_batch_rejects_bad_signature(self) -> None:
        """A batch with a bad signature is rejected."""
        predictions, batch_hash, _ = _make_signed_batch()
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            secret=_SECRET,
        )
        receipt = orchestrator.store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
            signature="bad-signature",
        )
        assert receipt.stored == 0
        assert len(receipt.rejected) > 0
        assert receipt.rejected[0].reason == CallbackRejectionReason.BAD_SIGNATURE

    def test_store_batch_rejects_bad_hash(self) -> None:
        """A batch with a bad hash (tamper) is rejected."""
        predictions, _, signature = _make_signed_batch()
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            secret=_SECRET,
        )
        receipt = orchestrator.store_batch(
            predictions=predictions,
            batch_hash="bad-hash",
            signature=signature,
        )
        # The signature won't match because the hash is wrong.
        assert receipt.stored == 0
        assert len(receipt.rejected) > 0

    def test_store_batch_rejects_bad_schema(self) -> None:
        """A batch with a bad schema (missing required fields) is rejected."""
        bad_predictions = [{"prediction_id": "p1"}]  # missing required fields
        batch_hash = compute_batch_hash(bad_predictions)
        signature = hmac.new(_SECRET, batch_hash.encode(), hashlib.sha256).hexdigest()
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            secret=_SECRET,
        )
        receipt = orchestrator.store_batch(
            predictions=bad_predictions,
            batch_hash=batch_hash,
            signature=signature,
        )
        assert receipt.stored == 0
        assert len(receipt.rejected) > 0
        assert receipt.rejected[0].reason == CallbackRejectionReason.BAD_SCHEMA


# ---------------------------------------------------------------------------
# Mark predictions pending by horizon
# ===========================================================================


class TestPendingByHorizon:
    """Mark predictions pending by horizon."""

    def test_prediction_is_pending_before_horizon_expires(self) -> None:
        """A prediction is pending_time before the horizon expires."""
        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        # Settle before horizon expires.
        record = orchestrator.settle_prediction(
            prediction=predictions[0],
            prices=_make_prices(),
            cost_model=_make_cost_model(),
            now_ns=1000 + 100,  # before horizon
        )
        assert record.status == SettlementStatus.PENDING_TIME

    def test_prediction_settles_after_horizon_expires(self) -> None:
        """A prediction settles after the horizon expires."""
        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        # Settle after horizon expires.
        record = orchestrator.settle_prediction(
            prediction=predictions[0],
            prices=_make_prices(),
            cost_model=_make_cost_model(),
            now_ns=1000 + 3600_000_000_000 + 1,  # after horizon
        )
        assert record.status == SettlementStatus.SETTLED


# ---------------------------------------------------------------------------
# Settlement lag is visible
# ===========================================================================


class TestSettlementLag:
    """Settlement lag is visible."""

    def test_settlement_receipt_includes_lag(self) -> None:
        """The settlement receipt includes the settlement lag."""
        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        now_ns = 1000 + 3600_000_000_000 + 100
        receipt = orchestrator.settle_batch(
            predictions=predictions,
            prices_by_symbol={"AAPL": _make_prices()},
            cost_model=_make_cost_model(),
            now_ns=now_ns,
        )
        assert hasattr(receipt, "settlement_lag_ns")
        assert receipt.settlement_lag_ns >= 0

    def test_settlement_receipt_includes_settled_count(self) -> None:
        """The settlement receipt includes the count of settled predictions."""
        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        now_ns = 1000 + 3600_000_000_000 + 100
        receipt = orchestrator.settle_batch(
            predictions=predictions,
            prices_by_symbol={"AAPL": _make_prices()},
            cost_model=_make_cost_model(),
            now_ns=now_ns,
        )
        assert receipt.settled_count >= 1
        assert receipt.pending_count == 0


# ---------------------------------------------------------------------------
# No prediction reaches sig.predict
# ===========================================================================


class TestNoTradingAuthority:
    """No prediction reaches `sig.predict`."""

    def test_all_predictions_have_shadow_only_authority(self) -> None:
        """All stored predictions have authority=shadow_only."""
        predictions, batch_hash, signature = _make_signed_batch()
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        records = orchestrator.shadow_ledger.list()
        for record in records:
            assert record.authority == Authority.SHADOW_ONLY

    def test_receipt_to_dict_has_no_order_fields(self) -> None:
        """The settlement receipt dict has no order/trading fields."""
        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        now_ns = 1000 + 3600_000_000_000 + 100
        receipt = orchestrator.settle_batch(
            predictions=predictions,
            prices_by_symbol={"AAPL": _make_prices()},
            cost_model=_make_cost_model(),
            now_ns=now_ns,
        )
        d = receipt.to_dict()
        order_keys = {"order", "signal", "trade", "position", "allocation",
                      "quantity", "price", "side", "sig_predict"}
        assert not any(k in d for k in order_keys)


# ---------------------------------------------------------------------------
# Emit receipt for settled batches
# ===========================================================================


class TestSettlementReceipt:
    """Emit receipt for settled batches."""

    def test_receipt_has_settled_records(self) -> None:
        """The receipt includes the settled records."""
        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        now_ns = 1000 + 3600_000_000_000 + 100
        receipt = orchestrator.settle_batch(
            predictions=predictions,
            prices_by_symbol={"AAPL": _make_prices()},
            cost_model=_make_cost_model(),
            now_ns=now_ns,
        )
        assert hasattr(receipt, "records")
        assert len(receipt.records) > 0
        for record in receipt.records:
            assert isinstance(record, SettlementRecord)

    def test_receipt_to_dict_is_json_serializable(self) -> None:
        """The receipt can be serialized to JSON."""
        import json as _json

        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        now_ns = 1000 + 3600_000_000_000 + 100
        receipt = orchestrator.settle_batch(
            predictions=predictions,
            prices_by_symbol={"AAPL": _make_prices()},
            cost_model=_make_cost_model(),
            now_ns=now_ns,
        )
        d = receipt.to_dict()
        _json.dumps(d)
        assert "records" in d
        assert "settled_count" in d
        assert "pending_count" in d


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestStoreAndSettleBatch:
    """The convenience function store_and_settle_batch works end-to-end."""

    def test_store_and_settle_batch_returns_receipt(self) -> None:
        """store_and_settle_batch stores + settles and returns a receipt."""
        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        receipt = store_and_settle_batch(
            predictions=predictions,
            batch_hash=batch_hash,
            signature=signature,
            prices_by_symbol={"AAPL": _make_prices()},
            cost_model=_make_cost_model(),
            now_ns=1000 + 3600_000_000_000 + 100,
            secret=_SECRET,
            shadow_ledger_dir=tmpdir,
            settlement_ledger_dir=tempfile.mkdtemp(),
        )
        assert isinstance(receipt, SettlementReceipt)
        assert receipt.settled_count >= 1


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInSettlementOutput:
    """Settlement output must not leak secrets."""

    def test_receipt_to_dict_has_no_secret_keys(self) -> None:

        predictions, batch_hash, signature = _make_signed_batch(
            predictions=[_make_prediction(ts_event=1000, horizon_ns=3600_000_000_000)]
        )
        tmpdir = tempfile.mkdtemp()
        orchestrator = ShadowSettlementOrchestrator(
            shadow_ledger=ShadowLedger(base_dir=tmpdir),
            settlement_ledger=SettlementLedger(root=pathlib.Path(tempfile.mkdtemp())),
            secret=_SECRET,
        )
        orchestrator.store_batch(
            predictions=predictions, batch_hash=batch_hash, signature=signature,
        )
        now_ns = 1000 + 3600_000_000_000 + 100
        receipt = orchestrator.settle_batch(
            predictions=predictions,
            prices_by_symbol={"AAPL": _make_prices()},
            cost_model=_make_cost_model(),
            now_ns=now_ns,
        )
        d = receipt.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password",
                        "broker_account", "credential", "shared_secret"}
        assert not _has_secret(d, secret_names)
