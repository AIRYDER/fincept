from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from news_impact_model.analogs import AnalogScoringWeights, HistoricalAnalogIndex
from news_impact_model.data import load_historical_outcomes
from news_impact_model.model import NewsImpactModel
from news_impact_model.schema import (
    HistoricalOutcome,
    MarketContext,
    NewsEvent,
    NewsImpactPrediction,
)
from news_impact_model.training import (
    optimize_analog_weights,
    walk_forward_optimize_analog_weights,
)

HORIZON_ORDER = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1_440,
    "5d": 7_200,
}


@dataclass
class WorkbenchState:
    """Mutable in-memory state for the standalone local workbench."""

    outcomes: list[HistoricalOutcome] = field(default_factory=list)
    dataset_path: Path | None = None
    profile: dict[str, Any] | None = None
    optimized_weights: AnalogScoringWeights | None = None
    last_optimization: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        return {
            "app": "News Impact Workbench",
            "dataset_loaded": bool(self.outcomes),
            "profile": self.profile,
            "last_optimization": self.last_optimization,
        }

    def load_dataset(self, path: str | Path) -> dict[str, Any]:
        dataset_path = Path(path)
        outcomes = load_historical_outcomes(dataset_path)
        if not outcomes:
            raise ValueError("dataset has no historical outcomes")
        self.outcomes = outcomes
        self.dataset_path = dataset_path
        self.profile = _profile_outcomes(dataset_path, outcomes)
        self.optimized_weights = None
        self.last_optimization = None
        return self.profile

    def optimize(
        self,
        *,
        horizon: str,
        mode: str = "walk-forward",
        min_train_events: int = 250,
        top_k: int = 5,
    ) -> dict[str, Any]:
        self._require_dataset()
        if mode == "walk-forward":
            result = walk_forward_optimize_analog_weights(
                self.outcomes,
                horizon=horizon,
                min_train_events=min_train_events,
                top_k=top_k,
            )
            folds = [
                {
                    "target_event_id": fold.target_event_id,
                    "train_events": len(fold.train_event_ids),
                    "predicted": fold.predicted,
                    "actual": fold.actual,
                    "abs_error": fold.abs_error,
                    "direction_hit": fold.direction_hit,
                }
                for fold in result.evaluation.folds
            ]
        elif mode == "leave-one-out":
            result = optimize_analog_weights(
                self.outcomes,
                horizon=horizon,
                top_k=top_k,
            )
            folds = []
        else:
            raise ValueError("mode must be 'walk-forward' or 'leave-one-out'")

        self.optimized_weights = result.weights
        self.last_optimization = {
            "mode": mode,
            "horizon": horizon,
            "n_predictions": result.evaluation.n_predictions,
            "metrics": {
                "mae": _json_float(result.evaluation.mae),
                "directional_accuracy": result.evaluation.directional_accuracy,
            },
            "candidates_tested": result.candidates_tested,
            "weights": asdict(result.weights),
            "folds": folds,
        }
        return self.last_optimization

    def predict(
        self,
        *,
        event: dict[str, Any],
        context: dict[str, Any],
        horizons: tuple[str, ...],
        top_k: int = 5,
        weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        self._require_dataset()
        active_weights = (
            AnalogScoringWeights(**weights)
            if weights is not None
            else self.optimized_weights or AnalogScoringWeights()
        )
        index = HistoricalAnalogIndex(weights=active_weights)
        index.extend(self.outcomes)
        model = NewsImpactModel(index=index, horizons=horizons, top_k=top_k)
        prediction = model.predict(
            _news_event_from_payload(event),
            _market_context_from_payload(context),
        )
        return _prediction_to_dict(prediction)

    def _require_dataset(self) -> None:
        if not self.outcomes:
            raise ValueError("load a historical outcome dataset first")


def serve_workbench(
    *,
    host: str,
    port: int,
    state: WorkbenchState,
    static_dir: Path,
) -> ThreadingHTTPServer:
    handler = make_workbench_handler(state=state, static_dir=static_dir)
    return ThreadingHTTPServer((host, port), handler)


def make_workbench_handler(
    *,
    state: WorkbenchState,
    static_dir: Path,
) -> type[BaseHTTPRequestHandler]:
    static_root = static_dir.resolve()

    class WorkbenchRequestHandler(BaseHTTPRequestHandler):
        server_version = "NewsImpactWorkbench/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                self._send_json(state.status())
                return
            self._send_static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
                "/api/dataset/load": self._load_dataset,
                "/api/optimize": self._optimize,
                "/api/predict": self._predict,
            }
            route = routes.get(parsed.path)
            if route is None:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self._read_json()
                self._send_json(route(payload))
            except Exception as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _load_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
            path = payload.get("path")
            if not path:
                raise ValueError("path is required")
            return {"profile": state.load_dataset(str(path))}

        def _optimize(self, payload: dict[str, Any]) -> dict[str, Any]:
            horizon = str(payload.get("horizon") or "")
            if not horizon:
                raise ValueError("horizon is required")
            return {
                "optimization": state.optimize(
                    horizon=horizon,
                    mode=str(payload.get("mode") or "walk-forward"),
                    min_train_events=int(payload.get("min_train_events") or 250),
                    top_k=int(payload.get("top_k") or 5),
                )
            }

        def _predict(self, payload: dict[str, Any]) -> dict[str, Any]:
            horizons = tuple(str(item) for item in payload.get("horizons") or ())
            if not horizons:
                raise ValueError("at least one horizon is required")
            return {
                "prediction": state.predict(
                    event=dict(payload.get("event") or {}),
                    context=dict(payload.get("context") or {}),
                    horizons=horizons,
                    top_k=int(payload.get("top_k") or 5),
                    weights=payload.get("weights"),
                )
            }

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send_json(
            self,
            payload: dict[str, Any],
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, request_path: str) -> None:
            relative = "index.html" if request_path in ("", "/") else request_path[1:]
            relative = unquote(relative)
            target = (static_root / relative).resolve()
            if not str(target).startswith(str(static_root)) or not target.is_file():
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", _content_type(target))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return WorkbenchRequestHandler


def _profile_outcomes(path: Path, outcomes: list[HistoricalOutcome]) -> dict[str, Any]:
    horizons = sorted(
        {horizon for outcome in outcomes for horizon in outcome.abnormal_returns},
        key=lambda item: (HORIZON_ORDER.get(item, 1_000_000), item),
    )
    sources = Counter(outcome.source.lower() for outcome in outcomes)
    event_types = Counter(outcome.event_type for outcome in outcomes)
    symbols = Counter(symbol for outcome in outcomes for symbol in outcome.symbols)
    timestamps = [outcome.available_at_ns for outcome in outcomes]
    return {
        "path": str(path),
        "event_count": len(outcomes),
        "horizons": horizons,
        "sources": dict(sorted(sources.items())),
        "event_types": dict(sorted(event_types.items())),
        "symbols": dict(sorted(symbols.items())),
        "time_range_ns": {
            "start": min(timestamps),
            "end": max(timestamps),
        },
    }


def _news_event_from_payload(payload: dict[str, Any]) -> NewsEvent:
    return NewsEvent(
        event_id=str(payload.get("event_id") or "live-event"),
        available_at_ns=int(payload.get("available_at_ns") or 0),
        source=str(payload.get("source") or "manual"),
        headline=str(payload.get("headline") or ""),
        body=str(payload.get("body") or ""),
        symbols=_parse_symbols(payload.get("symbols")),
        event_type=str(payload.get("event_type") or "general"),
        language=str(payload.get("language") or "en"),
    )


def _market_context_from_payload(payload: dict[str, Any]) -> MarketContext:
    return MarketContext(
        symbol=str(payload.get("symbol") or ""),
        market_regime=str(payload.get("market_regime") or "unknown"),
        pre_event_return=_optional_float(payload.get("pre_event_return")),
        realized_volatility=_optional_float(payload.get("realized_volatility")),
        relative_volume=_optional_float(payload.get("relative_volume")),
        spread_bps=_optional_float(payload.get("spread_bps")),
        liquidity_score=_optional_float(payload.get("liquidity_score")),
    )


def _parse_symbols(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        normalized = value.replace(";", "|").replace(",", "|")
        return tuple(symbol.strip() for symbol in normalized.split("|") if symbol.strip())
    return tuple(str(symbol).strip() for symbol in value if str(symbol).strip())


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _prediction_to_dict(prediction: NewsImpactPrediction) -> dict[str, Any]:
    return {
        "event_id": prediction.event_id,
        "symbol": prediction.symbol,
        "event_type": prediction.event_type,
        "horizons": {horizon: asdict(impact) for horizon, impact in prediction.horizons.items()},
        "volatility_impact": prediction.volatility_impact,
        "volume_impact": prediction.volume_impact,
        "confidence": prediction.confidence,
        "similar_events": [asdict(event) for event in prediction.similar_events],
        "model_version": prediction.model_version,
    }


def _json_float(value: float) -> float | None:
    return value if isfinite(value) else None


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
    }.get(suffix, "application/octet-stream")
