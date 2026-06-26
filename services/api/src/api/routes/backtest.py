"""
api.routes.backtest - run backtests + browse persisted reports.

Endpoints:

  POST /backtest/run
      Run a backtest synchronously and return the resulting report.
      Body schema mirrors :func:`backtester.runner.run_backtest`'s args:
          bars_path           Parquet relative to repo root (or absolute)
          strategy            registered key from STRATEGY_REGISTRY
          strategy_params     dict of strategy constructor kwargs
          starting_cash       USD
          freq                "1m" | "5m" | "15m" | "1h" | "1d"
          venue, asset_class  enum tags stamped on bars

      Response: {run_id, manifest, report}.  Persisted on disk.

  GET /backtest/runs
      List every persisted run's manifest, newest first.

  GET /backtest/runs/{run_id}
      Return one run's full report (equity curve + trades) plus
      manifest.  404 if unknown.

The runner is invoked synchronously from inside FastAPI's request
handler.  Backtests on the synthetic 15k-bar fixture finish in <1s; for
larger runs we'll move to a background queue (TASK follow-up).  Until
then the endpoint contract is the same so the dashboard doesn't have to
change when async lands.
"""

from __future__ import annotations

import asyncio
import pathlib
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.approved_roots import ApprovedRoots, get_approved_roots
from api.auth import require_user
from backtester.report import report_to_dict
from backtester.runner import (
    REPORTS_ROOT,
    list_run_manifests,
    load_run_report,
    run_backtest,
)
from backtester.strategies import STRATEGY_REGISTRY
from fincept_core.schemas import AssetClass, Venue

router = APIRouter()

# Single-run lock: a backtester run loads the entire parquet into memory
# and runs the engine on the request thread.  Concurrent runs would
# fight for memory + CPU and the on-disk persisted run dirs would be
# fine but the user experience would be confusing (which run am I
# watching?).  An asyncio.Lock keeps this simple; the dashboard polls
# /runs to see when a run completes.
_RUN_LOCK = asyncio.Lock()


class RunBacktestRequest(BaseModel):
    """JSON body for ``POST /backtest/run``.

    All fields except ``bars_path`` and ``strategy`` are optional with
    sensible defaults so the dashboard can submit a minimal payload.
    """

    bars_path: str = Field(
        ...,
        description="Parquet path with columns symbol, ts_event, OHLCV. "
        "Relative paths are resolved against the API's working directory.",
    )
    strategy: str = Field(..., description="Registered strategy key.")
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    starting_cash: float = Field(100_000.0, ge=0)
    freq: str = "1m"
    venue: str = Venue.PAPER.value
    asset_class: str = AssetClass.CRYPTO_SPOT.value


@router.get("/strategies")
async def list_strategies(
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return the registered strategy keys + a short hint per key.

    Used by the dashboard's run form to populate the strategy dropdown
    without hardcoding the list on the frontend (single source of
    truth lives in :mod:`backtester.strategies`).
    """
    descriptions = {
        "buy_and_hold": "Open a long at the first bar per symbol; never trade again.",
        "position_tracker": "Track adopted/manual positions without submitting orders.",
        "ma_crossover": "SMA(fast) vs SMA(slow) crossover; long-only, no pyramiding.",
    }
    return {
        "strategies": [
            {
                "key": key,
                "class_name": cls.__name__,
                "strategy_id": getattr(cls, "strategy_id", key),
                "description": descriptions.get(key, ""),
            }
            for key, cls in sorted(STRATEGY_REGISTRY.items())
        ]
    }


@router.post("/run")
async def post_run(
    body: RunBacktestRequest,
    _: dict[str, Any] = Depends(require_user),
    approved_roots: ApprovedRoots = Depends(get_approved_roots),
) -> dict[str, Any]:
    """Run a backtest and return the resulting report (persisted)."""
    # Approved-root gate: layered on top of the existing existence /
    # traversal checks below.  Runs before anything touches the
    # filesystem so a probing caller learns nothing about on-disk
    # layout beyond the approved-roots verdict.  The resolved absolute
    # path is intentionally not logged on success.  An
    # ``ApprovedRootsError`` propagates to the shared exception handler
    # registered in ``api.main`` which renders the uniform 422 body
    # ``{"detail": ..., "code": "approved_roots_violation"}``.
    resolved = approved_roots.resolve(body.bars_path)

    # Use the symlink-resolved absolute path from the gate downstream
    # (closes the TOCTOU window where a symlink could be swapped
    # between the check and the file read).
    bars_path = resolved.path
    if not bars_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"bars_path does not exist: {bars_path}",
        )
    if body.strategy not in STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown strategy {body.strategy!r}; "
                f"valid: {sorted(STRATEGY_REGISTRY)}"
            ),
        )
    try:
        venue = Venue(body.venue)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        asset_class = AssetClass(body.asset_class)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if _RUN_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="another backtest is already running; try again shortly",
        )
    async with _RUN_LOCK:
        try:
            result = await run_backtest(
                parquet_path=bars_path,
                strategy_name=body.strategy,
                strategy_params=body.strategy_params,
                starting_cash=Decimal(str(body.starting_cash)),
                venue=venue,
                asset_class=asset_class,
                freq=body.freq,
                persist=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "run_id": result.run_id,
        "manifest": result.manifest,
        "report": report_to_dict(result.report),
    }


@router.get("/runs")
async def get_runs(
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """List every persisted run's manifest, newest first."""
    manifests = list_run_manifests()
    return {
        "runs": manifests,
        "summary": {
            "count": len(manifests),
            "reports_root": str(REPORTS_ROOT),
        },
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return a single run's full report + manifest by run_id."""
    report = load_run_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    # Find the manifest in the listing - cheap because there are at
    # most a few hundred runs.
    manifest: dict[str, Any] | None = None
    for m in list_run_manifests():
        if m.get("run_id") == run_id:
            manifest = m
            break
    return {"run_id": run_id, "manifest": manifest, "report": report}
