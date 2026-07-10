"""C8 -- Live Proof Ladder Prep (probe definitions, dry-run validated).

This module defines the 9-probe live proof ladder for the RunPod training
worker. It is PREP ONLY: no live RunPod jobs are dispatched unless the
caller explicitly opts in with ``--live`` AND confirms the cost prompt.

Each probe is a self-contained function that:
  1. Builds the RunPod job payload (input dict) for the probe leg.
  2. Declares its expected pass/fail criteria.
  3. Declares the evidence artifact it should produce.
  4. Estimates the RunPod cost/risk.

In ``--dry-run`` mode (default), every probe validates its payload shape,
prints the expected evidence, and exits without touching the RunPod API.
This lets the orchestrator and reviewers audit the ladder before any
cloud spend.

Usage::

    # Validate all 9 probes (no cloud spend):
    uv run python scripts/runpod/c8_probe_ladder.py --dry-run

    # Validate a single probe:
    uv run python scripts/runpod/c8_probe_ladder.py --dry-run --probe pit_fail_closed_probe

    # Live execution (REQUIRES explicit confirmation -- NOT used in C8 prep):
    uv run python scripts/runpod/c8_probe_ladder.py --live --probe lightgbm_single_bundle_probe --sha <sha>

Probe legs (9 total):
  1. pit_fail_closed_probe           -- PIT violation fails closed (production)
  2. feature_set_version_mismatch_probe -- feature schema hash mismatch fails closed
  3. lightgbm_single_bundle_probe    -- single LightGBM bundle round-trip
  4. lightgbm_meta_bundle_probe      -- meta-labeling bundle round-trip
  5. optuna_trial_count_probe        -- honest Optuna trial count in callback
  6. cpcv_pbo_probe                  -- CPCV/PBO validation metrics present
  7. triple_barrier_meta_probe       -- triple-barrier meta-labeling pipeline
  8. checkpoint_kill_resume_probe    -- checkpoint + resume after simulated kill
  9. durable_artifact_receipt_probe  -- durable artifact + signed receipt on volume
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Windows consoles default to cp1252 which cannot encode some Unicode
# characters used in the evidence output. Reconfigure stdout/stderr to
# UTF-8 so the dry-run evidence prints cleanly on all platforms.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, Exception):
        pass

# Shared lifecycle helpers (unique naming, retry cleanup, timeout config).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from runpod.runpod_lifecycle import (  # noqa: E402
    DEFAULT_DEADLINE_S,
    MIN_EXECUTION_TIMEOUT_S,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Synthetic dataset shape (matches run_train_model.py canary defaults).
DATASET_ROWS = 300
DATASET_SEED = 42
N_FOLDS = 2

# Cost model (rough, for the prep report). ADA_24 (RTX 6000 Ada) ~$0.45/hr
# on RunPod serverless. Each probe is sized to complete well under the
# handler deadline (1800s); we budget conservatively.
GPU_RATE_PER_HOUR = 0.45
COLD_START_S = 120  # worker cold-pull + init
PROBE_RUN_S = 60  # typical canary-class probe execution

# All probe names (single source of truth).
PROBE_NAMES = [
    "pit_fail_closed_probe",
    "feature_set_version_mismatch_probe",
    "lightgbm_single_bundle_probe",
    "lightgbm_meta_bundle_probe",
    "optuna_trial_count_probe",
    "cpcv_pbo_probe",
    "triple_barrier_meta_probe",
    "checkpoint_kill_resume_probe",
    "durable_artifact_receipt_probe",
]


# ---------------------------------------------------------------------------
# Probe definition dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeSpec:
    """Static specification for a single probe leg."""

    name: str
    what_it_tests: str
    expected_pass_criteria: list[str]
    expected_fail_criteria: list[str]
    required_env_vars: list[str]
    expected_evidence: list[str]
    estimated_cost_usd: float
    risk_notes: str
    requires_network_volume: bool = False
    requires_kill_simulation: bool = False
    handler_mode: str = "canary"  # canary | research | production

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Synthetic dataset builder (mirrors run_train_model.py)
# ---------------------------------------------------------------------------


def build_synthetic_csv(rows: int = DATASET_ROWS, seed: int = DATASET_SEED) -> str:
    """Build a tiny deterministic synthetic dataset as CSV text.

    Legacy loader layout: header row, first column = timestamp, middle
    columns = features, last column = label (binary 0/1).
    """
    rng = random.Random(seed)
    base_ts = 1_700_000_000_000_000_000  # ns epoch, strictly increasing
    lines = ["timestamp,f1,f2,f3,label"]
    for i in range(rows):
        f1 = rng.gauss(0.0, 1.0)
        f2 = rng.gauss(0.0, 1.0)
        f3 = rng.gauss(0.0, 1.0)
        noise = rng.gauss(0.0, 0.3)
        label = 1 if (0.8 * f1 - 0.5 * f2 + 0.3 * f3 + noise) > 0 else 0
        ts = base_ts + i * 60_000_000_000  # 1-minute bars
        lines.append(f"{ts},{f1:.6f},{f2:.6f},{f3:.6f},{label}")
    return "\n".join(lines) + "\n"


def _base_train_input(job_id: str, **overrides: Any) -> dict[str, Any]:
    """Build the minimal implicit train_model payload (canary defaults)."""
    extra: dict[str, Any] = {"training_mode": "canary"}
    extra.update(overrides.pop("extra_constraints", {}))
    payload: dict[str, Any] = {
        "job_id": job_id,
        "dataset_manifest_ref": "inline://c8-probe",
        "model_family": "lightgbm",
        "random_seed": DATASET_SEED,
        "search_space": {},
        "extra_constraints": extra,
        "inline_dataset_csv": build_synthetic_csv(),
        "n_folds": N_FOLDS,
        "output_prefix": "/tmp/c8-probe-artifacts",  # noqa: S108 - canary default
    }
    payload.update(overrides)
    return payload


def _estimate_cost(run_s: int = PROBE_RUN_S, cold_s: int = COLD_START_S) -> float:
    """Estimate the RunPod cost for a single probe (USD)."""
    total_hours = (run_s + cold_s) / 3600.0
    return round(total_hours * GPU_RATE_PER_HOUR, 4)


# ---------------------------------------------------------------------------
# Probe specifications (9 legs)
# ---------------------------------------------------------------------------


def _all_probe_specs() -> list[ProbeSpec]:
    """Return the full 9-probe specification list."""
    return [
        ProbeSpec(
            name="pit_fail_closed_probe",
            what_it_tests=(
                "Point-in-time (PIT) proof gate fails closed in production "
                "mode when the dataset manifest's pit_proof_verified flag "
                "is not True. The handler must emit a signed failure "
                "envelope with error_code='pit_proof_not_verified' and "
                "NOT start training."
            ),
            expected_pass_criteria=[
                "Job status = FAILED with error_code='pit_proof_not_verified'",
                "Signed failure envelope present (callback_signature non-empty)",
                "No artifact_result in output (training did not start)",
                "training_mode='production' in the failure context",
            ],
            expected_fail_criteria=[
                "Job completes with an artifact (training ran despite PIT violation)",
                "Missing or unsigned failure envelope",
                "error_code != 'pit_proof_not_verified'",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/pit_fail_closed/receipt.json "
                "(signed failure envelope with error_code)",
                "reports/c8-live-proof-prep/pit_fail_closed/run-input.json "
                "(payload showing training_mode=production + pit_proof_verified=false)",
            ],
            estimated_cost_usd=_estimate_cost(run_s=15),
            risk_notes=(
                "Low cost -- fails fast before training. Risk: if the gate "
                "is bypassed, production training on leaky data. This probe "
                "MUST fail; a 'success' here is a regression."
            ),
            handler_mode="production",
        ),
        ProbeSpec(
            name="feature_set_version_mismatch_probe",
            what_it_tests=(
                "Manifest-first dataset load fails closed when the "
                "feature_schema_hash in the load spec does not match the "
                "verified manifest. The handler's ManifestDatasetLoader "
                "must raise DatasetLoadError (fail-closed) before training."
            ),
            expected_pass_criteria=[
                "Job status = FAILED with error_code containing 'schema' or 'DatasetLoadError'",
                "Signed failure envelope present",
                "dataset_load_receipt shows schema_verified=False",
                "No artifact_result (training did not start)",
            ],
            expected_fail_criteria=[
                "Job completes with an artifact (mismatched schema accepted)",
                "schema_verified=True in the load receipt",
                "Missing failure envelope",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/feature_set_mismatch/receipt.json",
                "reports/c8-live-proof-prep/feature_set_mismatch/load-spec.json "
                "(deliberately wrong feature_schema_hash)",
            ],
            estimated_cost_usd=_estimate_cost(run_s=15),
            risk_notes=(
                "Low cost -- fails at load stage. Requires a dataset_load_spec "
                "with a deliberately mismatched feature_schema_hash. Risk: "
                "schema drift silently accepted -> wrong-feature model promoted."
            ),
            handler_mode="production",
        ),
        ProbeSpec(
            name="lightgbm_single_bundle_probe",
            what_it_tests=(
                "A single LightGBM bundle round-trips end-to-end: train -> "
                "bundle write (ModelBundle v1 zip) -> bundle load -> score -> "
                "selfcheck pass. Proves the write-only gap is closed for "
                "the single-model path."
            ),
            expected_pass_criteria=[
                "Job status = COMPLETED",
                "artifact_result present with non-empty artifact_uri, sha256, size",
                "artifact_write_receipt present (HMAC-signed)",
                "bundle_kind='single' in the artifact manifest",
                "Selfcheck passed (bundle_selfcheck passed=True)",
                "callback_signature present",
            ],
            expected_fail_criteria=[
                "No artifact or unsigned write receipt",
                "Selfcheck failed (bundle_selfcheck_failed)",
                "Bundle cannot be loaded (BundleLoadError)",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/lightgbm_single_bundle/receipt.json",
                "reports/c8-live-proof-prep/lightgbm_single_bundle/bundle.zip "
                "(the ModelBundle v1 artifact)",
                "reports/c8-live-proof-prep/lightgbm_single_bundle/selfcheck.json",
            ],
            estimated_cost_usd=_estimate_cost(run_s=90),
            risk_notes=(
                "Medium cost -- runs a full LightGBM training + bundle "
                "round-trip. This is the canonical 'happy path' probe; "
                "failure here blocks all downstream probes."
            ),
            handler_mode="canary",
        ),
        ProbeSpec(
            name="lightgbm_meta_bundle_probe",
            what_it_tests=(
                "A meta-labeling bundle round-trips: primary model + meta "
                "model -> bundle write (bundle_kind='meta_labeled') -> load "
                "-> score with meta gating -> selfcheck pass. Proves the "
                "meta-labeling bundle path is closed end-to-end."
            ),
            expected_pass_criteria=[
                "Job status = COMPLETED",
                "bundle_kind='meta_labeled' in the artifact manifest",
                "Both primary.pkl and meta.pkl members present in the bundle",
                "Selfcheck passed",
                "artifact_write_receipt present",
                "callback_signature present",
            ],
            expected_fail_criteria=[
                "Missing meta member (fail-closed invariant violated)",
                "bundle_kind='single' (meta model not written)",
                "Selfcheck failed",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/lightgbm_meta_bundle/receipt.json",
                "reports/c8-live-proof-prep/lightgbm_meta_bundle/bundle.zip",
                "reports/c8-live-proof-prep/lightgbm_meta_bundle/selfcheck.json",
            ],
            estimated_cost_usd=_estimate_cost(run_s=120),
            risk_notes=(
                "Medium-high cost -- trains two models (primary + meta). "
                "Requires the meta-labeling path to be enabled in the "
                "trainer config. Risk: meta member silently omitted."
            ),
            handler_mode="canary",
        ),
        ProbeSpec(
            name="optuna_trial_count_probe",
            what_it_tests=(
                "When Optuna is enabled, the typed callback's "
                "metrics_summary includes an honest optuna_trial_count "
                "matching the number of trials actually evaluated (not a "
                "hardcoded or inflated value). The Deflated Sharpe Ratio "
                "applies the multiple-trials penalty using this count."
            ),
            expected_pass_criteria=[
                "Job status = COMPLETED",
                "metrics_summary.optuna_trial_count present and > 0",
                "optuna_trial_count matches the study artifact's trial list length",
                "deflated_sharpe reflects the trial-count penalty",
                "optuna_best_params present in the callback",
            ],
            expected_fail_criteria=[
                "optuna_trial_count missing or = 0 when Optuna was enabled",
                "Trial count inflated (does not match study artifact)",
                "Deflated Sharpe ignores the trial count",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/optuna_trial_count/receipt.json",
                "reports/c8-live-proof-prep/optuna_trial_count/study-artifact.json "
                "(trial list + best trial)",
                "reports/c8-live-proof-prep/optuna_trial_count/metrics-summary.json",
            ],
            estimated_cost_usd=_estimate_cost(run_s=180),
            risk_notes=(
                "Higher cost -- Optuna runs multiple trials within the "
                "deadline budget. Keep optuna_max_trials small (e.g. 5) "
                "for the probe. Risk: inflated trial count -> no DSR penalty."
            ),
            handler_mode="canary",
        ),
        ProbeSpec(
            name="cpcv_pbo_probe",
            what_it_tests=(
                "Combinatorial Purged Cross-Validation (CPCV) runs and the "
                "Probability of Backtest Overfitting (PBO) is reported in "
                "the dossier/callback. Proves the advanced validation "
                "metrics are computed and propagated, not stubbed."
            ),
            expected_pass_criteria=[
                "Job status = COMPLETED",
                "dossier.pbo present and is a float in [0.0, 1.0]",
                "pbo_method documented (not 'unknown' or null)",
                "fold_source documented",
                "callback carries the PBO value",
            ],
            expected_fail_criteria=[
                "pbo missing or null",
                "pbo outside [0.0, 1.0]",
                "pbo_method undocumented",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/cpcv_pbo/receipt.json",
                "reports/c8-live-proof-prep/cpcv_pbo/dossier.json (pbo + pbo_method + fold_source)",
            ],
            estimated_cost_usd=_estimate_cost(run_s=150),
            risk_notes=(
                "Medium-high cost -- CPCV runs multiple fold combinations. "
                "Risk: PBO stubbed to a constant (e.g. 1.0) without real "
                "computation. Compare against the fold_overfit_ratio method."
            ),
            handler_mode="canary",
        ),
        ProbeSpec(
            name="triple_barrier_meta_probe",
            what_it_tests=(
                "Triple-barrier labeling + meta-labeling pipeline runs "
                "end-to-end: triple-barrier labels applied to the primary "
                "signal -> meta model trained on the labeled outcomes -> "
                "bundle written with meta gating. Proves the labeling + "
                "meta path is wired through the trainer."
            ),
            expected_pass_criteria=[
                "Job status = COMPLETED",
                "bundle_kind='meta_labeled' (meta model present)",
                "Labeling config in the dossier reflects triple-barrier params",
                "Selfcheck passed",
                "artifact_write_receipt present",
            ],
            expected_fail_criteria=[
                "Labels are binary (triple-barrier not applied)",
                "No meta model (bundle_kind='single')",
                "Selfcheck failed",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/triple_barrier_meta/receipt.json",
                "reports/c8-live-proof-prep/triple_barrier_meta/bundle.zip",
                "reports/c8-live-proof-prep/triple_barrier_meta/labeling-config.json",
            ],
            estimated_cost_usd=_estimate_cost(run_s=150),
            risk_notes=(
                "Medium-high cost -- triple-barrier labeling + meta training. "
                "Requires the labeling config to be forwarded via "
                "extra_constraints. Risk: labeling silently falls back to "
                "binary."
            ),
            handler_mode="canary",
        ),
        ProbeSpec(
            name="checkpoint_kill_resume_probe",
            what_it_tests=(
                "A training job checkpoints per-fold, is 'killed' "
                "(simulated preemption) mid-fold, then resumes from the "
                "last checkpoint -- skipping completed folds and continuing "
                "from the resume fold index. Proves spot-fleet preemption "
                "recovery."
            ),
            expected_pass_criteria=[
                "First leg: job writes at least one checkpoint file",
                "Resume leg: job status includes 'resuming from fold N'",
                "Resume leg: completed folds are NOT re-trained",
                "Final job status = COMPLETED with a valid artifact",
                "checkpoint_dir contains fold checkpoint files",
            ],
            expected_fail_criteria=[
                "No checkpoint files written",
                "Resume re-trains all folds from scratch",
                "Resume fails with checkpoint_load_failed",
                "No final artifact after resume",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/checkpoint_kill_resume/leg1-receipt.json",
                "reports/c8-live-proof-prep/checkpoint_kill_resume/leg2-resume-receipt.json",
                "reports/c8-live-proof-prep/checkpoint_kill_resume/checkpoint-list.json",
            ],
            estimated_cost_usd=_estimate_cost(run_s=200) * 2,  # two legs
            risk_notes=(
                "Highest cost -- two job legs (checkpoint + resume). "
                "Requires a network volume for checkpoint persistence "
                "across worker restarts. The 'kill' is simulated by "
                "canceling the job mid-fold (NOT a real spot preemption). "
                "Risk: checkpoint corruption or resume fold-off-by-one."
            ),
            requires_network_volume=True,
            requires_kill_simulation=True,
            handler_mode="canary",
        ),
        ProbeSpec(
            name="durable_artifact_receipt_probe",
            what_it_tests=(
                "A real (non-canary) job writes its artifact to a RunPod "
                "network volume (/runpod-volume/), the durable-artifact "
                "deny gate passes, and the signed write receipt points at "
                "the durable URI. Proves artifacts survive worker shutdown."
            ),
            expected_pass_criteria=[
                "Job status = COMPLETED",
                "artifact_uri starts with 'file:///runpod-volume/' or 's3://'",
                "artifact_destination_not_durable error NOT raised",
                "artifact_write_receipt present (HMAC-signed)",
                "Artifact readable from the volume after worker shutdown",
                "training_mode='research' (non-canary, deny gate active)",
            ],
            expected_fail_criteria=[
                "artifact_uri points at /tmp (deny gate bypassed)",
                "No write receipt",
                "Artifact missing from the volume after worker shutdown",
            ],
            required_env_vars=[
                "RUNPOD_API_KEY",
                "QUANT_FOUNDRY_CALLBACK_SECRET",
            ],
            expected_evidence=[
                "reports/c8-live-proof-prep/durable_artifact_receipt/receipt.json",
                "reports/c8-live-proof-prep/durable_artifact_receipt/volume-listing.json "
                "(post-shutdown volume contents)",
                "reports/c8-live-proof-prep/durable_artifact_receipt/write-receipt.json",
            ],
            estimated_cost_usd=_estimate_cost(run_s=90),
            risk_notes=(
                "Medium cost -- requires a pre-provisioned network volume. "
                "Risk: artifact written to ephemeral storage and lost on "
                "worker shutdown (the exact failure the deny gate prevents)."
            ),
            requires_network_volume=True,
            handler_mode="research",
        ),
    ]


# ---------------------------------------------------------------------------
# Probe payload builders (what would be dispatched live)
# ---------------------------------------------------------------------------


def pit_fail_closed_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the PIT fail-closed probe.

    Uses production mode with a dataset manifest whose pit_proof_verified
    is not True. The handler must reject this with a signed failure.
    """
    return _base_train_input(
        job_id,
        extra_constraints={"training_mode": "production"},
        # The inline dataset path does not carry a pit_proof_verified flag,
        # so the handler's PIT gate sees None (not True) -> fail closed.
        dataset_manifest_ref="inline://c8-pit-probe",
    )


def feature_set_version_mismatch_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the feature schema mismatch probe.

    Provides a dataset_load_spec with a deliberately wrong
    feature_schema_hash. The ManifestDatasetLoader must reject this.
    """
    return _base_train_input(
        job_id,
        extra_constraints={"training_mode": "production"},
        dataset_load_spec={
            "manifest_uri": "inline://c8-schema-mismatch",
            "manifest_sha256": "0" * 64,  # deliberately wrong
            "data_uri": "inline://c8-schema-mismatch",
            "data_sha256": "0" * 64,
            "data_format": "csv",
            "row_count": DATASET_ROWS,
            "feature_schema_hash": "deadbeefdeadbeef",  # deliberately wrong
            "label_schema_hash": "0" * 16,
        },
    )


def lightgbm_single_bundle_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the single LightGBM bundle round-trip probe."""
    return _base_train_input(
        job_id,
        extra_constraints={"training_mode": "canary"},
        output_prefix="/tmp/c8-single-bundle",  # noqa: S108
    )


def lightgbm_meta_bundle_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the meta-labeling bundle round-trip probe."""
    return _base_train_input(
        job_id,
        extra_constraints={
            "training_mode": "canary",
            "enable_meta_labeling": "true",
        },
        output_prefix="/tmp/c8-meta-bundle",  # noqa: S108
    )


def optuna_trial_count_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the Optuna trial-count probe.

    Enables Optuna with a small max_trials and a search space so the
    study artifact records the real trial count.
    """
    return _base_train_input(
        job_id,
        extra_constraints={
            "training_mode": "canary",
            "enable_optuna": "true",
            "optuna_max_trials": "5",
            "optuna_metric": "logloss",
            "optuna_direction": "minimize",
        },
        search_space={
            "num_leaves": [15, 31, 63],
            "learning_rate": [0.05, 0.1, 0.2],
        },
        output_prefix="/tmp/c8-optuna",  # noqa: S108
    )


def cpcv_pbo_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the CPCV/PBO validation probe."""
    return _base_train_input(
        job_id,
        extra_constraints={
            "training_mode": "canary",
            "cv_method": "cpcv",
            "n_folds": "4",
        },
        n_folds=4,
        output_prefix="/tmp/c8-cpcv-pbo",  # noqa: S108
    )


def triple_barrier_meta_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the triple-barrier meta-labeling probe."""
    return _base_train_input(
        job_id,
        extra_constraints={
            "training_mode": "canary",
            "enable_meta_labeling": "true",
            "labeling_method": "triple_barrier",
            "barrier_width": "0.02",
            "holding_period": "10",
        },
        output_prefix="/tmp/c8-triple-barrier",  # noqa: S108
    )


def checkpoint_kill_resume_probe_payload(job_id: str, *, leg: int = 1) -> dict[str, Any]:
    """Build the payload for the checkpoint + resume probe.

    Leg 1: start training with checkpoint_dir set on the volume.
    Leg 2: resume from the latest checkpoint after a simulated kill.
    """
    extra: dict[str, Any] = {"training_mode": "canary"}
    payload = _base_train_input(
        job_id,
        extra_constraints=extra,
        output_prefix="/runpod-volume/models/c8-checkpoint/",
        n_folds=4,  # more folds -> more checkpoints to test resume
    )
    payload["checkpoint_dir"] = "/runpod-volume/checkpoints/c8-checkpoint/"
    if leg == 2:
        payload["resume_from_checkpoint"] = "latest"
    return payload


def durable_artifact_receipt_probe_payload(job_id: str) -> dict[str, Any]:
    """Build the payload for the durable artifact + receipt probe.

    Uses research mode (non-canary) with a volume output_prefix so the
    deny gate is active and the artifact lands on durable storage.
    """
    return _base_train_input(
        job_id,
        extra_constraints={"training_mode": "research"},
        output_prefix=f"/runpod-volume/models/{job_id}/",
    )


# Map probe names -> payload builders.
PAYLOAD_BUILDERS = {
    "pit_fail_closed_probe": pit_fail_closed_probe_payload,
    "feature_set_version_mismatch_probe": feature_set_version_mismatch_probe_payload,
    "lightgbm_single_bundle_probe": lightgbm_single_bundle_probe_payload,
    "lightgbm_meta_bundle_probe": lightgbm_meta_bundle_probe_payload,
    "optuna_trial_count_probe": optuna_trial_count_probe_payload,
    "cpcv_pbo_probe": cpcv_pbo_probe_payload,
    "triple_barrier_meta_probe": triple_barrier_meta_probe_payload,
    "checkpoint_kill_resume_probe": checkpoint_kill_resume_probe_payload,
    "durable_artifact_receipt_probe": durable_artifact_receipt_probe_payload,
}


# ---------------------------------------------------------------------------
# Dry-run validation
# ---------------------------------------------------------------------------


def _validate_payload(probe_name: str, payload: dict[str, Any]) -> list[str]:
    """Validate a probe payload shape. Returns a list of problems (empty = OK)."""
    problems: list[str] = []
    required_keys = {"job_id", "model_family", "extra_constraints"}
    for key in required_keys:
        if key not in payload:
            problems.append(f"missing required key: {key}")
    if "job_id" in payload and not isinstance(payload["job_id"], str):
        problems.append("job_id must be a string")
    if "extra_constraints" in payload:
        ec = payload["extra_constraints"]
        if not isinstance(ec, dict):
            problems.append("extra_constraints must be a dict")
        elif "training_mode" not in ec:
            problems.append("extra_constraints missing training_mode")
    # Probe-specific checks.
    if probe_name == "checkpoint_kill_resume_probe":
        if "checkpoint_dir" not in payload:
            problems.append("checkpoint_kill_resume_probe requires checkpoint_dir")
    if probe_name == "durable_artifact_receipt_probe":
        op = payload.get("output_prefix", "")
        if not (op.startswith("/runpod-volume/") or op.startswith("/workspace/")):
            problems.append(
                "durable_artifact_receipt_probe output_prefix must be a "
                "volume path (/runpod-volume/ or /workspace/)"
            )
    if probe_name == "optuna_trial_count_probe":
        ec = payload.get("extra_constraints", {})
        if ec.get("enable_optuna") != "true":
            problems.append("optuna_trial_count_probe must set enable_optuna=true")
        if not payload.get("search_space"):
            problems.append("optuna_trial_count_probe requires a non-empty search_space")
    return problems


def _check_env_vars(required: list[str]) -> list[str]:
    """Return the list of missing env vars."""
    return [v for v in required if not os.environ.get(v)]


def _print_probe_dry_run(spec: ProbeSpec) -> bool:
    """Print the dry-run evidence for a single probe. Returns True if valid."""
    builder = PAYLOAD_BUILDERS[spec.name]
    job_id = f"c8:dry-run:{spec.name}:{int(time.time())}"
    # checkpoint probe has a leg parameter.
    if spec.name == "checkpoint_kill_resume_probe":
        payload = builder(job_id, leg=1)
    else:
        payload = builder(job_id)

    problems = _validate_payload(spec.name, payload)
    missing_env = _check_env_vars(spec.required_env_vars)

    print(f"\n{'=' * 72}")
    print(f"PROBE: {spec.name}")
    print(f"{'=' * 72}")
    print(f"What it tests: {spec.what_it_tests}")
    print(f"\nHandler mode: {spec.handler_mode}")
    print(f"Requires network volume: {spec.requires_network_volume}")
    print(f"Requires kill simulation: {spec.requires_kill_simulation}")
    print("\nExpected PASS criteria:")
    for c in spec.expected_pass_criteria:
        print(f"  [PASS] {c}")
    print("\nExpected FAIL criteria (probe regression signals):")
    for c in spec.expected_fail_criteria:
        print(f"  [FAIL] {c}")
    print(f"\nRequired env vars: {', '.join(spec.required_env_vars)}")
    if missing_env:
        print(f"  WARNING: missing env vars (would block live run): {', '.join(missing_env)}")
    print("\nExpected evidence artifacts:")
    for e in spec.expected_evidence:
        print(f"  -> {e}")
    print(f"\nEstimated cost: ${spec.estimated_cost_usd:.4f} (GPU rate ${GPU_RATE_PER_HOUR}/hr)")
    print(f"Risk notes: {spec.risk_notes}")
    print("\nPayload (dry-run, NOT dispatched):")
    # Redact the large CSV for readability.
    display = dict(payload)
    if "inline_dataset_csv" in display:
        csv_text = display.pop("inline_dataset_csv")
        display["inline_dataset_rows"] = csv_text.count("\n") - 1
        display["inline_dataset_bytes"] = len(csv_text.encode("utf-8"))
        display["inline_dataset_header"] = csv_text.splitlines()[0]
    print(json.dumps(display, indent=2, default=str, sort_keys=True))

    if problems:
        print("\nPAYLOAD VALIDATION PROBLEMS:")
        for p in problems:
            print(f"  [!] {p}")
        return False
    print("\nPAYLOAD VALIDATION: OK (dry-run)")
    return True


# ---------------------------------------------------------------------------
# Live execution safety gate (NOT used in C8 prep -- prep is dry-run only)
# ---------------------------------------------------------------------------


def _confirm_live_execution(spec: ProbeSpec, sha: str) -> bool:
    """Interactive confirmation gate before live RunPod dispatch.

    This is a SAFETY CHECK. C8 prep never calls this (dry-run only). It
    exists so the script is ready for the live-proof execution phase.
    """
    print("\n" + "!" * 72)
    print("LIVE EXECUTION REQUESTED -- this will incur RunPod cloud spend!")
    print("!" * 72)
    print(f"Probe: {spec.name}")
    print(f"SHA: {sha}")
    print(f"Estimated cost: ${spec.estimated_cost_usd:.4f}")
    print(f"Risk: {spec.risk_notes}")
    if spec.requires_network_volume:
        print("REQUIRES a pre-provisioned RunPod network volume.")
    if spec.requires_kill_simulation:
        print("REQUIRES job cancellation mid-fold (simulated preemption).")
    print("\nType the probe name to confirm: ", end="", flush=True)
    try:
        confirmation = input().strip()
    except EOFError:
        return False
    return confirmation == spec.name


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="C8 Live Proof Ladder Prep (9-probe RunPod proof ladder)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Validate probe logic without dispatching RunPod jobs (default).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Dispatch live RunPod jobs (REQUIRES confirmation -- NOT for C8 prep).",
    )
    parser.add_argument(
        "--probe",
        default=None,
        choices=PROBE_NAMES,
        help="Run a single probe (default: all 9).",
    )
    parser.add_argument(
        "--sha",
        default=None,
        help="Full git SHA for the image tag (required for --live).",
    )
    parser.add_argument(
        "--network-volume-id",
        default=None,
        help="RunPod network volume ID (required for volume-dependent probes in --live).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all probe names and exit.",
    )
    args = parser.parse_args()

    if args.list:
        print("C8 Live Proof Ladder -- 9 probes:")
        for name in PROBE_NAMES:
            print(f"  - {name}")
        return 0

    specs = _all_probe_specs()
    specs_by_name = {s.name: s for s in specs}
    selected = [specs_by_name[args.probe]] if args.probe else specs

    # --- Live mode safety gate ---
    if args.live:
        if not args.sha:
            print("ERROR: --sha is required for --live execution")
            return 1
        print("LIVE MODE -- C8 prep does NOT dispatch live jobs.")
        print("This script is prepared for live execution but C8 is PREP ONLY.")
        print("To execute live, remove the prep guard in a follow-up task.")
        # Print what would run for review.
        for spec in selected:
            print(f"\n[WOULD RUN LIVE] {spec.name} -- est. ${spec.estimated_cost_usd:.4f}")
            if spec.requires_network_volume and not args.network_volume_id:
                print("  BLOCKED: requires --network-volume-id")
        return 0

    # --- Dry-run mode (default) ---
    print("C8 Live Proof Ladder -- DRY RUN (no cloud spend)")
    print(f"Probes: {len(selected)} of {len(specs)}")
    print(f"GPU rate: ${GPU_RATE_PER_HOUR}/hr (ADA_24 estimate)")
    print(
        f"Handler deadline: {DEFAULT_DEADLINE_S}s, min execution timeout: {MIN_EXECUTION_TIMEOUT_S}s"
    )

    all_ok = True
    total_cost = 0.0
    for spec in selected:
        ok = _print_probe_dry_run(spec)
        all_ok = all_ok and ok
        total_cost += spec.estimated_cost_usd

    print(f"\n{'=' * 72}")
    print("DRY RUN SUMMARY")
    print(f"{'=' * 72}")
    print(f"Probes validated: {len(selected)}")
    print(f"All payloads valid: {all_ok}")
    print(f"Total estimated live cost (if all run): ${total_cost:.4f}")
    print("\nDo NOT run live until:")
    print("  1. RUNPOD_API_KEY and QUANT_FOUNDRY_CALLBACK_SECRET are set")
    print("  2. A valid image SHA is built and pushed")
    print("  3. Network volume is provisioned (for volume-dependent probes)")
    print("  4. Orchestrator approves live execution")
    print("  5. Cost budget is confirmed")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
