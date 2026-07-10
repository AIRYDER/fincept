"""
quant_foundry.verification_matrix — tiered verification matrix for local CI.

T-TV.1: A categorised, tiered test suite that runs schema validation,
manifest hashing, request creation, callback verification, artifact
verification, promotion-gate, point-in-time safety, and cost-tracking
checks locally so that contract regressions are caught before any code
reaches a hosted worker.

Design:
- :class:`VerificationTier` — the execution tier (unit / integration /
  canary / hosted).  Local CI runs UNIT + INTEGRATION.
- :class:`VerificationCategory` — the contract surface under test.
- :class:`VerificationTestSpec` — a frozen, extra-forbid Pydantic v2
  model describing a single test (deterministic id, tier, category,
  module path, function name, min passes, timeout).
- :class:`VerificationResult` — the outcome of running one spec.
- :class:`VerificationMatrixConfig` — which tiers/categories to run.
- :class:`VerificationMatrix` — registry + runner + reporter.

The runner imports ``spec.module``, resolves ``spec.test_function`` and
calls it ``spec.min_passes`` times with no arguments.  A test passes if
the function returns without raising; it fails if it raises.  This keeps
the matrix decoupled from pytest internals while still exercising real
production code (the default specs point at self-contained validators
defined in this module that import and exercise the production models /
hash / callback / artifact / promotion / PIT / budget functions).
"""

from __future__ import annotations

import importlib
import time
import traceback
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VerificationTier(StrEnum):
    """Execution tier for a verification test.

    UNIT: pure-Python, no I/O, runs on every commit.
    INTEGRATION: touches multiple modules / temp files, runs in local CI.
    CANARY: exercises a local mock worker end-to-end.
    HOSTED: requires a real external worker (RunPod) — not run locally.
    """

    UNIT = "unit"
    INTEGRATION = "integration"
    CANARY = "canary"
    HOSTED = "hosted"


class VerificationCategory(StrEnum):
    """Contract surface under test."""

    SCHEMA_VALIDATION = "schema_validation"
    MANIFEST_HASHING = "manifest_hashing"
    REQUEST_CREATION = "request_creation"
    CALLBACK_VERIFICATION = "callback_verification"
    ARTIFACT_VERIFICATION = "artifact_verification"
    PROMOTION_GATE = "promotion_gate"
    PIT_SAFETY = "pit_safety"
    COST_TRACKING = "cost_tracking"


# ---------------------------------------------------------------------------
# Pydantic v2 models
# ---------------------------------------------------------------------------


class VerificationTestSpec(BaseModel):
    """Specification for a single verification test.

    The ``test_id`` is deterministic and SHOULD be of the form
    ``"{tier}_{category}_{name}"`` so that the same logical test always
    maps to the same id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str
    tier: VerificationTier
    category: VerificationCategory
    name: str
    description: str
    module: str
    test_function: str
    min_passes: int = 1
    timeout_seconds: float = 30.0
    enabled: bool = True

    @field_validator("test_id")
    @classmethod
    def _test_id_non_empty(cls, v: str) -> str:
        """Reject empty test ids."""
        if not v or not v.strip():
            raise ValueError("test_id must be a non-empty string")
        return v

    @field_validator("min_passes")
    @classmethod
    def _min_passes_at_least_one(cls, v: int) -> int:
        """Require at least one pass."""
        if v < 1:
            raise ValueError("min_passes must be >= 1")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        """Require a positive timeout."""
        if v <= 0:
            raise ValueError("timeout_seconds must be > 0")
        return v


class VerificationResult(BaseModel):
    """Outcome of running a single :class:`VerificationTestSpec`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str
    tier: VerificationTier
    category: VerificationCategory
    passed: bool
    n_passes: int
    n_failures: int
    duration_seconds: float
    error: str | None = None


class VerificationMatrixConfig(BaseModel):
    """Configuration for a :class:`VerificationMatrix` run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tiers: list[VerificationTier] = Field(default_factory=list)
    categories: list[VerificationCategory] = Field(default_factory=list)
    fail_fast: bool = False
    parallel: bool = False
    report_dir: str = "reports/verification"

    @model_validator(mode="after")
    def _validate_non_empty(self) -> VerificationMatrixConfig:
        """Ensure at least one tier and one category are configured."""
        if not self.tiers:
            raise ValueError("tiers must be a non-empty list")
        if not self.categories:
            raise ValueError("categories must be a non-empty list")
        return self


# ---------------------------------------------------------------------------
# Default test functions (self-contained validators exercising real code)
# ---------------------------------------------------------------------------


def _test_schema_validation_dataset_manifest() -> None:
    """Construct and validate a DatasetManifest (schemas.py)."""
    from quant_foundry.schemas import DatasetManifest

    m = DatasetManifest(
        dataset_id="ds-1",
        feature_schema_hash="a" * 64,
        label_schema_hash="b" * 64,
        as_of_ts=1_700_000_000_000_000_000,
        universe_hash="c" * 64,
        row_count=100,
    )
    assert m.dataset_id == "ds-1"
    assert m.schema_version == 1


def _test_schema_validation_runpod_training_request() -> None:
    """Construct and validate a RunPodTrainingRequest (schemas.py)."""
    from quant_foundry.schemas import RunPodTrainingRequest

    req = RunPodTrainingRequest(
        job_id="job-1",
        dataset_manifest_ref="manifest-uri",
        model_family="xgboost",
    )
    assert req.job_id == "job-1"
    assert req.schema_version == 1


def _test_schema_validation_shadow_prediction() -> None:
    """Construct and validate a ShadowPrediction (schemas.py)."""
    from quant_foundry.schemas import ShadowPrediction

    p = ShadowPrediction(
        prediction_id="pred-1",
        model_id="model-1",
        symbol="AAPL",
        ts_event=1_700_000_000_000_000_000,
        horizon_ns=86_400_000_000_000,
        direction=0.5,
        confidence=0.8,
    )
    assert p.authority.value == "shadow-only"


def _test_schema_validation_artifact_manifest() -> None:
    """Construct and validate an ArtifactManifest (schemas.py)."""
    from quant_foundry.schemas import ArtifactManifest

    am = ArtifactManifest(
        artifact_id="art-1",
        sha256="d" * 64,
        size_bytes=1024,
        model_family="xgboost",
        created_at_ns=1_700_000_000_000_000_000,
        feature_schema_hash="a" * 64,
        label_schema_hash="b" * 64,
    )
    assert am.artifact_id == "art-1"


def _test_manifest_hashing_event() -> None:
    """Exercise compute_event_data_hash determinism."""
    from quant_foundry.event_manifest import EventRecord, compute_event_data_hash

    common = dict(
        event_id="e1",
        source_id="src-1",
        published_at="2024-01-01T00:00:00Z",
        available_at="2024-01-01T00:00:00Z",
        affected_symbols=["AAPL"],
        event_type="earnings",
        label_horizons=[1, 5],
    )
    e1 = EventRecord(**common)  # type: ignore[arg-type]  # mypy can't verify **dict unpacking matches Pydantic model fields
    e2 = EventRecord(**common)  # type: ignore[arg-type]  # mypy can't verify **dict unpacking matches Pydantic model fields
    h1 = compute_event_data_hash([e1])
    h2 = compute_event_data_hash([e2])
    assert h1 == h2
    assert len(h1) == 64


def _test_manifest_hashing_graph() -> None:
    """Exercise compute_graph_data_hash determinism."""
    from quant_foundry.graph_manifest import (
        GraphEdge,
        GraphNode,
        compute_graph_data_hash,
    )

    edges = [
        GraphEdge(
            edge_id="A_B_corr_2024-01-01T00:00:00Z",
            src_node="A",
            dst_node="B",
            edge_type="correlation",
            edge_weight=0.5,
            edge_observed_at="2024-01-01T00:00:00Z",
            edge_available_at="2024-01-01T00:00:00Z",
        ),
    ]
    nodes = [
        GraphNode(
            node_id="A",
            node_type="symbol",
            features={"x": 1.0},
            observed_at="2024-01-01T00:00:00Z",
        ),
    ]
    h1 = compute_graph_data_hash(edges, nodes)
    h2 = compute_graph_data_hash(edges, nodes)
    assert h1 == h2
    assert len(h1) == 64


def _test_manifest_hashing_sequence() -> None:
    """Exercise compute_sequence_data_hash determinism."""
    import numpy as np

    from quant_foundry.sequence_manifest import compute_sequence_data_hash

    data = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
    h1 = compute_sequence_data_hash(data)
    h2 = compute_sequence_data_hash(data)
    assert h1 == h2
    assert len(h1) == 64


def _test_request_creation_runpod_training() -> None:
    """Exercise RunPod training request creation (schemas.py)."""
    from quant_foundry.schemas import RunPodTrainingRequest

    req = RunPodTrainingRequest(
        job_id="job-req-1",
        dataset_manifest_ref="s3://bucket/manifest.json",
        model_family="xgboost",
        search_space={"max_depth": [3, 5, 7]},
        random_seed=42,
    )
    assert req.job_id == "job-req-1"
    assert req.search_space["max_depth"] == [3, 5, 7]
    # Ensure the runpod_training module is importable (request creation path).
    import quant_foundry.runpod_training  # noqa: F401


def _test_request_creation_inference() -> None:
    """Exercise RunPod inference request creation (schemas.py)."""
    from quant_foundry.schemas import RunPodInferenceRequest

    req = RunPodInferenceRequest(
        job_id="job-inf-1",
        artifact_ref="s3://bucket/artifact.tar",
        symbols=["AAPL", "MSFT"],
        horizons_ns=[86_400_000_000_000],
    )
    assert req.symbols == ["AAPL", "MSFT"]


def _test_callback_verification_hmac() -> None:
    """Exercise sign_callback + verify_callback round-trip (signatures.py)."""
    import time as _time

    from quant_foundry.signatures import sign_callback, verify_callback

    payload = b'{"job_id":"job-1","result":"ok"}'
    secret = "test-secret"
    ts = int(_time.time())
    sig = sign_callback(payload, secret=secret, ts=ts, job_id="job-1")
    assert verify_callback(payload, sig, secret=secret, ts=ts, job_id="job-1") is True
    assert verify_callback(payload, "bad", secret=secret, ts=ts, job_id="job-1") is False


def _test_callback_verification_training() -> None:
    """Exercise runpod_training.build_callback + verify_callback round-trip."""
    from quant_foundry.runpod_training import (
        TrainingMode,
        build_callback,
        verify_callback,
    )

    cb = build_callback(
        job_id="job-cb-1",
        training_manifest_hash="a" * 64,
        dataset_manifest_hash="b" * 64,
        runtime_fingerprint={"git_sha": "g", "lockfile_hash": "l", "container_digest": "c"},
        primary_artifact={"artifact_id": "art-1", "sha256": "e" * 64},
        mode=TrainingMode.RESEARCH,
        secret="secret-1",
    )
    assert verify_callback(cb, secret="secret-1") is True
    assert verify_callback(cb, secret="wrong") is False


def _test_artifact_verification_hash() -> None:
    """Exercise verify_artifact_hash (artifacts.py)."""
    import hashlib

    from quant_foundry.artifacts import verify_artifact_hash

    data = b"hello-world-artifact"
    h = hashlib.sha256(data).hexdigest()
    assert verify_artifact_hash(data, h) is True


def _test_artifact_verification_mismatch() -> None:
    """verify_artifact_hash must reject a mismatch (fail closed)."""
    from quant_foundry.artifacts import ArtifactHashMismatchError, verify_artifact_hash

    try:
        verify_artifact_hash(b"real", "f" * 64)
    except ArtifactHashMismatchError:
        return
    raise AssertionError("expected ArtifactHashMismatchError for mismatched hash")


def _test_promotion_gate_reject() -> None:
    """PromotionGate must reject when no dossier is present."""
    from quant_foundry.dossier import DossierStatus
    from quant_foundry.promotion import (
        PromotionEvidence,
        PromotionGate,
        PromotionRequest,
        ReviewDecision,
    )

    req = PromotionRequest(
        model_id="model-1",
        target_level=DossierStatus.PAPER_APPROVED,
        review_note="test",
    )
    evidence = PromotionEvidence(dossier=None)
    gate = PromotionGate()
    receipt = gate.evaluate(req, evidence)
    assert receipt.decision == ReviewDecision.REJECTED


def _test_pit_safety_event() -> None:
    """validate_point_in_time must reject future-observed events."""
    from quant_foundry.event_manifest import EventRecord, validate_point_in_time

    future_event = EventRecord(
        event_id="e-fut",
        source_id="src-1",
        published_at="2024-06-01T00:00:00Z",
        available_at="2024-06-01T00:00:00Z",
        affected_symbols=["AAPL"],
        event_type="earnings",
        label_horizons=[1],
    )
    decision_time = "2024-01-01T00:00:00Z"
    # An event available after the decision time must raise (future leakage).
    try:
        validate_point_in_time(future_event, decision_time)
    except ValueError:
        return
    raise AssertionError("expected ValueError for future-leakage event")


def _test_cost_tracking_budget() -> None:
    """BudgetGuard must reject a job that exceeds the monthly ceiling."""
    import tempfile

    from quant_foundry.budget import BudgetGuard

    with tempfile.TemporaryDirectory() as tmp:
        guard = BudgetGuard(base_dir=tmp, monthly_budget_cents=100)
        # A job costing more than the ceiling must be rejected.
        decision = guard.check_and_reserve(amount_cents=200, job_type="training")
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Default test spec registry
# ---------------------------------------------------------------------------


_DEFAULT_MODULE = "quant_foundry.verification_matrix"


def _default_specs() -> list[VerificationTestSpec]:
    """Return the default verification test specs for all categories."""
    specs: list[VerificationTestSpec] = []

    def _spec(
        tier: VerificationTier,
        category: VerificationCategory,
        name: str,
        fn: str,
        description: str = "",
    ) -> VerificationTestSpec:
        return VerificationTestSpec(
            test_id=f"{tier.value}_{category.value}_{name}",
            tier=tier,
            category=category,
            name=name,
            description=description or f"Default {category.value} check: {name}",
            module=_DEFAULT_MODULE,
            test_function=fn,
        )

    # SCHEMA_VALIDATION
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.SCHEMA_VALIDATION,
            "dataset_manifest",
            "_test_schema_validation_dataset_manifest",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.SCHEMA_VALIDATION,
            "runpod_training_request",
            "_test_schema_validation_runpod_training_request",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.SCHEMA_VALIDATION,
            "shadow_prediction",
            "_test_schema_validation_shadow_prediction",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.SCHEMA_VALIDATION,
            "artifact_manifest",
            "_test_schema_validation_artifact_manifest",
        )
    )

    # MANIFEST_HASHING
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.MANIFEST_HASHING,
            "event_data_hash",
            "_test_manifest_hashing_event",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.MANIFEST_HASHING,
            "graph_data_hash",
            "_test_manifest_hashing_graph",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.MANIFEST_HASHING,
            "sequence_data_hash",
            "_test_manifest_hashing_sequence",
        )
    )

    # REQUEST_CREATION
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.REQUEST_CREATION,
            "runpod_training_request",
            "_test_request_creation_runpod_training",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.REQUEST_CREATION,
            "runpod_inference_request",
            "_test_request_creation_inference",
        )
    )

    # CALLBACK_VERIFICATION
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.CALLBACK_VERIFICATION,
            "hmac_roundtrip",
            "_test_callback_verification_hmac",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.CALLBACK_VERIFICATION,
            "training_callback",
            "_test_callback_verification_training",
        )
    )

    # ARTIFACT_VERIFICATION
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.ARTIFACT_VERIFICATION,
            "hash_match",
            "_test_artifact_verification_hash",
        )
    )
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.ARTIFACT_VERIFICATION,
            "hash_mismatch",
            "_test_artifact_verification_mismatch",
        )
    )

    # PROMOTION_GATE
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.PROMOTION_GATE,
            "reject_no_dossier",
            "_test_promotion_gate_reject",
        )
    )

    # PIT_SAFETY
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.PIT_SAFETY,
            "future_event_rejected",
            "_test_pit_safety_event",
        )
    )

    # COST_TRACKING
    specs.append(
        _spec(
            VerificationTier.UNIT,
            VerificationCategory.COST_TRACKING,
            "budget_reject_over_ceiling",
            "_test_cost_tracking_budget",
        )
    )

    return specs


# ---------------------------------------------------------------------------
# VerificationMatrix
# ---------------------------------------------------------------------------


class VerificationMatrix:
    """Registry, runner and reporter for tiered verification tests.

    The matrix is configured with a :class:`VerificationMatrixConfig`
    that selects which tiers and categories to run.  Tests are registered
    via :meth:`register_test` (or :meth:`register_default_tests`) and
    executed via :meth:`run`, :meth:`run_tier` or :meth:`run_category`.
    """

    def __init__(self, config: VerificationMatrixConfig) -> None:
        """Initialise the matrix with a configuration.

        Args:
            config: the tiers/categories/fail-fast/parallel settings.
        """
        self.config = config
        self._specs: dict[str, VerificationTestSpec] = {}

    # -- registration ------------------------------------------------------

    def register_test(self, spec: VerificationTestSpec) -> None:
        """Register a single test spec.

        Args:
            spec: the test specification to register.

        Raises:
            ValueError: if a spec with the same ``test_id`` is already
                registered.
        """
        if spec.test_id in self._specs:
            raise ValueError(f"duplicate test_id: {spec.test_id!r} already registered")
        self._specs[spec.test_id] = spec

    def register_default_tests(self) -> None:
        """Register the built-in default test specs for all categories."""
        for spec in _default_specs():
            # Skip defaults that collide with an already-registered id
            # (allows callers to override individual defaults).
            if spec.test_id not in self._specs:
                self._specs[spec.test_id] = spec

    # -- selection ---------------------------------------------------------

    def _select(
        self,
        *,
        tier: VerificationTier | None = None,
        category: VerificationCategory | None = None,
    ) -> list[VerificationTestSpec]:
        """Return the specs matching the config + optional tier/category."""
        out: list[VerificationTestSpec] = []
        for spec in self._specs.values():
            if not spec.enabled:
                continue
            if spec.tier not in self.config.tiers:
                continue
            if spec.category not in self.config.categories:
                continue
            if tier is not None and spec.tier != tier:
                continue
            if category is not None and spec.category != category:
                continue
            out.append(spec)
        return out

    # -- execution ---------------------------------------------------------

    def _resolve(self, spec: VerificationTestSpec) -> Callable[..., Any]:
        """Import ``spec.module`` and return ``spec.test_function``."""
        mod = importlib.import_module(spec.module)
        try:
            fn = getattr(mod, spec.test_function)
        except AttributeError as exc:  # pragma: no cover - defensive
            raise AttributeError(
                f"module {spec.module!r} has no attribute {spec.test_function!r}"
            ) from exc
        return cast("Callable[..., Any]", fn)

    def _run_one(self, spec: VerificationTestSpec) -> VerificationResult:
        """Run a single spec ``min_passes`` times and return a result."""
        n_passes = 0
        n_failures = 0
        error: str | None = None
        start = time.perf_counter()
        try:
            fn = self._resolve(spec)
        except Exception as exc:
            n_failures = spec.min_passes
            error = f"import error: {exc}\n{traceback.format_exc()}"
        else:
            for _ in range(spec.min_passes):
                try:
                    fn()
                    n_passes += 1
                except Exception as exc:
                    n_failures += 1
                    error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        duration = time.perf_counter() - start
        passed = n_failures == 0 and n_passes >= spec.min_passes
        return VerificationResult(
            test_id=spec.test_id,
            tier=spec.tier,
            category=spec.category,
            passed=passed,
            n_passes=n_passes,
            n_failures=n_failures,
            duration_seconds=duration,
            error=error if not passed else None,
        )

    def run(self) -> list[VerificationResult]:
        """Run all registered, enabled tests matching the config.

        If ``config.fail_fast`` is True, execution stops after the first
        failing test (earlier results are returned).
        """
        results: list[VerificationResult] = []
        for spec in self._select():
            result = self._run_one(spec)
            results.append(result)
            if self.config.fail_fast and not result.passed:
                break
        return results

    def run_tier(self, tier: VerificationTier) -> list[VerificationResult]:
        """Run only the tests in ``tier`` (respecting fail_fast)."""
        results: list[VerificationResult] = []
        for spec in self._select(tier=tier):
            result = self._run_one(spec)
            results.append(result)
            if self.config.fail_fast and not result.passed:
                break
        return results

    def run_category(
        self,
        category: VerificationCategory,
    ) -> list[VerificationResult]:
        """Run only the tests in ``category`` (respecting fail_fast)."""
        results: list[VerificationResult] = []
        for spec in self._select(category=category):
            result = self._run_one(spec)
            results.append(result)
            if self.config.fail_fast and not result.passed:
                break
        return results

    # -- reporting ---------------------------------------------------------

    def generate_report(self, results: list[VerificationResult]) -> str:
        """Generate a markdown report with pass/fail counts by tier/category."""
        summary = summarize_results(results)
        lines: list[str] = []
        lines.append("# Verification Matrix Report")
        lines.append("")
        lines.append(
            f"**Total:** {summary['total']}  "
            f"**Passed:** {summary['passed']}  "
            f"**Failed:** {summary['failed']}"
        )
        lines.append("")
        lines.append("## By Tier")
        lines.append("")
        lines.append("| Tier | Passed | Failed | Total |")
        lines.append("|------|--------|--------|-------|")
        for tier, counts in summary["by_tier"].items():
            lines.append(
                f"| {tier} | {counts['passed']} | {counts['failed']} | {counts['total']} |"
            )
        lines.append("")
        lines.append("## By Category")
        lines.append("")
        lines.append("| Category | Passed | Failed | Total |")
        lines.append("|----------|--------|--------|-------|")
        for cat, counts in summary["by_category"].items():
            lines.append(f"| {cat} | {counts['passed']} | {counts['failed']} | {counts['total']} |")
        lines.append("")
        lines.append("## Results")
        lines.append("")
        lines.append(format_result_table(results))
        return "\n".join(lines)

    def save_report(self, results: list[VerificationResult], path: str) -> None:
        """Save the markdown report to ``path`` (creating parent dirs)."""
        report = self.generate_report(results)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(report, encoding="utf-8")

    def is_passing(self, results: list[VerificationResult]) -> bool:
        """Return True if every result passed."""
        return all(r.passed for r in results)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def summarize_results(results: list[VerificationResult]) -> dict[str, Any]:
    """Return a summary dict with total/passed/failed and per-tier/category counts.

    The returned dict has the shape::

        {
            "total": int,
            "passed": int,
            "failed": int,
            "by_tier": {tier: {"total", "passed", "failed"}},
            "by_category": {category: {"total", "passed", "failed"}},
        }
    """
    by_tier: dict[str, dict[str, int]] = {}
    by_category: dict[str, dict[str, int]] = {}
    passed = 0
    failed = 0
    for r in results:
        tier_key = r.tier.value
        cat_key = r.category.value
        by_tier.setdefault(tier_key, {"total": 0, "passed": 0, "failed": 0})
        by_category.setdefault(cat_key, {"total": 0, "passed": 0, "failed": 0})
        by_tier[tier_key]["total"] += 1
        by_category[cat_key]["total"] += 1
        if r.passed:
            passed += 1
            by_tier[tier_key]["passed"] += 1
            by_category[cat_key]["passed"] += 1
        else:
            failed += 1
            by_tier[tier_key]["failed"] += 1
            by_category[cat_key]["failed"] += 1
    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "by_tier": by_tier,
        "by_category": by_category,
    }


def format_result_table(results: list[VerificationResult]) -> str:
    """Return a markdown table of all results."""
    lines: list[str] = []
    lines.append(
        "| Test ID | Tier | Category | Passed | Passes | Failures | Duration (s) | Error |"
    )
    lines.append(
        "|---------|------|----------|--------|--------|----------|--------------|-------|"
    )
    for r in results:
        err = (r.error or "").replace("\n", " ").replace("|", "\\|")
        if len(err) > 80:
            err = err[:77] + "..."
        lines.append(
            f"| {r.test_id} | {r.tier.value} | {r.category.value} | "
            f"{'PASS' if r.passed else 'FAIL'} | {r.n_passes} | "
            f"{r.n_failures} | {r.duration_seconds:.4f} | {err} |"
        )
    return "\n".join(lines)


__all__ = [
    "VerificationCategory",
    "VerificationMatrix",
    "VerificationMatrixConfig",
    "VerificationResult",
    "VerificationTestSpec",
    "VerificationTier",
    "format_result_table",
    "summarize_results",
]
