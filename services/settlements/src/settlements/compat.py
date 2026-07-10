"""settlements.compat — Path A compatibility wrapper over Path B settlement.

This module bridges the legacy Path A settlement interface (keyed by
``agent_id``, cost model ``v1.default``) to the canonical Path B
settlement ledger (``quant_foundry.SettlementLedger``, keyed by
``model_id``, cost model ``cm-v1``).

The wrapper:

  1. Accepts ``PredictionRow`` inputs (agent_id keyed, no ``p_up``).
  2. Maps ``agent_id`` → ``model_id`` via a configurable mapping function.
  3. Derives ``p_up`` from ``confidence`` (since ``PredictionRow`` lacks it).
  4. Converts ``PredictionRow`` + price lookups into Path B's
     ``PredictionInput`` + ``PriceTick`` shapes.
  5. Delegates to ``SettlementLedger.settle()`` with the ``cm-v1`` cost model.
  6. Returns a ``fincept_core.datasets.SettlementRecord``-shaped dict for
     API compatibility (the wrapper translates Path B's
     ``quant_foundry.outcomes.SettlementRecord`` into the shape
     ``build_evidence_receipt`` expects).

Design constraints:
  - No duplicate settlement math — Path B is the sole computation engine.
  - ``model_id`` is canonical; ``agent_id`` is a legacy compatibility input.
  - Missing mapping should fail clearly, not silently invent a model_id.
  - The wrapper is clearly marked as deprecated.

See ``reports/c6-settlement-replay/5cfb6cfa/C6_CANONICAL_SETTLEMENT_DESIGN.md``
for the full design document.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fincept_core.datasets import SettlementRecord as PathARecord
from fincept_core.datasets import SettlementStore
from fincept_core.prediction_log import PredictionRow

# Canonical cost model — cm-v1 from Path B.
from quant_foundry.outcomes import CostModel, SettlementRecord as PathBRecord
from quant_foundry.outcomes import SettlementStatus
from quant_foundry.settlement import SettlementLedger
from quant_foundry.settlement_sweep import default_cost_model


# --------------------------------------------------------------------------- #
# Identity mapping                                                            #
# --------------------------------------------------------------------------- #

# Default agent_id → model_id mapping: replace '.' with '-'.
# e.g. "gbm_predictor.v1" → "gbm_predictor-v1".
# This can be overridden with a custom mapping function for non-standard
# agent IDs.


def default_agent_to_model_id(agent_id: str) -> str:
    """Map agent_id to model_id.

    Default: replace '.' with '-' (gbm_predictor.v1 → gbm_predictor-v1).
    This can be overridden with a custom mapping function for
    non-standard agent IDs.

    Raises ``ValueError`` if ``agent_id`` is empty.
    """
    if not agent_id:
        raise ValueError("agent_id must be non-empty — cannot map to model_id")
    return agent_id.replace(".", "-")


def default_model_to_agent_id(model_id: str) -> str:
    """Reverse map model_id to agent_id for API response compatibility.

    Default: replace '-' with '.' (gbm_predictor-v1 → gbm_predictor.v1).
    """
    if not model_id:
        raise ValueError("model_id must be non-empty — cannot reverse map to agent_id")
    return model_id.replace("-", ".")


def derive_p_up_from_confidence(
    direction: float,
    confidence: float,
) -> float:
    """Derive p_up from direction and confidence when PredictionRow lacks p_up.

    For a long (direction > 0): p_up = confidence.
    For a short (direction < 0): p_up = 1 - confidence.
    For flat (direction == 0): p_up = 0.5.

    This is a fallback — if the prediction has an explicit p_up, it should
    be used directly. PredictionRow does not carry p_up, so this derivation
    is the best available proxy.
    """
    if direction > 0:
        return max(0.0, min(1.0, confidence))
    if direction < 0:
        return max(0.0, min(1.0, 1.0 - confidence))
    return 0.5


# --------------------------------------------------------------------------- #
# Path B → Path A record translation                                          #
# --------------------------------------------------------------------------- #


def path_b_to_path_a_record(
    b_record: PathBRecord,
    *,
    agent_id: str,
    model_name: str,
    cost_model: CostModel,
) -> PathARecord:
    """Translate a Path B SettlementRecord into a Path A SettlementRecord.

    This preserves the Path A response shape (agent_id, model_name,
    cost_breakdown_* fields) while carrying Path B's canonical semantics
    (direction-aware gross, cm-v1 cost model, abnormal_return,
    calibration_bucket).
    """
    return PathARecord(
        prediction_id=b_record.prediction_id,
        agent_id=agent_id,
        model_name=model_name,
        symbol=b_record.symbol,
        ts_event=b_record.ts_event,
        horizon_ns=b_record.horizon_ns,
        decision_window_start_ns=b_record.decision_window_start,
        decision_window_end_ns=b_record.decision_window_end,
        cost_model_version=b_record.cost_model_version,
        realized_return_gross=b_record.realized_return_gross,
        realized_return_net=b_record.realized_return_net,
        cost_breakdown_fee_bps=cost_model.fee_bps,
        cost_breakdown_spread_bps=cost_model.spread_bps,
        cost_breakdown_slippage_bps=cost_model.slippage_bps,
        brier_component=b_record.brier,
        status=b_record.status.value,
        settled_at_ns=b_record.settled_at_ns,
    )


# --------------------------------------------------------------------------- #
# Compatibility adapter                                                       #
# --------------------------------------------------------------------------- #


class PathACompatAdapter:
    """Compatibility wrapper that delegates Path A settlement to Path B.

    Accepts ``PredictionRow`` inputs (agent_id keyed) and delegates to
    ``quant_foundry.SettlementLedger`` (model_id keyed) with the ``cm-v1``
    cost model. Returns ``fincept_core.datasets.SettlementRecord``-shaped
    records for API compatibility.

    The adapter also writes to the legacy ``SettlementStore`` so existing
    API routes that read from ``data/settlements/`` continue to work
    during the migration period. When the migration is complete
    (Phase 6), the legacy write can be removed.
    """

    def __init__(
        self,
        *,
        agent_to_model_id: Callable[[str], str] = default_agent_to_model_id,
        cost_model: CostModel | None = None,
        settlement_ledger: SettlementLedger | None = None,
        legacy_store: SettlementStore | None = None,
    ) -> None:
        self._agent_to_model_id = agent_to_model_id
        self._cost_model = cost_model or default_cost_model()
        self._ledger = settlement_ledger or SettlementLedger()
        self._legacy_store = legacy_store

    @property
    def cost_model(self) -> CostModel:
        return self._cost_model

    @property
    def ledger(self) -> SettlementLedger:
        return self._ledger

    def settle_prediction(
        self,
        pred: PredictionRow,
        *,
        prices: Sequence[Any],  # PriceTick list
        benchmark_prices: Sequence[Any] | None,
        now_ns: int,
        holding_days: int = 1,
    ) -> PathARecord:
        """Settle a single PredictionRow via Path B and return a Path A record.

        Steps:
          1. Map agent_id → model_id (fails clearly if mapping is empty).
          2. Derive p_up from direction + confidence.
          3. Build PredictionInput dict for Path B.
          4. Delegate to SettlementLedger.settle().
          5. Translate the Path B record back to Path A shape.
          6. Optionally write to the legacy SettlementStore.
        """
        model_id = self._agent_to_model_id(pred.agent_id)
        if not model_id:
            raise ValueError(
                f"agent_id mapping returned empty model_id for agent_id={pred.agent_id!r}"
            )

        p_up = derive_p_up_from_confidence(pred.direction, pred.confidence)

        prediction_input = {
            "prediction_id": pred.id,
            "model_id": model_id,
            "symbol": pred.symbol,
            "ts_event": pred.ts_event,
            "horizon_ns": pred.horizon_ns,
            "direction": pred.direction,
            "confidence": pred.confidence,
            "p_up": p_up,
        }

        b_record = self._ledger.settle(
            prediction=prediction_input,
            prices=prices,
            benchmark_prices=benchmark_prices,
            cost_model=self._cost_model,
            now_ns=now_ns,
            holding_days=holding_days,
        )

        a_record = path_b_to_path_a_record(
            b_record,
            agent_id=pred.agent_id,
            model_name=pred.model_name,
            cost_model=self._cost_model,
        )

        # Optionally write to the legacy store for backward-compatible reads.
        if self._legacy_store is not None and a_record.status in (
            "settled",
            "pending_data",
        ):
            try:
                self._legacy_store.append(a_record, now_ns=now_ns)
            except Exception:
                # Legacy write is best-effort — do not fail the settlement
                # if the legacy store rejects a duplicate or has an issue.
                pass

        return a_record

    def settle_due_predictions(
        self,
        predictions_dir: pathlib.Path,
        *,
        now_ns: int,
        market_data_source: Callable[[str, int, int], float | None],
        benchmark_symbol: str = "SPY",
        holding_days: int = 1,
    ) -> list[PathARecord]:
        """Settle all due predictions from the prediction log (sync version).

        This is the sync equivalent of the poller's tick — it scans the
        prediction log for due predictions, fetches prices, and settles
        them via Path B. Used by the replay harness and sync test paths.
        """
        from settlements.worker import _load_due_predictions

        due = _load_due_predictions(predictions_dir, now_ns=now_ns)
        appended: list[PathARecord] = []

        for pred in due:
            # Check idempotency: skip if already settled under cm-v1.
            existing = self._ledger._find(pred.id, self._cost_model.version)
            if existing is not None and existing.status == SettlementStatus.SETTLED:
                continue

            ts_event = pred.ts_event
            ts_horizon = ts_event + pred.horizon_ns

            close_t1 = market_data_source(pred.symbol, ts_event, ts_event)
            close_t2 = market_data_source(pred.symbol, ts_event, ts_horizon)

            prices: list[Any] = []
            if close_t1 is not None:
                prices.append(_PriceTickSimple(ts=ts_event, price=close_t1))
            if close_t2 is not None:
                prices.append(_PriceTickSimple(ts=ts_horizon, price=close_t2))

            benchmark_prices: list[Any] | None = None
            bench_t1 = market_data_source(benchmark_symbol, ts_event, ts_event)
            bench_t2 = market_data_source(benchmark_symbol, ts_event, ts_horizon)
            if bench_t1 is not None and bench_t2 is not None:
                benchmark_prices = [
                    _PriceTickSimple(ts=ts_event, price=bench_t1),
                    _PriceTickSimple(ts=ts_horizon, price=bench_t2),
                ]

            record = self.settle_prediction(
                pred,
                prices=prices,
                benchmark_prices=benchmark_prices,
                now_ns=now_ns,
                holding_days=holding_days,
            )
            appended.append(record)

        return appended

    async def settle_due_predictions_async(
        self,
        predictions_dir: pathlib.Path,
        *,
        now_ns: int,
        market_data_source: Callable[[str, int, int], Awaitable[float | None]],
        benchmark_symbol: str = "SPY",
        holding_days: int = 1,
    ) -> list[PathARecord]:
        """Settle all due predictions from the prediction log (async version).

        This is the async version used by the API poller.
        """
        from settlements.worker import _load_due_predictions

        due = _load_due_predictions(predictions_dir, now_ns=now_ns)
        appended: list[PathARecord] = []

        for pred in due:
            # Check idempotency: skip if already settled under cm-v1.
            existing = self._ledger._find(pred.id, self._cost_model.version)
            if existing is not None and existing.status == SettlementStatus.SETTLED:
                continue

            ts_event = pred.ts_event
            ts_horizon = ts_event + pred.horizon_ns

            close_t1 = await market_data_source(pred.symbol, ts_event, ts_event)
            close_t2 = await market_data_source(pred.symbol, ts_event, ts_horizon)

            prices: list[Any] = []
            if close_t1 is not None:
                prices.append(_PriceTickSimple(ts=ts_event, price=close_t1))
            if close_t2 is not None:
                prices.append(_PriceTickSimple(ts=ts_horizon, price=close_t2))

            benchmark_prices: list[Any] | None = None
            bench_t1 = await market_data_source(benchmark_symbol, ts_event, ts_event)
            bench_t2 = await market_data_source(benchmark_symbol, ts_event, ts_horizon)
            if bench_t1 is not None and bench_t2 is not None:
                benchmark_prices = [
                    _PriceTickSimple(ts=ts_event, price=bench_t1),
                    _PriceTickSimple(ts=ts_horizon, price=bench_t2),
                ]

            record = self.settle_prediction(
                pred,
                prices=prices,
                benchmark_prices=benchmark_prices,
                now_ns=now_ns,
                holding_days=holding_days,
            )
            appended.append(record)

        return appended


# --------------------------------------------------------------------------- #
# Internal helper                                                             #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _PriceTickSimple:
    """Minimal PriceTick-compatible struct for the adapter.

    Avoids importing ``quant_foundry.metrics.PriceTick`` at module level
    to keep the import graph clean — the adapter constructs these from
    the legacy market_data_source contract (which returns floats, not
    PriceTick objects) and passes them to ``SettlementLedger.settle``,
    which accepts any object with ``.ts`` and ``.price`` attributes.
    """

    ts: int
    price: float


__all__ = [
    "PathACompatAdapter",
    "default_agent_to_model_id",
    "default_model_to_agent_id",
    "derive_p_up_from_confidence",
    "path_b_to_path_a_record",
]
