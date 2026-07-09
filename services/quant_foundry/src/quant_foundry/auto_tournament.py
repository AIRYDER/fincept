"""Auto-tournament consumer (Tier 2e).

Automatically consumes settled shadow predictions from the
:class:`SettlementLedger`, runs the :class:`Tournament` scorer for
each model with enough settled samples, and records the tournament
metrics into the model registry via
:meth:`ModelRegistryDB.record_metrics`.

This closes Gap 2 from the Tier 2a audit: tournament metrics were
previously recorded manually. Now the tournament is auto-consumed
from settled predictions, making the product loop fully automated:

  settlement → auto-tournament → auto-promotion

The consumer reuses :class:`TournamentSweep._build_scoring_input()`
to build the :class:`ScoringInput` from settled records, ensuring
consistency with the manual sweep path.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fincept_db.registry_tables import ModelVersionRow
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.settlement import SettlementLedger
from quant_foundry.tournament import Tournament, TournamentResult
from quant_foundry.tournament_sweep import TournamentSweep

__all__ = ["AutoTournamentConsumer", "AutoTournamentReceipt", "TournamentScoreResult"]


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


class TournamentScoreResult(BaseModel):
    """The result of scoring one model in an auto-tournament run.

    Frozen + extra='forbid' (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    version_id: str
    settled_count: int
    tournament_result: TournamentResult
    metric_id: str
    skipped: bool = False
    error: str | None = None


class AutoTournamentReceipt(BaseModel):
    """Receipt for one auto-tournament run.

    Frozen + extra='forbid'. Lists all models that were scored,
    skipped, or errored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_at_ns: int
    total: int = 0
    scored: int = 0
    skipped: int = 0
    errored: int = 0
    results: list[TournamentScoreResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AutoTournamentConsumer
# ---------------------------------------------------------------------------


class AutoTournamentConsumer:
    """Auto-consume settled predictions → tournament metrics.

    Reads all settled records from the settlement ledger, groups by
    model_id, finds the latest version for each model in the
    registry, builds a ScoringInput, runs the tournament scorer, and
    records the result via ``registry.record_metrics(metric_type="tournament")``.

    Usage::

        consumer = AutoTournamentConsumer(
            settlement_ledger=settlement_ledger,
            tournament=Tournament(),
            registry=registry,
            tournament_sweep=sweep,  # for _build_scoring_input
            min_settled_samples=30,
        )
        receipt = consumer.run()

    Args:
        settlement_ledger: the settlement ledger containing settled
            shadow predictions.
        tournament: the tournament scorer.
        registry: the model registry DB (for version lookup and
            recording metrics).
        tournament_sweep: a TournamentSweep instance (used for its
            ``_build_scoring_input()`` method to ensure consistency
            with the manual sweep path).
        min_settled_count: minimum number of settled predictions
            required to score a model. Models with fewer are skipped.
        stale_threshold_ns: staleness threshold for the tournament
            (default 7 days).
    """

    def __init__(
        self,
        settlement_ledger: SettlementLedger,
        tournament: Tournament,
        registry: ModelRegistryDB,
        tournament_sweep: TournamentSweep,
        *,
        min_settled_count: int = 30,
        stale_threshold_ns: int = 7 * 24 * 3600 * 1_000_000_000,
    ) -> None:
        self.settlement_ledger = settlement_ledger
        self.tournament = tournament
        self.registry = registry
        self.tournament_sweep = tournament_sweep
        self.min_settled_count = min_settled_count
        self.stale_threshold_ns = stale_threshold_ns

    def run(self) -> AutoTournamentReceipt:
        """Run one auto-tournament pass.

        Returns a frozen :class:`AutoTournamentReceipt` listing all
        models that were scored, skipped, or errored.
        """
        now_ns = time.time_ns()
        results: list[TournamentScoreResult] = []
        scored = 0
        skipped = 0
        errored = 0

        # Read all settled records and group by model_id.
        all_records = self.settlement_ledger.read_all()
        by_model = self._group_settled_by_model(all_records)

        for model_id, records in sorted(by_model.items()):
            if len(records) < self.min_settled_count:
                results.append(TournamentScoreResult(
                    model_id=model_id,
                    version_id="",
                    settled_count=len(records),
                    tournament_result=TournamentResult(
                        model_id=model_id,
                        total_score=0.0,
                        settled_count=len(records),
                    ),
                    metric_id="",
                    skipped=True,
                    error=f"insufficient settled count: {len(records)} < {self.min_settled_count}",
                ))
                skipped += 1
                continue

            # Find the latest version for this model in the registry.
            version_id = self._find_latest_version(model_id)
            if version_id is None:
                results.append(TournamentScoreResult(
                    model_id=model_id,
                    version_id="",
                    settled_count=len(records),
                    tournament_result=TournamentResult(
                        model_id=model_id,
                        total_score=0.0,
                        settled_count=len(records),
                    ),
                    metric_id="",
                    skipped=True,
                    error=f"no version found for model_id {model_id}",
                ))
                skipped += 1
                continue

            try:
                result = self._score_model(model_id, records, now_ns)
                metric_id = self.registry.record_metrics(
                    version_id=version_id,
                    metric_type="tournament",
                    metrics_dict=result.to_dict(),
                )
                results.append(TournamentScoreResult(
                    model_id=model_id,
                    version_id=version_id,
                    settled_count=len(records),
                    tournament_result=result,
                    metric_id=metric_id,
                ))
                scored += 1
            except Exception as exc:
                results.append(TournamentScoreResult(
                    model_id=model_id,
                    version_id=version_id,
                    settled_count=len(records),
                    tournament_result=TournamentResult(
                        model_id=model_id,
                        total_score=0.0,
                        settled_count=len(records),
                    ),
                    metric_id="",
                    skipped=True,
                    error=str(exc),
                ))
                errored += 1

        return AutoTournamentReceipt(
            run_at_ns=now_ns,
            total=len(results),
            scored=scored,
            skipped=skipped,
            errored=errored,
            results=results,
        )

    def _group_settled_by_model(
        self, records: list[SettlementRecord]
    ) -> dict[str, list[SettlementRecord]]:
        """Group SETTLED records by model_id."""
        by_model: dict[str, list[SettlementRecord]] = {}
        for r in records:
            if r.status != SettlementStatus.SETTLED:
                continue
            if r.realized_return_net is None:
                continue
            by_model.setdefault(r.model_id, []).append(r)
        return by_model

    def _find_latest_version(self, model_id: str) -> str | None:
        """Find the latest version_id for a model_id in the registry."""
        engine = self.registry.engine
        with Session(engine) as session:
            version_row = session.scalars(
                select(ModelVersionRow)
                .where(ModelVersionRow.model_id == model_id)
                .order_by(ModelVersionRow.version_number.desc())
            ).first()
            if version_row is None:
                return None
            return version_row.version_id

    def _score_model(
        self,
        model_id: str,
        records: list[SettlementRecord],
        now_ns: int,
    ) -> TournamentResult:
        """Build ScoringInput and run the tournament scorer."""
        last_settled_at_ns = max(
            (r.settled_at_ns or 0) for r in records
        )
        age_ns = now_ns - last_settled_at_ns
        is_stale = age_ns > self.stale_threshold_ns

        scoring_input = self.tournament_sweep._build_scoring_input(
            model_id=model_id,
            records=records,
            now_ns=now_ns,
            last_settled_at_ns=last_settled_at_ns,
            is_stale=is_stale,
        )
        return self.tournament.score(scoring_input)
