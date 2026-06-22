"""
tests for quant_foundry.shadow_ledger — TASK-0402: Shadow Prediction Ledger Storage.

TDD: tests written FIRST, expected to fail until shadow_ledger.py is implemented.

Acceptance criteria (NEXT_STEPS_PLAN TASK-0402):
- Shadow predictions store safely (local storage first).
- Duplicate batches are idempotent.
- Order-like fields are rejected.
- No write path to `sig.predict` exists.
- Idempotency by prediction ID and batch hash.

Additional invariants (from Builder 1/3 design notes + cross-cutting rigor):
- Diff-hash rejection: same prediction_id + different batch_hash → security event.
- authority is always shadow-only (enforced at store time).
- Read API by model_id / symbol / time window.
- Batch hashing reuses ids.hash_payload (deterministic).
- Restart-durable (JSONL replay).
- Frozen + extra="forbid" on all record models.
- Structural no-sig.predict / no-fincept_bus source guard (defense in depth).
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError
from quant_foundry.ids import hash_payload
from quant_foundry.schemas import Authority
from quant_foundry.shadow_ledger import (
    ORDER_LIKE_FIELDS,
    ShadowLedger,
    ShadowLedgerRecord,
    compute_batch_hash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pred(
    *,
    prediction_id: str,
    model_id: str = "m1",
    symbol: str = "AAPL",
    ts_event: int = 1_000_000_000,
    horizon_ns: int = 60_000_000_000,
    direction: float = 0.6,
    confidence: float = 0.7,
    authority: Authority = Authority.SHADOW_ONLY,
    extra: dict | None = None,
) -> dict:
    """Build a shadow-prediction dict matching schemas.ShadowPrediction."""
    p = {
        "prediction_id": prediction_id,
        "model_id": model_id,
        "symbol": symbol,
        "ts_event": ts_event,
        "horizon_ns": horizon_ns,
        "direction": direction,
        "confidence": confidence,
        "authority": authority,
        "expected_return": 0.002,
        "p_up": 0.65,
    }
    if extra:
        p.update(extra)
    return p


def _batch_hash(predictions: list[dict]) -> str:
    """Compute a deterministic batch hash over a list of prediction dicts."""
    payload = json.dumps(predictions, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hash_payload(payload)


# ---------------------------------------------------------------------------
# compute_batch_hash
# ---------------------------------------------------------------------------


class TestComputeBatchHash:
    def test_deterministic_for_same_input(self) -> None:
        preds = [_pred(prediction_id="p1"), _pred(prediction_id="p2")]
        assert compute_batch_hash(preds) == compute_batch_hash(preds)

    def test_changes_when_content_changes(self) -> None:
        preds1 = [_pred(prediction_id="p1")]
        preds2 = [_pred(prediction_id="p1", direction=0.9)]
        assert compute_batch_hash(preds1) != compute_batch_hash(preds2)

    def test_order_matters(self) -> None:
        a = [_pred(prediction_id="p1"), _pred(prediction_id="p2")]
        b = [_pred(prediction_id="p2"), _pred(prediction_id="p1")]
        assert compute_batch_hash(a) != compute_batch_hash(b)

    def test_reuses_hash_payload_shape(self) -> None:
        preds = [_pred(prediction_id="p1")]
        h = compute_batch_hash(preds)
        # SHA-256 hex = 64 chars (same as ids.hash_payload)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# ShadowLedgerRecord
# ---------------------------------------------------------------------------


class TestShadowLedgerRecord:
    def test_frozen_and_strict(self) -> None:
        r = ShadowLedgerRecord(
            prediction_id="p1",
            model_id="m1",
            symbol="AAPL",
            ts_event=1_000_000_000,
            horizon_ns=60_000_000_000,
            direction=0.6,
            confidence=0.7,
            authority=Authority.SHADOW_ONLY,
            batch_hash="abc",
            stored_at_ns=2_000_000_000,
        )
        assert r.schema_version == 1
        with pytest.raises(ValidationError):
            r.prediction_id = "x"  # frozen
        with pytest.raises(ValidationError):
            ShadowLedgerRecord(  # type: ignore[call-arg]
                prediction_id="p1",
                model_id="m1",
                symbol="AAPL",
                ts_event=1,
                horizon_ns=1,
                direction=0.6,
                confidence=0.7,
                authority=Authority.SHADOW_ONLY,
                batch_hash="abc",
                stored_at_ns=2,
                unexpected_field=1,
            )

    def test_authority_defaults_to_shadow_only(self) -> None:
        r = ShadowLedgerRecord(
            prediction_id="p1",
            model_id="m1",
            symbol="AAPL",
            ts_event=1,
            horizon_ns=1,
            direction=0.6,
            confidence=0.7,
            batch_hash="abc",
            stored_at_ns=2,
        )
        assert r.authority == Authority.SHADOW_ONLY


# ---------------------------------------------------------------------------
# ShadowLedger — store_batch + idempotency
# ---------------------------------------------------------------------------


class TestStoreBatch:
    def test_stores_predictions_safely(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1"), _pred(prediction_id="p2")]
        bh = compute_batch_hash(preds)
        receipt = ledger.store_batch(preds, batch_hash=bh)
        assert receipt.stored == 2
        assert receipt.duplicates == 0
        assert len(ledger.list()) == 2

    def test_duplicate_batch_is_idempotent(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1"), _pred(prediction_id="p2")]
        bh = compute_batch_hash(preds)
        ledger.store_batch(preds, batch_hash=bh)
        receipt2 = ledger.store_batch(preds, batch_hash=bh)
        assert receipt2.stored == 0
        assert receipt2.duplicates == 2
        assert len(ledger.list()) == 2  # no duplication

    def test_same_prediction_id_different_batch_hash_rejected(
        self, tmp_path: pathlib.Path
    ) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds1 = [_pred(prediction_id="p1", direction=0.6)]
        bh1 = compute_batch_hash(preds1)
        ledger.store_batch(preds1, batch_hash=bh1)

        preds2 = [_pred(prediction_id="p1", direction=0.9)]  # same id, diff content
        bh2 = compute_batch_hash(preds2)
        with pytest.raises(ValueError, match=r"security|tamper|diff.*hash"):
            ledger.store_batch(preds2, batch_hash=bh2)
        # Original record intact
        assert len(ledger.list()) == 1
        assert ledger.list()[0].prediction_id == "p1"
        assert ledger.list()[0].direction == 0.6

    def test_new_prediction_id_in_same_batch_hash_appends(
        self, tmp_path: pathlib.Path
    ) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds1 = [_pred(prediction_id="p1")]
        bh1 = compute_batch_hash(preds1)
        ledger.store_batch(preds1, batch_hash=bh1)

        # A different batch with a new prediction_id stores fine.
        preds2 = [_pred(prediction_id="p2")]
        bh2 = compute_batch_hash(preds2)
        receipt = ledger.store_batch(preds2, batch_hash=bh2)
        assert receipt.stored == 1
        assert len(ledger.list()) == 2


# ---------------------------------------------------------------------------
# Order-like field rejection
# ---------------------------------------------------------------------------


class TestOrderLikeFieldRejection:
    def test_quantity_rejected(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1", extra={"quantity": 100})]
        bh = compute_batch_hash(preds)
        with pytest.raises((ValidationError, ValueError), match=r"order|quantity|extra"):
            ledger.store_batch(preds, batch_hash=bh)

    def test_side_rejected(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1", extra={"side": "buy"})]
        bh = compute_batch_hash(preds)
        with pytest.raises((ValidationError, ValueError), match=r"order|side|extra"):
            ledger.store_batch(preds, batch_hash=bh)

    def test_broker_rejected(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1", extra={"broker": "alpaca"})]
        bh = compute_batch_hash(preds)
        with pytest.raises((ValidationError, ValueError), match=r"order|broker|extra"):
            ledger.store_batch(preds, batch_hash=bh)

    def test_order_type_rejected(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1", extra={"order_type": "market"})]
        bh = compute_batch_hash(preds)
        with pytest.raises((ValidationError, ValueError), match=r"order|order_type|extra"):
            ledger.store_batch(preds, batch_hash=bh)

    def test_order_like_fields_constant_lists_them(self) -> None:
        # The module exports the set of forbidden order-like field names.
        assert "quantity" in ORDER_LIKE_FIELDS
        assert "side" in ORDER_LIKE_FIELDS
        assert "broker" in ORDER_LIKE_FIELDS
        assert "order_type" in ORDER_LIKE_FIELDS


# ---------------------------------------------------------------------------
# Shadow-only authority enforcement
# ---------------------------------------------------------------------------


class TestShadowOnlyAuthority:
    def test_non_shadow_authority_rejected(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        # ShadowPrediction only allows shadow-only authority, so constructing a
        # non-shadow dict at the schema level is impossible. But we test the
        # ledger's own guard explicitly by bypassing schema with a raw dict that
        # carries a forged authority string.
        preds = [_pred(prediction_id="p1")]
        preds[0]["authority"] = "trading"  # not a valid Authority value
        bh = compute_batch_hash(preds)
        with pytest.raises((ValidationError, ValueError), match=r"authority|shadow"):
            ledger.store_batch(preds, batch_hash=bh)

    def test_all_stored_records_are_shadow_only(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1"), _pred(prediction_id="p2")]
        bh = compute_batch_hash(preds)
        ledger.store_batch(preds, batch_hash=bh)
        for r in ledger.list():
            assert r.authority == Authority.SHADOW_ONLY


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


class TestReadAPI:
    def test_read_by_model(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [
            _pred(prediction_id="p1", model_id="ma"),
            _pred(prediction_id="p2", model_id="mb"),
            _pred(prediction_id="p3", model_id="ma"),
        ]
        bh = compute_batch_hash(preds)
        ledger.store_batch(preds, batch_hash=bh)
        ma = ledger.read_by_model("ma")
        assert len(ma) == 2
        assert all(r.model_id == "ma" for r in ma)

    def test_read_by_symbol(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [
            _pred(prediction_id="p1", symbol="AAPL"),
            _pred(prediction_id="p2", symbol="MSFT"),
            _pred(prediction_id="p3", symbol="AAPL"),
        ]
        bh = compute_batch_hash(preds)
        ledger.store_batch(preds, batch_hash=bh)
        aapl = ledger.read_by_symbol("AAPL")
        assert len(aapl) == 2
        assert all(r.symbol == "AAPL" for r in aapl)

    def test_read_by_window(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [
            _pred(prediction_id="p1", ts_event=1_000_000_000),
            _pred(prediction_id="p2", ts_event=5_000_000_000),
            _pred(prediction_id="p3", ts_event=9_000_000_000),
        ]
        bh = compute_batch_hash(preds)
        ledger.store_batch(preds, batch_hash=bh)
        window = ledger.read_by_window(start_ns=2_000_000_000, end_ns=8_000_000_000)
        assert len(window) == 1
        assert window[0].prediction_id == "p2"


# ---------------------------------------------------------------------------
# Restart durability
# ---------------------------------------------------------------------------


class TestRestartDurability:
    def test_reload_after_restart(self, tmp_path: pathlib.Path) -> None:
        ledger1 = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1"), _pred(prediction_id="p2")]
        bh = compute_batch_hash(preds)
        ledger1.store_batch(preds, batch_hash=bh)
        assert len(ledger1.list()) == 2

        # Simulate restart: new instance reads the same JSONL.
        ledger2 = ShadowLedger(base_dir=tmp_path)
        assert len(ledger2.list()) == 2
        ids = {r.prediction_id for r in ledger2.list()}
        assert ids == {"p1", "p2"}

    def test_idempotent_after_restart(self, tmp_path: pathlib.Path) -> None:
        ledger1 = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1"), _pred(prediction_id="p2")]
        bh = compute_batch_hash(preds)
        ledger1.store_batch(preds, batch_hash=bh)

        # After restart, re-storing the same batch is idempotent.
        ledger2 = ShadowLedger(base_dir=tmp_path)
        receipt = ledger2.store_batch(preds, batch_hash=bh)
        assert receipt.stored == 0
        assert receipt.duplicates == 2
        assert len(ledger2.list()) == 2


# ---------------------------------------------------------------------------
# Structural no-sig.predict / no-fincept_bus guard
# ---------------------------------------------------------------------------


class TestNoTradingStreamGuard:
    def test_module_source_has_no_sig_predict_reference(self) -> None:
        """The shadow ledger module must NOT reference sig.predict or fincept_bus
        producers — shadow output never feeds the orchestrator's trading stream
        until TASK-0704.
        """
        import quant_foundry.shadow_ledger as mod

        source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "sig.predict" not in source
        assert "fincept_bus" not in source
        # No bus producer / stream writer attribute on the ledger class.
        ledger = ShadowLedger(base_dir=None)
        for forbidden in ("producer", "stream_writer", "bus", "publish", "emit"):
            assert not hasattr(ledger, forbidden), (
                f"ShadowLedger must not expose a '{forbidden}' attribute "
                "(no trading-stream write path)"
            )

    def test_no_write_path_to_sig_predict(self, tmp_path: pathlib.Path) -> None:
        """Storing predictions must not touch any sig.predict stream."""
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1")]
        bh = compute_batch_hash(preds)
        ledger.store_batch(preds, batch_hash=bh)
        # No sig.predict file / artifact should exist under base_dir.
        for f in tmp_path.rglob("*"):
            assert "sig.predict" not in f.name


# ---------------------------------------------------------------------------
# Batch hash mismatch (caller-supplied hash vs computed)
# ---------------------------------------------------------------------------


class TestBatchHashMismatch:
    def test_wrong_batch_hash_rejected(self, tmp_path: pathlib.Path) -> None:
        ledger = ShadowLedger(base_dir=tmp_path)
        preds = [_pred(prediction_id="p1")]
        wrong_hash = "0" * 64  # not the real hash
        with pytest.raises(ValueError, match=r"batch.*hash|mismatch|tamper"):
            ledger.store_batch(preds, batch_hash=wrong_hash)
