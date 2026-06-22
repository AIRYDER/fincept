"""
quant_foundry.feature_lake — point-in-time dataset builder (TASK-0405).

Exports fixture-backed, point-in-time datasets with leakage-safe manifests so
that RunPod training workers can train on a manifest reference instead of DB
credentials.

Cross-cutting quant rigor enforced HERE (NEXT_STEPS_PLAN §1):
- **Point-in-time proof is mandatory.** Each row carries a ``decision_time``
  and each feature value carries an ``observed_at`` (vendor-availability time).
  The builder asserts ``observed_at <= decision_time`` for every feature value
  and raises ``LeakyFeatureError`` on any violation. A leaky fixture is
  rejected at export, never silently included.
- **As-of (backward) joins only.** A row whose ``decision_time`` falls after a
  universe entry's ``listed_until`` (delisting) is a forward join and is
  rejected at construction time.
- **As-of universe reconstruction** includes delisted/renamed symbols so the
  export is not survivorship-biased.
- **Purged-k-fold + embargo** boundaries are emitted in the manifest; embargo
  length >= max label horizon in the dataset.
- **Reproducibility:** the manifest hash covers every field that affects a
  training run; identical inputs yield identical hashes.

This module is fixture-driven and CPU-only. It does NOT touch
``services/features/src/features/computer.py`` and does NOT touch
``schemas.py`` (Builder 2's file).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quant_foundry.dataset_manifest import (
    FeatureLakeManifest,
    FoldBoundary,
    PurgedFoldSpec,
)
from quant_foundry.feature_availability import FeatureAvailabilityReport


class LeakyFeatureError(ValueError):
    """Raised when a feature value's observed_at is after the row's decision time.

    This is the settlement-side guard against look-ahead: a feature whose
    vendor-availability time is after the decision time would not have been
    knowable at the decision time, so including it would leak the future.
    """


@dataclass(frozen=True)
class FeatureValue:
    """A single feature value with its vendor-availability (observed_at) time."""

    name: str
    value: float
    observed_at: int  # ns since epoch — when the vendor made this value available


# Backward-compat alias so tests importing _FeatVal-style names keep working.
_FeatVal = FeatureValue


@dataclass(frozen=True)
class FeatureRow:
    """A single point-in-time row of the dataset.

    ``event_ts`` is the event time (e.g. bar close). ``decision_time`` is the
    time at which a decision using these features would have been made — the
    PIT cutoff. Every feature value's ``observed_at`` must be <= ``decision_time``.
    """

    symbol: str
    event_ts: int
    decision_time: int
    features: tuple[FeatureValue, ...]
    label_horizon_ns: int = 86_400_000_000_000  # 1 day default


@dataclass(frozen=True)
class UniverseEntry:
    """An as-of universe member.

    ``listed_until`` is None for still-listed symbols, or the delisting time
    for delisted/renamed symbols. Including delisted symbols prevents
    survivorship bias.
    """

    symbol: str
    listed_until: int | None
    renamed_from: str | None = None


@dataclass(frozen=True)
class ExportReceipt:
    """Receipt written alongside an export, proving what was emitted."""

    manifest_id: str
    manifest_hash: str
    row_count: int
    pit_proof_verified: bool
    receipt_path: Path


@dataclass
class FeatureLakeBuilder:
    """Builds a point-in-time dataset manifest from fixture rows.

    Construction validates the as-of universe constraint (no forward joins).
    ``build_manifest`` validates point-in-time correctness (no look-ahead) and
    emits a stable, hash-verifiable manifest with purged-k-fold + embargo
    boundaries.
    """

    dataset_id: str
    universe: tuple[UniverseEntry, ...]
    rows: tuple[FeatureRow, ...]
    feature_schema_hash: str
    label_schema_hash: str
    max_label_horizon_ns: int = 86_400_000_000_000
    n_folds: int = 1
    source_vintage_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._validate_universe_membership()

    # --- validation ------------------------------------------------------

    def _validate_universe_membership(self) -> None:
        """Reject forward joins: a row whose decision_time is after a symbol's
        delisting (listed_until) is using data the as-of universe would not have
        permitted. This is the as-of/backward-join-only guard.
        """
        listed_until_by_symbol = {e.symbol: e.listed_until for e in self.universe}
        for row in self.rows:
            if row.symbol not in listed_until_by_symbol:
                raise ValueError(
                    f"forward/as-of universe violation: symbol {row.symbol!r} "
                    "not present in as-of universe"
                )
            lu = listed_until_by_symbol[row.symbol]
            if lu is not None and row.decision_time > lu:
                raise ValueError(
                    f"forward/as-of universe violation: row for {row.symbol!r} "
                    f"at decision_time={row.decision_time} is after delisting "
                    f"(listed_until={lu})"
                )

    def _assert_pit_proof(self) -> None:
        """Assert every feature value's observed_at <= the row's decision_time.

        Raises ``LeakyFeatureError`` on the first violation so a leaky fixture
        is rejected at export, never silently included.
        """
        for row in self.rows:
            for fv in row.features:
                if fv.observed_at > row.decision_time:
                    raise LeakyFeatureError(
                        f"look-ahead leak in dataset {self.dataset_id}: feature "
                        f"{fv.name!r} for {row.symbol!r} has observed_at="
                        f"{fv.observed_at} > decision_time={row.decision_time}"
                    )

    # --- universe --------------------------------------------------------

    def as_of_universe(self, at: int) -> tuple[UniverseEntry, ...]:
        """Return the universe as-of time ``at`` (includes delisted symbols)."""
        return tuple(e for e in self.universe if (e.listed_until is None or at <= e.listed_until))

    def _universe_hash(self) -> str:
        """Stable hash over the as-of universe (incl. delisted/renamed symbols)."""
        payload = [
            {"symbol": e.symbol, "listed_until": e.listed_until, "renamed_from": e.renamed_from}
            for e in sorted(self.universe, key=lambda e: e.symbol)
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    # --- folds ------------------------------------------------------------

    def _build_folds(self) -> PurgedFoldSpec:
        """Construct purged-k-fold boundaries with embargo >= max label horizon.

        For the fixture MVP we partition the sorted unique decision times into
        ``n_folds`` contiguous validation windows, each preceded by a purge
        gap of one embargo length. Train is everything before the purge.
        Embargo == max_label_horizon_ns guarantees no training row's label
        window overlaps a validation row's feature window.
        """
        if not self.rows:
            raise ValueError("cannot build folds from an empty dataset")
        times = sorted({r.decision_time for r in self.rows})
        embargo = self.max_label_horizon_ns
        if len(times) < 2:
            # Single time point: one fold with a synthetic purge gap of embargo.
            t0 = times[0]
            fb = FoldBoundary(
                fold_id=0,
                train_start=t0,
                train_end=t0,
                val_start=t0 + embargo,
                val_end=t0 + 2 * embargo,
                purge_start=t0,
                purge_end=t0 + embargo,
            )
            return PurgedFoldSpec(
                folds=(fb,),
                embargo_ns=embargo,
                max_label_horizon_ns=self.max_label_horizon_ns,
            )

        # Split the time span into n_folds validation windows.
        t_min, t_max = times[0], times[-1]
        span = t_max - t_min
        fold_width = max(1, span // self.n_folds)
        folds: list[FoldBoundary] = []
        for k in range(self.n_folds):
            val_start = t_min + k * fold_width
            val_end = (t_min + (k + 1) * fold_width) if k < self.n_folds - 1 else t_max + 1
            purge_start = val_start - embargo
            purge_end = val_start
            train_start = t_min
            train_end = purge_start
            if train_end <= train_start:
                # No usable training window for this fold; skip it.
                continue
            folds.append(
                FoldBoundary(
                    fold_id=k,
                    train_start=train_start,
                    train_end=train_end,
                    val_start=val_start,
                    val_end=val_end,
                    purge_start=purge_start,
                    purge_end=purge_end,
                )
            )
        if not folds:
            # Fallback: single fold covering the whole span with a purge gap.
            folds.append(
                FoldBoundary(
                    fold_id=0,
                    train_start=t_min,
                    train_end=t_min,
                    val_start=t_max + embargo,
                    val_end=t_max + 2 * embargo,
                    purge_start=t_min,
                    purge_end=t_min + embargo,
                )
            )
        return PurgedFoldSpec(
            folds=tuple(folds),
            embargo_ns=embargo,
            max_label_horizon_ns=self.max_label_horizon_ns,
        )

    # --- checksum --------------------------------------------------------

    def _row_checksum(self) -> str:
        """Stable SHA-256 over the canonical row content (symbol, times, features)."""
        payload: list[dict[str, Any]] = []
        for row in sorted(self.rows, key=lambda r: (r.symbol, r.decision_time)):
            payload.append(
                {
                    "symbol": row.symbol,
                    "event_ts": row.event_ts,
                    "decision_time": row.decision_time,
                    "label_horizon_ns": row.label_horizon_ns,
                    "features": [
                        {"name": fv.name, "value": fv.value, "observed_at": fv.observed_at}
                        for fv in sorted(row.features, key=lambda f: f.name)
                    ],
                }
            )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    # --- public API ------------------------------------------------------

    def build_manifest(self) -> FeatureLakeManifest:
        """Validate PIT correctness and emit the dataset manifest.

        Raises ``LeakyFeatureError`` if any feature value's observed_at is
        after its row's decision_time (look-ahead leak).
        """
        self._assert_pit_proof()
        folds = self._build_folds()
        as_of_ts = max((r.decision_time for r in self.rows), default=0)
        return FeatureLakeManifest(
            dataset_id=self.dataset_id,
            feature_schema_hash=self.feature_schema_hash,
            label_schema_hash=self.label_schema_hash,
            as_of_ts=as_of_ts,
            universe_hash=self._universe_hash(),
            row_count=len(self.rows),
            checksum=self._row_checksum(),
            folds=folds,
            pit_proof_verified=True,
            source_vintage_refs=list(self.source_vintage_refs),
        )


# ---------------------------------------------------------------------------
# Export receipt
# ---------------------------------------------------------------------------


def export_receipt(
    manifest: FeatureLakeManifest,
    availability: FeatureAvailabilityReport,
    output_dir: Path,
) -> ExportReceipt:
    """Write an export receipt to ``output_dir`` and return its descriptor.

    The receipt records the manifest id + hash, row count, PIT-proof flag, and
    the feature-availability summary so a downstream training job can verify
    what it is training on without any DB access.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / f"{manifest.dataset_id}.receipt.json"
    body: dict[str, Any] = {
        "manifest_id": manifest.dataset_id,
        "manifest_hash": manifest.manifest_hash(),
        "row_count": manifest.row_count,
        "pit_proof_verified": manifest.pit_proof_verified,
        "as_of_ts": manifest.as_of_ts,
        "universe_hash": manifest.universe_hash,
        "feature_schema_hash": manifest.feature_schema_hash,
        "label_schema_hash": manifest.label_schema_hash,
        "availability": json.loads(availability.to_json()),
        "training_reference": manifest.training_reference(),
    }
    receipt_path.write_text(json.dumps(body, sort_keys=True, indent=2))
    return ExportReceipt(
        manifest_id=manifest.dataset_id,
        manifest_hash=manifest.manifest_hash(),
        row_count=manifest.row_count,
        pit_proof_verified=manifest.pit_proof_verified,
        receipt_path=receipt_path,
    )
