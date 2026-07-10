"""Settlements worker poller — settles due predictions via the canonical
Path B ledger (``quant_foundry.SettlementLedger``).

Settlement strategy
~~~~~~~~~~~~~~~~~~~

As of C6 settlement unification, the canonical settlement path is
**Path B** — ``quant_foundry.SettlementLedger`` via
``settlements.compat.PathACompatAdapter``. The adapter:

  * Accepts ``PredictionRow`` inputs (agent_id keyed).
  * Maps ``agent_id`` → ``model_id``.
  * Derives ``p_up`` from ``confidence``.
  * Delegates to ``SettlementLedger.settle()`` with the ``cm-v1`` cost model.
  * Writes to both the canonical store (``data/quant-foundry/settlements/``)
    and the legacy store (``data/settlements/``) for backward-compatible reads.

The legacy Path A math (``settlements.worker._build_settled_record``) is
deprecated. When ``SETTLEMENTS_USE_PATH_B=1`` (the default), the poller
uses the adapter. When ``SETTLEMENTS_USE_PATH_B=0``, the poller falls
back to the legacy Path A math for rollback.

The env var ``SETTLEMENTS_WORKER_POLL_S`` controls the poll interval
(default ``60`` seconds; set to ``0`` to disable the worker).
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


def _use_path_b() -> bool:
    """Check whether the poller should use Path B (canonical) settlement.

    Defaults to True (Path B is canonical). Set ``SETTLEMENTS_USE_PATH_B=0``
    to fall back to the legacy Path A math for rollback.
    """
    return os.environ.get("SETTLEMENTS_USE_PATH_B", "1") != "0"


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


def _build_compat_adapter() -> Any:
    """Construct the PathACompatAdapter with the canonical cost model.

    Imported lazily so the api service does not pay the import cost when
    the poller is disabled.
    """
    from fincept_core.datasets import SettlementStore
    from settlements.compat import PathACompatAdapter

    settlements_dir = pathlib.Path(
        os.environ.get("SETTLEMENTS_DIR", "data/settlements")
    )
    legacy_store = SettlementStore(root=settlements_dir)

    return PathACompatAdapter(legacy_store=legacy_store)


async def _poll_settlements_worker(interval_seconds: float) -> None:
    """Periodically settle due predictions via the canonical Path B ledger.

    Best-effort: any exception raised by the settlement path (or the
    market-data source) is logged and swallowed so a stuck worker never
    crashes the API process.  The loop runs forever until cancelled by
    the lifespan shutdown handler.
    """
    predictions_dir = pathlib.Path(
        os.environ.get("PREDICTIONS_DIR", "data/predictions")
    )
    market_data_source = _build_market_data_source()

    use_path_b = _use_path_b()
    adapter = _build_compat_adapter() if use_path_b else None

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            if use_path_b and adapter is not None:
                records = await adapter.settle_due_predictions_async(
                    predictions_dir,
                    now_ns=_now_ns(),
                    market_data_source=market_data_source,
                )
                if records:
                    log.info(
                        "settlements.compat.settle",
                        settled=len(records),
                        path="B",
                    )
            else:
                from settlements.worker import tick

                settlements_dir = pathlib.Path(
                    os.environ.get("SETTLEMENTS_DIR", "data/settlements")
                )
                records = await tick(
                    _now_ns(),
                    predictions_dir=predictions_dir,
                    settlements_dir=settlements_dir,
                    market_data_source=market_data_source,
                )
                if records:
                    log.info(
                        "settlements.worker.tick",
                        settled=len(records),
                        path="A_legacy",
                    )
        except Exception as exc:
            log.warning(
                "settlements.worker_poll_failed",
                error=f"{type(exc).__name__}: {exc}",
            )


__all__ = [
    "_build_compat_adapter",
    "_build_market_data_source",
    "_poll_settlements_worker",
    "_settlements_worker_interval_seconds",
    "_use_path_b",
]
