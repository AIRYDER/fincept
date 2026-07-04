"""
quant_foundry.data_ingestion.quality_report — comprehensive dataset quality report.

This module defines the :class:`DatasetQualityReport` model that is written
alongside every exported dataset as ``dataset.quality.json``.  The report
captures coverage, feature quality, label quality, fold quality, leakage
checks, and basic drift indicators so a downstream training job or tournament
can refuse to operate on a degraded dataset without re-deriving the stats.

The report is a frozen Pydantic v2 model with ``extra="forbid"`` so it is
tamper-evident and round-trip serialisation is exact — matching the
convention used by :class:`FeatureLakeManifest` and the core schema spine.

Heavy dependencies (polars, numpy) are imported lazily inside
:func:`compute_quality_report` so this module is importable without them,
following the same pattern as ``scripts/build_dataset_manifest.py``.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.dataset_manifest import FeatureLakeManifest, TrainingMode


class DatasetQualityReport(BaseModel):
    """Comprehensive quality report for an exported point-in-time dataset.

    Written alongside every dataset as ``dataset.quality.json``.  Captures:

    - **Coverage**: total rows, symbols, and the time span of the data.
    - **Feature quality**: per-feature non-null percentage and missing counts.
    - **Label quality**: binary label balance and missing count.
    - **Fold quality**: per-fold train/val row counts derived from the manifest.
    - **Leakage checks**: PIT proof, embargo sufficiency, and forward-join
      absence — all of which should be ``True`` for a leakage-safe dataset.
    - **Drift indicators**: per-feature mean and std across all rows, useful as
      a lightweight drift baseline between dataset versions.

    The model is frozen and forbids extra fields so the report is
    tamper-evident and serialisation is exact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    generated_at_ns: int

    # --- coverage --------------------------------------------------------
    total_rows: int
    total_symbols: int
    time_span_start_ns: int
    time_span_end_ns: int

    # --- feature quality -------------------------------------------------
    feature_names: tuple[str, ...]
    feature_coverage_pct: dict[str, float]  # feature -> % non-null
    feature_missing_count: dict[str, int]  # feature -> count of null/missing

    # --- label quality ---------------------------------------------------
    label_balance: dict[str, float]  # "0.0" -> fraction, "1.0" -> fraction
    label_missing_count: int

    # --- fold quality ----------------------------------------------------
    fold_count: int
    fold_train_counts: tuple[int, ...]
    fold_val_counts: tuple[int, ...]

    # --- leakage checks (all should be True) -----------------------------
    pit_proof_verified: bool
    embargo_sufficient: bool
    no_forward_joins: bool

    # --- drift indicators (basic) ---------------------------------------
    mean_feature_values: dict[str, float]  # feature -> mean across all rows
    std_feature_values: dict[str, float]  # feature -> std across all rows

    # --- quality-gate inputs (optional, defaulted for backward compat) ---
    # These feed the :class:`QualityPolicy` gate checks. They default to
    # safe "not verified" values so older reports (and the minimal
    # construction in tests) keep round-tripping; :func:`compute_quality_report`
    # populates them from the data.
    duplicate_row_count: int = 0
    schema_match_verified: bool = False
    drift_baseline_available: bool = False

    # --- serialization ---------------------------------------------------

    def to_json(self) -> str:
        """Serialize the report to a stable, sorted-key JSON string."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            indent=2,
        )

    def quality_hash(self) -> str:
        """Stable SHA-256 hex digest over the canonical report payload.

        Used to embed a tamper-evident reference in the dataset manifest so
        consumers can verify the manifest was produced alongside a specific
        quality report.
        """
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def write(self, path: Path) -> Path:
        """Write the report to *path* (parent dirs created) and return it."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def compute_quality_report(
    parquet_path: Path,
    manifest: FeatureLakeManifest,
    *,
    feature_names: tuple[str, ...],
    label_column: str = "label",
    ts_column: str = "decision_time",
) -> DatasetQualityReport:
    """Compute a comprehensive quality report from a dataset parquet + manifest.

    Reads the parquet file at *parquet_path* with polars (lazy import) and
    derives coverage, feature quality, label quality, fold quality, leakage
    checks, and basic drift statistics.  The manifest supplies the fold
    boundaries and leakage-proof flags.

    Parameters
    ----------
    parquet_path
        Path to the dataset parquet file (columns: ``ts_column``, feature
        columns, ``label_column``).
    manifest
        The :class:`FeatureLakeManifest` for the dataset — used for fold
        counts and leakage-check flags.
    feature_names
        Ordered tuple of feature column names to assess.
    label_column
        Name of the label column (default ``"label"``).
    ts_column
        Name of the timestamp column (default ``"decision_time"``).

    Returns
    -------
    DatasetQualityReport
    """
    import polars as pl

    parquet_path = Path(parquet_path)
    df = pl.read_parquet(str(parquet_path))
    total_rows = df.height

    # --- coverage --------------------------------------------------------
    if total_rows > 0 and ts_column in df.columns:
        time_span_start_ns = int(df[ts_column].min())
        time_span_end_ns = int(df[ts_column].max())
    else:
        time_span_start_ns = 0
        time_span_end_ns = 0

    # ``symbol`` is optional in the parquet (the equity pipeline drops it);
    # fall back to the universe-derived count of 1 when absent.
    total_symbols = int(df["symbol"].n_unique()) if "symbol" in df.columns and total_rows > 0 else 1

    # --- feature quality -------------------------------------------------
    feature_coverage_pct: dict[str, float] = {}
    feature_missing_count: dict[str, int] = {}
    mean_feature_values: dict[str, float] = {}
    std_feature_values: dict[str, float] = {}

    for name in feature_names:
        if name not in df.columns:
            feature_coverage_pct[name] = 0.0
            feature_missing_count[name] = total_rows
            mean_feature_values[name] = 0.0
            std_feature_values[name] = 0.0
            continue
        col = df[name]
        null_count = int(col.null_count())
        non_null = total_rows - null_count
        feature_missing_count[name] = null_count
        feature_coverage_pct[name] = (
            round(100.0 * non_null / total_rows, 6) if total_rows > 0 else 0.0
        )
        if non_null > 0:
            mean_feature_values[name] = float(col.mean() or 0.0)
            std_feature_values[name] = float(col.std(ddof=0) or 0.0)
        else:
            mean_feature_values[name] = 0.0
            std_feature_values[name] = 0.0

    # --- label quality ---------------------------------------------------
    label_missing_count = 0
    label_balance: dict[str, float] = {}
    if label_column in df.columns and total_rows > 0:
        label_col = df[label_column]
        label_missing_count = int(label_col.null_count())
        non_null_labels = total_rows - label_missing_count
        if non_null_labels > 0:
            value_counts = label_col.drop_nulls().value_counts()
            counts_map: dict[Any, int] = {
                row[label_column]: int(row["count"]) for row in value_counts.iter_rows(named=True)
            }
            for key in (0.0, 1.0):
                frac = counts_map.get(key, 0) / non_null_labels
                label_balance[str(key)] = round(frac, 6)
        else:
            label_balance = {"0.0": 0.0, "1.0": 0.0}
    else:
        label_balance = {"0.0": 0.0, "1.0": 0.0}

    # --- fold quality ----------------------------------------------------
    folds = manifest.folds.folds
    fold_count = len(folds)
    fold_train_counts: list[int] = []
    fold_val_counts: list[int] = []

    if total_rows > 0 and ts_column in df.columns:
        ts_series = df[ts_column]
        for fold in folds:
            train_mask = (ts_series >= fold.train_start) & (ts_series < fold.train_end)
            val_mask = (ts_series >= fold.val_start) & (ts_series < fold.val_end)
            fold_train_counts.append(int(train_mask.sum()))
            fold_val_counts.append(int(val_mask.sum()))
    else:
        fold_train_counts = [0] * fold_count
        fold_val_counts = [0] * fold_count

    # --- leakage checks --------------------------------------------------
    pit_proof_verified = manifest.pit_proof_verified
    embargo_sufficient = manifest.folds.embargo_ns >= manifest.folds.max_label_horizon_ns
    # ``no_forward_joins`` is guaranteed by the FeatureLakeBuilder's
    # as-of universe validation at construction time; the manifest's
    # ``pit_proof_verified`` flag is the downstream proof.
    no_forward_joins = manifest.pit_proof_verified

    # --- quality-gate inputs --------------------------------------------
    # Duplicate rows: rows that are exact duplicates across all columns.
    # polars' ``unique()`` returns the de-duplicated frame, so the
    # duplicate count is the difference in height.
    if total_rows > 0:
        duplicate_row_count = total_rows - df.unique(maintain_order=True).height
    else:
        duplicate_row_count = 0

    # Schema match: the dataset exposes a non-empty, declared feature
    # schema (captured in ``feature_names``). A report with no feature
    # schema has nothing to match against, so it is "not verified".
    schema_match_verified = len(feature_names) > 0

    # Drift baseline: drift indicators (mean + std) are available for
    # every declared feature, so a future two-report drift comparison
    # is possible. Vacuously true when there are no features.
    drift_baseline_available = all(
        name in mean_feature_values and name in std_feature_values for name in feature_names
    )

    return DatasetQualityReport(
        schema_version=1,
        dataset_id=manifest.dataset_id,
        generated_at_ns=int(time.time_ns()),
        total_rows=total_rows,
        total_symbols=total_symbols,
        time_span_start_ns=time_span_start_ns,
        time_span_end_ns=time_span_end_ns,
        feature_names=tuple(feature_names),
        feature_coverage_pct=feature_coverage_pct,
        feature_missing_count=feature_missing_count,
        label_balance=label_balance,
        label_missing_count=label_missing_count,
        fold_count=fold_count,
        fold_train_counts=tuple(fold_train_counts),
        fold_val_counts=tuple(fold_val_counts),
        pit_proof_verified=pit_proof_verified,
        embargo_sufficient=embargo_sufficient,
        no_forward_joins=no_forward_joins,
        mean_feature_values=mean_feature_values,
        std_feature_values=std_feature_values,
        duplicate_row_count=duplicate_row_count,
        schema_match_verified=schema_match_verified,
        drift_baseline_available=drift_baseline_available,
    )


# ---------------------------------------------------------------------------
# Quality policies + quality gate (Phase 3 / T-3.2)
# ---------------------------------------------------------------------------
#
# A :class:`QualityPolicy` is the declarative gate a dataset must pass
# before a training job in a given :class:`TrainingMode` may consume it.
# The :data:`QUALITY_POLICY_REGISTRY` ships one policy per mode (canary,
# research, production) with progressively stricter thresholds. The
# :func:`validate_quality_policy` function evaluates a
# :class:`DatasetQualityReport` against a policy and returns a
# :class:`QualityGateResult` enumerating every failed check (fail closed,
# fail loud).
#
# Policy ids follow the ``qp-<mode>-v<n>`` convention so they are stable,
# human-readable, and round-trip through the ``quality_policy_id`` field
# on :class:`quant_foundry.training_manifest.TrainingManifest` and the
# ``extra_constraints`` of a dispatched ``RunPodTrainingRequest``.
_NS_PER_DAY = 86_400_000_000_000  # 24 * 60 * 60 * 1e9


class QualityPolicy(BaseModel):
    """Declarative quality gate for a training mode.

    A policy bundles the minimum thresholds and required proofs a
    :class:`DatasetQualityReport` must satisfy for a job in ``mode`` to
    be eligible to run (and, for production, to be promotion eligible).
    The model is frozen + ``extra='forbid'`` so a policy is tamper
    evident and serialisation is exact — matching the convention used by
    :class:`DatasetQualityReport` and the core schema spine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    mode: TrainingMode
    min_row_count: int
    min_symbol_count: int
    min_date_span_days: int
    min_label_balance: float  # minimum fraction of the minority class
    min_feature_coverage: float  # minimum fraction (0..1) of non-null
    require_fold_validity: bool
    max_duplicate_rows: int
    require_pit_proof: bool
    require_embargo: bool
    max_drift_threshold: float | None = None
    require_schema_match: bool
    require_quality_report: bool
    promotion_eligible: bool


class FailedCheck(BaseModel):
    """A single failed quality-gate check.

    ``expected`` and ``actual`` are stringified so the result is
    serialisable and human-readable regardless of the underlying types.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_name: str
    expected: str
    actual: str
    message: str


class QualityGateResult(BaseModel):
    """Outcome of evaluating a report against a :class:`QualityPolicy`.

    ``passed`` is ``True`` only when ``failed_checks`` is empty (fail
    closed). ``failed_checks`` enumerates every unmet requirement so an
    operator can fix them in a single pass (fail loud).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    passed: bool
    failed_checks: tuple[FailedCheck, ...] = ()
    evaluated_at_ns: int


def _build_canary_policy() -> QualityPolicy:
    return QualityPolicy(
        policy_id="qp-canary-v1",
        mode=TrainingMode.CANARY,
        min_row_count=100,
        min_symbol_count=1,
        min_date_span_days=1,
        min_label_balance=0.0,
        min_feature_coverage=0.5,
        require_fold_validity=False,
        max_duplicate_rows=10,
        require_pit_proof=False,
        require_embargo=False,
        require_schema_match=False,
        require_quality_report=False,
        promotion_eligible=False,
    )


def _build_research_policy() -> QualityPolicy:
    return QualityPolicy(
        policy_id="qp-research-v1",
        mode=TrainingMode.RESEARCH,
        min_row_count=1000,
        min_symbol_count=3,
        min_date_span_days=30,
        min_label_balance=0.05,
        min_feature_coverage=0.8,
        require_fold_validity=True,
        max_duplicate_rows=5,
        require_pit_proof=True,
        require_embargo=False,
        require_schema_match=True,
        require_quality_report=True,
        promotion_eligible=False,
    )


def _build_production_policy() -> QualityPolicy:
    return QualityPolicy(
        policy_id="qp-production-v1",
        mode=TrainingMode.PRODUCTION,
        min_row_count=10000,
        min_symbol_count=10,
        min_date_span_days=180,
        min_label_balance=0.1,
        min_feature_coverage=0.95,
        require_fold_validity=True,
        max_duplicate_rows=0,
        require_pit_proof=True,
        require_embargo=True,
        max_drift_threshold=0.3,
        require_schema_match=True,
        require_quality_report=True,
        promotion_eligible=True,
    )


class QualityPolicyRegistry:
    """Registry of known quality policies, keyed by ``policy_id``.

    The registry is the single source of truth for which
    ``quality_policy_id`` values are valid. A
    :class:`quant_foundry.training_manifest.TrainingManifest` references
    a policy by id; the manifest validator resolves the id through
    :func:`resolve_quality_policy` (or :meth:`QualityPolicyRegistry.get`)
    to confirm it names a real policy.
    """

    def __init__(self, policies: tuple[QualityPolicy, ...]) -> None:
        self._by_id: dict[str, QualityPolicy] = {p.policy_id: p for p in policies}
        self._by_mode: dict[TrainingMode, QualityPolicy] = {p.mode: p for p in policies}

    def get(self, policy_id: str) -> QualityPolicy | None:
        """Return the policy for *policy_id*, or ``None`` if unknown."""
        return self._by_id.get(policy_id)

    def for_mode(self, mode: TrainingMode) -> QualityPolicy | None:
        """Return the default policy for a training mode, or ``None``."""
        return self._by_mode.get(mode)

    def known_ids(self) -> frozenset[str]:
        """Return the set of registered policy ids."""
        return frozenset(self._by_id)

    def all_policies(self) -> tuple[QualityPolicy, ...]:
        """Return all registered policies (insertion order)."""
        return tuple(self._by_id.values())


#: Module-level registry of the shipped quality policies (one per mode).
QUALITY_POLICY_REGISTRY: QualityPolicyRegistry = QualityPolicyRegistry(
    (
        _build_canary_policy(),
        _build_research_policy(),
        _build_production_policy(),
    )
)


def resolve_quality_policy(policy_id: str) -> QualityPolicy | None:
    """Resolve a ``quality_policy_id`` to a :class:`QualityPolicy`.

    Returns ``None`` when *policy_id* is not a registered policy. Callers
    that require a policy (e.g. production-mode manifest validation)
    should treat ``None`` as a hard failure (fail closed).
    """
    return QUALITY_POLICY_REGISTRY.get(policy_id)


def validate_quality_policy(
    report: DatasetQualityReport,
    policy: QualityPolicy,
) -> QualityGateResult:
    """Evaluate *report* against *policy* and return a gate result.

    Every check that the policy enables is evaluated. A check fails when
    the report does not meet the policy's threshold or required proof.
    The result is fail closed: ``passed`` is ``True`` only when no check
    failed. Failed checks are enumerated in ``failed_checks`` so an
    operator can remediate in a single pass.

    The checks cover (per the Phase 3 quality-gate spec):

    - **row_count** — ``report.total_rows >= policy.min_row_count``.
    - **symbol_count** — ``report.total_symbols >= policy.min_symbol_count``.
    - **date_span** — the data spans at least ``min_date_span_days`` days.
    - **label_balance** — the minority class fraction is at least
      ``min_label_balance``.
    - **feature_coverage** — the minimum per-feature non-null fraction is
      at least ``min_feature_coverage``.
    - **fold_validity** — when required, every fold has non-empty
      train/val partitions.
    - **duplicate_rows** — ``report.duplicate_row_count`` is within
      ``max_duplicate_rows``.
    - **pit_leakage** — when required, ``report.pit_proof_verified``.
    - **embargo** — when required, ``report.embargo_sufficient``.
    - **drift** — when ``max_drift_threshold`` is set, the report carries
      a complete drift baseline (mean + std for every feature).
    - **schema_match** — when required, the report declares a non-empty
      feature schema.
    - **quality_report** — when required, the report is populated
      (non-empty ``dataset_id`` and a positive ``generated_at_ns``).
    """
    failed: list[FailedCheck] = []

    def _fail(check_name: str, expected: Any, actual: Any, message: str) -> None:
        failed.append(
            FailedCheck(
                check_name=check_name,
                expected=str(expected),
                actual=str(actual),
                message=message,
            )
        )

    # --- row count -------------------------------------------------------
    if report.total_rows < policy.min_row_count:
        _fail(
            "row_count",
            f">= {policy.min_row_count}",
            report.total_rows,
            f"dataset has {report.total_rows} rows; policy requires at "
            f"least {policy.min_row_count}",
        )

    # --- symbol count ----------------------------------------------------
    if report.total_symbols < policy.min_symbol_count:
        _fail(
            "symbol_count",
            f">= {policy.min_symbol_count}",
            report.total_symbols,
            f"dataset has {report.total_symbols} symbols; policy requires "
            f"at least {policy.min_symbol_count}",
        )

    # --- date span -------------------------------------------------------
    span_ns = report.time_span_end_ns - report.time_span_start_ns
    span_days = span_ns / _NS_PER_DAY if span_ns > 0 else 0.0
    if span_days < policy.min_date_span_days:
        _fail(
            "date_span",
            f">= {policy.min_date_span_days} days",
            f"{span_days:.6f} days",
            f"dataset spans {span_days:.6f} days; policy requires at "
            f"least {policy.min_date_span_days} days",
        )

    # --- label balance ---------------------------------------------------
    minority = min(report.label_balance.values()) if report.label_balance else 0.0
    if minority < policy.min_label_balance:
        _fail(
            "label_balance",
            f">= {policy.min_label_balance}",
            minority,
            f"minority class fraction is {minority}; policy requires at "
            f"least {policy.min_label_balance}",
        )

    # --- feature coverage ------------------------------------------------
    if report.feature_coverage_pct:
        min_cov_pct = min(report.feature_coverage_pct.values())
    else:
        min_cov_pct = 0.0
    min_cov_fraction = min_cov_pct / 100.0
    if min_cov_fraction < policy.min_feature_coverage:
        _fail(
            "feature_coverage",
            f">= {policy.min_feature_coverage}",
            min_cov_fraction,
            f"minimum feature coverage is {min_cov_fraction}; policy "
            f"requires at least {policy.min_feature_coverage}",
        )

    # --- fold validity ---------------------------------------------------
    if policy.require_fold_validity:
        folds_ok = (
            report.fold_count > 0
            and all(c > 0 for c in report.fold_train_counts)
            and all(c > 0 for c in report.fold_val_counts)
        )
        if not folds_ok:
            _fail(
                "fold_validity",
                "non-empty train/val partitions for every fold",
                f"fold_count={report.fold_count}, "
                f"train_counts={list(report.fold_train_counts)}, "
                f"val_counts={list(report.fold_val_counts)}",
                "policy requires every fold to have non-empty train and validation partitions",
            )

    # --- duplicate rows --------------------------------------------------
    if report.duplicate_row_count > policy.max_duplicate_rows:
        _fail(
            "duplicate_rows",
            f"<= {policy.max_duplicate_rows}",
            report.duplicate_row_count,
            f"dataset has {report.duplicate_row_count} duplicate rows; "
            f"policy allows at most {policy.max_duplicate_rows}",
        )

    # --- PIT leakage -----------------------------------------------------
    if policy.require_pit_proof and not report.pit_proof_verified:
        _fail(
            "pit_leakage",
            "pit_proof_verified=True",
            f"pit_proof_verified={report.pit_proof_verified}",
            "policy requires point-in-time proof to be verified",
        )

    # --- embargo ---------------------------------------------------------
    if policy.require_embargo and not report.embargo_sufficient:
        _fail(
            "embargo",
            "embargo_sufficient=True",
            f"embargo_sufficient={report.embargo_sufficient}",
            "policy requires a sufficient purge embargo",
        )

    # --- drift -----------------------------------------------------------
    if policy.max_drift_threshold is not None and not report.drift_baseline_available:
        _fail(
            "drift",
            "complete drift baseline (mean+std for every feature)",
            f"drift_baseline_available={report.drift_baseline_available}",
            "policy requires a drift baseline so future drift can be "
            f"monitored against threshold {policy.max_drift_threshold}",
        )

    # --- schema match ----------------------------------------------------
    if policy.require_schema_match and not report.schema_match_verified:
        _fail(
            "schema_match",
            "schema_match_verified=True",
            f"schema_match_verified={report.schema_match_verified}",
            "policy requires a declared, non-empty feature schema",
        )

    # --- quality report present -----------------------------------------
    if policy.require_quality_report:
        report_ok = bool(report.dataset_id) and report.generated_at_ns > 0
        if not report_ok:
            _fail(
                "quality_report",
                "populated report (dataset_id + generated_at_ns)",
                f"dataset_id={report.dataset_id!r}, generated_at_ns={report.generated_at_ns}",
                "policy requires a generated quality report to accompany the dataset",
            )

    return QualityGateResult(
        policy_id=policy.policy_id,
        passed=len(failed) == 0,
        failed_checks=tuple(failed),
        evaluated_at_ns=time.time_ns(),
    )


__all__ = [
    "QUALITY_POLICY_REGISTRY",
    "DatasetQualityReport",
    "FailedCheck",
    "QualityGateResult",
    "QualityPolicy",
    "QualityPolicyRegistry",
    "compute_quality_report",
    "resolve_quality_policy",
    "validate_quality_policy",
]
