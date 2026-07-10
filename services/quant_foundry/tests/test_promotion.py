"""
Tests for TASK-0702: Build Promotion Review Queue.

TDD red phase â€” these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `promotion.py` exists.

Acceptance criteria covered:
- No model can be promoted without a dossier.
- No model can be promoted without settlement evidence.
- Human approval is stored.
- Rejection is stored with reason.

Additional checks from the spec:
- Require: dossier, artifact hash, settlement evidence, tournament score,
  a clean leakage/overfit sentinel result (TASK-0406), an empty (or
  explicitly human-waived) blocking issues list, and a human review note.
- A non-empty blocking issue cannot be promoted past without a recorded,
  named waiver â€” the gate fails closed.
- Enforce a minimum settled-evidence bar server-side.
- Add rejection reasons.
- Add immutable promotion receipt.
- For MVP, allow only up to `paper_approved`.

File-disjoint from Builder 2's `services/api/src/api/routes/quant_foundry.py`
and Builder 1's `apps/dashboard/`. Imports from my `dossier.py` (TASK-0403),
`sentinel.py` (TASK-0406), `tournament.py` (TASK-0404).
"""

from __future__ import annotations

from typing import Any

import pytest
from quant_foundry.bundle_io import TrainingSelfCheck
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.promotion import (
    BlockingIssue,
    CallbackReceiptRef,
    PITEvidenceRef,
    PromotionEvidence,
    PromotionGate,
    PromotionReceipt,
    PromotionRejectionReason,
    PromotionRequest,
    PromotionReviewQueue,
    PromotionWaiver,
    ReviewDecision,
)
from quant_foundry.sentinel import SentinelReceipt, SentinelSeverity
from quant_foundry.tournament import (
    PromotionRecommendation,
    TournamentResult,
    TournamentStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dossier(
    model_id: str = "m1",
    status: DossierStatus = DossierStatus.CANDIDATE,
    artifact_sha256: str = "a" * 64,
) -> DossierRecord:
    """Build a minimal dossier for testing."""
    return DossierRecord(
        model_id=model_id,
        artifact_sha256=artifact_sha256,
        artifact_manifest_id="manifest-1",
        dataset_manifest_id="ds-1",
        code_git_sha="gitsha",
        lockfile_hash="lockhash",
        container_image_digest="digest",
        feature_schema_hash="f" * 64,
        label_schema_hash="l" * 64,
        status=status,
        trial_count=1,
    )


def _make_tournament_result(
    model_id: str = "m1",
    settled_count: int = 100,
    status: TournamentStatus = TournamentStatus.ELIGIBLE,
) -> TournamentResult:
    return TournamentResult(
        model_id=model_id,
        total_score=0.8,
        settled_count=settled_count,
        status=status,
        recommendation=PromotionRecommendation.PROMOTE,
    )


def _make_sentinel_receipt(
    model_id: str = "m1",
    passed: bool = True,
) -> SentinelReceipt:
    return SentinelReceipt(
        model_id=model_id,
        issues=[],
        passed=passed,
        checks_run=["shuffled_label"],
        ts_ns=1000,
    )


def _make_evidence(
    dossier: DossierRecord | None | str = "default",
    tournament_result: TournamentResult | None = None,
    sentinel_receipt: SentinelReceipt | None = None,
    blocking_issues: list[BlockingIssue] | None = None,
    selfcheck: TrainingSelfCheck | None = "default",
    callback_receipt: CallbackReceiptRef | None = "default",
    artifact_uri: str | None = "default",
    dossier_hash: str | None = "default",
    feature_set_version: str | None = "default",
    pit_evidence: PITEvidenceRef | None = "default",
    backend_eligible: bool = True,
) -> PromotionEvidence:
    """Build a complete C7 promotion evidence packet.

    Pass ``dossier=None`` to explicitly omit the dossier. Pass
    ``dossier="default"`` (the default) to use a default dossier.
    New C7 fields default to ``"default"`` sentinel which is replaced
    with a valid value; pass ``None`` to explicitly omit any field.
    """
    d = _make_dossier() if dossier == "default" else dossier
    return PromotionEvidence(
        dossier=d,
        tournament_result=tournament_result or _make_tournament_result(),
        sentinel_receipt=sentinel_receipt or _make_sentinel_receipt(),
        blocking_issues=blocking_issues or [],
        selfcheck=(
            TrainingSelfCheck(passed=True, bundle_sha256="a" * 64, n_rows_scored=10)
            if selfcheck == "default"
            else selfcheck
        ),
        callback_receipt=(
            CallbackReceiptRef(status="processed", receipt_id="cb-1")
            if callback_receipt == "default"
            else callback_receipt
        ),
        artifact_uri="file:///durable/artifact.zip" if artifact_uri == "default" else artifact_uri,
        dossier_hash=(d.content_hash if d is not None else "h" * 64) if dossier_hash == "default" else dossier_hash,
        feature_set_version="fs-v1" if feature_set_version == "default" else feature_set_version,
        pit_evidence=(
            PITEvidenceRef(verified=True, evidence_sha256="e" * 64, manifest_hash="m" * 64)
            if pit_evidence == "default"
            else pit_evidence
        ),
        backend_eligible=backend_eligible,
    )


# ---------------------------------------------------------------------------
# PromotionEvidence
# ===========================================================================


class TestPromotionEvidence:
    """The evidence packet required for promotion."""

    def test_evidence_has_required_fields(self) -> None:
        """Evidence has dossier, tournament_result, sentinel_receipt, blocking_issues."""
        ev = _make_evidence()
        assert ev.dossier is not None
        assert ev.tournament_result is not None
        assert ev.sentinel_receipt is not None
        assert isinstance(ev.blocking_issues, list)

    def test_evidence_is_frozen(self) -> None:
        """Evidence is frozen (immutable for audit)."""
        ev = _make_evidence()
        with pytest.raises((TypeError, ValueError)):
            ev.blocking_issues = []  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BlockingIssue + PromotionWaiver
# ===========================================================================


class TestBlockingIssue:
    """Blocking issues prevent promotion unless waived."""

    def test_blocking_issue_has_required_fields(self) -> None:
        """BlockingIssue has code, severity, message."""
        issue = BlockingIssue(
            code="low_settled_count",
            severity=SentinelSeverity.BLOCKING,
            message="only 10 settled predictions",
        )
        assert issue.code == "low_settled_count"
        assert issue.severity == SentinelSeverity.BLOCKING
        assert issue.message == "only 10 settled predictions"


class TestPromotionWaiver:
    """A waiver allows promotion past a blocking issue."""

    def test_waiver_has_required_fields(self) -> None:
        """Waiver has issue_code, waived_by, reason."""
        waiver = PromotionWaiver(
            issue_code="low_settled_count",
            waived_by="operator@example.com",
            reason="acceptable for shadow testing",
        )
        assert waiver.issue_code == "low_settled_count"
        assert waiver.waived_by == "operator@example.com"
        assert waiver.reason == "acceptable for shadow testing"


# ---------------------------------------------------------------------------
# PromotionRequest
# ===========================================================================


class TestPromotionRequest:
    """A promotion request with target level + review note + waivers."""

    def test_request_has_required_fields(self) -> None:
        """Request has model_id, target_level, review_note, waivers."""
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="looks good",
            waivers=[],
        )
        assert req.model_id == "m1"
        assert req.target_level == DossierStatus.SHADOW_APPROVED
        assert req.review_note == "looks good"

    def test_request_is_frozen(self) -> None:
        """Request is frozen."""
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        with pytest.raises((TypeError, ValueError)):
            req.review_note = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PromotionGate â€” no model can be promoted without a dossier
# ===========================================================================


class TestNoPromotionWithoutDossier:
    """No model can be promoted without a dossier."""

    def test_promotion_without_dossier_is_rejected(self) -> None:
        """Promotion without a dossier is rejected."""
        ev = _make_evidence(dossier=None)
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.NO_DOSSIER


# ---------------------------------------------------------------------------
# No model can be promoted without settlement evidence
# ===========================================================================


class TestNoPromotionWithoutSettlement:
    """No model can be promoted without settlement evidence."""

    def test_promotion_without_settlement_is_rejected(self) -> None:
        """Promotion with 0 settled predictions is rejected."""
        ev = _make_evidence(
            tournament_result=_make_tournament_result(settled_count=0),
        )
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate(min_settled_count=10)
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.INSUFFICIENT_EVIDENCE

    def test_promotion_below_min_settled_is_rejected(self) -> None:
        """Promotion below the minimum settled count is rejected."""
        ev = _make_evidence(
            tournament_result=_make_tournament_result(settled_count=5),
        )
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate(min_settled_count=10)
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Clean sentinel result required
# ===========================================================================


class TestCleanSentinelRequired:
    """A clean leakage/overfit sentinel result is required."""

    def test_promotion_with_failed_sentinel_is_rejected(self) -> None:
        """Promotion with a failed sentinel is rejected."""
        ev = _make_evidence(
            sentinel_receipt=_make_sentinel_receipt(passed=False),
        )
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.SENTINEL_FAILED


# ---------------------------------------------------------------------------
# Blocking issues + waivers
# ===========================================================================


class TestBlockingIssuesAndWaivers:
    """Blocking issues prevent promotion unless waived."""

    def test_blocking_issue_without_waiver_rejects(self) -> None:
        """A blocking issue without a waiver rejects promotion."""
        issue = BlockingIssue(
            code="low_calibration",
            severity=SentinelSeverity.BLOCKING,
            message="calibration below threshold",
        )
        ev = _make_evidence(blocking_issues=[issue])
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.BLOCKING_ISSUE

    def test_blocking_issue_with_waiver_passes(self) -> None:
        """A blocking issue with a waiver allows promotion."""
        issue = BlockingIssue(
            code="low_calibration",
            severity=SentinelSeverity.BLOCKING,
            message="calibration below threshold",
        )
        waiver = PromotionWaiver(
            issue_code="low_calibration",
            waived_by="operator@example.com",
            reason="acceptable for shadow testing",
        )
        ev = _make_evidence(blocking_issues=[issue])
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[waiver],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.APPROVED


# ---------------------------------------------------------------------------
# MVP: only up to paper-approved
# ===========================================================================


class TestMvpPromotionLimit:
    """For MVP, allow only up to `paper_approved`."""

    def test_promotion_to_shadow_approved_is_allowed(self) -> None:
        """Promotion to shadow_approved is allowed."""
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.APPROVED

    def test_promotion_to_paper_approved_succeeds_with_evidence(self) -> None:
        """Promotion to paper_approved is approved when evidence is complete."""
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.PAPER_APPROVED,
            review_note="ready for paper trading",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.APPROVED
        assert receipt.rejection_reason is None

    def test_promotion_to_limited_live_approved_is_rejected(self) -> None:
        """Promotion to limited_live_approved is rejected (MVP limit)."""
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.LIMITED_LIVE_APPROVED,
            review_note="attempting limited live pilot",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.MVP_LEVEL_LIMIT


# ---------------------------------------------------------------------------
# Human approval is stored
# ===========================================================================


class TestHumanApprovalStored:
    """Human approval is stored in the promotion receipt."""

    def test_receipt_includes_review_note(self) -> None:
        """The receipt includes the human review note."""
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="model looks good for shadow testing",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.review_note == "model looks good for shadow testing"

    def test_receipt_includes_request(self) -> None:
        """The receipt includes the original request."""
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.request.model_id == "m1"


# ---------------------------------------------------------------------------
# Rejection is stored with reason
# ===========================================================================


class TestRejectionStoredWithReason:
    """Rejection is stored with reason."""

    def test_rejected_receipt_has_reason(self) -> None:
        """A rejected receipt has a rejection reason."""
        ev = _make_evidence(dossier=None)
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason is not None
        assert receipt.rejection_reason == PromotionRejectionReason.NO_DOSSIER


# ---------------------------------------------------------------------------
# Immutable promotion receipt
# ===========================================================================


class TestPromotionReceipt:
    """The promotion receipt is immutable."""

    def test_receipt_is_frozen(self) -> None:
        """The receipt is frozen (immutable for audit)."""
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        with pytest.raises((TypeError, ValueError)):
            receipt.decision = ReviewDecision.REJECTED  # type: ignore[misc]

    def test_receipt_to_dict_is_json_serializable(self) -> None:
        """The receipt can be serialized to JSON."""
        import json

        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        d = receipt.to_dict()
        json.dumps(d)
        assert "decision" in d
        assert "request" in d
        assert "review_note" in d


# ---------------------------------------------------------------------------
# Promotion review queue
# ===========================================================================


class TestPromotionReviewQueue:
    """The promotion review queue stores requests + receipts."""

    def test_queue_stores_pending_requests(self) -> None:
        """The queue stores pending promotion requests."""
        queue = PromotionReviewQueue()
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        queue.submit(request=req, evidence=ev)
        pending = queue.pending()
        assert len(pending) == 1
        assert pending[0].request.model_id == "m1"

    def test_queue_processes_requests(self) -> None:
        """The queue processes requests through the gate."""
        queue = PromotionReviewQueue()
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        queue.submit(request=req, evidence=ev)
        receipt = queue.process_next()
        assert isinstance(receipt, PromotionReceipt)
        assert receipt.decision == ReviewDecision.APPROVED

    def test_queue_stores_completed_receipts(self) -> None:
        """The queue stores completed receipts."""
        queue = PromotionReviewQueue()
        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        queue.submit(request=req, evidence=ev)
        queue.process_next()
        completed = queue.completed()
        assert len(completed) == 1
        assert completed[0].decision == ReviewDecision.APPROVED

    def test_queue_stores_rejected_receipts(self) -> None:
        """The queue stores rejected receipts with reasons."""
        queue = PromotionReviewQueue()
        ev = _make_evidence(dossier=None)
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        queue.submit(request=req, evidence=ev)
        queue.process_next()
        rejected = queue.rejected()
        assert len(rejected) == 1
        assert rejected[0].rejection_reason == PromotionRejectionReason.NO_DOSSIER


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInPromotionOutput:
    """Promotion output must not leak secrets."""

    def test_receipt_to_dict_has_no_secret_keys(self) -> None:

        ev = _make_evidence()
        req = PromotionRequest(
            model_id="m1",
            target_level=DossierStatus.SHADOW_APPROVED,
            review_note="test",
            waivers=[],
        )
        gate = PromotionGate()
        receipt = gate.evaluate(request=req, evidence=ev)
        d = receipt.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password", "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
