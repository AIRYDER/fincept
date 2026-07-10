"""
api.routes.strategies — strategy registry + config CRUD + lifecycle.

Two surfaces:

1. **Runtime view** (existing):

     GET /strategies
       Per-strategy_id summary derived from the portfolio service's
       Redis-backed PositionStore.  Tells the dashboard "which
       strategy_ids have published positions and how many".

2. **Config CRUD + lifecycle** (Phase F):

     POST   /strategies/configs                 Create
     GET    /strategies/configs                 List all configs
     GET    /strategies/configs/{id}            Read one config
     PATCH  /strategies/configs/{id}            Update fields (partial)
     DELETE /strategies/configs/{id}            Delete (history kept)
     POST   /strategies/configs/{id}/start      Set enabled=True
     POST   /strategies/configs/{id}/stop       Set enabled=False
     GET    /strategies/configs/{id}/history    Recent change history

The two surfaces use different stores (Redis position cache vs
filesystem-backed StrategyConfigStore) and different lifecycles
(positions are derived from fills; configs are operator-set).  We
keep them in one router module because they share the
``strategy_id`` namespace and an operator browsing strategies wants
to see both views in one place.

Auditing
~~~~~~~~

Every write through ``StrategyConfigStore`` appends a JSONL record
to ``strategies/<id>.history.jsonl``.  The routes don't add their
own audit calls because the store's audit is the single source of
truth -- routing a write through the store guarantees the record
is captured even if a future route forgets to log explicitly.

Validation
~~~~~~~~~~

  * ``strategy_id`` filesystem-safety -- enforced by the store
    (raises StrategyConfigError -> 400).
  * ``class_name`` is in STRATEGY_REGISTRY -- enforced here so a
    typo doesn't slip through to the host (which would also catch
    it but with a less actionable error message).
  * ``symbols`` non-empty -- enforced here.  An empty symbols list
    would create a strategy that the host runs but that never
    receives bar events -- silent waste of a runner slot.
"""

from __future__ import annotations

from typing import Any

from backtester.strategies import STRATEGY_REGISTRY
from fastapi import APIRouter, Body, Depends, HTTPException
from fincept_core.strategy_config import (
    StrategyConfig,
    StrategyConfigError,
    StrategyConfigStore,
)
from portfolio.store import PositionStore
from pydantic import BaseModel, Field

from api.auth import require_user
from api.deps import get_position_store, get_strategy_config_store

router = APIRouter()


# --------------------------------------------------------------------------- #
# Existing: runtime view from positions                                       #
# --------------------------------------------------------------------------- #


@router.get("")
async def list_runtime_strategies(
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
) -> list[dict[str, Any]]:
    """Return ``[{strategy_id, position_count, open_positions}, ...]``.

    Sorted by ``strategy_id``.  Derived from the portfolio service's
    Redis-backed position cache, NOT from the StrategyConfigStore --
    a strategy can have positions without a config (e.g., manual
    orders tagged with that strategy_id) and a config can exist
    without positions (it just hasn't traded yet).  This endpoint
    answers the operational question "what's actually trading".
    """
    out: list[dict[str, Any]] = []
    for strategy_id in sorted(await store.known_strategies()):
        positions = await store.get_all(strategy_id)
        out.append(
            {
                "strategy_id": strategy_id,
                "position_count": len(positions),
                "open_positions": sum(1 for p in positions.values() if p.quantity != 0),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Phase F: StrategyConfig CRUD + lifecycle                                    #
# --------------------------------------------------------------------------- #


class CreateStrategyConfigRequest(BaseModel):
    """Body for ``POST /strategies/configs``.

    All fields are required for a new config except
    ``model_binding`` (optional) and ``enabled`` (defaults False so a
    create-then-edit-then-start workflow is the default; an operator
    must explicitly set ``enabled=true`` to start the runner on
    creation).
    """

    strategy_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Operator-visible identifier; stamped on every "
        "OrderIntent.  Must be filesystem-safe.",
    )
    class_name: str = Field(
        ...,
        description=(
            "Registered strategy key from backtester.strategies."
            "STRATEGY_REGISTRY (e.g. buy_and_hold, ma_crossover, gbm)."
        ),
    )
    symbols: list[str] = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    model_binding: str | None = None
    enabled: bool = False


class UpdateStrategyConfigRequest(BaseModel):
    """Body for ``PATCH /strategies/configs/{id}``.

    Every field is optional; only the keys present in the body are
    updated.  ``strategy_id`` is NOT updatable -- it's the primary
    key in both the filesystem store and the live OrderIntent
    audit chain, so mutating it would orphan history.  Operators
    who want a different id should DELETE + POST.
    """

    class_name: str | None = None
    symbols: list[str] | None = Field(default=None, min_length=1)
    params: dict[str, Any] | None = None
    model_binding: str | None = None
    enabled: bool | None = None


def _validate_class_name(class_name: str) -> None:
    """Reject class_names not in the strategy registry.

    Catches typos at the API boundary so the host doesn't have to
    refuse-to-start with a less actionable error.  We don't keep a
    cached copy of the registry keys -- the registry is a module
    constant, lookup is O(1), and reading it directly means a future
    new strategy class is automatically accepted.
    """
    if class_name not in STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown class_name {class_name!r}; valid: {sorted(STRATEGY_REGISTRY)}"
            ),
        )


def _config_to_response(cfg: StrategyConfig) -> dict[str, Any]:
    """Serialise a StrategyConfig for an HTTP response.

    Centralised so a future schema change (added fields, changed
    serialisation) updates one place.
    """
    return cfg.to_dict()


# ------ Create ------------------------------------------------------------- #


@router.post("/configs", status_code=201)
async def create_strategy_config(
    body: CreateStrategyConfigRequest = Body(...),
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> dict[str, Any]:
    """Create a new StrategyConfig.

    Returns 409 if ``strategy_id`` already exists -- update is
    PATCH, not POST.  This is stricter than the store's idempotent
    upsert behaviour (the store would happily overwrite); the API
    layer is the place to enforce CRUD semantics so accidental
    re-POSTs don't silently clobber.
    """
    _validate_class_name(body.class_name)
    try:
        # Both ``get`` and ``upsert`` validate strategy_id on the
        # store side, so we wrap both in the same try/except; a
        # filesystem-unsafe id raised from ``get`` must become a
        # 400 just like it would from ``upsert``.
        existing = store.get(body.strategy_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"strategy {body.strategy_id!r} already exists",
            )
        sealed = store.upsert(
            StrategyConfig(
                strategy_id=body.strategy_id,
                class_name=body.class_name,
                symbols=list(body.symbols),
                params=dict(body.params),
                model_binding=body.model_binding,
                enabled=body.enabled,
                # ``upsert`` overwrites these with wall-clock now;
                # passing 0.0 here is just to satisfy the dataclass.
                created_at=0.0,
                updated_at=0.0,
            )
        )
    except StrategyConfigError as exc:
        # Bad strategy_id (filesystem-unsafe).  Store raises before
        # touching disk.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _config_to_response(sealed)


@router.post("/configs/{strategy_id}/adopt", status_code=201)
async def adopt_runtime_strategy(
    strategy_id: str,
    _: dict[str, Any] = Depends(require_user),
    config_store: StrategyConfigStore = Depends(get_strategy_config_store),
    position_store: PositionStore = Depends(get_position_store),
) -> dict[str, Any]:
    try:
        existing = config_store.get(strategy_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"strategy {strategy_id!r} already exists",
            )
        positions = await position_store.get_all(strategy_id)
        symbols = sorted(
            symbol for symbol, position in positions.items() if position.quantity != 0
        )
        if not symbols:
            raise HTTPException(
                status_code=404,
                detail=f"strategy {strategy_id!r} has no open positions to adopt",
            )
        sealed = config_store.upsert(
            StrategyConfig(
                strategy_id=strategy_id,
                class_name="position_tracker",
                symbols=symbols,
                params={},
                model_binding=None,
                enabled=False,
                created_at=0.0,
                updated_at=0.0,
            )
        )
    except StrategyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _config_to_response(sealed)


# ------ List + Read ------------------------------------------------------- #


@router.get("/configs")
async def list_strategy_configs(
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> list[dict[str, Any]]:
    """List all configured strategies, sorted by ``strategy_id``."""
    return [_config_to_response(cfg) for cfg in store.list_all()]


@router.get("/configs/{strategy_id}")
async def get_strategy_config(
    strategy_id: str,
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> dict[str, Any]:
    """Fetch one config by ``strategy_id``.  404 if not found."""
    try:
        cfg = store.get(strategy_id)
    except StrategyConfigError as exc:
        # Filesystem-unsafe id -- still a 400 because the request
        # itself is malformed (we never got to "look up").
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"strategy {strategy_id!r} not found",
        )
    return _config_to_response(cfg)


# ------ Update ------------------------------------------------------------ #


@router.patch("/configs/{strategy_id}")
async def patch_strategy_config(
    strategy_id: str,
    body: UpdateStrategyConfigRequest = Body(...),
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> dict[str, Any]:
    """Partially update an existing config.

    Only the fields present in the body are replaced; absent fields
    keep their stored values.  This is intentionally a PATCH (not
    a full PUT) because most operator changes are single-field
    (toggle enabled, swap model_binding) and a full body would
    invite race conditions where two parallel edits each replace
    the other's changes.

    ``strategy_id`` is NOT in the body -- it's the URL key.  Renaming
    a strategy requires DELETE + POST so the audit history doesn't
    awkwardly span two ids.
    """
    try:
        existing = store.get(strategy_id)
    except StrategyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"strategy {strategy_id!r} not found",
        )

    # Build the updated record by overlaying body fields on existing.
    # ``model_dump(exclude_unset=True)`` returns only the keys the
    # client explicitly sent, which is exactly the partial-update
    # semantics PATCH expects.
    patch = body.model_dump(exclude_unset=True)
    if "class_name" in patch:
        _validate_class_name(patch["class_name"])
    merged = StrategyConfig(
        strategy_id=existing.strategy_id,
        class_name=patch.get("class_name", existing.class_name),
        symbols=list(patch.get("symbols", existing.symbols)),
        params=dict(patch.get("params", existing.params)),
        model_binding=(
            patch["model_binding"]
            if "model_binding" in patch
            else existing.model_binding
        ),
        enabled=patch.get("enabled", existing.enabled),
        # Timestamps regenerated by upsert.
        created_at=existing.created_at,
        updated_at=existing.updated_at,
    )
    sealed = store.upsert(merged)
    return _config_to_response(sealed)


# ------ Delete ------------------------------------------------------------ #


@router.delete("/configs/{strategy_id}", status_code=204)
async def delete_strategy_config(
    strategy_id: str,
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> None:
    """Remove a config.  History is retained on disk.

    Returns 204 on success and 404 if the config didn't exist
    (rather than the store's idempotent True/False return) -- a
    delete-not-found is operationally interesting, the operator
    might be looking at a stale dashboard.
    """
    try:
        removed = store.delete(strategy_id)
    except StrategyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"strategy {strategy_id!r} not found",
        )
    return None


# ------ Lifecycle: start / stop ------------------------------------------- #


@router.post("/configs/{strategy_id}/start")
async def start_strategy(
    strategy_id: str,
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> dict[str, Any]:
    """Set ``enabled=True`` so the strategy host's supervisor will
    start a runner on its next reconcile tick.

    Idempotent: a start on an already-started strategy is a no-op
    (returns the current config without writing to disk or the
    audit log -- saves audit-log spam from a UI that pessimistically
    POSTs the current state).
    """
    try:
        sealed = store.set_enabled(strategy_id, enabled=True)
    except StrategyConfigError as exc:
        # set_enabled raises if the config doesn't exist.  Bubble
        # up as 404 because that's the more useful operator signal
        # than a generic 400 -- "create it first" rather than
        # "your request was malformed".
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _config_to_response(sealed)


@router.post("/configs/{strategy_id}/stop")
async def stop_strategy(
    strategy_id: str,
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> dict[str, Any]:
    """Set ``enabled=False`` so the host's supervisor cancels the
    runner on its next tick.

    Idempotent under the same logic as ``start``.

    Note: stopping does NOT cancel any in-flight orders or close
    open positions.  An operator who needs to flatten must do so
    via the OMS kill-switch / explicit close orders -- stopping a
    strategy just halts new bar dispatch.
    """
    try:
        sealed = store.set_enabled(strategy_id, enabled=False)
    except StrategyConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _config_to_response(sealed)


# ------ History ----------------------------------------------------------- #


@router.get("/configs/{strategy_id}/history")
async def get_strategy_history(
    strategy_id: str,
    limit: int = 50,
    _: dict[str, Any] = Depends(require_user),
    store: StrategyConfigStore = Depends(get_strategy_config_store),
) -> list[dict[str, Any]]:
    """Most-recent ``limit`` snapshots from the JSONL audit, newest first.

    Returns an empty list if the strategy has no history file
    (never been written) -- distinct from 404 because an audit
    request for a now-deleted strategy is still meaningful (the
    operator may want to see what was there before).

    ``limit`` clamped to [1, 500] to bound response size; a
    runaway-history situation (operator scripting accidental
    100k upserts) shouldn't OOM the API.
    """
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")
    if limit > 500:
        limit = 500
    try:
        history = store.get_history(strategy_id, limit=limit)
    except StrategyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_config_to_response(cfg) for cfg in history]
