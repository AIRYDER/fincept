"""
api.routes.models - per-model status and CV provenance.

Walks ``MODELS_DIR`` (env override; default ``models/``) for any subdir
containing a ``meta.json`` and returns a normalized record per model:

  - basic identity:   name, path, exists, trained_at_unix, age_seconds
  - training mode:    eval_mode (walk_forward | holdout_80_20)
  - inference inputs: features, feature_count, horizon_bars, horizon_ns
  - quality:          cv_summary (mean/std/min/max AUC + median best_iter)
                      OR holdout AUC for legacy single-split models
  - warnings:         non-fatal issues so the dashboard can show a yellow
                      badge without breaking (missing model.txt, malformed
                      meta.json, etc.)

This endpoint is the dashboard's read-side of the GBM walk-forward CV
work landed in TASK-023.  The /risk page surfaces the records as model
cards with stability badges.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from fincept_core.datasets import (
    ApprovedRoots,
    SettlementStore,
    build_evidence_receipt,
    default_approved_roots,
)
from fincept_core.prediction_log import PredictionLog

from api.auth import require_user
from api.feature_importance import compute_feature_importance
from api.promotions import (
    PromotionError,
    get_promotion_store,
)
from api.training import (
    TrainingRequest,
    TrainingValidationError,
    get_store,
)


def _get_prediction_log() -> PredictionLog:
    """Return a :class:`PredictionLog` rooted at ``$PREDICTIONS_DIR``.

    A fresh instance per request is fine -- the constructor is cheap
    (just stores the directory path) and there is no in-memory state
    to share.  Tests monkey-patch this function to inject a fixture
    directory.
    """
    return PredictionLog()


def _get_approved_roots() -> ApprovedRoots:
    """Return the process-default :class:`ApprovedRoots`.

    Called fresh per request so an env-var change between requests is
    honored without a reload.  Tests monkey-patch this function to
    inject a fixture-approved-roots instance with extra dev roots.
    """
    return default_approved_roots()


def _get_settlement_store() -> SettlementStore:
    """Return a :class:`SettlementStore` rooted at ``$SETTLEMENTS_DIR``.

    A fresh instance per request is fine -- the constructor is cheap
    (just stores the directory path) and there is no in-memory state
    to share.  Tests monkey-patch this function to inject a fixture
    directory.
    """
    return SettlementStore()


# Default agent_id when the dashboard hits /models/promote/* without
# specifying one.  Today only ``gbm_predictor.v1`` has a model-backed
# inference loop; other agents (sentiment, regime) are LLM-/rules-driven.
_DEFAULT_AGENT_ID = "gbm_predictor.v1"

router = APIRouter()

# Root directory containing model subdirs.  Each subdir is expected to
# have ``meta.json`` (required) + ``model.txt`` (LightGBM, optional -
# the meta is enough to classify the model as 'trained but missing
# binary').
_MODELS_DIR = pathlib.Path(os.environ.get("MODELS_DIR", "models"))
_NEWS_ALPHA_CANDIDATE_REPORT = pathlib.Path(
    os.environ.get(
        "NEWS_ALPHA_CANDIDATE_REPORT",
        "reports/news_alpha_candidate_report.json",
    )
)


def _safe_read_meta(
    meta_path: pathlib.Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read meta.json, returning (meta, warning_message_or_None).

    Any IO / parse error becomes a non-fatal warning so the endpoint
    keeps reporting the rest of the models even if one is broken.
    """
    try:
        return json.loads(meta_path.read_text()), None
    except FileNotFoundError:
        return None, "meta.json missing"
    except json.JSONDecodeError as exc:
        return None, f"meta.json malformed: {exc.msg}"
    except OSError as exc:
        return None, f"meta.json read failed: {exc}"


def _normalize_training_request(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    model_name = data.get("model_name")
    input_path = data.get("input_path")
    if not isinstance(model_name, str) or not isinstance(input_path, str):
        return None
    normalized: dict[str, Any] = {
        "model_name": model_name,
        "input_path": input_path,
    }
    for key in (
        "horizon_bars",
        "bar_seconds",
        "cv_folds",
        "purge_bars",
        "embargo_bars",
        "num_boost_round",
        "early_stopping_rounds",
    ):
        value = data.get(key)
        if not isinstance(value, int):
            return None
        normalized[key] = int(value)
    return normalized


def _latest_training_requests_by_model() -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    runs_dir = pathlib.Path(os.environ.get("TRAINING_RUNS_DIR", "data/training_runs"))
    if not runs_dir.is_dir():
        return latest
    try:
        paths = sorted(
            runs_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return latest
    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed":
            continue
        request = _normalize_training_request(payload.get("request"))
        if request is None:
            continue
        model_name = request["model_name"]
        if model_name not in latest:
            latest[model_name] = request
    return latest


def _attach_training_request(
    record: dict[str, Any],
    latest_requests: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if record.get("training_request") is not None:
        return record
    name = record.get("name")
    request = latest_requests.get(name) if isinstance(name, str) else None
    if request is not None:
        record["training_request"] = request
        record["training_input_path"] = request.get("input_path")
    return record


def _build_record(model_dir: pathlib.Path, *, now: float) -> dict[str, Any]:
    """Normalize a single model dir into the API response shape.

    All fields are optional - the dashboard expects a stable schema but
    will gracefully render missing values.  Warnings flag operational
    issues without removing the entry from the listing.
    """
    name = model_dir.name
    meta_path = model_dir / "meta.json"
    model_path = model_dir / "model.txt"

    record: dict[str, Any] = {
        "name": name,
        "path": str(model_dir),
        "model_file_exists": model_path.is_file(),
        "trained_at_unix": None,
        "age_seconds": None,
        "eval_mode": None,
        "horizon_bars": None,
        "horizon_ns": None,
        "bar_seconds": None,
        "features": [],
        "feature_count": 0,
        "cv_summary": None,
        "cv_folds": None,
        "purge_bars": None,
        "embargo_bars": None,
        "final_train_rows": None,
        "final_num_boost_round": None,
        "holdout_auc": None,
        "holdout_rows": None,
        "training_input_path": None,
        "training_request": None,
        "warnings": [],
    }

    meta, warning = _safe_read_meta(meta_path)
    if warning is not None:
        record["warnings"].append(warning)
    if not record["model_file_exists"]:
        record["warnings"].append("model.txt missing")
    if meta is None:
        return record

    trained_at = meta.get("trained_at")
    if isinstance(trained_at, (int, float)):
        record["trained_at_unix"] = int(trained_at)
        record["age_seconds"] = max(0, int(now - float(trained_at)))

    eval_mode = meta.get("eval_mode")
    if isinstance(eval_mode, str):
        record["eval_mode"] = eval_mode

    training_input_path = meta.get("training_input_path")
    if isinstance(training_input_path, str):
        record["training_input_path"] = training_input_path
    training_request = _normalize_training_request(meta.get("training_request"))
    if training_request is not None:
        record["training_request"] = training_request
        record["training_input_path"] = training_request["input_path"]

    horizon_bars = meta.get("horizon_bars")
    if isinstance(horizon_bars, int):
        record["horizon_bars"] = horizon_bars
    horizon_ns = meta.get("horizon_ns")
    if isinstance(horizon_ns, int):
        record["horizon_ns"] = horizon_ns

    features = meta.get("features")
    if isinstance(features, list) and all(isinstance(f, str) for f in features):
        record["features"] = features
        record["feature_count"] = len(features)

    cv_summary = meta.get("cv_summary")
    if isinstance(cv_summary, dict):
        # Pass through but coerce numeric fields so the JSON payload is
        # always typed sensibly even if the trainer skipped a stat.
        normalized: dict[str, Any] = {}
        for key in ("n_folds", "n_scored", "n_skipped", "median_best_iter"):
            v = cv_summary.get(key)
            normalized[key] = int(v) if isinstance(v, (int, float)) else None
        for key in ("mean_auc", "std_auc", "min_auc", "max_auc"):
            v = cv_summary.get(key)
            normalized[key] = float(v) if isinstance(v, (int, float)) else None
        record["cv_summary"] = normalized

    # cv_folds is the per-fold breakdown that backs the summary.  We
    # only surface it on the detail endpoint (not the listing) to keep
    # the listing payload bounded — but it's cheap to normalize here.
    cv_folds = meta.get("cv_folds")
    if isinstance(cv_folds, list):
        normalized_folds: list[dict[str, Any]] = []
        for fold in cv_folds:
            if not isinstance(fold, dict):
                continue
            row: dict[str, Any] = {
                "fold": int(fold["fold"])
                if isinstance(fold.get("fold"), int)
                else None,
                "train_rows": (
                    int(fold["train_rows"])
                    if isinstance(fold.get("train_rows"), int)
                    else None
                ),
                "val_rows": (
                    int(fold["val_rows"])
                    if isinstance(fold.get("val_rows"), int)
                    else None
                ),
                "best_iter": (
                    int(fold["best_iter"])
                    if isinstance(fold.get("best_iter"), int)
                    else None
                ),
                "best_auc": (
                    float(fold["best_auc"])
                    if isinstance(fold.get("best_auc"), (int, float))
                    else None
                ),
                "reason_skipped": (
                    fold["reason_skipped"]
                    if isinstance(fold.get("reason_skipped"), str)
                    else None
                ),
            }
            normalized_folds.append(row)
        record["cv_folds"] = normalized_folds

    # Pass through training-config fields so the UI can show them on
    # the detail panel (helps reproduce a run).
    for key in (
        "purge_bars",
        "embargo_bars",
        "final_train_rows",
        "final_num_boost_round",
        "bar_seconds",
    ):
        v = meta.get(key)
        if isinstance(v, int):
            record[key] = v

    # Legacy 80/20 holdout exposes best_auc + val_rows directly.
    holdout_auc = meta.get("best_auc")
    if isinstance(holdout_auc, (int, float)):
        record["holdout_auc"] = float(holdout_auc)
    holdout_rows = meta.get("val_rows")
    if isinstance(holdout_rows, int):
        record["holdout_rows"] = holdout_rows

    return record


def list_models(root: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Return all model records under ``root`` (default ``MODELS_DIR``).

    Public helper so tests + future tooling can call without spinning
    up the FastAPI app.  Subdirs without a ``meta.json`` are skipped
    silently (they're not models, just other artifacts).
    """
    base = root or _MODELS_DIR
    if not base.exists():
        return []
    records: list[dict[str, Any]] = []
    now = time.time()
    latest_requests = _latest_training_requests_by_model()
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "meta.json").exists():
            # Not a model directory; skip without a warning.
            continue
        records.append(
            _attach_training_request(_build_record(entry, now=now), latest_requests)
        )
    return records


@router.get("")
async def get_models(
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return one record per model directory under ``MODELS_DIR``.

    Response shape::

        {
          "models": [...],
          "summary": {
            "count": int,
            "with_cv": int,         # walk_forward eval_mode count
            "with_holdout": int,    # legacy holdout_80_20 count
            "with_warnings": int,   # at least one warning entry
            "models_dir": str
          }
        }
    """
    models = list_models()
    with_cv = sum(1 for m in models if m.get("eval_mode") == "walk_forward")
    with_holdout = sum(1 for m in models if m.get("eval_mode") == "holdout_80_20")
    with_warnings = sum(1 for m in models if m.get("warnings"))
    return {
        "models": models,
        "summary": {
            "count": len(models),
            "with_cv": with_cv,
            "with_holdout": with_holdout,
            "with_warnings": with_warnings,
            "models_dir": str(_MODELS_DIR),
        },
    }


@router.get("/news-alpha/candidate-report")
async def get_news_alpha_candidate_report(
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    path = _NEWS_ALPHA_CANDIDATE_REPORT
    if not path.is_file():
        return {
            "exists": False,
            "report_path": str(path),
            "report": None,
        }
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"candidate report malformed: {exc.msg}",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"candidate report read failed: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=500,
            detail="candidate report must be a JSON object",
        )
    return {
        "exists": True,
        "report_path": str(path),
        "report": payload,
    }


# --------------------------------------------------------------------------- #
# Training runs                                                              #
# --------------------------------------------------------------------------- #
#
# Order matters: these routes must be registered before ``/{name}`` or
# 'train' / 'runs' would be matched as candidate model names.


class TrainBody(BaseModel):
    """Request body for ``POST /models/train``.

    Defaults match ``agents.gbm_predictor.train``'s argparse defaults so
    "click Train, leave everything alone" produces the same model the
    CLI would.
    """

    model_name: str = Field(
        ..., description="Subdirectory under MODELS_DIR to write into."
    )
    input_path: str = Field(
        ...,
        description="Parquet file with FEATURES + 'close' columns.  "
        "Server-side path; the api process must be able to read it.",
    )
    horizon_bars: int = Field(15, gt=0, le=10_000)
    bar_seconds: int = Field(60, gt=0, le=86_400)
    cv_folds: int = Field(
        5,
        ge=0,
        le=50,
        description="0 = legacy 80/20 holdout; otherwise walk-forward CV.",
    )
    purge_bars: int = Field(
        -1,
        ge=-1,
        le=10_000,
        description="-1 (default) means 'use horizon_bars'.",
    )
    embargo_bars: int = Field(0, ge=0, le=10_000)
    num_boost_round: int = Field(500, gt=0, le=100_000)
    early_stopping_rounds: int = Field(30, gt=0, le=10_000)


@router.post("/train", status_code=202)
async def post_train(
    body: TrainBody,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Kick off a training subprocess and return the run record.

    Returns 202 (Accepted): the subprocess starts in the background; the
    UI polls ``GET /models/runs/{id}`` for status.

    Validation errors come back as 400; concurrency-cap rejection comes
    back as 429 so a "Train" double-click on the dashboard surfaces a
    clear message rather than 500.

    Approved-root enforcement (fail-closed): ``body.input_path`` is
    validated via :meth:`ApprovedRoots.resolve` before the training
    orchestrator sees it.  A violation returns 422 with
    ``{"detail": ..., "code": "approved_roots_violation"}``.  The gate
    cannot be disabled at runtime (no env-var bypass).
    """
    # Layer 1: non-empty string check (the Pydantic ``str`` type accepts
    # ``""``; we reject it explicitly here so the operator gets a clear
    # 422 rather than a confusing "not found" downstream).
    if not body.input_path:
        raise HTTPException(
            status_code=422,
            detail="input_path must be a non-empty string",
        )

    # Layer 2: approved-root gate (fail-closed).  We resolve before
    # constructing the TrainingRequest so the orchestrator never sees a
    # path that hasn't passed the gate.  The resolved absolute path is
    # NOT logged on success (per the plan's must-not-do).  An
    # ``ApprovedRootsError`` propagates to the shared exception handler
    # registered in ``api.main`` which renders the uniform 422 body
    # ``{"detail": ..., "code": "approved_roots_violation"}``.
    resolved = _get_approved_roots().resolve(body.input_path)

    req = TrainingRequest(
        model_name=body.model_name,
        input_path=str(resolved.path),
        horizon_bars=body.horizon_bars,
        bar_seconds=body.bar_seconds,
        cv_folds=body.cv_folds,
        purge_bars=body.purge_bars,
        embargo_bars=body.embargo_bars,
        num_boost_round=body.num_boost_round,
        early_stopping_rounds=body.early_stopping_rounds,
    )
    try:
        run = await get_store().start_run(req)
    except TrainingValidationError as exc:
        # Distinguish 'too many in flight' (429) from 'bad input' (400).
        msg = str(exc)
        status = 429 if "in flight" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    return run.to_payload()


@router.get("/runs")
async def get_runs(
    status: str | None = None,
    limit: int = 50,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """List training runs, newest first.

    ``status`` filters to a single state (queued/running/completed/failed).
    ``limit`` caps the response payload (1..200) so a long history
    doesn't blow up the dashboard JSON.
    """
    if limit <= 0 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be in [1, 200]")
    runs = get_store().list_runs()
    if status is not None:
        if status not in ("queued", "running", "completed", "failed"):
            raise HTTPException(status_code=400, detail=f"invalid status: {status}")
        runs = [r for r in runs if r.status == status]
    runs = runs[:limit]
    summary = {
        "count": len(runs),
        "running": sum(1 for r in runs if r.status == "running"),
        "queued": sum(1 for r in runs if r.status == "queued"),
        "completed": sum(1 for r in runs if r.status == "completed"),
        "failed": sum(1 for r in runs if r.status == "failed"),
    }
    return {
        "runs": [r.to_payload() for r in runs],
        "summary": summary,
    }


@router.get("/runs/{run_id}")
async def get_run_detail(
    run_id: str,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Single run detail with the tail of the log.

    Log tail length is bounded server-side (LOG_TAIL_LINES) - the full
    log lives on disk for a postmortem with ``cat``/``less``.
    """
    store = get_store()
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    log_tail = store.get_log_tail(run_id)
    return run.to_payload(log_tail=log_tail)


# --------------------------------------------------------------------------- #
# Promotion -- bind a trained model to an agent                              #
# --------------------------------------------------------------------------- #
#
# /promote/active and /promote/rollback share the single-segment shape
# with /{name}, so they must register *before* the catch-all or
# ``promote`` would be interpreted as a model name.


class PromoteBody(BaseModel):
    """Body for ``POST /models/{name}/promote``.

    ``agent_id`` is optional; we default to the only ML-backed agent
    that exists today (``gbm_predictor.v1``).  When more agents grow
    inference-style entry points, the dashboard will need to start
    sending an explicit value.
    """

    agent_id: str = Field(
        default=_DEFAULT_AGENT_ID,
        description="Agent whose active-model pointer to update.",
    )
    promoted_by: str = Field(
        default="operator",
        description="Free-text actor for the audit trail.",
    )


@router.get("/promote/active")
async def get_active_promotion(
    agent_id: str = _DEFAULT_AGENT_ID,
    history_limit: int = 10,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return the active + shadow model bindings + recent history.

    ``history_limit`` is bounded so a long-lived deployment doesn't
    spam the dashboard with thousands of past promotions.

    The shadow binding (Phase E1) is returned alongside the active one
    so the dashboard can render both with a single round-trip.
    ``shadow`` is ``null`` when no shadow is set, which is the
    default state.  Shadow events do not appear in ``history`` --
    that timeline is for the active pointer only.
    """
    if history_limit <= 0 or history_limit > 200:
        raise HTTPException(status_code=400, detail="history_limit must be in [1, 200]")
    store = get_promotion_store()
    try:
        active = store.get_active(agent_id)
        shadow = store.get_shadow(agent_id)
        history = store.get_history(agent_id, limit=history_limit)
    except PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "active": active.to_dict() if active else None,
        "shadow": shadow.to_dict() if shadow else None,
        "history": [h.to_dict() for h in history],
    }


@router.post("/promote/rollback")
async def post_rollback_promotion(
    body: PromoteBody,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Roll the active pointer back to the previous binding.

    Returns the new active state (which may be ``null`` when there
    was only one historical entry).  See ``PromotionStore.rollback``
    for the exact behaviour.
    """
    store = get_promotion_store()
    try:
        new_active = store.rollback(
            agent_id=body.agent_id, promoted_by=body.promoted_by
        )
    except PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    shadow = store.get_shadow(body.agent_id)
    return {
        "agent_id": body.agent_id,
        "active": new_active.to_dict() if new_active else None,
        "shadow": shadow.to_dict() if shadow else None,
        "history": [h.to_dict() for h in store.get_history(body.agent_id, limit=10)],
    }


@router.post("/promote/shadow/clear")
async def post_clear_shadow(
    body: PromoteBody,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Remove the shadow pointer for ``agent_id``.

    Idempotent: clearing an already-empty shadow returns
    ``cleared=False`` rather than 404, so the dashboard can wire a
    "Clear shadow" button without first reading state.

    Returns the active binding alongside so a single mutation refreshes
    the dashboard's promotion-state query in one round-trip.
    """
    store = get_promotion_store()
    try:
        cleared = store.clear_shadow(body.agent_id)
        active = store.get_active(body.agent_id)
    except PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "agent_id": body.agent_id,
        "cleared": cleared,
        "active": active.to_dict() if active else None,
        "shadow": None,
    }


# --------------------------------------------------------------------------- #
# Per-model detail (must come after the 'train' / 'runs' / 'promote' routes) #
# --------------------------------------------------------------------------- #


def _resolve_model_dir(name: str) -> pathlib.Path:
    """Validate ``name`` and return the model directory.

    Returns 404 if the directory is missing.  Defends against path
    traversal (``../`` etc.) by rejecting any name containing path
    separators or starting with a dot — model names are simple
    identifiers, never paths.
    """
    if "/" in name or "\\" in name or name.startswith(".") or name == "":
        raise HTTPException(status_code=400, detail=f"invalid model name: {name!r}")
    model_dir = _MODELS_DIR / name
    # Resolve symlinks before checking containment so a symlinked model
    # dir still works while a crafted ``..`` traversal is caught.
    try:
        resolved = model_dir.resolve(strict=False)
        root = _MODELS_DIR.resolve(strict=False)
    except OSError:
        raise HTTPException(
            status_code=404, detail=f"model not found: {name}"
        ) from None
    if root not in resolved.parents and resolved != root:
        raise HTTPException(status_code=400, detail=f"invalid model path: {name!r}")
    if not model_dir.is_dir() or not (model_dir / "meta.json").exists():
        raise HTTPException(status_code=404, detail=f"model not found: {name}")
    return model_dir


@router.get("/{name}")
async def get_model_detail(
    name: str,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return a single model record including ``cv_folds`` detail.

    Same shape as the entries in ``GET /models``, just guaranteed to
    include the per-fold breakdown when the model was trained with
    walk-forward CV.  404 if the model directory is missing or has no
    ``meta.json``.
    """
    model_dir = _resolve_model_dir(name)
    return _attach_training_request(
        _build_record(model_dir, now=time.time()),
        _latest_training_requests_by_model(),
    )


@router.get("/{name}/feature-importance")
async def get_feature_importance(
    name: str,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Per-feature importance for a single model.

    Uses :mod:`api.feature_importance` so the api service stays free
    of the heavy lightgbm runtime — see that module's docstring for
    the trade-offs (split-count importance only until a future trainer
    change adds a ``feature_importance.json`` sidecar with gain values).

    Response shape::

        {
          "model": "gbm_predictor",
          "importances": [
            {"feature": "ret_5m", "split_count": 312, "gain": null, "rank": 1},
            ...
          ],
          "importance_type": "split_count" | "gain_and_split",
          "source":          "model_text" | "sidecar",
          "warnings":        [...]
        }
    """
    model_dir = _resolve_model_dir(name)
    meta_raw, _warning = _safe_read_meta(model_dir / "meta.json")
    features: list[str] = []
    if meta_raw is not None:
        feats = meta_raw.get("features")
        if isinstance(feats, list) and all(isinstance(f, str) for f in feats):
            features = feats
    payload = compute_feature_importance(model_dir, features=features)
    return {"model": name, **payload}


@router.get("/{name}/predictions")
async def get_predictions(
    name: str,
    agent_id: str = _DEFAULT_AGENT_ID,
    limit: int = 100,
    since_ns: int | None = None,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Recent predictions emitted by ``agent_id`` while ``name`` was active.

    The JSONL log is appended by the agent's publish loop (Phase D2);
    this endpoint reads the tail filtered to one model so the dashboard
    can show a "what's the model doing right now" tail without joining
    against the active-pointer history.

    Limits:
      * ``limit``    -- 1..1000 (max chosen so a misbehaving query
                        can't read an unbounded JSONL into memory).
      * ``since_ns`` -- optional, drop rows with ``ts_recorded`` older
                        than this nanosecond timestamp.

    Response::

        {
          "model": "gbm_predictor",
          "agent_id": "gbm_predictor.v1",
          "count": 42,
          "predictions": [{...}, ...]
        }
    """
    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=400,
            detail="limit must be between 1 and 1000",
        )
    log = _get_prediction_log()
    rows = log.read(
        agent_id=agent_id,
        model_name=name,
        limit=limit,
        since_ns=since_ns,
    )
    return {
        "model": name,
        "agent_id": agent_id,
        "count": len(rows),
        "predictions": [
            {
                "id": r.id,
                "ts_recorded": r.ts_recorded,
                "ts_event": r.ts_event,
                "horizon_ns": r.horizon_ns,
                "symbol": r.symbol,
                "direction": r.direction,
                "confidence": r.confidence,
            }
            for r in rows
        ],
    }


@router.get("/{name}/prediction-stats")
async def get_prediction_stats(
    name: str,
    agent_id: str = _DEFAULT_AGENT_ID,
    since_ns: int | None = None,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Summary of recent predictions for the dashboard's KPI tiles.

    Returns count, mean confidence, and direction distribution over
    the optional ``since_ns`` window.  Hit-rate / Brier-score will land
    in Phase E once the settlement worker writes a settlements log.

    Response::

        {
          "model":    "gbm_predictor",
          "agent_id": "gbm_predictor.v1",
          "stats": {
            "count":           42,
            "mean_confidence": 0.41,
            "long_count":      22,
            "short_count":     19,
            "flat_count":      1
          }
        }
    """
    log = _get_prediction_log()
    stats = log.stats(agent_id=agent_id, model_name=name, since_ns=since_ns)
    return {
        "model": name,
        "agent_id": agent_id,
        "stats": stats.to_dict(),
    }


@router.get("/{name}/outcomes")
async def get_outcomes(
    name: str,
    agent_id: str = _DEFAULT_AGENT_ID,
    limit: int = 200,
    since_ns: int | None = None,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Joined prediction + settlement outcomes for ``name``.

    Reads the prediction log (filtered by ``agent_id`` + ``model_name``)
    and left-joins each row with the settlement side-store by
    ``prediction_id``.  Predictions whose horizon has not yet elapsed
    (or whose settlement worker has not caught up) are returned with
    ``settlement_status: "pending_time"`` -- they are NOT silently
    dropped, so the dashboard can show "awaiting outcome" rows.

    The join is computed per-request (no caching) so a freshly-written
    settlement is visible on the next poll without a cache invalidation
    signal.  Raw feature snapshots are intentionally excluded from the
    response to keep the payload bounded.

    Limits:
      * ``limit``    -- 1..1000 (caps the prediction-log read).
      * ``since_ns`` -- optional, drop predictions with ``ts_recorded``
                        older than this nanosecond timestamp.

    Response::

        {
          "model": "gbm_predictor",
          "agent_id": "gbm_predictor.v1",
          "count": 3,
          "outcomes": [
            {
              "prediction_id": "...",
              "agent_id": "...",
              "model_name": "...",
              "ts_event": 123,
              "horizon_ns": 900000000000,
              "symbol": "BTC-USD",
              "direction": 0.5,
              "confidence": 0.5,
              "settlement_status": "settled",
              "realized_return_gross": 0.01,
              "realized_return_net": 0.0002,
              "settled_at_ns": 456,
              "brier_component": 0.25
            },
            ...
          ]
        }
    """
    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=400,
            detail="limit must be between 1 and 1000",
        )

    log = _get_prediction_log()
    predictions = log.read(
        agent_id=agent_id,
        model_name=name,
        limit=limit,
        since_ns=since_ns,
    )

    # Load settlements for this agent and index by prediction_id.
    # read_for_agent tolerates a missing file (returns []) and skips
    # malformed JSONL lines, so a corrupt settlement log never takes
    # the route down.
    settlements = _get_settlement_store().read_for_agent(agent_id=agent_id)
    settlement_by_pid: dict[str, Any] = {s.prediction_id: s for s in settlements}

    outcomes = [
        build_evidence_receipt(
            prediction=pred,
            settlement=settlement_by_pid.get(pred.id),
            feature_snapshot=None,
        )
        for pred in predictions
    ]

    return {
        "model": name,
        "agent_id": agent_id,
        "count": len(outcomes),
        "outcomes": outcomes,
    }


@router.post("/{name}/promote")
async def post_promote(
    name: str,
    body: PromoteBody,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Bind ``name`` as the active model for ``body.agent_id``.

    The pointer is written immediately.  As of Phase D1, the running
    ``gbm_predictor`` agent polls ``models/active/<agent_id>.json``
    every ~30s and hot-reloads on change, so promotion takes effect
    without a service restart.  The ``restart_required`` field in the
    response is kept for backward-compatibility with older dashboard
    builds; new clients can ignore it.

    Validates the model name twice (once via ``_resolve_model_dir`` for
    path-traversal defence, once inside ``PromotionStore.promote`` for
    artifact existence) so a missing ``model.txt`` is rejected before
    the active pointer is changed.
    """
    # Path-traversal + identity check.  The model_dir result is unused
    # here; we just want the same 400/404 behaviour the detail route
    # offers, before falling through to the promotion-side validation
    # (which adds artifact existence checks).
    _resolve_model_dir(name)
    try:
        binding = get_promotion_store().promote(
            agent_id=body.agent_id,
            model_name=name,
            promoted_by=body.promoted_by,
        )
    except PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "agent_id": body.agent_id,
        "active": binding.to_dict(),
        # Legacy field; agents hot-reload now (Phase D1) so a manual
        # restart is no longer required.  Kept ``True`` so older
        # dashboard builds still render their "promoted" toast.
        "restart_required": True,
    }


@router.post("/{name}/shadow")
async def post_shadow(
    name: str,
    body: PromoteBody,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Bind ``name`` as the shadow model for ``body.agent_id`` (Phase E1).

    Shadow models run in parallel with the active model; their
    predictions are recorded but NOT consumed by the orchestrator, so
    operators can validate a candidate against live features before
    swapping it into production.

    Validation matches :meth:`PromotionStore.set_shadow`:

      * Path-traversal + identity check (same 400/404 behaviour the
        detail route offers).
      * ``model.txt`` and ``meta.json`` must exist on disk.
      * Refuses to set the same model that's currently active --
        the resulting "compare against itself" would be tautological.

    Returns ``{ active, shadow }`` so the dashboard's promotion-state
    query refreshes in one round-trip.
    """
    _resolve_model_dir(name)
    try:
        store = get_promotion_store()
        shadow = store.set_shadow(
            agent_id=body.agent_id,
            model_name=name,
            promoted_by=body.promoted_by,
        )
        active = store.get_active(body.agent_id)
    except PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "agent_id": body.agent_id,
        "active": active.to_dict() if active else None,
        "shadow": shadow.to_dict(),
    }
