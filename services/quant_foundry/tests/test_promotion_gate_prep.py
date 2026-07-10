"""
C7 Promotion Gate Hardening â€” tests for the hardened promotion gate.

These tests verify the C7 evidence chain: no model can advance unless the
full receipt chain is complete.

  - durable artifact URI exists
  - artifact sha256 exists
  - callback receipt exists and is processed
  - dossier hash is consistent
  - bundle selfcheck exists
  - selfcheck.passed == true
  - backend is production eligible
  - feature_set_version is present and verified
  - PIT evidence exists and is verified
  - retired is terminal (no promotion out of retired)

Non-goals: do not touch the scheduler, live promotion automation, or
RunPod live probes from this file.
"""

from __future__ import annotations

import pytest
from quant_foundry.bundle_io import TrainingSelfCheck
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.promotion import (
    CallbackReceiptRef,
    PITEvidenceRef,
    PromotionEvidence,
    PromotionGate,
    PromotionRejectionReason,
    PromotionRequest,
    ReviewDecision,
)
from quant_foundry.sentinel import SentinelReceipt
from quant_foundry.tournament import (
    PromotionRecommendation,
    TournamentResult,
    TournamentStatus,
)


# ---------------------------------------------------------------------------
# Helpers â€” minimal evidence factory
# ---------------------------------------------------------------------------


def _make_dossier(
    model_id: str = "m1",
    artifact_sha256: str = "a" * 64,
    status: DossierStatus = DossierStatus.CANDIDATE,
) -> DossierRecord:
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
) -> TournamentResult:
    return TournamentResult(
        model_id=model_id,
        total_score=0.8,
        settled_count=settled_count,
        status=TournamentStatus.ELIGIBLE,
        recommendation=PromotionRecommendation.PROMOTE,
    )


def _make_sentinel_receipt(model_id: str = "m1") -> SentinelReceipt:
    return SentinelReceipt(
        model_id=model_id,
        issues=[],
        passed=True,
        checks_run=["shuffled_label"],
        ts_ns=1000,
    )


def _make_selfcheck(passed: bool = True) -> TrainingSelfCheck:
    return TrainingSelfCheck(
        passed=passed,
        bundle_sha256="a" * 64,
        n_rows_scored=10,
    )


def _make_callback_receipt(status: str = "processed") -> CallbackReceiptRef:
    return CallbackReceiptRef(status=status, receipt_id="cb-1")


def _make_pit_evidence_ref(verified: bool = True) -> PITEvidenceRef:
    return PITEvidenceRef(verified=verified, evidence_sha256="e" * 64, manifest_hash="m" * 64)


def _make_request(
    target_level: DossierStatus = DossierStatus.SHADOW_APPROVED,
) -> PromotionRequest:
    return PromotionRequest(
        model_id="m1",
        target_level=target_level,
        review_note="C7 test",
        waivers=[],
    )


def _make_evidence_complete(
    dossier: DossierRecord | None = None,
    selfcheck: TrainingSelfCheck | None = "default",
    callback_receipt: CallbackReceiptRef | None = "default",
    artifact_uri: str | None = "default",
    dossier_hash: str | None = "default",
    feature_set_version: str | None = "default",
    pit_evidence: PITEvidenceRef | None = "default",
    backend_eligible: bool = True,
) -> PromotionEvidence:
    """Build evidence with the full C7 receipt chain.

    Any field set to ``None`` is omitted from the evidence, allowing
    tests to verify individual rejection reasons.
    """
    d = dossier or _make_dossier()
    return PromotionEvidence(
        dossier=d,
        tournament_result=_make_tournament_result(),
        sentinel_receipt=_make_sentinel_receipt(),
        blocking_issues=[],
        selfcheck=_make_selfcheck() if selfcheck == "default" else selfcheck,
        callback_receipt=_make_callback_receipt() if callback_receipt == "default" else callback_receipt,
        artifact_uri="file:///durable/artifact.zip" if artifact_uri == "default" else artifact_uri,
        dossier_hash=d.content_hash if dossier_hash == "default" else dossier_hash,
        feature_set_version="fs-v1" if feature_set_version == "default" else feature_set_version,
        pit_evidence=_make_pit_evidence_ref() if pit_evidence == "default" else pit_evidence,
        backend_eligible=backend_eligible,
    )


# ---------------------------------------------------------------------------
# Tests â€” selfcheck
# ===========================================================================


def test_promotion_rejects_missing_selfcheck() -> None:
    """The gate must reject when no selfcheck receipt is present."""
    ev = _make_evidence_complete(selfcheck=None)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.MISSING_SELFCHECK


def test_promotion_rejects_selfcheck_failed() -> None:
    """The gate must reject when selfcheck.passed is False."""
    ev = _make_evidence_complete(selfcheck=_make_selfcheck(passed=False))
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.SELFCHECK_FAILED


# ---------------------------------------------------------------------------
# Tests â€” bundle sha256
# ===========================================================================


def test_promotion_rejects_missing_bundle_sha256() -> None:
    """The gate must reject when the dossier has no artifact_sha256."""
    dossier = _make_dossier(artifact_sha256="")
    ev = _make_evidence_complete(dossier=dossier)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.MISSING_BUNDLE_SHA256


# ---------------------------------------------------------------------------
# Tests â€” backend production eligibility
# ===========================================================================


def test_promotion_rejects_backend_not_production_eligible() -> None:
    """The gate must reject when the training backend is not production-eligible."""
    ev = _make_evidence_complete(backend_eligible=False)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.BACKEND_NOT_PRODUCTION_ELIGIBLE


# ---------------------------------------------------------------------------
# Tests â€” complete receipt chain (positive path)
# ===========================================================================


def test_promotion_accepts_complete_receipt_chain() -> None:
    """The gate must APPROVE when the full receipt chain is present + valid."""
    ev = _make_evidence_complete()
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.APPROVED
    assert receipt.rejection_reason is None


# ---------------------------------------------------------------------------
# Tests â€” retired terminal status
# ===========================================================================


def test_retired_is_terminal_status() -> None:
    """``retired`` must be a terminal DossierStatus with no outgoing transitions."""
    retired = DossierStatus.RETIRED
    assert retired.value == "retired"
    # A retired version cannot be promoted to any other level.
    dossier = _make_dossier(status=DossierStatus.RETIRED)
    ev = _make_evidence_complete(dossier=dossier)
    gate = PromotionGate()
    receipt = gate.evaluate(
        request=_make_request(target_level=DossierStatus.RESEARCH_APPROVED),
        evidence=ev,
    )
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.RETIRED_IS_TERMINAL


def test_rejected_is_terminal_status() -> None:
    """``rejected`` is also terminal â€” no promotion out of rejected."""
    dossier = _make_dossier(status=DossierStatus.REJECTED)
    ev = _make_evidence_complete(dossier=dossier)
    gate = PromotionGate()
    receipt = gate.evaluate(
        request=_make_request(target_level=DossierStatus.RESEARCH_APPROVED),
        evidence=ev,
    )
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.RETIRED_IS_TERMINAL


# ---------------------------------------------------------------------------
# Tests â€” callback receipt
# ===========================================================================


def test_promotion_rejects_missing_callback_receipt() -> None:
    """The gate must reject when no callback receipt is present."""
    ev = _make_evidence_complete(callback_receipt=None)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.MISSING_CALLBACK_RECEIPT


def test_promotion_rejects_callback_not_processed() -> None:
    """The gate must reject when the callback receipt status is not 'processed'."""
    ev = _make_evidence_complete(callback_receipt=_make_callback_receipt(status="pending"))
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.CALLBACK_NOT_PROCESSED


# ---------------------------------------------------------------------------
# Tests â€” artifact URI
# ===========================================================================


def test_promotion_rejects_missing_artifact_uri() -> None:
    """The gate must reject when no durable artifact URI is present."""
    ev = _make_evidence_complete(artifact_uri=None)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.MISSING_ARTIFACT_URI


# ---------------------------------------------------------------------------
# Tests â€” dossier hash
# ===========================================================================


def test_promotion_rejects_dossier_hash_mismatch() -> None:
    """The gate must reject when the dossier hash does not match the dossier content_hash."""
    ev = _make_evidence_complete(dossier_hash="b" * 64)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.DOSSIER_HASH_MISMATCH


def test_promotion_rejects_missing_dossier_hash() -> None:
    """The gate must reject when no dossier hash is present."""
    ev = _make_evidence_complete(dossier_hash=None)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.DOSSIER_HASH_MISMATCH


# ---------------------------------------------------------------------------
# Tests â€” feature set version
# ===========================================================================


def test_promotion_rejects_unverified_feature_set_version() -> None:
    """The gate must reject when feature_set_version is missing or empty."""
    ev = _make_evidence_complete(feature_set_version=None)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.FEATURE_SET_VERSION_NOT_VERIFIED


def test_promotion_rejects_empty_feature_set_version() -> None:
    """The gate must reject when feature_set_version is an empty string."""
    ev = _make_evidence_complete(feature_set_version="")
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.FEATURE_SET_VERSION_NOT_VERIFIED


# ---------------------------------------------------------------------------
# Tests â€” PIT evidence
# ===========================================================================


def test_promotion_rejects_missing_pit_evidence() -> None:
    """The gate must reject when no PIT evidence is present."""
    ev = _make_evidence_complete(pit_evidence=None)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.PIT_EVIDENCE_MISSING


def test_promotion_rejects_unverified_pit_evidence() -> None:
    """The gate must reject when PIT evidence.verified is False."""
    ev = _make_evidence_complete(pit_evidence=_make_pit_evidence_ref(verified=False))
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.PIT_EVIDENCE_NOT_VERIFIED
