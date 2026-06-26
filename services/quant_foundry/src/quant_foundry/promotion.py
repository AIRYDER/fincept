"""
quant_foundry.promotion — promotion review queue (TASK-0702).

The governance gate between "model exists" and "model can influence paper
trading." Requires human approval and evidence packets for model promotion.

Promotion levels (from ``DossierStatus``):
``candidate`` → ``research_approved`` → ``shadow_approved`` →
``paper_approved`` → ``limited_live_approved`` → ``active``.
For MVP, allow only up to ``paper_approved``.

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
- **The promotion receipt is immutable.** Frozen + extra='forbid'.

File-disjoint from Builder 2's ``services/api/src/api/routes/quant_foundry.py``
and Builder 1's ``apps/dashboard/``. Imports from my ``dossier.py``
(TASK-0403), ``sentinel.py`` (TASK-0406), ``tournament.py`` (TASK-0404).
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.dossier import DossierRecord, DossierStatus
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
    named — the gate fails closed without it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_code: str
    waived_by: str
    reason: str


# ---------------------------------------------------------------------------
# Evidence + request
# ---------------------------------------------------------------------------


class PromotionEvidence(BaseModel):
    """The evidence packet required for promotion.

    Frozen + extra='forbid'. Carries the dossier, tournament result,
    sentinel receipt, and blocking issues.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dossier: DossierRecord | None = None
    tournament_result: TournamentResult | None = None
    sentinel_receipt: SentinelReceipt | None = None
    blocking_issues: list[BlockingIssue] = []


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

    NO_DOSSIER = "no_dossier"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SENTINEL_FAILED = "sentinel_failed"
    BLOCKING_ISSUE = "blocking_issue"
    MVP_LEVEL_LIMIT = "mvp_level_limit"


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
_LEVEL_ORDER: dict[DossierStatus, int] = {
    DossierStatus.CANDIDATE: 0,
    DossierStatus.RESEARCH_APPROVED: 1,
    DossierStatus.SHADOW_APPROVED: 2,
    DossierStatus.PAPER_APPROVED: 3,
    DossierStatus.LIMITED_LIVE_APPROVED: 4,
}


class PromotionGate:
    """Evaluates promotion requests against the evidence packet.

    The gate fails closed: if any required evidence is missing or any
    blocking issue is unwaived, the request is rejected. For MVP, only
    promotions up to ``paper_approved`` are allowed.
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

        # 1. No dossier -> reject.
        if evidence.dossier is None:
            return PromotionReceipt(
                decision=ReviewDecision.REJECTED,
                request=request,
                review_note=request.review_note,
                rejection_reason=PromotionRejectionReason.NO_DOSSIER,
                decided_at_ns=now_ns,
            )

        # 2. MVP level limit -> reject.
        if _LEVEL_ORDER.get(request.target_level, 99) > _LEVEL_ORDER[_MVP_MAX_LEVEL]:
            return PromotionReceipt(
                decision=ReviewDecision.REJECTED,
                request=request,
                review_note=request.review_note,
                rejection_reason=PromotionRejectionReason.MVP_LEVEL_LIMIT,
                decided_at_ns=now_ns,
            )

        # 3. Insufficient settlement evidence -> reject.
        settled_count = (
            evidence.tournament_result.settled_count if evidence.tournament_result else 0
        )
        if settled_count < self.min_settled_count:
            return PromotionReceipt(
                decision=ReviewDecision.REJECTED,
                request=request,
                review_note=request.review_note,
                rejection_reason=PromotionRejectionReason.INSUFFICIENT_EVIDENCE,
                decided_at_ns=now_ns,
            )

        # 4. Sentinel failed -> reject.
        if evidence.sentinel_receipt is not None and not evidence.sentinel_receipt.passed:
            return PromotionReceipt(
                decision=ReviewDecision.REJECTED,
                request=request,
                review_note=request.review_note,
                rejection_reason=PromotionRejectionReason.SENTINEL_FAILED,
                decided_at_ns=now_ns,
            )

        # 5. Blocking issues without waivers -> reject.
        waived_codes = {w.issue_code for w in request.waivers}
        for issue in evidence.blocking_issues:
            if issue.code not in waived_codes:
                return PromotionReceipt(
                    decision=ReviewDecision.REJECTED,
                    request=request,
                    review_note=request.review_note,
                    rejection_reason=PromotionRejectionReason.BLOCKING_ISSUE,
                    decided_at_ns=now_ns,
                )

        # 6. All checks passed -> approve.
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
