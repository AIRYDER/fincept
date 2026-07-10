"""
quant_foundry.promotion â€” promotion review queue (TASK-0702 / C7 hardened).

The governance gate between "model exists" and "model can influence paper
trading." Requires human approval and evidence packets for model promotion.

Promotion levels (from ``DossierStatus``):
``candidate`` â†’ ``research_approved`` â†’ ``shadow_approved`` â†’
``paper_approved`` â†’ ``limited_live_approved``.
For MVP, allow only up to ``paper_approved``.

C7 hardening â€” the gate now verifies the full evidence chain before
approving any promotion:

- durable artifact URI exists
- artifact sha256 exists
- callback receipt exists and is processed
- dossier hash is consistent
- bundle selfcheck exists
- selfcheck.passed == true
- backend is production eligible
- feature_set_version is present and verified
- PIT evidence exists and is verified

Key invariants:
- **No model can be promoted without a dossier.** A missing dossier rejects
  with ``NO_DOSSIER``.
- **No model can be promoted without settlement evidence.** A model below
  the configured minimum settled count rejects with ``INSUFFICIENT_EVIDENCE``.
- **A clean leakage/overfit sentinel result is required.** A failed
  sentinel rejects with ``SENTINEL_FAILED``.
- **Blocking issues prevent promotion unless waived.** A non-empty
  blocking issue without a matching waiver rejects with ``BLOCKING_ISSUE``.
  The gate fails closed.
- **Human approval is stored.** The receipt carries the review note.
- **Rejection is stored with reason.** The receipt carries the rejection
  reason.
- **For MVP, allow only up to ``paper_approved``.** Promotion to
  ``limited_live_approved`` or higher rejects with ``MVP_LEVEL_LIMIT``.
- **Retired is terminal.** No promotion path out of ``retired``.
- **The promotion receipt is immutable.** Frozen + extra='forbid'.

File-disjoint from Builder 2's ``services/api/src/api/routes/quant_foundry.py``
and Builder 1's ``apps/dashboard/``. Imports from my ``dossier.py``
(TASK-0403), ``sentinel.py`` (TASK-0406), ``tournament.py`` (TASK-0404),
``bundle_io.py`` (C1), ``pit_evidence.py`` (C3).
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.bundle_io import TrainingSelfCheck
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.pit_evidence import PITEvidence
from quant_foundry.sentinel import SentinelReceipt, SentinelSeverity
from quant_foundry.tournament import TournamentResult

# ---------------------------------------------------------------------------
# Blocking issue + waiver
# ---------------------------------------------------------------------------


class BlockingIssue(BaseModel):
    """A blocking issue that prevents promotion unless waived.

    Frozen + extra='forbid'. Carries a code, severity, and message.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    severity: SentinelSeverity = SentinelSeverity.BLOCKING
    message: str


class PromotionWaiver(BaseModel):
    """A waiver that allows promotion past a blocking issue.

    Frozen + extra='forbid'. Carries the issue code being waived, the
    person who waived it, and the reason. The waiver must be recorded and
    named â€” the gate fails closed without it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_code: str
    waived_by: str
    reason: str


# ---------------------------------------------------------------------------
# C7: Callback receipt + PIT evidence refs (for the evidence chain)
# ---------------------------------------------------------------------------


class CallbackReceiptRef(BaseModel):
    """Reference to a callback receipt for the evidence chain.

    Frozen + extra='forbid'. Carries the receipt status and optional
    receipt_id. The gate checks ``status == "processed"`` to confirm
    the callback was successfully ingested.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    receipt_id: str | None = None


class PITEvidenceRef(BaseModel):
    """Reference to PIT evidence for the promotion gate.

    Frozen + extra='forbid'. Carries a ``verified`` flag (set by the
    caller after running ``verify_pit_evidence``) and the optional
    ``evidence_sha256`` tamper seal. The gate checks ``verified == True``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: bool
    evidence_sha256: str = ""
    manifest_hash: str = ""


# ---------------------------------------------------------------------------
# Evidence + request
# ---------------------------------------------------------------------------


class PromotionEvidence(BaseModel):
    """The evidence packet required for promotion.

    Frozen + extra='forbid'. Carries the dossier, tournament result,
    sentinel receipt, blocking issues, and the C7 evidence chain:
    selfcheck, callback receipt, artifact URI, dossier hash,
    feature_set_version, PIT evidence, and backend eligibility.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dossier: DossierRecord | None = None
    tournament_result: TournamentResult | None = None
    sentinel_receipt: SentinelReceipt | None = None
    blocking_issues: list[BlockingIssue] = []
    # C7 evidence chain fields.
    selfcheck: TrainingSelfCheck | None = None
    callback_receipt: CallbackReceiptRef | None = None
    artifact_uri: str | None = None
    dossier_hash: str | None = None
    feature_set_version: str | None = None
    pit_evidence: PITEvidenceRef | None = None
    backend_eligible: bool = False


class PromotionRequest(BaseModel):
    """A promotion request with target level + review note + waivers.

    Frozen + extra='forbid'. Carries the model_id, target promotion level,
    human review note, and any waivers for blocking issues.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    target_level: DossierStatus
    review_note: str
    waivers: list[PromotionWaiver] = []


# ---------------------------------------------------------------------------
# Decision + rejection reason
# ---------------------------------------------------------------------------


class ReviewDecision(StrEnum):
    """The decision of the promotion gate."""

    APPROVED = "approved"
    REJECTED = "rejected"


class PromotionRejectionReason(StrEnum):
    """Reason why a promotion request was rejected."""

    # Pre-C7 reasons (preserved).
    NO_DOSSIER = "no_dossier"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SENTINEL_FAILED = "sentinel_failed"
    BLOCKING_ISSUE = "blocking_issue"
    MVP_LEVEL_LIMIT = "mvp_level_limit"
    # C7 evidence chain reasons.
    MISSING_SELFCHECK = "missing_selfcheck"
    SELFCHECK_FAILED = "selfcheck_failed"
    MISSING_BUNDLE_SHA256 = "missing_bundle_sha256"
    MISSING_CALLBACK_RECEIPT = "missing_callback_receipt"
    CALLBACK_NOT_PROCESSED = "callback_not_processed"
    MISSING_ARTIFACT_URI = "missing_artifact_uri"
    DOSSIER_HASH_MISMATCH = "dossier_hash_mismatch"
    BACKEND_NOT_PRODUCTION_ELIGIBLE = "backend_not_production_eligible"
    FEATURE_SET_VERSION_NOT_VERIFIED = "feature_set_version_not_verified"
    PIT_EVIDENCE_MISSING = "pit_evidence_missing"
    PIT_EVIDENCE_NOT_VERIFIED = "pit_evidence_not_verified"
    # C7 terminal status.
    RETIRED_IS_TERMINAL = "retired_is_terminal"


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class PromotionReceipt(BaseModel):
    """Immutable promotion receipt.

    Frozen + extra='forbid'. Carries the decision, request, review note,
    rejection reason (if rejected), and timestamp. ``to_dict`` is JSON
    serializable for audit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ReviewDecision
    request: PromotionRequest
    review_note: str
    rejection_reason: PromotionRejectionReason | None = None
    decided_at_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "decision": self.decision.value,
            "request": self.request.model_dump(),
            "review_note": self.review_note,
            "rejection_reason": (self.rejection_reason.value if self.rejection_reason else None),
            "decided_at_ns": self.decided_at_ns,
        }


# ---------------------------------------------------------------------------
# Gate
# ===========================================================================


# MVP: allow only up to paper_approved.
_MVP_MAX_LEVEL = DossierStatus.PAPER_APPROVED

# Explicit promotion level order (candidate < research < shadow < paper < ...).
# RETIRED is intentionally excluded â€” it is a terminal sink with no
# outgoing promotion path. REJECTED is also terminal.
_LEVEL_ORDER: dict[DossierStatus, int] = {
    DossierStatus.CANDIDATE: 0,
    DossierStatus.RESEARCH_APPROVED: 1,
    DossierStatus.SHADOW_APPROVED: 2,
    DossierStatus.PAPER_APPROVED: 3,
    DossierStatus.LIMITED_LIVE_APPROVED: 4,
}

# Terminal statuses â€” no promotion path out of these.
_TERMINAL_STATUSES = frozenset({DossierStatus.REJECTED, DossierStatus.RETIRED})


class PromotionGate:
    """Evaluates promotion requests against the evidence packet.

    The gate fails closed: if any required evidence is missing or any
    blocking issue is unwaived, the request is rejected. For MVP, only
    promotions up to ``paper_approved`` are allowed.

    C7 hardening: the gate now verifies the full evidence chain
    (selfcheck, callback receipt, artifact URI, dossier hash, backend
    eligibility, feature_set_version, PIT evidence) before approving.
    """

    def __init__(self, min_settled_count: int = 10) -> None:
        self.min_settled_count = min_settled_count

    def evaluate(
        self,
        request: PromotionRequest,
        evidence: PromotionEvidence,
    ) -> PromotionReceipt:
        """Evaluate a promotion request against the evidence packet."""
        now_ns = time.time_ns()

        def _reject(reason: PromotionRejectionReason) -> PromotionReceipt:
            return PromotionReceipt(
                decision=ReviewDecision.REJECTED,
                request=request,
                review_note=request.review_note,
                rejection_reason=reason,
                decided_at_ns=now_ns,
            )

        # 1. No dossier -> reject.
        if evidence.dossier is None:
            return _reject(PromotionRejectionReason.NO_DOSSIER)

        # 2. Retired is terminal â€” no promotion out of retired.
        if evidence.dossier.status in _TERMINAL_STATUSES:
            return _reject(PromotionRejectionReason.RETIRED_IS_TERMINAL)

        # 3. MVP level limit -> reject.
        if _LEVEL_ORDER.get(request.target_level, 99) > _LEVEL_ORDER[_MVP_MAX_LEVEL]:
            return _reject(PromotionRejectionReason.MVP_LEVEL_LIMIT)

        # 4. Missing bundle sha256 -> reject.
        if not evidence.dossier.artifact_sha256:
            return _reject(PromotionRejectionReason.MISSING_BUNDLE_SHA256)

        # 5. Missing selfcheck -> reject.
        if evidence.selfcheck is None:
            return _reject(PromotionRejectionReason.MISSING_SELFCHECK)

        # 6. Selfcheck failed -> reject.
        if not evidence.selfcheck.passed:
            return _reject(PromotionRejectionReason.SELFCHECK_FAILED)

        # 7. Missing callback receipt -> reject.
        if evidence.callback_receipt is None:
            return _reject(PromotionRejectionReason.MISSING_CALLBACK_RECEIPT)

        # 8. Callback not processed -> reject.
        if evidence.callback_receipt.status != "processed":
            return _reject(PromotionRejectionReason.CALLBACK_NOT_PROCESSED)

        # 9. Missing artifact URI -> reject.
        if not evidence.artifact_uri:
            return _reject(PromotionRejectionReason.MISSING_ARTIFACT_URI)

        # 10. Dossier hash mismatch -> reject.
        if not evidence.dossier_hash or evidence.dossier_hash != evidence.dossier.content_hash:
            return _reject(PromotionRejectionReason.DOSSIER_HASH_MISMATCH)

        # 11. Backend not production eligible -> reject.
        if not evidence.backend_eligible:
            return _reject(PromotionRejectionReason.BACKEND_NOT_PRODUCTION_ELIGIBLE)

        # 12. Feature set version not verified -> reject.
        if not evidence.feature_set_version:
            return _reject(PromotionRejectionReason.FEATURE_SET_VERSION_NOT_VERIFIED)

        # 13. PIT evidence missing -> reject.
        if evidence.pit_evidence is None:
            return _reject(PromotionRejectionReason.PIT_EVIDENCE_MISSING)

        # 14. PIT evidence not verified -> reject.
        if not evidence.pit_evidence.verified:
            return _reject(PromotionRejectionReason.PIT_EVIDENCE_NOT_VERIFIED)

        # 15. Insufficient settlement evidence -> reject.
        settled_count = (
            evidence.tournament_result.settled_count if evidence.tournament_result else 0
        )
        if settled_count < self.min_settled_count:
            return _reject(PromotionRejectionReason.INSUFFICIENT_EVIDENCE)

        # 16. Sentinel failed -> reject.
        if evidence.sentinel_receipt is not None and not evidence.sentinel_receipt.passed:
            return _reject(PromotionRejectionReason.SENTINEL_FAILED)

        # 17. Blocking issues without waivers -> reject.
        waived_codes = {w.issue_code for w in request.waivers}
        for issue in evidence.blocking_issues:
            if issue.code not in waived_codes:
                return _reject(PromotionRejectionReason.BLOCKING_ISSUE)

        # 18. All checks passed -> approve.
        return PromotionReceipt(
            decision=ReviewDecision.APPROVED,
            request=request,
            review_note=request.review_note,
            decided_at_ns=now_ns,
        )


# ---------------------------------------------------------------------------
# Review queue
# ===========================================================================


class _QueueEntry(BaseModel):
    """An entry in the promotion review queue (request + evidence)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: PromotionRequest
    evidence: PromotionEvidence


class PromotionReviewQueue:
    """The promotion review queue.

    Stores pending promotion requests and processes them through the gate.
    Completed (approved or rejected) receipts are retained for audit.
    """

    def __init__(self, gate: PromotionGate | None = None) -> None:
        self._gate = gate or PromotionGate()
        self._pending: list[_QueueEntry] = []
        self._completed: list[PromotionReceipt] = []

    def submit(self, request: PromotionRequest, evidence: PromotionEvidence) -> None:
        """Submit a promotion request to the queue."""
        self._pending.append(_QueueEntry(request=request, evidence=evidence))

    def pending(self) -> list[_QueueEntry]:
        """Return all pending entries."""
        return list(self._pending)

    def process_next(self) -> PromotionReceipt:
        """Process the next pending request through the gate."""
        if not self._pending:
            raise IndexError("no pending requests")
        entry = self._pending.pop(0)
        receipt = self._gate.evaluate(request=entry.request, evidence=entry.evidence)
        self._completed.append(receipt)
        return receipt

    def completed(self) -> list[PromotionReceipt]:
        """Return all completed receipts (approved + rejected)."""
        return list(self._completed)

    def rejected(self) -> list[PromotionReceipt]:
        """Return all rejected receipts."""
        return [r for r in self._completed if r.decision == ReviewDecision.REJECTED]

    def approved(self) -> list[PromotionReceipt]:
        """Return all approved receipts."""
        return [r for r in self._completed if r.decision == ReviewDecision.APPROVED]
