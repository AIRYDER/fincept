"""
quant_foundry.tournament_sweep — periodic tournament scoring sweep.

Reads settled predictions from the ``SettlementLedger``, groups them by
``model_id``, builds a ``ScoringInput`` per model, runs the ``Tournament``
scorer, and populates the ``ExpandedLeaderboard`` with ranked entries
(horizon / regime / symbol-cluster slices where data is available).

The sweep is advisory-only — it scores and ranks models but never
promotes them. Promotion requires human approval through the
``PromotionReviewQueue`` (TASK-0702).

Key invariants:
- **Models with insufficient evidence are blocked, not scored.** A model
  with fewer than ``min_settled_samples`` settled records is returned in
  the ``blocked_models`` list with ``INSUFFICIENT_EVIDENCE``.
- **Stale models are flagged.** A model whose last settlement is older
  than ``stale_threshold_ns`` is returned in the ``stale_models`` list.
- **The sweep is deterministic** given a fixed ``Tournament`` seed and
  the same settlement records.
- **No secrets in receipts.** The receipt carries only model_ids, scores,
  statuses, and blocking issue codes/messages.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.leaderboard_expanded import (
    CalibrationSummary,
    DecayIndicator,
    ExpandedLeaderboard,
    ExpandedLeaderboardEntry,
)
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.retirement import RetirementFlagger
from quant_foundry.tournament import (
    ScoringInput,
    Tournament,
    TournamentResult,
    TournamentStatus,
)

# ---------------------------------------------------------------------------
# Sweep receipt
# ---------------------------------------------------------------------------


class ScoredModel(BaseModel):
    """One model scored in a sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    tournament_result: dict[str, Any]
    retirement_flag: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "tournament_result": self.tournament_result,
            "retirement_flag": self.retirement_flag,
        }


class BlockedModel(BaseModel):
    """A model blocked from scoring (insufficient evidence)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    status: str
    settled_count: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "status": self.status,
            "settled_count": self.settled_count,
            "reason": self.reason,
        }


class StaleModel(BaseModel):
    """A model flagged as stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    last_settled_at_ns: int
    age_ns: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "last_settled_at_ns": self.last_settled_at_ns,
            "age_ns": self.age_ns,
            "reason": self.reason,
        }


class TournamentSweepReceipt(BaseModel):
    """The result of one tournament sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scored_models: list[ScoredModel] = []
    blocked_models: list[BlockedModel] = []
    stale_models: list[StaleModel] = []
    trial_count: int = 0
    swept_at_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scored_models": [m.to_dict() for m in self.scored_models],
            "blocked_models": [m.to_dict() for m in self.blocked_models],
            "stale_models": [m.to_dict() for m in self.stale_models],
            "trial_count": self.trial_count,
            "swept_at_ns": self.swept_at_ns,
        }


# ---------------------------------------------------------------------------
# The sweep worker
# ===========================================================================


class TournamentSweep:
    """Periodic tournament scoring sweep.

    Reads all settlement records, groups by model_id, builds ScoringInput
    per model, runs the Tournament scorer, and populates the
    ExpandedLeaderboard.
    """

    def __init__(
        self,
        settlement_ledger: Any,
        dossier_registry: Any,
        tournament: Tournament,
        leaderboard: ExpandedLeaderboard,
        retirement_checker: RetirementFlagger | None = None,
        *,
        min_settled_samples: int = 10,
        stale_threshold_ns: int = 7 * 24 * 3600 * 1_000_000_000,
        now_ns: int | None = None,
    ) -> None:
        self.settlement_ledger = settlement_ledger
        self.dossier_registry = dossier_registry
        self.tournament = tournament
        self.leaderboard = leaderboard
        self.retirement_checker = retirement_checker or RetirementFlagger()
        self.min_settled_samples = min_settled_samples
        self.stale_threshold_ns = stale_threshold_ns
        self._now_ns = now_ns

    def sweep(self, now_ns: int | None = None) -> TournamentSweepReceipt:
        """Run one tournament sweep.

        1. Read all settlement records from the SettlementLedger.
        2. Group by model_id (only SETTLED records with realized returns).
        3. For each model:
           a. Build ScoringInput from settlement records.
           b. Run Tournament.score().
           c. Build ExpandedLeaderboardEntry with slices.
           d. Check retirement/decay flags.
           e. Add to ExpandedLeaderboard.
        4. Return receipt with scored/blocked/stale lists.
        """
        current_ns = (
            now_ns
            if now_ns is not None
            else (self._now_ns if self._now_ns is not None else time.time_ns())
        )

        records = self.settlement_ledger.read_all()
        by_model = self._group_settled_by_model(records)

        scored: list[ScoredModel] = []
        blocked: list[BlockedModel] = []
        stale: list[StaleModel] = []

        # Trial count = number of models with any settled records.
        trial_count = len(by_model)

        for model_id, model_records in by_model.items():
            settled_records = [
                r
                for r in model_records
                if r.status == SettlementStatus.SETTLED and r.realized_return_net is not None
            ]

            if len(settled_records) < self.min_settled_samples:
                blocked.append(
                    BlockedModel(
                        model_id=model_id,
                        status=TournamentStatus.INSUFFICIENT_EVIDENCE.value,
                        settled_count=len(settled_records),
                        reason=(
                            f"settled_count={len(settled_records)} < "
                            f"min_settled_samples={self.min_settled_samples}"
                        ),
                    )
                )
                continue

            last_settled_ns = max((r.settled_at_ns or 0) for r in settled_records)
            age_ns = current_ns - last_settled_ns
            is_stale = age_ns > self.stale_threshold_ns

            if is_stale:
                stale.append(
                    StaleModel(
                        model_id=model_id,
                        last_settled_at_ns=last_settled_ns,
                        age_ns=age_ns,
                        reason=(
                            f"last_settled_at_ns={last_settled_ns} is "
                            f"{age_ns}ns old (> threshold {self.stale_threshold_ns}ns)"
                        ),
                    )
                )

            scoring_input = self._build_scoring_input(
                model_id=model_id,
                records=settled_records,
                now_ns=current_ns,
                last_settled_at_ns=last_settled_ns,
                is_stale=is_stale,
            )

            result = self.tournament.score(scoring_input)

            entry = self._build_leaderboard_entry(
                model_id=model_id,
                records=settled_records,
                result=result,
                now_ns=current_ns,
                last_settled_ns=last_settled_ns,
                age_ns=age_ns,
                is_stale=is_stale,
            )
            self.leaderboard.add(entry)

            retirement_flag_dict: dict[str, Any] | None = None
            flag = self.retirement_checker.evaluate(entry)
            if flag is not None:
                retirement_flag_dict = flag.to_dict()

            scored.append(
                ScoredModel(
                    model_id=model_id,
                    tournament_result=result.to_dict(),
                    retirement_flag=retirement_flag_dict,
                )
            )

        return TournamentSweepReceipt(
            scored_models=scored,
            blocked_models=blocked,
            stale_models=stale,
            trial_count=trial_count,
            swept_at_ns=current_ns,
        )

    # -- helpers --------------------------------------------------------

    def _group_settled_by_model(
        self, records: list[SettlementRecord]
    ) -> dict[str, list[SettlementRecord]]:
        """Group all records by model_id (preserving insertion order)."""
        grouped: dict[str, list[SettlementRecord]] = {}
        for rec in records:
            grouped.setdefault(rec.model_id, []).append(rec)
        return grouped

    def _build_scoring_input(
        self,
        *,
        model_id: str,
        records: list[SettlementRecord],
        now_ns: int,
        last_settled_at_ns: int,
        is_stale: bool,
    ) -> ScoringInput:
        """Build a ScoringInput from a model's settled records.

        - oos_returns_net = realized_return_net for each settled record.
        - oos_returns_gross = realized_return_gross (falls back to net
          if gross is None).
        - oos_returns_baseline = zero-skill baseline (all zeros).
        - brier = mean of non-None brier scores.
        - confidence_buckets = aggregated (bucket, confidence, realized)
          tuples from calibration_bucket + realized_return_net.
        - trial_count from dossier if available, else 1.
        - cost_model_version from the most recent record.
        """
        oos_net = [r.realized_return_net or 0.0 for r in records]
        oos_gross = [
            r.realized_return_gross
            if r.realized_return_gross is not None
            else (r.realized_return_net or 0.0)
            for r in records
        ]
        oos_baseline = [0.0] * len(records)

        brier_values = [r.brier for r in records if r.brier is not None]
        mean_brier = sum(brier_values) / len(brier_values) if brier_values else None

        confidence_buckets: list[tuple[str, float, float]] = []
        for r in records:
            if r.calibration_bucket is not None and r.realized_return_net is not None:
                confidence_buckets.append(
                    (
                        r.calibration_bucket,
                        _bucket_confidence(r.calibration_bucket),
                        r.realized_return_net,
                    )
                )

        trial_count = 1
        cost_model_version = "cm-v1"
        if records:
            cost_model_version = records[0].cost_model_version
        if self.dossier_registry is not None:
            dossier = self.dossier_registry.get(model_id)
            if dossier is not None:
                trial_count = dossier.trial_count

        return ScoringInput(
            model_id=model_id,
            oos_returns_net=oos_net,
            oos_returns_gross=oos_gross,
            oos_returns_baseline=oos_baseline,
            trial_count=trial_count,
            brier=mean_brier,
            confidence_buckets=confidence_buckets,
            settled_count=len(records),
            last_settled_at_ns=last_settled_at_ns,
            now_ns=now_ns,
            stale_threshold_ns=self.stale_threshold_ns,
            min_settled_samples=self.min_settled_samples,
            cost_model_version=cost_model_version,
        )

    def _build_leaderboard_entry(
        self,
        *,
        model_id: str,
        records: list[SettlementRecord],
        result: TournamentResult,
        now_ns: int,
        last_settled_ns: int,
        age_ns: int,
        is_stale: bool,
    ) -> ExpandedLeaderboardEntry:
        """Build an ExpandedLeaderboardEntry from a tournament result + records."""
        days_since = max(0, age_ns // (24 * 3600 * 1_000_000_000))

        brier_values = [r.brier for r in records if r.brier is not None]
        calibration_summary: CalibrationSummary | None = None
        if brier_values:
            mean_brier = sum(brier_values) / len(brier_values)
            reliability = 1.0 - min(max(mean_brier, 0.0), 1.0)
            calibration_summary = CalibrationSummary(
                brier_score=mean_brier,
                reliability=reliability,
                n_bins=len(set(r.calibration_bucket for r in records if r.calibration_bucket))
                or 10,
            )

        decay_indicator = DecayIndicator(
            decay_score=0.0,
            is_stale=is_stale,
            is_decayed=False,
            days_since_last_settlement=days_since,
        )

        return ExpandedLeaderboardEntry(
            model_id=model_id,
            total_score=result.total_score,
            settled_count=len(records),
            calibration_summary=calibration_summary,
            decay_indicator=decay_indicator,
        )


def _bucket_confidence(bucket: str) -> float:
    """Map a calibration bucket name to a representative confidence value."""
    mapping = {
        "very_low": 0.1,
        "low": 0.3,
        "medium": 0.5,
        "high": 0.7,
        "very_high": 0.9,
    }
    return mapping.get(bucket, 0.5)
