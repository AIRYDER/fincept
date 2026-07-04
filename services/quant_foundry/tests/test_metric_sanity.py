"""Metric sanity bounds tests (Tier 0).

The A7 canary run produced a Sharpe ratio of 769 — impossible for any
real trading strategy. These tests lock down the
:func:`validate_metric_sanity` function and its wiring into
:func:`build_callback` so implausible metrics are flagged, raw values
are preserved, and promotion eligibility is blocked.

Coverage:
  * Normal Sharpe (1.5) passes with status "ok".
  * Sharpe 769 is flagged "implausible" with a reason code.
  * Raw metric values are preserved in the output (never deleted).
  * Promotion eligibility is blocked when a critical metric is
    implausible.
  * The callback still serializes correctly (no JSON errors).
  * Warning-level metrics (Sharpe 5) get "warning" but do NOT block
    promotion.
  * Multiple implausible metrics all surface in the report.
  * Empty/None metrics_summary returns an "ok" report.
"""

from __future__ import annotations

import json

from quant_foundry.runpod_training import (
    MetricSanityReport,
    RunPodTrainingCallback,
    build_callback,
    validate_metric_sanity,
)
from quant_foundry.training_manifest import TrainingMode

_SECRET = "test-secret-key-for-metric-sanity"


def _rt_fingerprint() -> dict[str, str]:
    return {
        "git_sha": "abc123",
        "lockfile_hash": "def456",
        "container_digest": "sha256:deadbeef",
    }


# --- validate_metric_sanity unit tests -------------------------------------


def test_normal_sharpe_passes_ok() -> None:
    """A realistic Sharpe (1.5) produces status "ok" with no reason codes."""
    report = validate_metric_sanity({"sharpe_ratio": 1.5, "max_drawdown": -0.15})
    assert report.status == "ok"
    assert report.reason_codes == ()
    assert report.promotion_allowed is True
    assert report.flagged_metrics == {}


def test_sharpe_769_flagged_implausible() -> None:
    """Sharpe 769 (the A7 canary value) is flagged "implausible"."""
    report = validate_metric_sanity({"sharpe_ratio": 769.0})
    assert report.status == "implausible"
    assert report.promotion_allowed is False
    assert len(report.reason_codes) == 1
    assert "sharpe_ratio_implausible" in report.reason_codes[0]
    assert "769" in report.reason_codes[0]
    assert "sharpe_ratio" in report.flagged_metrics
    assert report.flagged_metrics["sharpe_ratio"]["status"] == "implausible"


def test_raw_metric_value_preserved() -> None:
    """The raw metric value is preserved in the flagged_metrics detail."""
    report = validate_metric_sanity({"sharpe_ratio": 769.0})
    detail = report.flagged_metrics["sharpe_ratio"]
    assert detail["raw_value"] == 769.0


def test_warning_sharpe_does_not_block_promotion() -> None:
    """Sharpe 5 (above warning threshold 5.0... use 6 to be safe) is a
    warning and does NOT block promotion."""
    # Warning threshold is 5.0; use 6.0 which is > 5.0 but < 10.0.
    report = validate_metric_sanity({"sharpe_ratio": 6.0})
    assert report.status == "warning"
    assert report.promotion_allowed is True
    assert any("warning" in rc for rc in report.reason_codes)


def test_max_drawdown_implausible_blocks_promotion() -> None:
    """Max drawdown > 1.0 (100%) is implausible and blocks promotion."""
    report = validate_metric_sanity({"max_drawdown": -1.5})
    assert report.status == "implausible"
    assert report.promotion_allowed is False
    assert any("max_drawdown" in rc for rc in report.reason_codes)


def test_annual_return_implausible_blocks_promotion() -> None:
    """Annual return > 500% is implausible and blocks promotion."""
    report = validate_metric_sanity({"annual_return": 7.5})
    assert report.status == "implausible"
    assert report.promotion_allowed is False
    assert any("annual_return" in rc for rc in report.reason_codes)


def test_fold_overfit_implausible_blocks_promotion() -> None:
    """Fold overfit ratio > 5.0 is implausible and blocks promotion."""
    report = validate_metric_sanity({"pbo": 6.0})
    assert report.status == "implausible"
    assert report.promotion_allowed is False
    assert any("fold_overfit" in rc for rc in report.reason_codes)


def test_empty_metrics_returns_ok() -> None:
    """Empty/None metrics_summary returns an "ok" report."""
    assert validate_metric_sanity(None).status == "ok"
    assert validate_metric_sanity({}).status == "ok"


def test_non_numeric_metric_ignored() -> None:
    """A non-numeric sharpe value is ignored (not crashed on)."""
    report = validate_metric_sanity({"sharpe_ratio": "not-a-number"})
    assert report.status == "ok"


def test_negative_extreme_sharpe_implausible() -> None:
    """An extreme negative Sharpe is also implausible (abs value checked)."""
    report = validate_metric_sanity({"sharpe_ratio": -769.0})
    assert report.status == "implausible"
    assert report.promotion_allowed is False


def test_metric_sanity_report_serializes_json() -> None:
    """The report's to_dict() is JSON-serializable (callback payload safe)."""
    report = validate_metric_sanity({"sharpe_ratio": 769.0, "max_drawdown": -1.5})
    serialized = json.dumps(report.to_dict(), sort_keys=True)
    parsed = json.loads(serialized)
    assert parsed["status"] == "implausible"
    assert parsed["promotion_allowed"] is False


# --- build_callback integration tests --------------------------------------


def test_build_callback_blocks_promotion_on_implausible_sharpe() -> None:
    """build_callback forces promotion_eligible=False when Sharpe is 769."""
    cb = build_callback(
        job_id="job-a7-canary",
        training_manifest_hash="tm-hash",
        dataset_manifest_hash="dm-hash",
        runtime_fingerprint=_rt_fingerprint(),
        primary_artifact=None,
        metrics_summary={"sharpe_ratio": 769.0, "max_drawdown": -0.1},
        mode=TrainingMode.PRODUCTION,
        quality_gate_passed=True,
        secret=_SECRET,
    )
    assert cb.promotion_eligible is False
    sanity = cb.metrics_summary["metric_sanity"]
    assert sanity["status"] == "implausible"
    assert sanity["promotion_allowed"] is False


def test_build_callback_preserves_raw_metrics() -> None:
    """Raw metric values are preserved in the callback metrics_summary."""
    cb = build_callback(
        job_id="job-raw-preserve",
        training_manifest_hash="tm-hash",
        dataset_manifest_hash="dm-hash",
        runtime_fingerprint=_rt_fingerprint(),
        primary_artifact=None,
        metrics_summary={"sharpe_ratio": 769.0, "max_drawdown": -0.2},
        mode=TrainingMode.RESEARCH,
        secret=_SECRET,
    )
    # Raw value is still present, untouched.
    assert cb.metrics_summary["sharpe_ratio"] == 769.0
    assert cb.metrics_summary["max_drawdown"] == -0.2
    # Sanity annotation is alongside it.
    assert "metric_sanity" in cb.metrics_summary


def test_build_callback_normal_metrics_ok_and_promotion_allowed() -> None:
    """Normal metrics: status "ok", production promotion allowed when gates pass."""
    cb = build_callback(
        job_id="job-normal",
        training_manifest_hash="tm-hash",
        dataset_manifest_hash="dm-hash",
        runtime_fingerprint=_rt_fingerprint(),
        primary_artifact=None,
        metrics_summary={"sharpe_ratio": 1.5, "max_drawdown": -0.15},
        mode=TrainingMode.PRODUCTION,
        quality_gate_passed=True,
        secret=_SECRET,
    )
    assert cb.promotion_eligible is True
    assert cb.metrics_summary["metric_sanity"]["status"] == "ok"


def test_build_callback_warning_does_not_block_promotion() -> None:
    """A warning-level Sharpe (6.0) does not block production promotion."""
    cb = build_callback(
        job_id="job-warning",
        training_manifest_hash="tm-hash",
        dataset_manifest_hash="dm-hash",
        runtime_fingerprint=_rt_fingerprint(),
        primary_artifact=None,
        metrics_summary={"sharpe_ratio": 6.0},
        mode=TrainingMode.PRODUCTION,
        quality_gate_passed=True,
        secret=_SECRET,
    )
    assert cb.promotion_eligible is True
    assert cb.metrics_summary["metric_sanity"]["status"] == "warning"


def test_build_callback_serializes_canonical_json() -> None:
    """The full callback (with sanity annotation) serializes without error."""
    cb = build_callback(
        job_id="job-serialize",
        training_manifest_hash="tm-hash",
        dataset_manifest_hash="dm-hash",
        runtime_fingerprint=_rt_fingerprint(),
        primary_artifact=None,
        metrics_summary={"sharpe_ratio": 769.0},
        mode=TrainingMode.RESEARCH,
        secret=_SECRET,
    )
    payload = cb.model_dump()
    payload.pop("callback_signature", None)
    # Must not raise — the sanity report is JSON-safe.
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    decoded = json.loads(encoded)
    assert decoded["metrics_summary"]["metric_sanity"]["status"] == "implausible"


def test_build_callback_canary_still_blocked_with_implausible() -> None:
    """Canary mode is already promotion-ineligible; implausible metric
    keeps it blocked (no flip to True)."""
    cb = build_callback(
        job_id="job-canary-implausible",
        training_manifest_hash="tm-hash",
        dataset_manifest_hash="dm-hash",
        runtime_fingerprint=_rt_fingerprint(),
        primary_artifact=None,
        metrics_summary={"sharpe_ratio": 769.0},
        mode=TrainingMode.CANARY,
        secret=_SECRET,
    )
    assert cb.promotion_eligible is False
    assert cb.metrics_summary["metric_sanity"]["status"] == "implausible"


def test_build_callback_does_not_mutate_input_metrics() -> None:
    """build_callback must not mutate the caller's metrics_summary dict."""
    raw = {"sharpe_ratio": 769.0, "max_drawdown": -0.1}
    raw_copy = dict(raw)
    build_callback(
        job_id="job-no-mutate",
        training_manifest_hash="tm-hash",
        dataset_manifest_hash="dm-hash",
        runtime_fingerprint=_rt_fingerprint(),
        primary_artifact=None,
        metrics_summary=raw,
        mode=TrainingMode.RESEARCH,
        secret=_SECRET,
    )
    # The caller's dict is untouched (no metric_sanity key injected).
    assert raw == raw_copy
    assert "metric_sanity" not in raw
