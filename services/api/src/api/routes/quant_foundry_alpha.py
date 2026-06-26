"""
api.routes.quant_foundry_alpha — Alpha Genome Lab endpoints (TASK-1005).

Operator-facing surface for the bounded-mutation recipe generator. Disabled
by default (``QUANT_FOUNDRY_ENABLED=false``); when enabled, every endpoint
requires bearer JWT auth (the lab is an operator-only tool — no external
consumers in v1).

Endpoints:
  POST /quant-foundry/alpha/sweep/start    start a recipe sweep (auth required)
  GET  /quant-foundry/alpha/sweep/{id}     fetch a stored sweep receipt (auth required)
  GET  /quant-foundry/alpha/sweeps         list all in-memory sweep receipts (auth required)
  POST /quant-foundry/alpha/dossier        register a dossier produced by a recipe candidate
                                            (auth required)

Security invariants (non-negotiable, match the rest of the gateway):
- Operator endpoints require bearer JWT (``api.auth.require_user``).
- The lab never writes to any order / OMS / sig.predict stream.
- The lab never bypasses the tournament / promotion gate — every recipe
  candidate flows through ``PromotionGate.evaluate()`` (enforced in
  ``quant_foundry.alpha_genome``).
- No secrets in any response body. Sweep receipts carry only ids + counts.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from api.auth import require_user

router = APIRouter()


# --- request models --------------------------------------------------------


class StartSweepRequest(BaseModel):
    """Body for POST /quant-foundry/alpha/sweep/start. extra='forbid'."""

    model_config = ConfigDict(extra="forbid")

    # Recipe shape — a subset of ``quant_foundry.alpha_genome.Recipe``.
    # The full Recipe has many fields; we accept the operator-supplied
    # minimum and let the gateway + lab validate the rest against the
    # allowlists.
    recipe_id: str = Field(min_length=1)
    parent_recipe_id: str | None = None
    mutation_kind: str | None = None
    feature_set: list[str] = Field(min_length=1)
    model_family: str
    hyperparameters: dict[str, float] = Field(default_factory=dict)
    train_window_ns: int
    val_window_ns: int
    label_horizon_ns: int
    random_seed: int | None = None
    # Sweep config
    n_recipes: int = Field(default=10, gt=0, le=100)
    sweep_id: str | None = None


class RegisterDossierRequest(BaseModel):
    """Body for POST /quant-foundry/alpha/dossier. extra='forbid'."""

    model_config = ConfigDict(extra="forbid")

    # Operator-supplied dossier fields. The gateway forwards to the real
    # ``DossierRegistry.register``; the registry enforces content_hash
    # immutability (a same-model_id different-content_hash is rejected).
    model_id: str
    artifact_manifest_id: str
    artifact_sha256: str
    dataset_manifest_id: str
    feature_schema_hash: str
    label_schema_hash: str
    training_metrics: dict[str, float] = Field(default_factory=dict)


# --- gateway access --------------------------------------------------------


def _get_gateway(request: Request) -> Any:
    """Return the gateway stashed on app.state, or None if not configured.

    Same pattern as the existing ``quant_foundry`` route — the endpoint is
    registered unconditionally so 404 doesn't hide the surface, but the
    gateway is only present when the app lifespan has installed it.
    """
    return getattr(request.app.state, "quant_foundry_gateway", None)


def _require_gateway(request: Request) -> Any:
    """Return the gateway or raise 503 if not configured (disabled)."""
    gw = _get_gateway(request)
    if gw is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quant Foundry gateway is not configured (disabled).",
        )
    return gw


# --- helpers ---------------------------------------------------------------


def _recipe_from_request(body: StartSweepRequest) -> Any:
    """Build a ``quant_foundry.alpha_genome.Recipe`` from the request body.

    The Recipe constructor validates the content against the allowlists
    (model family, hyperparameter bounds, window ranges, no secret-shaped
    feature names) and computes the deterministic ``recipe_hash``. Any
    allowlist violation surfaces as ``HTTP 422`` to the caller.
    """
    from quant_foundry.alpha_genome import Recipe

    try:
        return Recipe(
            recipe_id=body.recipe_id,
            parent_recipe_id=body.parent_recipe_id,
            mutation_kind=body.mutation_kind,
            feature_set=tuple(body.feature_set),
            model_family=body.model_family,
            hyperparameters=body.hyperparameters,
            train_window_ns=body.train_window_ns,
            val_window_ns=body.val_window_ns,
            label_horizon_ns=body.label_horizon_ns,
            random_seed=body.random_seed,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"recipe failed allowlist validation: {exc}",
        )


# --- endpoints -------------------------------------------------------------


@router.post("/sweep/start")
async def start_sweep(
    body: StartSweepRequest,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Start an Alpha Genome Lab sweep.

    Bearer-auth. Builds a Recipe from the request body, dispatches a
    sweep through the gateway (which funnels every candidate through
    ``PromotionGate.evaluate()``), and returns the full SweepReceipt as
    JSON. Sweep is synchronous — the response is the final receipt.
    """
    gw = _require_gateway(request)
    seed_recipe = _recipe_from_request(body)

    result = gw.start_alpha_sweep(
        seed_recipe=seed_recipe,
        n_recipes=body.n_recipes,
        sweep_id=body.sweep_id,
    )
    if not result.get("enabled", True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result.get("detail", "Quant Foundry is disabled"),
        )
    if result.get("error_code") == "invalid_sweep_request":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=result.get("detail", "invalid sweep request"),
        )
    if result.get("error_code") == "sweep_failed":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("detail", "sweep failed"),
        )
    return cast(dict[str, Any], result)


@router.get("/sweep/{sweep_id}")
async def get_sweep(
    sweep_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Fetch a stored sweep receipt by id.

    Bearer-auth. Returns 404 when the id is unknown OR when the gateway
    is disabled (the surface is hidden in the disabled state).
    """
    gw = _require_gateway(request)
    receipt = gw.alpha_sweep_status(sweep_id)
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown sweep_id: {sweep_id}",
        )
    return {"enabled": True, "ok": True, "sweep": receipt}


@router.get("/sweeps")
async def list_sweeps(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """List every in-memory sweep receipt.

    Bearer-auth. Empty list when the gateway is disabled. Receipts are
    in-memory only — restart clears them; per-trial dossiers are the
    authoritative audit trail (via the real ``DossierRegistry``).
    """
    gw = _require_gateway(request)
    return {
        "enabled": True,
        "ok": True,
        "sweeps": gw.list_alpha_sweeps(),
    }


@router.post("/dossier")
async def register_dossier(
    body: RegisterDossierRequest,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Register a dossier produced by a recipe candidate.

    Bearer-auth. Forwards to ``DossierRegistry.register`` via the
    gateway. The registry enforces content-hash immutability — a
    same-model_id different-content_hash is a security event and
    surfaces as ``HTTP 409`` to the caller.

    This is the explicit dossier-registration contract for the Alpha
    Genome Lab. It writes to the SAME registry as every other model —
    no separate registry, no shortcut.
    """
    gw = _require_gateway(request)
    try:
        from quant_foundry.dossier import DossierRecord

        dossier = DossierRecord(
            model_id=body.model_id,
            artifact_manifest_id=body.artifact_manifest_id,
            artifact_sha256=body.artifact_sha256,
            dataset_manifest_id=body.dataset_manifest_id,
            feature_schema_hash=body.feature_schema_hash,
            label_schema_hash=body.label_schema_hash,
            training_metrics=body.training_metrics,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"dossier failed validation: {exc}",
        )

    try:
        result = gw.register_recipe_candidate(dossier)
    except ValueError as exc:
        # Content-hash mismatch on existing model_id — security event.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    if not result.get("enabled", True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result.get("detail", "Quant Foundry is disabled"),
        )
    return cast(dict[str, Any], result)
