"""Auto-promotion orchestrator (Tier 2b).

Automates the champion/challenger comparison → promotion gate workflow.
The orchestrator finds challenger versions that are ready for promotion,
runs the champion/challenger comparison, and if the comparison passes,
auto-promotes through the gate — recording the shadow evaluation and
promotion decision receipt.

Design:
  * The orchestrator is a pure-Python class that takes a
    :class:`ModelRegistryDB` and an optional
    :class:`ChampionChallengerConfig`.
  * A ``comparison_input_provider`` callable supplies the
    :class:`ComparisonInput` for each version. In production this reads
    from the settlement ledger; in tests it provides synthetic data.
    This decouples the orchestrator from the settlement system (which
    is not yet auto-wired).
  * The orchestrator never bypasses the promotion gate. It calls
    ``registry.promote()``, which assembles evidence from the DB and
    runs the gate. If the gate rejects (e.g. missing sentinel), the
    auto-promotion is recorded but the status does not change.
  * Every action produces an immutable receipt for audit.

Promotion ladder:
  candidate → research_approved → shadow_approved → paper_approved

The orchestrator promotes one level at a time. A challenger at
``shadow_approved`` is considered for promotion to ``paper_approved``.
A challenger at ``research_approved`` is considered for promotion to
``shadow_approved``. A challenger at ``candidate`` is considered for
promotion to ``research_approved`` (but only if it has tournament +
sentinel evidence — the gate enforces this).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.champion_challenger import (
    ChampionChallengerConfig,
    ComparisonInput,
    PromotionDecision,
)
from quant_foundry.dossier import DossierStatus
from quant_foundry.promotion import PromotionReceipt, ReviewDecision
from quant_foundry.registry_db import ModelRegistryDB

__all__ = [
    "AutoPromotionOrchestrator",
    "AutoPromotionResult",
    "AutoPromotionReceipt",
    "PromotionTarget",
]

# --------------------------------------------------------------------------- #
# Types                                                                        #
# --------------------------------------------------------------------------- #

# A callable that provides ComparisonInput for a given version_id.
# In production: reads from the settlement ledger.
# In tests: returns synthetic data.
ComparisonInputProvider = Callable[[str], ComparisonInput | None]


class PromotionTarget(BaseModel):
    """A single promotion target identified by the orchestrator.

    Fields:
        model_id: the model ID.
        champion_version_id: the current champion version ID (may be
            None if there is no champion yet — first model).
        challenger_version_id: the challenger version ID.
        from_status: the challenger's current status.
        to_status: the target status to promote to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    champion_version_id: str | None = None
    challenger_version_id: str
    from_status: DossierStatus
    to_status: DossierStatus


class AutoPromotionResult(BaseModel):
    """The result of a single auto-promotion attempt.

    Fields:
        target: the promotion target.
        comparison_decision: the champion/challenger comparison decision
            ("promote", "insufficient_evidence", "no_edge",
            "not_significant", "low_dsr") or None if the comparison
            was skipped (no champion).
        comparison_reason: the comparison reason string.
        shadow_evaluation_id: the shadow_evaluations row ID (if
            comparison was run).
        promotion_receipt: the promotion gate receipt (if promotion
            was attempted).
        promoted: True if the version was promoted (gate approved).
        skipped: True if the comparison or promotion was skipped.
        error: error message if the attempt failed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: PromotionTarget
    comparison_decision: str | None = None
    comparison_reason: str | None = None
    shadow_evaluation_id: str | None = None
    promotion_receipt: PromotionReceipt | None = None
    promoted: bool = False
    skipped: bool = False
    error: str | None = None


class AutoPromotionReceipt(BaseModel):
    """The receipt from an auto-promotion orchestrator run.

    Fields:
        timestamp_utc: ISO timestamp of the run.
        results: list of per-target results.
        promoted_count: number of versions promoted.
        skipped_count: number of versions skipped.
        failed_count: number of versions that failed.
        total: total number of targets evaluated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp_utc: str
    results: list[AutoPromotionResult]
    promoted_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    total: int = 0


# --------------------------------------------------------------------------- #
# Promotion ladder                                                             #
# --------------------------------------------------------------------------- #

# The promotion ladder: each status maps to the next level up.
_PROMOTION_LADDER: dict[DossierStatus, DossierStatus] = {
    DossierStatus.CANDIDATE: DossierStatus.RESEARCH_APPROVED,
    DossierStatus.RESEARCH_APPROVED: DossierStatus.SHADOW_APPROVED,
    DossierStatus.SHADOW_APPROVED: DossierStatus.PAPER_APPROVED,
    # paper_approved is the MVP max — no auto-promotion beyond this.
}

# Statuses that are eligible for auto-promotion consideration.
_ELIGIBLE_STATUSES = set(_PROMOTION_LADDER.keys())


# --------------------------------------------------------------------------- #
# Orchestrator                                                                 #
# --------------------------------------------------------------------------- #


class AutoPromotionOrchestrator:
    """Automates champion/challenger comparison → promotion gate.

    The orchestrator scans the model registry for challenger versions
    that are eligible for promotion, runs the champion/challenger
    comparison, and if the comparison passes, calls
    ``registry.promote()`` to auto-promote through the gate.

    Usage::

        registry = ModelRegistryDB(engine=engine, gate=PromotionGate())
        orchestrator = AutoPromotionOrchestrator(
            registry=registry,
            comparison_input_provider=my_settlement_provider,
        )
        receipt = orchestrator.run()
        print(f"Promoted {receipt.promoted_count}/{receipt.total}")
    """

    def __init__(
        self,
        registry: ModelRegistryDB,
        *,
        config: ChampionChallengerConfig | None = None,
        comparison_input_provider: ComparisonInputProvider | None = None,
    ) -> None:
        self.registry = registry
        self.config = config or ChampionChallengerConfig()
        self.comparison_input_provider = comparison_input_provider

    def run(self) -> AutoPromotionReceipt:
        """Run the auto-promotion sweep and return a receipt.

        Returns:
            An :class:`AutoPromotionReceipt` with per-target results.
        """
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results: list[AutoPromotionResult] = []

        targets = self._find_promotion_targets()
        for target in targets:
            result = self._evaluate_target(target)
            results.append(result)

        promoted = sum(1 for r in results if r.promoted)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if r.error is not None)

        return AutoPromotionReceipt(
            timestamp_utc=timestamp,
            results=results,
            promoted_count=promoted,
            skipped_count=skipped,
            failed_count=failed,
            total=len(results),
        )

    def _find_promotion_targets(self) -> list[PromotionTarget]:
        """Scan the registry for versions eligible for auto-promotion.

        For each model:
          * If there is only one version, it is a challenger with no
            champion (first-model scenario). It is a target if its
            status is in the promotion ladder.
          * If there are multiple versions, the highest-status version
            is the champion. All other versions with eligible statuses
            are challengers and become targets.
        """
        targets: list[PromotionTarget] = []
        engine = self.registry.engine

        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from fincept_db.registry_tables import ModelRow

        with Session(engine) as session:
            models = session.scalars(select(ModelRow)).all()
            for model in models:
                versions = self.registry.list_versions(model.model_id)
                if not versions:
                    continue

                if len(versions) == 1:
                    # First-model scenario: no champion.
                    v = versions[0]
                    try:
                        from_status = DossierStatus(v["status"])
                    except ValueError:
                        continue
                    to_status = _PROMOTION_LADDER.get(from_status)
                    if to_status is not None:
                        targets.append(PromotionTarget(
                            model_id=model.model_id,
                            champion_version_id=None,
                            challenger_version_id=v["version_id"],
                            from_status=from_status,
                            to_status=to_status,
                        ))
                else:
                    # Multiple versions: find the champion (highest status).
                    champion = self._find_champion(versions)
                    champion_vid = champion["version_id"] if champion else None
                    for v in versions:
                        if v["version_id"] == champion_vid:
                            continue
                        try:
                            from_status = DossierStatus(v["status"])
                        except ValueError:
                            continue
                        if from_status not in _ELIGIBLE_STATUSES:
                            continue
                        to_status = _PROMOTION_LADDER.get(from_status)
                        if to_status is not None:
                            targets.append(PromotionTarget(
                                model_id=model.model_id,
                                champion_version_id=champion_vid,
                                challenger_version_id=v["version_id"],
                                from_status=from_status,
                                to_status=to_status,
                            ))

        return targets

    def _find_champion(
        self, versions: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Find the champion (highest-status version) from a list.

        The champion is the version with the highest DossierStatus.
        If multiple versions have the same status, the one with the
        highest version_number wins.
        """
        _status_order = {
            DossierStatus.CANDIDATE: 0,
            DossierStatus.RESEARCH_APPROVED: 1,
            DossierStatus.SHADOW_APPROVED: 2,
            DossierStatus.PAPER_APPROVED: 3,
            DossierStatus.LIMITED_LIVE_APPROVED: 4,
        }

        best: dict[str, Any] | None = None
        best_order = -1
        for v in versions:
            try:
                status = DossierStatus(v["status"])
            except ValueError:
                continue
            order = _status_order.get(status, -1)
            if order > best_order or (
                order == best_order
                and best is not None
                and v["version_number"] > best["version_number"]
            ):
                best = v
                best_order = order
        return best

    def _evaluate_target(self, target: PromotionTarget) -> AutoPromotionResult:
        """Evaluate a single promotion target.

        1. Get ComparisonInput for champion and challenger.
        2. Run champion/challenger comparison (if champion exists).
        3. If comparison passes, call registry.promote().
        4. Return the result.
        """
        if self.comparison_input_provider is None:
            return AutoPromotionResult(
                target=target,
                skipped=True,
                error="no comparison_input_provider configured",
            )

        # Get comparison inputs.
        challenger_input = self.comparison_input_provider(target.challenger_version_id)
        if challenger_input is None:
            return AutoPromotionResult(
                target=target,
                skipped=True,
                error=f"no comparison input for challenger {target.challenger_version_id}",
            )

        # If there's no champion (first model), skip comparison and
        # go straight to promotion (the gate will check evidence).
        if target.champion_version_id is None:
            return self._attempt_promotion(
                target=target,
                comparison_decision=None,
                comparison_reason="no champion — first model promotion",
                shadow_evaluation_id=None,
            )

        champion_input = self.comparison_input_provider(target.champion_version_id)
        if champion_input is None:
            return AutoPromotionResult(
                target=target,
                skipped=True,
                error=f"no comparison input for champion {target.champion_version_id}",
            )

        # Run the champion/challenger comparison via the registry.
        try:
            evaluation_id, decision = self.registry.run_shadow_comparison(
                champion_version_id=target.champion_version_id,
                challenger_version_id=target.challenger_version_id,
                champion_input=champion_input,
                challenger_input=challenger_input,
                config=self.config,
            )
        except Exception as exc:
            return AutoPromotionResult(
                target=target,
                error=f"comparison failed: {exc!s}",
            )

        # Check the comparison decision.
        if decision.decision != "promote":
            return AutoPromotionResult(
                target=target,
                comparison_decision=decision.decision,
                comparison_reason=decision.reason,
                shadow_evaluation_id=evaluation_id,
                skipped=True,
            )

        # Comparison passed — attempt promotion through the gate.
        return self._attempt_promotion(
            target=target,
            comparison_decision=decision.decision,
            comparison_reason=decision.reason,
            shadow_evaluation_id=evaluation_id,
        )

    def _attempt_promotion(
        self,
        target: PromotionTarget,
        comparison_decision: str | None,
        comparison_reason: str | None,
        shadow_evaluation_id: str | None,
    ) -> AutoPromotionResult:
        """Attempt to promote the challenger through the gate."""
        review_note = (
            f"Auto-promoted via champion/challenger comparison "
            f"(decision={comparison_decision})"
        )

        try:
            receipt = self.registry.promote(
                version_id=target.challenger_version_id,
                target_status=target.to_status,
                review_note=review_note,
                decided_by="auto-promotion-orchestrator",
            )
        except Exception as exc:
            return AutoPromotionResult(
                target=target,
                comparison_decision=comparison_decision,
                comparison_reason=comparison_reason,
                shadow_evaluation_id=shadow_evaluation_id,
                error=f"promotion failed: {exc!s}",
            )

        promoted = receipt.decision == ReviewDecision.APPROVED

        return AutoPromotionResult(
            target=target,
            comparison_decision=comparison_decision,
            comparison_reason=comparison_reason,
            shadow_evaluation_id=shadow_evaluation_id,
            promotion_receipt=receipt,
            promoted=promoted,
            skipped=not promoted,
        )
