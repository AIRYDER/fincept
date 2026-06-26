"""settlements.worker - settlement worker MVP.

Tails ``fincept_core.prediction_log`` and writes settlement records to
``fincept_core.datasets.SettlementStore``.  The worker is a single
``tick`` coroutine that:

  1. Scans every ``<agent_id>.jsonl`` file under ``predictions_dir`` and
     loads every ``PredictionRow`` whose horizon has elapsed
     (``ts_event + horizon_ns <= now_ns``).
  2. Skips any prediction that already has a ``settled`` row for the
     current ``cost_model_version`` (idempotent on rerun).  A
     ``pending_data`` row does NOT block a retry -- the next tick
     re-attempts the price lookup and, if prices are now available,
     appends a ``settled`` row that supersedes the pending one.
  3. Queries the market data source for the realized close at
     ``ts_event`` (entry) and ``ts_event + horizon_ns`` (exit).
  4. Computes ``realized_return_gross``, applies the v1.default cost
     model (5 bps fee + 3 bps spread), and computes the Brier
     component.
  5. Appends a ``SettlementRecord`` with ``status="settled"`` -- or
     ``status="pending_data"`` when the market data source returns
     ``None`` for either price.

Market data source contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~

``market_data_source(symbol, ts1, ts2) -> float | None`` returns the
close price at ``ts2`` (the later of the two timestamps), or ``None``
if no bar is available at ``ts2``.  ``ts1`` is passed for context
(window validation / logging) but the returned float is always the
close at ``ts2``.  The worker calls the source twice per prediction:

  * ``market_data_source(symbol, ts_event, ts_event)``              -> close_t1
  * ``market_data_source(symbol, ts_event, ts_event + horizon_ns)`` -> close_t2

This keeps the source signature stable (one price per call) while
giving the worker both legs of the return.
"""

from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable

from fincept_core.datasets import (
    DEFAULT_COST_MODEL_VERSION,
    SettlementRecord,
    SettlementStore,
)
from fincept_core.prediction_log import PredictionRow

# v1.default cost model constants.  Mirrored from
# ``fincept_core.datasets.settlement.DEFAULT_COST_MODEL`` so the worker
# does not depend on the dict shape -- a future audit grep finds both.
_FEE_BPS = 5.0
_SPREAD_BPS = 3.0
_SLIPPAGE_BPS = 0.0


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #


def _load_due_predictions(
    predictions_dir: pathlib.Path,
    *,
    now_ns: int,
) -> list[PredictionRow]:
    """Return every PredictionRow whose horizon has elapsed by ``now_ns``.

    Scans all ``<agent_id>.jsonl`` files under ``predictions_dir`` so the
    worker settles every agent in one pass.  ``PredictionLog.read`` is
    per-agent, so the cross-agent scan is done here with a direct glob.
    Malformed lines are skipped (same tolerance policy as
    ``PredictionLog.read``).
    """
    if not predictions_dir.is_dir():
        return []
    due: list[PredictionRow] = []
    for path in sorted(predictions_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = PredictionRow.from_json(line)
                except (ValueError, KeyError):
                    continue
                if row.ts_event + row.horizon_ns <= now_ns:
                    due.append(row)
    return due


def _existing_status(
    store: SettlementStore,
    *,
    agent_id: str,
    prediction_id: str,
    cost_model_version: str,
) -> str | None:
    """Return the status of the existing row for the idempotency key, or None.

    Scans the agent's ledger for the most recent record matching
    ``(prediction_id, cost_model_version)``.  Returns its ``status`` so
    the worker can decide whether to skip (already settled), retry
    (pending_data), or append fresh (no prior row).
    """
    latest: SettlementRecord | None = None
    for rec in store.read_for_agent(agent_id):
        if (
            rec.prediction_id == prediction_id
            and rec.cost_model_version == cost_model_version
        ):
            latest = rec
    return latest.status if latest is not None else None


def _build_settled_record(
    pred: PredictionRow,
    *,
    now_ns: int,
    close_t1: float,
    close_t2: float,
) -> SettlementRecord:
    """Construct a ``status="settled"`` record from a prediction + prices."""
    realized_return_gross = (close_t2 / close_t1) - 1.0
    cost_bps = (_FEE_BPS + _SPREAD_BPS) / 10000.0
    realized_return_net = realized_return_gross - cost_bps

    prob_up = (pred.direction + 1.0) / 2.0
    prob_up = max(0.0, min(1.0, prob_up))
    actual_up = 1 if realized_return_gross > 0 else 0
    brier_component = (prob_up - actual_up) ** 2

    return SettlementRecord(
        prediction_id=pred.id,
        agent_id=pred.agent_id,
        model_name=pred.model_name,
        symbol=pred.symbol,
        ts_event=pred.ts_event,
        horizon_ns=pred.horizon_ns,
        decision_window_start_ns=pred.ts_event,
        decision_window_end_ns=pred.ts_event + pred.horizon_ns,
        cost_model_version=DEFAULT_COST_MODEL_VERSION,
        realized_return_gross=realized_return_gross,
        realized_return_net=realized_return_net,
        cost_breakdown_fee_bps=_FEE_BPS,
        cost_breakdown_spread_bps=_SPREAD_BPS,
        cost_breakdown_slippage_bps=_SLIPPAGE_BPS,
        brier_component=brier_component,
        status="settled",
        settled_at_ns=now_ns,
    )


def _build_pending_data_record(pred: PredictionRow) -> SettlementRecord:
    """Construct a ``status="pending_data"`` record (no realized return)."""
    return SettlementRecord(
        prediction_id=pred.id,
        agent_id=pred.agent_id,
        model_name=pred.model_name,
        symbol=pred.symbol,
        ts_event=pred.ts_event,
        horizon_ns=pred.horizon_ns,
        decision_window_start_ns=pred.ts_event,
        decision_window_end_ns=pred.ts_event + pred.horizon_ns,
        cost_model_version=DEFAULT_COST_MODEL_VERSION,
        realized_return_gross=None,
        realized_return_net=None,
        cost_breakdown_fee_bps=_FEE_BPS,
        cost_breakdown_spread_bps=_SPREAD_BPS,
        cost_breakdown_slippage_bps=_SLIPPAGE_BPS,
        brier_component=None,
        status="pending_data",
        settled_at_ns=None,
    )


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #


async def tick(
    now_ns: int,
    *,
    predictions_dir: pathlib.Path,
    settlements_dir: pathlib.Path,
    market_data_source: Callable[[str, int, int], Awaitable[float | None]],
) -> list[SettlementRecord]:
    """Settle every due prediction in one pass and return the appended records.

    The returned list is in settlement order (oldest-first per agent,
    agents processed in sorted filename order).  ``pending_data``
    records are included in the return value so a caller can observe
    that a retry was attempted but the price was unavailable.
    """
    store = SettlementStore(root=settlements_dir)
    due = _load_due_predictions(predictions_dir, now_ns=now_ns)

    appended: list[SettlementRecord] = []
    for pred in due:
        prior = _existing_status(
            store,
            agent_id=pred.agent_id,
            prediction_id=pred.id,
            cost_model_version=DEFAULT_COST_MODEL_VERSION,
        )
        if prior == "settled":
            # Idempotent: never re-settle an already-settled prediction.
            continue

        ts_event = pred.ts_event
        ts_horizon = ts_event + pred.horizon_ns
        close_t1 = await market_data_source(pred.symbol, ts_event, ts_event)
        close_t2 = await market_data_source(pred.symbol, ts_event, ts_horizon)

        if close_t1 is None or close_t2 is None or close_t1 == 0 or close_t2 == 0:
            if prior == "pending_data":
                # Still no data and we already recorded pending_data --
                # don't append a duplicate pending row on every retry.
                continue
            record = _build_pending_data_record(pred)
        else:
            record = _build_settled_record(
                pred, now_ns=now_ns, close_t1=close_t1, close_t2=close_t2
            )

        store.append(record, now_ns=now_ns)
        appended.append(record)
    return appended


def tick_sync(
    now_ns: int,
    *,
    predictions_dir: pathlib.Path,
    settlements_dir: pathlib.Path,
    market_data_source: Callable[[str, int, int], float | None],
) -> list[SettlementRecord]:
    """Synchronous wrapper around :func:`tick` for replay fixtures.

    The paper-spine replay test (todo 21) drives the worker with a
    deterministic in-memory price source.  Rather than spinning up an
    event loop for a purely synchronous fixture, this wrapper runs the
    same logic inline.  The behaviour is identical to ``tick`` modulo
    the await points.
    """
    store = SettlementStore(root=settlements_dir)
    due = _load_due_predictions(predictions_dir, now_ns=now_ns)

    appended: list[SettlementRecord] = []
    for pred in due:
        prior = _existing_status(
            store,
            agent_id=pred.agent_id,
            prediction_id=pred.id,
            cost_model_version=DEFAULT_COST_MODEL_VERSION,
        )
        if prior == "settled":
            continue

        ts_event = pred.ts_event
        ts_horizon = ts_event + pred.horizon_ns
        close_t1 = market_data_source(pred.symbol, ts_event, ts_event)
        close_t2 = market_data_source(pred.symbol, ts_event, ts_horizon)

        if close_t1 is None or close_t2 is None or close_t1 == 0 or close_t2 == 0:
            if prior == "pending_data":
                continue
            record = _build_pending_data_record(pred)
        else:
            record = _build_settled_record(
                pred, now_ns=now_ns, close_t1=close_t1, close_t2=close_t2
            )

        store.append(record, now_ns=now_ns)
        appended.append(record)
    return appended


__all__ = ["tick", "tick_sync"]
