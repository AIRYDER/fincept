"""
api.routes.news_impact — experimental news impact model bridge.

This route intentionally lives beside, not inside, the production /news feed.
It loads the isolated experiment in experiments/news-impact-model and exposes a
small demo API for scoring a news event against historical analog outcomes.

No trades are submitted and no signals are published.  This is a read-only
testing surface for evaluating whether the analog model is useful enough to
graduate into the main news pipeline later.
"""

from __future__ import annotations

from functools import lru_cache
import sys
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import require_user

ROOT = Path(__file__).resolve().parents[5]
EXPERIMENT_ROOT = ROOT / "experiments" / "news-impact-model"
EXPERIMENT_SRC = EXPERIMENT_ROOT / "src"
SAMPLE_DATA = EXPERIMENT_ROOT / "sample_data" / "historical_outcomes.jsonl"

if str(EXPERIMENT_SRC) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_SRC))

from news_impact_model.data import load_historical_outcomes  # noqa: E402
from news_impact_model.workbench import WorkbenchState  # noqa: E402

router = APIRouter()


class NewsImpactEventBody(BaseModel):
    event_id: str = Field("dashboard-demo", min_length=1, max_length=128)
    source: str = Field("manual", min_length=1, max_length=64)
    headline: str = Field(..., min_length=3, max_length=500)
    body: str = Field("", max_length=4000)
    symbols: list[str] = Field(default_factory=list, max_length=20)
    event_type: str = Field("general", min_length=1, max_length=64)
    language: str = Field("en", min_length=2, max_length=16)
    available_at_ns: int | None = None


class NewsImpactContextBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    market_regime: str = Field("unknown", max_length=64)
    pre_event_return: float | None = None
    realized_volatility: float | None = None
    relative_volume: float | None = None
    spread_bps: float | None = None
    liquidity_score: float | None = None


class NewsImpactPredictBody(BaseModel):
    event: NewsImpactEventBody
    context: NewsImpactContextBody
    horizons: list[str] = Field(default_factory=lambda: ["5m", "30m", "1h"])
    top_k: int = Field(5, ge=1, le=20)
    weights: dict[str, float] | None = None


class NewsImpactOptimizeBody(BaseModel):
    horizon: str = Field("5m", min_length=1, max_length=16)
    mode: Literal["leave-one-out", "walk-forward"] = "leave-one-out"
    min_train_events: int = Field(4, ge=2, le=10000)
    top_k: int = Field(5, ge=1, le=20)


@router.get("/status")
async def status(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    state = _state()
    return cast(dict[str, Any], state.status()) | {
        "experiment_root": str(EXPERIMENT_ROOT),
        "sample_data": str(SAMPLE_DATA),
        "mode": "experimental_demo",
    }


@router.post("/predict")
async def predict(
    body: NewsImpactPredictBody,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if not body.context.symbol.strip():
        raise HTTPException(status_code=422, detail="context.symbol is required")
    payload = {
        "event": _event_payload(body.event),
        "context": body.context.model_dump(),
        "horizons": body.horizons,
        "top_k": body.top_k,
        "weights": body.weights,
    }
    try:
        prediction = _state().predict(
            event=payload["event"],
            context=payload["context"],
            horizons=tuple(body.horizons),
            top_k=body.top_k,
            weights=body.weights,
        )
    except Exception as exc:  # noqa: BLE001 - API boundary for experiment.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "prediction": prediction,
        "dataset_profile": _state().profile,
        "mode": "experimental_demo",
    }


@router.post("/optimize")
async def optimize(
    body: NewsImpactOptimizeBody,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        optimization = _state().optimize(
            horizon=body.horizon,
            mode=body.mode,
            min_train_events=body.min_train_events,
            top_k=body.top_k,
        )
    except Exception as exc:  # noqa: BLE001 - API boundary for experiment.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "optimization": optimization,
        "dataset_profile": _state().profile,
        "mode": "experimental_demo",
    }


@lru_cache(maxsize=1)
def _state() -> WorkbenchState:
    if not SAMPLE_DATA.exists():
        raise RuntimeError(f"sample dataset not found: {SAMPLE_DATA}")
    outcomes = load_historical_outcomes(SAMPLE_DATA)
    state = WorkbenchState()
    state.outcomes = outcomes
    state.dataset_path = SAMPLE_DATA
    state.profile = _profile(SAMPLE_DATA, outcomes)
    return state


def _profile(path: Path, outcomes: list[Any]) -> dict[str, Any]:
    horizons = sorted({h for outcome in outcomes for h in outcome.abnormal_returns})
    return {
        "path": str(path),
        "event_count": len(outcomes),
        "horizons": horizons,
        "sources": dict(sorted(_counts(o.source.lower() for o in outcomes).items())),
        "event_types": dict(sorted(_counts(o.event_type for o in outcomes).items())),
        "symbols": dict(
            sorted(_counts(symbol for o in outcomes for symbol in o.symbols).items())
        ),
    }


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return out


def _event_payload(body: NewsImpactEventBody) -> dict[str, Any]:
    payload = body.model_dump()
    payload["symbols"] = [s.strip().upper() for s in body.symbols if s.strip()]
    payload["available_at_ns"] = body.available_at_ns or 0
    return payload
