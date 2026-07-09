"""Settlement-backed comparison input provider (Tier 2c).

Builds :class:`ComparisonInput` for the auto-promotion orchestrator
from real settled shadow predictions in the
:class:`SettlementLedger`.

This closes Gap 1 from the Tier 2a audit: the auto-promotion
orchestrator's ``comparison_input_provider`` was synthetic. This
module provides a production-ready provider that:

  1. Maps ``version_id`` → ``model_id`` via the model registry.
  2. Reads all settlement records from the
     :class:`SettlementLedger`.
  3. Filters by ``model_id`` and ``SettlementStatus.SETTLED``.
  4. Extracts ``realized_return_net`` as ``oos_returns_net``.
  5. Computes mean Brier score from settled records.
  6. Gets ``trial_count`` from the :class:`DossierRecord`.
  7. Returns a :class:`ComparisonInput` ready for
     :func:`compare_champion_challenger`.

If there are not enough settled predictions
(< ``min_settled_count``), returns ``None`` so the orchestrator
skips the comparison.
"""

from __future__ import annotations

import statistics
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fincept_db.callback_tables import ModelDossierRow
from fincept_db.registry_tables import ModelVersionRow
from quant_foundry.champion_challenger import ComparisonInput
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.settlement import SettlementLedger

__all__ = ["SettledComparisonInputProvider"]


class SettledComparisonInputProvider:
    """Build ComparisonInput from settled shadow predictions.

    Implements the ``ComparisonInputProvider`` callable protocol
    (``Callable[[str], ComparisonInput | None]``) expected by
    :class:`AutoPromotionOrchestrator`.

    Usage::

        provider = SettledComparisonInputProvider(
            registry=registry,
            settlement_ledger=settlement_ledger,
            min_settled_count=30,
        )
        orchestrator = AutoPromotionOrchestrator(
            registry=registry,
            comparison_input_provider=provider,
        )

    Args:
        registry: the model registry DB (for version_id → model_id
            lookup and trial_count from the dossier).
        settlement_ledger: the settlement ledger containing settled
            shadow predictions.
        min_settled_count: minimum number of settled predictions
            required to build a ComparisonInput. If fewer are
            settled, returns None (the orchestrator skips).
    """

    def __init__(
        self,
        registry: ModelRegistryDB,
        settlement_ledger: SettlementLedger,
        *,
        min_settled_count: int = 30,
    ) -> None:
        self.registry = registry
        self.settlement_ledger = settlement_ledger
        self.min_settled_count = min_settled_count

    def __call__(self, version_id: str) -> ComparisonInput | None:
        """Build a ComparisonInput for the given version_id.

        Returns None if:
          - The version_id is not found in the registry.
          - There are fewer than ``min_settled_count`` settled
            predictions for the model.
        """
        model_id, trial_count = self._lookup_version(version_id)
        if model_id is None:
            return None

        settled = self._read_settled_by_model(model_id)
        if len(settled) < self.min_settled_count:
            return None

        oos_returns_net = [
            r.realized_return_net
            for r in settled
            if r.realized_return_net is not None
        ]
        if len(oos_returns_net) < self.min_settled_count:
            return None

        brier_values = [
            r.brier for r in settled if r.brier is not None
        ]
        brier = statistics.mean(brier_values) if brier_values else None

        return ComparisonInput(
            model_id=model_id,
            oos_returns_net=oos_returns_net,
            trial_count=trial_count,
            brier=brier,
            settled_count=len(oos_returns_net),
        )

    def _lookup_version(
        self, version_id: str
    ) -> tuple[str | None, int]:
        """Look up model_id and trial_count for a version_id.

        Returns (model_id, trial_count). If the version is not
        found, returns (None, 0).
        """
        engine = self.registry.engine
        with Session(engine) as session:
            version_row = session.scalars(
                select(ModelVersionRow).where(
                    ModelVersionRow.version_id == version_id
                )
            ).first()
            if version_row is None:
                return None, 0
            model_id = version_row.model_id
            dossier_hash = version_row.dossier_content_hash

            # Look up the dossier for trial_count.
            dossier_row = session.scalars(
                select(ModelDossierRow).where(
                    ModelDossierRow.model_id == model_id,
                    ModelDossierRow.content_hash == dossier_hash,
                )
            ).first()
            trial_count = dossier_row.trial_count if dossier_row is not None else 1

        return model_id, trial_count

    def _read_settled_by_model(
        self, model_id: str
    ) -> list[SettlementRecord]:
        """Read all SETTLED records for a given model_id.

        Filters the settlement ledger's ``read_all()`` by model_id
        and ``SettlementStatus.SETTLED``.
        """
        all_records = self.settlement_ledger.read_all()
        return [
            r for r in all_records
            if r.model_id == model_id
            and r.status == SettlementStatus.SETTLED
        ]
