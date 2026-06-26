"""
quant_foundry.settlement — the prediction settlement ledger.

This is the worker that judges every prediction after its horizon expires.
It matches a prediction to realized market data and computes the metrics the
tournament (TASK-0404) ranks on.

Critical invariants (cross-cutting quant rigor §1 point-in-time correctness):
- Settlement uses ONLY prices observed after the prediction's decision time.
  The realized return is computed on the window (t, t+h] where t is the
  decision time and h is the horizon. A prediction whose horizon has not
  fully elapsed stays ``pending_time`` — settling it early would use look-ahead.
- ``pending_time`` (horizon not elapsed) and ``pending_data`` (market data
  missing) are distinct states.

Durability + idempotency:
- Records are appended to a JSONL file under ``<root>/<model_id>.settlements.jsonl``
  so a process restart does not lose settled outcomes.
- Re-settling a prediction with the SAME inputs and cost-model version yields
  the identical record and does NOT duplicate the file entry (the existing
  record is returned as-is).
- Re-settling with a DIFFERENT cost-model version appends a new record (history
  is preserved, never overwritten) — this is the §3 reproducibility guard.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from quant_foundry.metrics import (
    PriceTick,
    abnormal_return,
    apply_costs,
    brier_score,
    calibration_bucket,
    realized_return,
)
from quant_foundry.outcomes import CostModel, SettlementRecord, SettlementStatus

# Same allow-list as fincept_core.prediction_log for consistency.
_BAD_NAME_CHARS = set('/\\:*?"<>|\0')


def _validate_model_id(model_id: str) -> None:
    if not model_id:
        raise ValueError("model_id must be non-empty")
    if any(c in _BAD_NAME_CHARS for c in model_id):
        raise ValueError(f"model_id contains forbidden character: {model_id!r}")
    if model_id in {".", ".."} or model_id.startswith("."):
        raise ValueError(f"model_id may not start with '.': {model_id!r}")


@dataclass(frozen=True)
class PredictionInput:
    """Decoupled prediction shape the ledger accepts.

    Kept as a local dataclass (not importing schemas.ShadowPrediction) so the
    settlement ledger can also settle existing fincept_core PredictionRow
    records without coupling to the cross-boundary contract. Callers pass
    either a dict with these keys or a PredictionInput.
    """

    prediction_id: str
    model_id: str
    symbol: str
    ts_event: int
    horizon_ns: int
    direction: float
    confidence: float
    p_up: float


def _coerce_prediction(prediction: Any) -> PredictionInput:
    """Accept a dict or a PredictionInput and return a PredictionInput."""
    if isinstance(prediction, PredictionInput):
        return prediction
    if isinstance(prediction, dict):
        return PredictionInput(
            prediction_id=str(prediction["prediction_id"]),
            model_id=str(prediction["model_id"]),
            symbol=str(prediction["symbol"]),
            ts_event=int(prediction["ts_event"]),
            horizon_ns=int(prediction["horizon_ns"]),
            direction=float(prediction["direction"]),
            confidence=float(prediction["confidence"]),
            p_up=float(prediction.get("p_up") or prediction.get("confidence") or 0.5),
        )
    raise TypeError(f"Unsupported prediction type: {type(prediction)}")


class SettlementLedger:
    """Filesystem-backed settlement ledger.

    Layout: ``<root>/<model_id>.settlements.jsonl`` (append-only, one record
    per line). Tests pass a ``tmp_path``; production reads from
    ``$QUANT_FOUNDRY_SETTLEMENTS_DIR`` (default
    ``data/quant-foundry/settlements``).
    """

    def __init__(self, *, root: pathlib.Path | None = None) -> None:
        self._root = root or pathlib.Path(
            os.environ.get(
                "QUANT_FOUNDRY_SETTLEMENTS_DIR",
                "data/quant-foundry/settlements",
            )
        )

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def _path(self, model_id: str) -> pathlib.Path:
        _validate_model_id(model_id)
        return self._root / f"{model_id}.settlements.jsonl"

    # ------------------------------------------------------------------ #
    # Settle                                                             #
    # ------------------------------------------------------------------ #

    def settle(
        self,
        *,
        prediction: Any,
        prices: Sequence[PriceTick],
        benchmark_prices: Sequence[PriceTick] | None,
        cost_model: CostModel,
        now_ns: int,
        holding_days: int = 1,
    ) -> SettlementRecord:
        """Settle one prediction and persist the record (idempotently).

        Idempotency rule:
        - If a record already exists for ``(prediction_id, cost_model_version)``
          with the same status, return it as-is (no duplicate append).
        - If a record exists for ``prediction_id`` with a DIFFERENT
          ``cost_model_version``, append a new record (history preserved).

        Look-ahead guard: if ``now_ns < ts_event + horizon_ns``, the record is
        ``pending_time`` and no return is computed (regardless of available
        prices). If the horizon has elapsed but the entry/exit price is
        missing, the record is ``pending_data``.
        """
        pred = _coerce_prediction(prediction)
        window_end = pred.ts_event + pred.horizon_ns

        # Idempotency: same prediction_id + same cost_model_version -> return existing.
        existing = self._find(pred.prediction_id, cost_model.version)
        if existing is not None:
            return existing

        record = self._compute_record(
            pred=pred,
            prices=prices,
            benchmark_prices=benchmark_prices,
            cost_model=cost_model,
            now_ns=now_ns,
            holding_days=holding_days,
            window_end=window_end,
        )
        self._append(record)
        return record

    def _compute_record(
        self,
        *,
        pred: PredictionInput,
        prices: Sequence[PriceTick],
        benchmark_prices: Sequence[PriceTick] | None,
        cost_model: CostModel,
        now_ns: int,
        holding_days: int,
        window_end: int,
    ) -> SettlementRecord:
        # Look-ahead guard: horizon not elapsed -> pending_time.
        if now_ns < window_end:
            return SettlementRecord(
                prediction_id=pred.prediction_id,
                model_id=pred.model_id,
                symbol=pred.symbol,
                ts_event=pred.ts_event,
                horizon_ns=pred.horizon_ns,
                status=SettlementStatus.PENDING_TIME,
                settled_at_ns=None,
                realized_return_gross=None,
                realized_return_net=None,
                abnormal_return=None,
                brier=None,
                calibration_bucket=None,
                cost_model_version=cost_model.version,
                decision_window_start=pred.ts_event,
                decision_window_end=window_end,
            )

        gross = realized_return(
            prices=prices,
            decision_ts=pred.ts_event,
            horizon_ns=pred.horizon_ns,
            direction=pred.direction,
        )

        # Horizon elapsed but data missing -> pending_data (distinct from pending_time).
        if gross is None:
            return SettlementRecord(
                prediction_id=pred.prediction_id,
                model_id=pred.model_id,
                symbol=pred.symbol,
                ts_event=pred.ts_event,
                horizon_ns=pred.horizon_ns,
                status=SettlementStatus.PENDING_DATA,
                settled_at_ns=None,
                realized_return_gross=None,
                realized_return_net=None,
                abnormal_return=None,
                brier=None,
                calibration_bucket=None,
                cost_model_version=cost_model.version,
                decision_window_start=pred.ts_event,
                decision_window_end=window_end,
            )

        net = apply_costs(
            gross_return=gross,
            cost_model=cost_model,
            direction=pred.direction,
            holding_days=holding_days,
        )

        bench_ret: float | None = None
        if benchmark_prices is not None:
            bench_ret = realized_return(
                prices=benchmark_prices,
                decision_ts=pred.ts_event,
                horizon_ns=pred.horizon_ns,
                direction=1.0,  # benchmark is a long-only reference
            )
        ab = abnormal_return(realized=gross, benchmark=bench_ret)

        actual_up = gross > 0
        brier = brier_score(p_up=pred.p_up, actual_up=actual_up)
        bucket = calibration_bucket(pred.confidence)

        return SettlementRecord(
            prediction_id=pred.prediction_id,
            model_id=pred.model_id,
            symbol=pred.symbol,
            ts_event=pred.ts_event,
            horizon_ns=pred.horizon_ns,
            status=SettlementStatus.SETTLED,
            settled_at_ns=now_ns,
            realized_return_gross=gross,
            realized_return_net=net,
            abnormal_return=ab,
            brier=brier,
            calibration_bucket=bucket,
            cost_model_version=cost_model.version,
            decision_window_start=pred.ts_event,
            decision_window_end=window_end,
        )

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #

    def _append(self, record: SettlementRecord) -> None:
        _validate_model_id(record.model_id)
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.model_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(record.to_json() + "\n")

    def _find(self, prediction_id: str, cost_model_version: str) -> SettlementRecord | None:
        """Return the existing record for (prediction_id, cost_model_version) if any.

        Scans all model files since a prediction_id is globally unique (uuid4).
        In practice a prediction belongs to one model, but scanning all files
        is cheap at MVP volumes and avoids a cross-file index.
        """
        for rec in self.read_all():
            if rec.prediction_id == prediction_id and rec.cost_model_version == cost_model_version:
                return rec
        return None

    def read_all(self) -> list[SettlementRecord]:
        """Return all settled records across all model files (newest-first)."""
        if not self._root.is_dir():
            return []
        rows: list[SettlementRecord] = []
        for path in sorted(self._root.glob("*.settlements.jsonl")):
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(SettlementRecord.from_json(line))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        # Malformed line must not take the read down.
                        continue
        rows.sort(key=lambda r: r.settled_at_ns or 0, reverse=True)
        return rows
