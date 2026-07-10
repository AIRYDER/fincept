"""
C7 Promotion Gate Prep — draft tests (PREP-ONLY).

These tests describe the *target* behavior of the hardened promotion gate
that C7 will eventually implement. They are marked ``xfail`` with
``strict=True`` and reason ``"C7 prep — gate not yet hardened"`` so they:

  - do NOT break the live test suite,
  - do NOT flip gate behavior globally,
  - do NOT add DB migrations,
  - document the exact contract C7 must satisfy.

When C7 is implemented, remove the ``xfail`` markers and these tests must
pass. Until then, they serve as executable specification.

Non-goals: do not touch the scheduler, live promotion automation, or
RunPod live probes from this file.
"""

from __future__ import annotations

import pytest
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.promotion import (
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

# Mark every test in this module as prep-only. strict=True means an
# unexpected PASS is a failure (so we notice when C7 lands).
_PREP = pytest.mark.xfail(
    reason="C7 prep — gate not yet hardened",
    strict=True,
)


# ---------------------------------------------------------------------------
# Helpers — minimal evidence factory
# ---------------------------------------------------------------------------


def _make_dossier(
    model_id: str = "m1",
    artifact_sha256: str = "a" * 64,
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
        status=DossierStatus.CANDIDATE,
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


def _make_request(
    target_level: DossierStatus = DossierStatus.SHADOW_APPROVED,
) -> PromotionRequest:
    return PromotionRequest(
        model_id="m1",
        target_level=target_level,
        review_note="C7 prep",
        waivers=[],
    )


# ---------------------------------------------------------------------------
# Draft tests — selfcheck
# ===========================================================================


@_PREP
def test_promotion_rejects_missing_selfcheck() -> None:
    """The gate must reject when no selfcheck receipt is present.

    Target: PromotionEvidence gains a ``selfcheck`` field; a None value
    rejects with ``MISSING_SELFCHECK``.
    """
    ev = _make_evidence_complete()
    # Simulate the missing-selfcheck case by evidence lacking the field.
    # When C7 lands, PromotionEvidence.selfcheck will exist and default to
    # None — the gate must reject.
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.MISSING_SELFCHECK


@_PREP
def test_promotion_rejects_selfcheck_failed() -> None:
    """The gate must reject when selfcheck.passed is False.

    Target: a failed TrainingSelfCheck rejects with ``SELFCHECK_FAILED``
    (distinct from the existing ``SENTINEL_FAILED``).
    """
    ev = _make_evidence_complete()
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.SELFCHECK_FAILED


# ---------------------------------------------------------------------------
# Draft tests — bundle sha256
# ===========================================================================


@_PREP
def test_promotion_rejects_missing_bundle_sha256() -> None:
    """The gate must reject when the dossier has no artifact_sha256.

    Target: an empty/None ``artifact_sha256`` rejects with
    ``MISSING_BUNDLE_SHA256``.
    """
    dossier = _make_dossier(artifact_sha256="")
    ev = _make_evidence_complete(dossier=dossier)
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == PromotionRejectionReason.MISSING_BUNDLE_SHA256


# ---------------------------------------------------------------------------
# Draft tests — backend production eligibility
# ===========================================================================


@_PREP
def test_promotion_rejects_backend_not_production_eligible() -> None:
    """The gate must reject when the training backend is not production-eligible.

    Target: the dataset manifest's readiness_level < L3 (or quality gate
    not passed) rejects with ``BACKEND_NOT_PRODUCTION_ELIGIBLE``.
    """
    ev = _make_evidence_complete()
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.REJECTED
    assert receipt.rejection_reason == (PromotionRejectionReason.BACKEND_NOT_PRODUCTION_ELIGIBLE)


# ---------------------------------------------------------------------------
# Draft tests — complete receipt chain (positive path)
# ===========================================================================


@_PREP
def test_promotion_accepts_complete_receipt_chain() -> None:
    """The gate must APPROVE when the full receipt chain is present + valid.

    Target: with durable artifact URI, processed callback receipt,
    selfcheck.passed=True, dossier hash consistent, backend
    production-eligible, feature_set_version pinned, and PIT evidence
    verified, the gate approves with rejection_reason=None.

    Today this fails because ``PromotionEvidence`` does not accept the
    new fields (``selfcheck``, ``callback_receipt``, ``artifact_uri``,
    ``dossier_hash``, ``feature_set_version``, ``pit_evidence``,
    ``backend_eligible``) — constructing evidence with them raises
    ``TypeError`` / ``ValidationError``, which the xfail captures.
    """
    ev = _make_evidence_complete_with_chain()
    gate = PromotionGate()
    receipt = gate.evaluate(request=_make_request(), evidence=ev)
    assert receipt.decision == ReviewDecision.APPROVED
    assert receipt.rejection_reason is None


# ---------------------------------------------------------------------------
# Draft tests — retired terminal status
# ===========================================================================


@_PREP
def test_retired_is_terminal_status() -> None:
    """``retired`` must be a terminal DossierStatus with no outgoing transitions.

    Target: DossierStatus gains a ``RETIRED`` member; the promotion gate
    refuses to promote FROM retired (retired is terminal) and the level
    order treats retired as a sink.
    """
    # The RETIRED member does not exist yet — this attribute access fails
    # today, which is the expected xfail.
    retired = DossierStatus.RETIRED  # type: ignore[attr-defined]
    assert retired.value == "retired"
    # A retired version cannot be promoted to any other level.
    ev = _make_evidence_complete()
    gate = PromotionGate()
    receipt = gate.evaluate(
        request=_make_request(target_level=DossierStatus.RESEARCH_APPROVED),
        evidence=ev,
    )
    # The gate must reject promotion out of a terminal retired state.
    assert receipt.decision == ReviewDecision.REJECTED


# ---------------------------------------------------------------------------
# Internal helper — build "complete" evidence (target shape)
# ---------------------------------------------------------------------------


def _make_evidence_complete(
    dossier: DossierRecord | None = None,
) -> PromotionEvidence:
    """Build evidence that *would* be complete under the hardened gate.

    NOTE: PromotionEvidence today does not carry selfcheck, callback
    receipt, artifact URI, dossier hash, feature_set_version, PIT
    evidence, or backend eligibility. This helper builds the closest
    current-shape evidence; the xfail tests above assert the *future*
    rejection reasons that do not exist yet, so they fail as expected.
    """
    return PromotionEvidence(
        dossier=dossier or _make_dossier(),
        tournament_result=_make_tournament_result(),
        sentinel_receipt=_make_sentinel_receipt(),
        blocking_issues=[],
    )


def _make_evidence_complete_with_chain() -> PromotionEvidence:
    """Build evidence carrying the full C7 receipt chain.

    Attempts to pass the new fields (``selfcheck``, ``callback_receipt``,
    ``artifact_uri``, ``dossier_hash``, ``feature_set_version``,
    ``pit_evidence``, ``backend_eligible``) to ``PromotionEvidence``.
    Today the model is ``extra="forbid"`` so this raises — the xfail in
    ``test_promotion_accepts_complete_receipt_chain`` captures that.
    """
    return PromotionEvidence(
        dossier=_make_dossier(),
        tournament_result=_make_tournament_result(),
        sentinel_receipt=_make_sentinel_receipt(),
        blocking_issues=[],
        # New C7 fields — rejected by extra="forbid" today.
        selfcheck={"passed": True, "bundle_sha256": "a" * 64},
        callback_receipt={"status": "processed"},
        artifact_uri="file:///durable/artifact.zip",
        dossier_hash="h" * 64,
        feature_set_version="fs-v1",
        pit_evidence={"pit_proof_verified": True},
        backend_eligible=True,
    )
