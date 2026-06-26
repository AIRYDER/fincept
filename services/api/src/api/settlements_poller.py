"""Settlements worker poller — bridges the new fincept_core.datasets.SettlementStore
to the production prediction log.

Reconciliation strategy
~~~~~~~~~~~~~~~~~~~~~~~

Two settlement systems coexist in this codebase while the new
``fincept_core.datasets`` spine is validated operationally:

* **New** — ``settlements.worker.tick`` writes to
  ``fincept_core.datasets.SettlementStore`` (on-disk JSONL under
  ``data/settlements/``), keyed by ``agent_id``, cost model
  ``v1.default`` = 5 bps fee + 3 bps spread + 0 bps slippage.  This
  store feeds the ``/models/{name}/outcomes`` API route.

* **Old** — ``quant_foundry.settlement_sweep.SettlementSweep`` (driven
  by ``gateway.run_settlement_sweep``) writes to
  ``quant_foundry.settlement.SettlementLedger``, keyed by ``model_id``,
  cost model ``cm-v1`` = 10 bps fee + 5 bps spread + 3 bps slippage.
  This ledger feeds the quant_foundry dashboard.

Both systems run side-by-side: the new worker reads the same
``data/predictions/`` log but writes to a separate store, so neither
ledger mutates the other.  Full consolidation — unifying the two
ledgers, cost models, and keying (``agent_id`` vs ``model_id``) — is
deferred to a future task pending operational validation of the new
spine.

The env var ``SETTLEMENTS_WORKER_POLL_S`` controls the poll interval
(default ``60`` seconds; set to ``0`` to disable the new worker while
keeping the old sweep running).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from typing import Any

from fincept_core.clock import now_ns as _now_ns
from fincept_core.logging import get_logger

log = get_logger(__name__)


def _settlements_worker_interval_seconds() -> float:
    """Read the poll interval from ``SETTLEMENTS_WORKER_POLL_S`` (default 60s)."""
    raw = os.environ.get("SETTLEMENTS_WORKER_POLL_S", "60")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def _build_market_data_source() -> Any:
    """Construct the async market_data_source from the production BarDataAdapter.

    Imported lazily so the api service does not pay the import cost when
    the poller is disabled (``SETTLEMENTS_WORKER_POLL_S=0``) and so test
    code can monkeypatch this function without importing quant_foundry.
    """
    from quant_foundry.market_data_adapter import BarDataAdapter

    from settlements.market_data_bridge import make_async_market_data_source

    bar_adapter = BarDataAdapter()
    return make_async_market_data_source(bar_adapter)


async def _poll_settlements_worker(interval_seconds: float) -> None:
    """Periodically run the settlements worker to settle due predictions.

    Best-effort: any exception raised by ``tick`` (or the market-data
    source) is logged and swallowed so a stuck worker never crashes the
    API process.  The loop runs forever until cancelled by the lifespan
    shutdown handler.
    """
    from settlements.worker import tick

    predictions_dir = pathlib.Path(
        os.environ.get("PREDICTIONS_DIR", "data/predictions")
    )
    settlements_dir = pathlib.Path(
        os.environ.get("SETTLEMENTS_DIR", "data/settlements")
    )
    market_data_source = _build_market_data_source()

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            records = await tick(
                _now_ns(),
                predictions_dir=predictions_dir,
                settlements_dir=settlements_dir,
                market_data_source=market_data_source,
            )
            if records:
                log.info("settlements.worker.tick", settled=len(records))
        except Exception as exc:
            log.warning(
                "settlements.worker_poll_failed",
                error=f"{type(exc).__name__}: {exc}",
            )


__all__ = [
    "_build_market_data_source",
    "_poll_settlements_worker",
    "_settlements_worker_interval_seconds",
]
