"""
api.routes.quant_foundry — Quant Foundry gateway endpoints (TASK-0306).

Exposes the Quant Foundry gateway over HTTP in local_mock mode. Disabled by
default (`QUANT_FOUNDRY_ENABLED=false`); operator endpoints require bearer
auth; the callback endpoint uses HMAC headers (NOT bearer).

Endpoints:
  POST /quant-foundry/jobs                    create a job (auth required)
  GET  /quant-foundry/jobs                    list jobs (auth required)
  GET  /quant-foundry/jobs/{job_id}           job detail (auth required)
  POST /quant-foundry/callbacks/runpod        callback endpoint (HMAC auth)
  GET  /quant-foundry/health                  health state (auth required)
  GET  /quant-foundry/heartbeats              worker heartbeats (auth required)

Security invariants (non-negotiable):
- Operator endpoints require bearer JWT (api.auth.require_user).
- Callback endpoint does NOT use bearer; it uses HMAC headers
  (X-QF-Job-Id, X-QF-Timestamp, X-QF-Signature) verified via
  quant_foundry.signatures.verify_callback. Missing/bad signature -> 401/400.
- No order stream / sig.predict writes. The route has no bus producer.
- Disabled state returns safe responses (no job creation, no secrets).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from api.auth import require_user
from quant_foundry.dossier import DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outbox import JobStatus

router = APIRouter()


# --- request models ---------------------------------------------------------


class CreateJobRequest(BaseModel):
    """Body for POST /quant-foundry/jobs. extra='forbid' for safety."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    job_type: str
    idempotency_key: str
    request_payload: Any
    priority: int = 0
    budget_cents: int | None = None


# --- gateway access ---------------------------------------------------------


def _get_gateway(request: Request) -> QuantFoundryGateway | None:
    """Return the gateway stashed on app.state, or None if not configured.

    The route is registered unconditionally (so 404 doesn't hide the surface),
    but the gateway is only present when the app lifespan or a test fixture
    has installed it. When absent, endpoints return a safe disabled state.
    """
    return getattr(request.app.state, "quant_foundry_gateway", None)


def _require_gateway(request: Request) -> QuantFoundryGateway:
    """Return the gateway or raise 503 if not configured (disabled)."""
    gw = _get_gateway(request)
    if gw is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quant Foundry gateway is not configured (disabled).",
        )
    return gw


# --- operator endpoints (bearer auth) ---------------------------------------


@router.post("/jobs")
async def create_job(
    body: CreateJobRequest,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    gw = _require_gateway(request)
    result = gw.create_job(
        job_id=body.job_id,
        job_type=body.job_type,
        idempotency_key=body.idempotency_key,
        request_payload=body.request_payload,
        priority=body.priority,
        budget_cents=body.budget_cents,
    )
    # Budget guard rejections (fail-closed): the job was NOT enqueued.
    if result.get("error_code") == "budget_exceeded":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=result.get("detail", "monthly budget exceeded"),
        )
    if result.get("error_code") == "budget_kill_switch":
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=result.get("detail", "budget kill switch is active"),
        )
    return result


@router.get("/jobs")
async def list_jobs(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    gw = _require_gateway(request)
    job_status: JobStatus | None = None
    if status_filter is not None:
        try:
            job_status = JobStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid status filter: {status_filter}",
            )
    return gw.list_jobs(status=job_status)


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    gw = _require_gateway(request)
    rec = gw.get_job(job_id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown job_id: {job_id}",
        )
    return rec


@router.get("/dossiers")
async def list_dossiers(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    gw = _require_gateway(request)
    dossier_status: DossierStatus | None = None
    if status_filter is not None:
        try:
            dossier_status = DossierStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid status filter: {status_filter}",
            )
    return gw.list_dossiers(status=dossier_status)


@router.get("/dossiers/{model_id}")
async def get_dossier(
    model_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    gw = _require_gateway(request)
    rec = gw.get_dossier(model_id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown model_id: {model_id}",
        )
    return rec


@router.get("/tournament/leaderboard")
async def tournament_leaderboard(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    gw = _require_gateway(request)
    return gw.tournament_leaderboard()


@router.get("/promotion/queue")
async def promotion_queue(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    gw = _require_gateway(request)
    return gw.pending_promotions()


@router.get("/promotion/completed")
async def promotion_completed(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    gw = _require_gateway(request)
    return gw.completed_promotions()


# --- Tournament wiring (Agent B) -------------------------------------------


@router.get("/tournament/status")
async def tournament_status(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    gw = _require_gateway(request)
    return gw.tournament_status()


@router.get("/shadow/health")
async def shadow_health(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Aggregate read-only health for the shadow inference surface.

    Bearer-auth (TASK-0604). Returns a JSON-safe dict with documented
    keys. Returns 503 when the gateway is absent (default disabled state).
    When the gateway is present but the shadow ledger is empty, returns
    zero counts + null metrics (never crash). Never returns secrets or
    raw callback payloads.
    """
    gw = _require_gateway(request)
    return gw.shadow_health()


@router.get("/health")
async def health(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    gw = _get_gateway(request)
    if gw is None:
        return {"enabled": False, "mode": "local_mock", "detail": "not configured"}
    return gw.health()


@router.get("/heartbeats")
async def heartbeats(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    gw = _require_gateway(request)
    return gw.heartbeats()


# --- callback endpoint (HMAC auth, NOT bearer) ------------------------------


@router.post("/callbacks/runpod")
async def receive_callback(
    request: Request,
) -> dict[str, Any]:
    """Receive a signed callback from an external worker.

    Auth is via HMAC headers (X-QF-Job-Id, X-QF-Timestamp, X-QF-Signature),
    NOT bearer. Missing headers -> 401. Bad signature -> 401. Unknown job
    -> 404. The gateway verifies the signature, records the callback in the
    inbox, and processes it (fail-closed on bad signature).
    """
    gw = _require_gateway(request)
    job_id = request.headers.get("X-QF-Job-Id")
    ts_str = request.headers.get("X-QF-Timestamp")
    signature = request.headers.get("X-QF-Signature")

    if not job_id or not ts_str or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing HMAC headers (X-QF-Job-Id, X-QF-Timestamp, X-QF-Signature)",
        )
    try:
        ts = int(ts_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-QF-Timestamp must be an integer (unix seconds)",
        )

    payload = await request.body()
    receipt = gw.receive_callback(
        job_id=job_id, payload=payload, signature=signature, ts=ts,
    )

    if not receipt.get("enabled", True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quant Foundry is disabled",
        )
    if receipt.get("error_code") == "unknown_job":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=receipt.get("detail", "unknown job"),
        )
    if receipt.get("error_code") == "bad_signature":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="callback signature verification failed",
        )
    if receipt.get("error_code") == "payload_hash_mismatch":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=receipt.get("detail", "payload hash mismatch (security event)"),
        )
    return receipt


# --- Settlement wiring (Agent A) ---


@router.get("/settlement/status")
async def settlement_status(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return the current settlement state (settled / pending / total).

    Bearer-auth. Returns 503 when the gateway is absent (default disabled
    state). Never returns secrets or raw prediction payloads.
    """
    gw = _require_gateway(request)
    return gw.settlement_status()


# --- Shadow dispatch wiring (Agent C) ---


@router.post("/shadow/dispatch")
async def shadow_dispatch(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Manually trigger a shadow inference dispatch batch.

    Bearer-auth. Dispatches inference jobs for SHADOW_APPROVED models via
    ``dispatch_shadow_inference_batch``. Returns the dispatch receipt with
    dispatched / skipped counts and job_ids. Returns 503 when the gateway
    is absent (default disabled state). Never returns secrets.
    """
    gw = _require_gateway(request)
    return gw.dispatch_shadow_inference_batch()


@router.get("/shadow/dispatch-status")
async def shadow_dispatch_status(
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return the current shadow dispatch loop status.

    Bearer-auth. Returns the cumulative dispatch count, last dispatch
    timestamp (ns), and enabled flag. Returns 503 when the gateway is
    absent (default disabled state). Never returns secrets.
    """
    gw = _require_gateway(request)
    return gw.shadow_dispatch_status


# --- Promotion POST endpoints (Agent B) ---


class SubmitPromotionRequest(BaseModel):
    """Body for POST /quant-foundry/promotion/submit."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    target_level: str
    review_note: str = ""


class ApprovePromotionRequest(BaseModel):
    """Body for POST /quant-foundry/promotion/approve."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    review_note: str = ""


class RejectPromotionRequest(BaseModel):
    """Body for POST /quant-foundry/promotion/reject."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    review_note: str = ""
    rejection_reason: str | None = None


@router.post("/promotion/submit")
async def submit_promotion(
    body: SubmitPromotionRequest,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Submit a model for promotion review.

    Bearer-auth. Builds evidence from dossier + tournament + sentinel
    and submits to the review queue. Returns the pending entry.
    Advisory-only — does not promote.
    """
    gw = _require_gateway(request)
    result = gw.submit_promotion(
        model_id=body.model_id,
        target_level=body.target_level,
        review_note=body.review_note,
    )
    if not result.get("enabled", True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quant Foundry is disabled",
        )
    if result.get("error_code") == "no_dossier":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.get("detail", "no dossier found"),
        )
    if result.get("error_code") == "invalid_target_level":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=result.get("detail", "invalid target level"),
        )
    return result


@router.post("/promotion/approve")
async def approve_promotion(
    body: ApprovePromotionRequest,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Approve a pending promotion request.

    Bearer-auth. Processes the pending entry through the promotion gate.
    The gate fails closed — missing evidence or blocking issues result
    in REJECTED, not APPROVED. Returns the promotion receipt.
    """
    gw = _require_gateway(request)
    result = gw.process_promotion(
        model_id=body.model_id,
        approve=True,
        review_note=body.review_note,
    )
    if not result.get("enabled", True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quant Foundry is disabled",
        )
    if result.get("error_code") == "no_pending_request":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.get("detail", "no pending promotion request"),
        )
    return result


@router.post("/promotion/reject")
async def reject_promotion(
    body: RejectPromotionRequest,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Reject a pending promotion request.

    Bearer-auth. Rejects the pending entry with a reason. Returns the
    promotion receipt.
    """
    gw = _require_gateway(request)
    result = gw.process_promotion(
        model_id=body.model_id,
        approve=False,
        review_note=body.review_note,
        rejection_reason=body.rejection_reason,
    )
    if not result.get("enabled", True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quant Foundry is disabled",
        )
    if result.get("error_code") == "no_pending_request":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.get("detail", "no pending promotion request"),
        )
    if result.get("error_code") == "invalid_rejection_reason":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=result.get("detail", "invalid rejection reason"),
        )
    return result
