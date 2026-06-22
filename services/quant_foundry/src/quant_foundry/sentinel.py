"""
quant_foundry.sentinel — the Leakage and Overfit Sentinel (TASK-0406).

The sentinel is the adversarial check that an automated, high-throughput
training pipeline needs: when you train thousands of candidates, leakage
and luck are not edge cases, they are the default failure mode. This is the
cheapest insurance the Quant Foundry can buy — it runs on CPU against
fixtures and dossiers, with no GPU cost.

What the sentinel checks (cross-cutting rigor §5 — leakage/overfit defense):

1. **Negative-control battery:**
   - **Shuffled labels:** retrain on shuffled labels; if the model still
     "finds alpha" (claimed edge >> 0), it is leaking. A pipeline that still
     finds alpha on shuffled labels is leaking, full stop.
   - **Time-reversed features:** reverse the temporal order of features; if
     the model still finds edge, it is exploiting temporal structure that
     shouldn't exist if features are truly predictive.
   - **Future-leak injection:** check that no feature has
     ``observed_at > decision_time`` (point-in-time violation).

2. **Purged-fold verifier:** confirm a dossier's reported folds actually
   carry purge + embargo and that no training row overlaps a validation
   label window.

3. **PBO estimate:** compute the Probability of Backtest Overfitting (CSCV)
   over a candidate family and attach it to the dossier; flag families above
   a configurable threshold.

4. **Train/live gap check:** compare in-sample vs. settled live calibration
   and edge; a large, persistent gap is an overfit flag feeding TASK-0703
   (edge-decay).

5. **Feature stability:** flag features whose importance or distribution is
   wildly unstable across folds (likely artifacts, not signal).

A failing sentinel emits a **sentinel receipt** per candidate family and
writes a hard ``blocking_issue`` on the dossier via
``DossierRegistry.add_blocking_issue`` (TASK-0403). The promotion gate
(TASK-0702) refuses to override a sentinel blocking issue without an
explicit, recorded human waiver.

File-disjoint from all active builders (see BUILDER3.md). Imports from my
own ``dossier.py`` / ``registry.py`` (TASK-0403 — my files). Does NOT import
``outcomes.py`` / ``settlement.py`` (Builder 1), ``feature_lake.py`` /
``dataset_manifest.py`` (Builder 4) — uses local schemas for
feature/settlement data.
"""

from __future__ import annotations

import statistics
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_foundry.dossier import DossierRecord
from quant_foundry.pbo import probability_of_backtest_overfitting
from quant_foundry.registry import DossierRegistry

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LeakyFeatureError(ValueError):
    """Raised when a feature observation violates point-in-time (observed_at > decision_time).

    This mirrors the pattern from Builder 4's ``dataset_manifest.py`` but is
    defined here to keep file-disjoint (the sentinel does not import
    ``dataset_manifest.py``).
    """


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SentinelCheck(StrEnum):
    """Which check the sentinel should run.

    - ``SHUFFLED_LABEL``: negative control — retrain on shuffled labels.
    - ``TIME_REVERSE``: negative control — reverse temporal order of features.
    - ``FUTURE_LEAK``: negative control — check for point-in-time violations.
    - ``FULL_BATTERY``: run all checks (negative controls + purged-fold +
      PBO + train/live gap + feature stability, as provided).
    """

    SHUFFLED_LABEL = "shuffled_label"
    TIME_REVERSE = "time_reverse"
    FUTURE_LEAK = "future_leak"
    FULL_BATTERY = "full_battery"


class SentinelSeverity(StrEnum):
    """Severity of a sentinel issue.

    - ``BLOCKING``: a hard gate on promotion (writes a ``blocking_issue``).
    - ``WARNING``: a soft flag (visible but not a hard gate).
    """

    BLOCKING = "blocking"
    WARNING = "warning"


class FoldSpec(BaseModel):
    """One cross-validation fold spec for the purged-fold verifier.

    Frozen + extra='forbid'. The fold defines a train window [train_start,
    train_end) and a validation window [val_start, val_end). The purge gap
    is the required gap between train_end and val_start; the embargo gap is
    the required gap after val_end before the next fold's training can
    resume.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fold_id: int
    train_start: int
    train_end: int
    val_start: int
    val_end: int


class TrainLiveGapInput(BaseModel):
    """Input for the train/live gap check.

    Carries in-sample vs. settled live edge and calibration (Brier) so the
    sentinel can flag a large persistent gap (overfit signal).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    in_sample_edge: float
    live_edge: float
    in_sample_brier: float
    live_brier: float
    n_live_settled: int = 0


class FeatureStabilityInput(BaseModel):
    """Input for the feature stability check.

    Carries per-feature importance across folds so the sentinel can flag
    wildly unstable features (likely artifacts, not signal).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    feature_importances: dict[str, list[float]] = Field(default_factory=dict)


class SentinelInput(BaseModel):
    """Input to the sentinel for one candidate family.

    Frozen + extra='forbid'. Carries everything the sentinel needs to run
    one or more checks: the model_id, which check to run, and the data for
    each check (claimed edge, feature observations, folds, train/live gap,
    feature stability, PBO data).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    check: SentinelCheck = SentinelCheck.FULL_BATTERY
    # Negative-control inputs.
    claimed_edge: float = 0.0
    baseline_edge: float = 0.0
    n_samples: int = 0
    seed: int = 0
    feature_observations: list[dict[str, Any]] = Field(default_factory=list)
    # Purged-fold inputs.
    folds: list[FoldSpec] = Field(default_factory=list)
    purge_gap: int = 0
    embargo_gap: int = 0
    # Train/live gap input.
    train_live_gap: TrainLiveGapInput | None = None
    # Feature stability input.
    feature_stability: FeatureStabilityInput | None = None
    # PBO inputs (optional; if provided, PBO is computed).
    pbo_is_returns: list[list[float]] | None = None
    pbo_oos_returns: list[list[float]] | None = None
    pbo_threshold: float = 0.1

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_id must be non-empty")
        return v


class SentinelIssue(BaseModel):
    """One issue detected by the sentinel.

    Frozen + extra='forbid'. Carries the check code, severity, and a
    human-readable message.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    severity: SentinelSeverity = SentinelSeverity.BLOCKING
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class SentinelReceipt(BaseModel):
    """The result of running the sentinel on one candidate family.

    Frozen + extra='forbid'. Carries the model_id, the list of issues found,
    whether the sentinel passed (no blocking issues), the list of checks
    run, and a timestamp. ``to_dict`` is JSON serializable for audit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    issues: list[SentinelIssue] = Field(default_factory=list)
    passed: bool = True
    checks_run: list[str] = Field(default_factory=list)
    ts_ns: int = 0
    # Optional PBO result (if PBO was computed).
    pbo: float | None = None
    pbo_flagged: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "model_id": self.model_id,
            "issues": [i.model_dump() for i in self.issues],
            "passed": self.passed,
            "checks_run": list(self.checks_run),
            "ts_ns": self.ts_ns,
            "pbo": self.pbo,
            "pbo_flagged": self.pbo_flagged,
        }


# ---------------------------------------------------------------------------
# The sentinel
# ---------------------------------------------------------------------------


# Thresholds (conservative defaults; configurable per call).
_DEFAULT_EDGE_THRESHOLD = 0.001  # 10 bps — any "edge" above this on
# shuffled/reversed labels is a leak flag.
_DEFAULT_GAP_RATIO_THRESHOLD = 0.5  # live edge < 50% of IS edge => flag.
_DEFAULT_CALIBRATION_GAP_THRESHOLD = 0.15  # 15 bps Brier gap => flag.
_DEFAULT_FEATURE_CV_THRESHOLD = 0.5  # CV of importance > 50% => flag.


class LeakageSentinel:
    """The leakage and overfit sentinel.

    Runs one or more checks on a candidate family and emits a
    ``SentinelReceipt``. A failing sentinel (any blocking issue) can write
    ``blocking_issue`` entries on the dossier via
    ``DossierRegistry.add_blocking_issue`` (TASK-0403) — a hard gate on
    promotion.

    Deterministic given a fixed ``seed`` (the only randomized step is PBO
    combination sampling, which uses a seeded ``random.Random``).
    """

    def __init__(
        self,
        seed: int = 0,
        edge_threshold: float = _DEFAULT_EDGE_THRESHOLD,
        gap_ratio_threshold: float = _DEFAULT_GAP_RATIO_THRESHOLD,
        calibration_gap_threshold: float = _DEFAULT_CALIBRATION_GAP_THRESHOLD,
        feature_cv_threshold: float = _DEFAULT_FEATURE_CV_THRESHOLD,
    ) -> None:
        self.seed = seed
        self.edge_threshold = edge_threshold
        self.gap_ratio_threshold = gap_ratio_threshold
        self.calibration_gap_threshold = calibration_gap_threshold
        self.feature_cv_threshold = feature_cv_threshold

    # -- Point-in-time assertion ------------------------------------------

    @staticmethod
    def assert_point_in_time(
        decision_time: int, observed_at: int, feature: str
    ) -> None:
        """Assert that a feature observation is point-in-time correct.

        ``observed_at`` must be <= ``decision_time`` (the feature was
        observed before or at the decision time). Raises
        ``LeakyFeatureError`` if violated.
        """
        if observed_at > decision_time:
            raise LeakyFeatureError(
                f"feature '{feature}' observed_at={observed_at} > "
                f"decision_time={decision_time} (future leak)"
            )

    # -- Negative-control battery -----------------------------------------

    def run_negative_control(self, inp: SentinelInput) -> SentinelReceipt:
        """Run a single negative-control check (specified by ``inp.check``)."""
        if inp.check == SentinelCheck.SHUFFLED_LABEL:
            return self._check_shuffled_label(inp)
        if inp.check == SentinelCheck.TIME_REVERSE:
            return self._check_time_reverse(inp)
        if inp.check == SentinelCheck.FUTURE_LEAK:
            return self._check_future_leak(inp)
        if inp.check == SentinelCheck.FULL_BATTERY:
            return self.run(inp)
        raise ValueError(f"unknown sentinel check: {inp.check}")

    def _check_shuffled_label(self, inp: SentinelInput) -> SentinelReceipt:
        """Shuffled-label negative control.

        If the model claims a non-trivial edge on shuffled labels, it is
        leaking. The threshold is ``edge_threshold`` (default 10 bps).
        """
        issues: list[SentinelIssue] = []
        checks_run = ["shuffled_label"]
        excess = inp.claimed_edge - inp.baseline_edge
        if excess > self.edge_threshold:
            issues.append(SentinelIssue(
                code="shuffled_label_edge",
                severity=SentinelSeverity.BLOCKING,
                message=(
                    f"model claims edge {inp.claimed_edge:.6f} on shuffled "
                    f"labels (baseline {inp.baseline_edge:.6f}, excess "
                    f"{excess:.6f} > threshold {self.edge_threshold:.6f}); "
                    "a pipeline that finds alpha on shuffled labels is leaking"
                ),
                detail={
                    "claimed_edge": inp.claimed_edge,
                    "baseline_edge": inp.baseline_edge,
                    "excess": excess,
                    "threshold": self.edge_threshold,
                    "n_samples": inp.n_samples,
                },
            ))
        return SentinelReceipt(
            model_id=inp.model_id,
            issues=issues,
            passed=len(issues) == 0,
            checks_run=checks_run,
            ts_ns=time.time_ns(),
        )

    def _check_time_reverse(self, inp: SentinelInput) -> SentinelReceipt:
        """Time-reversed features negative control.

        If the model claims a non-trivial edge on time-reversed features, it
        is exploiting temporal structure that shouldn't exist if features
        are truly predictive.
        """
        issues: list[SentinelIssue] = []
        checks_run = ["time_reverse"]
        excess = inp.claimed_edge - inp.baseline_edge
        if excess > self.edge_threshold:
            issues.append(SentinelIssue(
                code="time_reversed_edge",
                severity=SentinelSeverity.BLOCKING,
                message=(
                    f"model claims edge {inp.claimed_edge:.6f} on "
                    f"time-reversed features (baseline "
                    f"{inp.baseline_edge:.6f}, excess {excess:.6f} > "
                    f"threshold {self.edge_threshold:.6f}); model is "
                    "exploiting temporal structure that shouldn't exist"
                ),
                detail={
                    "claimed_edge": inp.claimed_edge,
                    "baseline_edge": inp.baseline_edge,
                    "excess": excess,
                    "threshold": self.edge_threshold,
                    "n_samples": inp.n_samples,
                },
            ))
        return SentinelReceipt(
            model_id=inp.model_id,
            issues=issues,
            passed=len(issues) == 0,
            checks_run=checks_run,
            ts_ns=time.time_ns(),
        )

    def _check_future_leak(self, inp: SentinelInput) -> SentinelReceipt:
        """Future-leak negative control.

        Check that no feature observation has ``observed_at > decision_time``
        (point-in-time violation).
        """
        issues: list[SentinelIssue] = []
        checks_run = ["future_leak"]
        for obs in inp.feature_observations:
            decision_time = obs.get("decision_time", 0)
            observed_at = obs.get("observed_at", 0)
            feature = obs.get("feature", "unknown")
            if observed_at > decision_time:
                issues.append(SentinelIssue(
                    code="future_leak_feature",
                    severity=SentinelSeverity.BLOCKING,
                    message=(
                        f"feature '{feature}' observed_at={observed_at} > "
                        f"decision_time={decision_time} (future leak)"
                    ),
                    detail={
                        "feature": feature,
                        "decision_time": decision_time,
                        "observed_at": observed_at,
                    },
                ))
        return SentinelReceipt(
            model_id=inp.model_id,
            issues=issues,
            passed=len(issues) == 0,
            checks_run=checks_run,
            ts_ns=time.time_ns(),
        )

    # -- Purged-fold verifier ---------------------------------------------

    def verify_purged_folds(
        self,
        model_id: str,
        folds: list[FoldSpec],
        purge_gap: int,
        embargo_gap: int,
    ) -> SentinelReceipt:
        """Verify that folds carry purge + embargo and no train/val overlap.

        Checks:
        - For each fold, the gap between ``train_end`` and ``val_start`` must
          be >= ``purge_gap`` (purge).
        - For each fold, the gap between ``val_end`` and the next fold's
          ``train_start`` must be >= ``embargo_gap`` (embargo).
        - No training row overlaps a validation window.
        """
        issues: list[SentinelIssue] = []
        checks_run = ["purged_fold_verify"]

        for fold in folds:
            # Purge gap: val_start - train_end >= purge_gap.
            purge_actual = fold.val_start - fold.train_end
            if purge_actual < purge_gap:
                issues.append(SentinelIssue(
                    code="missing_purge_gap",
                    severity=SentinelSeverity.BLOCKING,
                    message=(
                        f"fold {fold.fold_id}: purge gap {purge_actual} < "
                        f"required {purge_gap} (train_end={fold.train_end}, "
                        f"val_start={fold.val_start})"
                    ),
                    detail={
                        "fold_id": fold.fold_id,
                        "purge_actual": purge_actual,
                        "purge_required": purge_gap,
                    },
                ))
            # Train/val overlap: train_end > val_start.
            if fold.train_end > fold.val_start:
                issues.append(SentinelIssue(
                    code="train_val_overlap",
                    severity=SentinelSeverity.BLOCKING,
                    message=(
                        f"fold {fold.fold_id}: train_end={fold.train_end} > "
                        f"val_start={fold.val_start} (training rows overlap "
                        "validation window)"
                    ),
                    detail={
                        "fold_id": fold.fold_id,
                        "train_end": fold.train_end,
                        "val_start": fold.val_start,
                    },
                ))

        # Embargo gap: for consecutive folds, next fold's train_start -
        # this fold's val_end >= embargo_gap.
        sorted_folds = sorted(folds, key=lambda f: f.fold_id)
        for i in range(len(sorted_folds) - 1):
            this_fold = sorted_folds[i]
            next_fold = sorted_folds[i + 1]
            embargo_actual = next_fold.train_start - this_fold.val_end
            if embargo_actual < embargo_gap:
                issues.append(SentinelIssue(
                    code="missing_embargo_gap",
                    severity=SentinelSeverity.BLOCKING,
                    message=(
                        f"embargo gap between fold {this_fold.fold_id} "
                        f"(val_end={this_fold.val_end}) and fold "
                        f"{next_fold.fold_id} (train_start="
                        f"{next_fold.train_start}) is {embargo_actual} < "
                        f"required {embargo_gap}"
                    ),
                    detail={
                        "fold_id_a": this_fold.fold_id,
                        "fold_id_b": next_fold.fold_id,
                        "embargo_actual": embargo_actual,
                        "embargo_required": embargo_gap,
                    },
                ))

        return SentinelReceipt(
            model_id=model_id,
            issues=issues,
            passed=len(issues) == 0,
            checks_run=checks_run,
            ts_ns=time.time_ns(),
        )

    # -- Train/live gap check ---------------------------------------------

    def check_train_live_gap(
        self, inp: TrainLiveGapInput
    ) -> SentinelReceipt:
        """Check for a large persistent train/live gap (overfit signal).

        Flags:
        - ``train_live_edge_gap``: live edge < gap_ratio_threshold * IS edge.
        - ``train_live_calibration_gap``: live Brier - IS Brier > threshold.
        """
        issues: list[SentinelIssue] = []
        checks_run = ["train_live_gap"]

        # Edge gap: live edge should be at least gap_ratio_threshold * IS edge.
        if inp.in_sample_edge > 0:
            ratio = inp.live_edge / inp.in_sample_edge if inp.in_sample_edge != 0 else 1.0
            if ratio < self.gap_ratio_threshold:
                issues.append(SentinelIssue(
                    code="train_live_edge_gap",
                    severity=SentinelSeverity.BLOCKING,
                    message=(
                        f"live edge {inp.live_edge:.6f} is "
                        f"{ratio:.1%} of IS edge {inp.in_sample_edge:.6f} "
                        f"(< threshold {self.gap_ratio_threshold:.0%}); "
                        "large persistent train/live gap is an overfit flag"
                    ),
                    detail={
                        "in_sample_edge": inp.in_sample_edge,
                        "live_edge": inp.live_edge,
                        "ratio": ratio,
                        "threshold": self.gap_ratio_threshold,
                        "n_live_settled": inp.n_live_settled,
                    },
                ))

        # Calibration gap: live Brier - IS Brier should be small.
        brier_gap = inp.live_brier - inp.in_sample_brier
        if brier_gap > self.calibration_gap_threshold:
            issues.append(SentinelIssue(
                code="train_live_calibration_gap",
                severity=SentinelSeverity.BLOCKING,
                message=(
                    f"live Brier {inp.live_brier:.4f} - IS Brier "
                    f"{inp.in_sample_brier:.4f} = {brier_gap:.4f} > "
                    f"threshold {self.calibration_gap_threshold:.4f}; "
                    "calibration has degraded significantly in live trading"
                ),
                detail={
                    "in_sample_brier": inp.in_sample_brier,
                    "live_brier": inp.live_brier,
                    "gap": brier_gap,
                    "threshold": self.calibration_gap_threshold,
                    "n_live_settled": inp.n_live_settled,
                },
            ))

        return SentinelReceipt(
            model_id=inp.model_id,
            issues=issues,
            passed=len(issues) == 0,
            checks_run=checks_run,
            ts_ns=time.time_ns(),
        )

    # -- Feature stability check ------------------------------------------

    def check_feature_stability(
        self, inp: FeatureStabilityInput
    ) -> SentinelReceipt:
        """Flag features whose importance is wildly unstable across folds.

        Uses the coefficient of variation (CV = std/mean) of each feature's
        importance across folds. A CV > ``feature_cv_threshold`` (default
        50%) indicates the feature's importance is unstable — likely an
        artifact, not signal.
        """
        issues: list[SentinelIssue] = []
        checks_run = ["feature_stability"]

        for feature, importances in inp.feature_importances.items():
            if len(importances) < 2:
                continue
            mean_imp = statistics.fmean(importances)
            if abs(mean_imp) < 1e-10:
                # Mean importance near zero — skip (can't compute meaningful CV).
                continue
            std_imp = statistics.pstdev(importances)
            cv = std_imp / abs(mean_imp)
            if cv > self.feature_cv_threshold:
                issues.append(SentinelIssue(
                    code="unstable_feature_importance",
                    severity=SentinelSeverity.BLOCKING,
                    message=(
                        f"feature '{feature}' importance CV={cv:.2%} > "
                        f"threshold {self.feature_cv_threshold:.0%} "
                        f"(mean={mean_imp:.4f}, std={std_imp:.4f}); "
                        "wildly unstable importance is likely an artifact"
                    ),
                    detail={
                        "feature": feature,
                        "cv": cv,
                        "mean": mean_imp,
                        "std": std_imp,
                        "threshold": self.feature_cv_threshold,
                        "importances": list(importances),
                    },
                ))

        return SentinelReceipt(
            model_id=inp.model_id,
            issues=issues,
            passed=len(issues) == 0,
            checks_run=checks_run,
            ts_ns=time.time_ns(),
        )

    # -- Full battery ------------------------------------------------------

    def run(self, inp: SentinelInput) -> SentinelReceipt:
        """Run the full sentinel battery on a candidate family.

        Runs all checks whose inputs are provided on ``inp``:
        - Shuffled-label negative control (always).
        - Time-reversed features negative control (always).
        - Future-leak check (if feature_observations provided).
        - Purged-fold verifier (if folds provided).
        - PBO (if pbo_is_returns + pbo_oos_returns provided).
        - Train/live gap (if train_live_gap provided).
        - Feature stability (if feature_stability provided).

        Returns a single ``SentinelReceipt`` aggregating all issues.
        """
        all_issues: list[SentinelIssue] = []
        checks_run: list[str] = []
        pbo_val: float | None = None
        pbo_flagged: bool | None = None

        # Shuffled label.
        r = self._check_shuffled_label(inp)
        all_issues.extend(r.issues)
        checks_run.extend(r.checks_run)

        # Time reverse.
        r = self._check_time_reverse(inp)
        all_issues.extend(r.issues)
        checks_run.extend(r.checks_run)

        # Future leak.
        if inp.feature_observations:
            r = self._check_future_leak(inp)
            all_issues.extend(r.issues)
            checks_run.extend(r.checks_run)

        # Purged folds.
        if inp.folds:
            r = self.verify_purged_folds(
                model_id=inp.model_id,
                folds=inp.folds,
                purge_gap=inp.purge_gap,
                embargo_gap=inp.embargo_gap,
            )
            all_issues.extend(r.issues)
            checks_run.extend(r.checks_run)

        # PBO.
        if inp.pbo_is_returns and inp.pbo_oos_returns:
            pbo_result = probability_of_backtest_overfitting(
                is_returns=inp.pbo_is_returns,
                oos_returns=inp.pbo_oos_returns,
                seed=inp.seed,
                threshold=inp.pbo_threshold,
            )
            pbo_val = pbo_result.pbo
            pbo_flagged = pbo_result.flagged
            checks_run.append("pbo")
            if pbo_result.flagged:
                all_issues.append(SentinelIssue(
                    code="pbo_overfit",
                    severity=SentinelSeverity.BLOCKING,
                    message=(
                        f"PBO={pbo_result.pbo:.4f} > threshold "
                        f"{inp.pbo_threshold:.4f}; family is likely overfit "
                        f"(logit={pbo_result.logit:.4f}, "
                        f"n_candidates={pbo_result.n_candidates}, "
                        f"n_combinations={pbo_result.n_combinations})"
                    ),
                    detail={
                        "pbo": pbo_result.pbo,
                        "logit": pbo_result.logit,
                        "n_candidates": pbo_result.n_candidates,
                        "n_combinations": pbo_result.n_combinations,
                        "threshold": inp.pbo_threshold,
                    },
                ))

        # Train/live gap.
        if inp.train_live_gap:
            r = self.check_train_live_gap(inp.train_live_gap)
            all_issues.extend(r.issues)
            checks_run.extend(r.checks_run)

        # Feature stability.
        if inp.feature_stability:
            r = self.check_feature_stability(inp.feature_stability)
            all_issues.extend(r.issues)
            checks_run.extend(r.checks_run)

        blocking = [i for i in all_issues if i.severity == SentinelSeverity.BLOCKING]
        return SentinelReceipt(
            model_id=inp.model_id,
            issues=all_issues,
            passed=len(blocking) == 0,
            checks_run=checks_run,
            ts_ns=time.time_ns(),
            pbo=pbo_val,
            pbo_flagged=pbo_flagged,
        )

    # -- Write blocking issues to dossier registry ------------------------

    def write_blocking_issues(
        self, registry: DossierRegistry, receipt: SentinelReceipt
    ) -> DossierRecord | None:
        """Write blocking issues from a sentinel receipt to the dossier registry.

        A failing sentinel (any blocking issue) writes a ``blocking_issue``
        on the dossier via ``DossierRegistry.add_blocking_issue``. The
        promotion gate (TASK-0702) refuses to override a sentinel blocking
        issue without an explicit, recorded human waiver.

        Returns the updated ``DossierRecord``, or ``None`` if the receipt
        passed (no blocking issues to write).
        """
        if receipt.passed:
            return None

        blocking = [i for i in receipt.issues if i.severity == SentinelSeverity.BLOCKING]
        if not blocking:
            return None

        # Write one blocking_issue per blocking issue found (each is a
        # separate gate so the operator can see exactly which checks failed).
        updated: DossierRecord | None = None
        for issue in blocking:
            updated = registry.add_blocking_issue(
                model_id=receipt.model_id,
                source="sentinel",
                code=issue.code,
                note=issue.message,
            )
        return updated
