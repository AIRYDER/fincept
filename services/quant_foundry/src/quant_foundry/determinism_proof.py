"""Determinism proof runner for CI gates (Tier 3.1).

Formalizes the A7 canary's bit-identical model hash proof into a
reusable, automated CI gate. The runner trains the same
(dataset manifest, code SHA, seed) recipe twice and **fails if the
SHA-256s differ**. Any nondeterminism regression (library bump,
threading change, GPU nondeterminism) is caught the day it lands.

The runner is designed to work in two modes:

  1. **In-process** (CI/local): trains both runs in the same Python
     process using the real LightGBM trainer with deterministic flags
     (``deterministic=True``, ``num_threads=1``, ``force_col_wise=True``).
     This catches library-level nondeterminism without needing RunPod.

  2. **Cross-worker** (nightly/full proof): dispatches two independent
     RunPod jobs and compares the returned artifact SHA-256s. This
     catches environment-level nondeterminism (OS, container, GPU
     model). This mode requires RunPod credentials and is not run in
     PR CI.

The proof only runs for backends with ``determinism_status="deterministic"``.
GPU backends (``xgboost_gpu``, ``catboost_gpu``) are skipped because
GPU floating-point summation order is inherently non-deterministic.

The receipt is a typed :class:`DeterminismProofReceipt` that records
both runs' SHA-256s, the verdict, and the recipe used. The receipt
can be persisted and audited.

Design:
  * Pure-Python, no torch/numpy at module level.
  * Pydantic v2 models for the receipt, consistent with the rest of
    ``quant_foundry``.
  * The runner is callable from CI (GitHub Actions) and from local
    scripts.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DeterminismProofReceipt",
    "DeterminismProofRunner",
    "DeterminismRecipe",
    "DeterminismRunResult",
    "DeterminismVerdict",
]


# --------------------------------------------------------------------------- #
# Types                                                                        #
# --------------------------------------------------------------------------- #


class DeterminismVerdict:
    """Verdict strings for a determinism proof."""

    BIT_DETERMINISTIC = "bit_deterministic"
    NON_DETERMINISTIC = "non_deterministic"
    FAILED = "failed"
    SKIPPED = "skipped"


class DeterminismRecipe(BaseModel):
    """A fixed training recipe for determinism proof.

    All fields that affect reproducibility are pinned. Two runs with
    the same recipe should produce the same model SHA-256.

    Fields:
        model_family: the model family (e.g. ``"lightgbm"``).
        random_seed: the random seed for training.
        search_space: the hyperparameter search space (single values
            for deterministic proof, not ranges).
        extra_constraints: extra training constraints.
        inline_dataset_csv: the inline dataset (for CI without a
            manifest). If None, ``dataset_manifest_ref`` must be set.
        dataset_manifest_ref: the dataset manifest reference (for
            cross-worker proof). If None, ``inline_dataset_csv`` must
            be set.
        determinism_status: the expected determinism status. Only
            ``"deterministic"`` backends are proofed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_family: str = "lightgbm"
    random_seed: int = 42
    search_space: dict[str, list[Any]] = Field(default_factory=dict)
    extra_constraints: dict[str, str] = Field(default_factory=dict)
    inline_dataset_csv: str | None = None
    dataset_manifest_ref: str | None = None
    determinism_status: str = "deterministic"

    @field_validator("model_family")
    @classmethod
    def _model_family_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("model_family must be non-empty")
        return v


class DeterminismRunResult(BaseModel):
    """The result of a single training run in a determinism proof.

    Fields:
        run_label: the run label (e.g. ``"run1"``).
        sha256: the model artifact SHA-256.
        artifact_id: the artifact ID.
        size_bytes: the model artifact size in bytes.
        accuracy: the training accuracy (if available).
        sharpe_ratio: the training Sharpe ratio (if available).
        elapsed_seconds: the wall-clock time for the run.
        error: error message if the run failed, None otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_label: str
    sha256: str = ""
    artifact_id: str = ""
    size_bytes: int = 0
    accuracy: float | None = None
    sharpe_ratio: float | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None


class DeterminismProofReceipt(BaseModel):
    """The receipt from a determinism proof run.

    Records both runs' results, the verdict, and the recipe used.
    The receipt is the auditable artifact that a CI gate or human
    operator can inspect.

    Fields:
        test: the test name (always ``"determinism_proof"``).
        timestamp_utc: ISO timestamp of the proof.
        recipe: the training recipe used.
        run1: the first run's result.
        run2: the second run's result.
        sha256_match: True if both runs produced the same SHA-256.
        critical_fields_match: True if SHA-256, artifact_id, size,
            accuracy, and Sharpe all match.
        verdict: one of ``DeterminismVerdict.*``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    test: str = "determinism_proof"
    timestamp_utc: str
    recipe: DeterminismRecipe
    run1: DeterminismRunResult
    run2: DeterminismRunResult
    sha256_match: bool
    critical_fields_match: bool
    verdict: str


# --------------------------------------------------------------------------- #
# Runner                                                                       #
# --------------------------------------------------------------------------- #


class DeterminismProofRunner:
    """Runs a determinism proof: train twice, compare SHA-256s.

    The runner trains the same recipe twice (in-process by default)
    and compares the model artifact SHA-256s. If they differ, the
    verdict is ``NON_DETERMINISTIC``. If either run fails, the verdict
    is ``FAILED``. If the backend is non-deterministic (e.g. GPU),
    the proof is skipped with verdict ``SKIPPED``.

    Usage::

        recipe = DeterminismRecipe(
            model_family="lightgbm",
            random_seed=42,
            inline_dataset_csv="feature_1,label\\n0.1,1\\n...",
        )
        runner = DeterminismProofRunner()
        receipt = runner.run(recipe)
        if receipt.verdict != DeterminismVerdict.BIT_DETERMINISTIC:
            raise SystemExit(f"DETERMINISM GATE FAILED: {receipt.verdict}")
    """

    def run(self, recipe: DeterminismRecipe) -> DeterminismProofReceipt:
        """Run the determinism proof and return a receipt.

        Args:
            recipe: the fixed training recipe.

        Returns:
            A :class:`DeterminismProofReceipt` with the verdict.
        """
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Skip non-deterministic backends (GPU).
        if recipe.determinism_status != "deterministic":
            skipped = DeterminismRunResult(
                run_label="skipped",
                error=f"backend determinism_status={recipe.determinism_status}",
            )
            return DeterminismProofReceipt(
                timestamp_utc=timestamp,
                recipe=recipe,
                run1=skipped,
                run2=skipped,
                sha256_match=False,
                critical_fields_match=False,
                verdict=DeterminismVerdict.SKIPPED,
            )

        r1 = self._run_training(recipe, "run1")
        r2 = self._run_training(recipe, "run2")

        sha256_match = (
            r1.error is None and r2.error is None and r1.sha256 == r2.sha256 and r1.sha256 != ""
        )
        critical_fields_match = (
            sha256_match
            and r1.artifact_id == r2.artifact_id
            and r1.size_bytes == r2.size_bytes
            and r1.accuracy == r2.accuracy
            and r1.sharpe_ratio == r2.sharpe_ratio
        )

        if r1.error is not None or r2.error is not None:
            verdict = DeterminismVerdict.FAILED
        elif sha256_match and critical_fields_match:
            verdict = DeterminismVerdict.BIT_DETERMINISTIC
        else:
            verdict = DeterminismVerdict.NON_DETERMINISTIC

        return DeterminismProofReceipt(
            timestamp_utc=timestamp,
            recipe=recipe,
            run1=r1,
            run2=r2,
            sha256_match=sha256_match,
            critical_fields_match=critical_fields_match,
            verdict=verdict,
        )

    def _run_training(
        self,
        recipe: DeterminismRecipe,
        run_label: str,
    ) -> DeterminismRunResult:
        """Run a single training job in-process.

        This uses the handler's in-process training path (same as the
        S2 determinism proof script). The handler is imported lazily
        so the module is importable without the handler dependencies.
        """
        import json
        import os
        import pathlib
        import sys

        # Ensure handler and quant_foundry are importable.
        repo_root = pathlib.Path(__file__).resolve().parent
        # Go up from services/quant_foundry/src/quant_foundry/ to repo root.
        # Path: fincept-terminal/services/quant_foundry/src/quant_foundry/determinism_proof.py
        # .parent → quant_foundry/ → src/ → quant_foundry/ → services/ → fincept-terminal/
        for _ in range(4):
            repo_root = repo_root.parent
        handler_dir = str(repo_root / "runpod" / "quant-foundry-training")
        qf_src = str(repo_root / "services" / "quant_foundry" / "src")
        for p in [handler_dir, qf_src]:
            if p not in sys.path:
                sys.path.insert(0, p)

        os.environ.setdefault(
            "QUANT_FOUNDRY_CALLBACK_SECRET",
            "determinism-proof-ci-secret",
        )
        os.environ.setdefault("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

        job_id = f"det-proof-{run_label}-{int(time.time())}"
        job_input: dict[str, Any] = {
            "schema_version": 1,
            "job_id": job_id,
            "dataset_manifest_ref": recipe.dataset_manifest_ref or "inline://placeholder",
            "model_family": recipe.model_family,
            "random_seed": recipe.random_seed,
            "search_space": recipe.search_space,
            "extra_constraints": {
                "bar_seconds": "86400",
                "horizon_bars": "5",
                "purge_bars": "5",
                "training_mode": "canary",
                **recipe.extra_constraints,
            },
        }
        if recipe.inline_dataset_csv:
            job_input["inline_dataset_csv"] = recipe.inline_dataset_csv

        event = {"input": job_input}

        start = time.time()
        try:
            import importlib

            handler_mod = importlib.import_module("handler")
            result = handler_mod.handler(event)
            elapsed = time.time() - start

            if "error_code" in result:
                return DeterminismRunResult(
                    run_label=run_label,
                    elapsed_seconds=elapsed,
                    error=f"{result.get('error_code')}: {result.get('error_summary', '')[:200]}",
                )

            callback_payload_str = result.get("callback_payload", "")
            envelope = json.loads(callback_payload_str)
            payload = envelope.get("payload", envelope)
            artifact = payload.get("artifact_manifest", {})
            metrics = payload.get("dossier", {}).get(
                "training_metrics", payload.get("training_metrics", {})
            )

            return DeterminismRunResult(
                run_label=run_label,
                sha256=artifact.get("sha256", ""),
                artifact_id=artifact.get("artifact_id", ""),
                size_bytes=artifact.get("size_bytes", 0),
                accuracy=metrics.get("accuracy"),
                sharpe_ratio=metrics.get("sharpe_ratio"),
                elapsed_seconds=elapsed,
            )
        except Exception as exc:
            elapsed = time.time() - start
            return DeterminismRunResult(
                run_label=run_label,
                elapsed_seconds=elapsed,
                error=str(exc)[:500],
            )


# --------------------------------------------------------------------------- #
# CI gate helper                                                               #
# --------------------------------------------------------------------------- #


def run_determinism_gate(recipe: DeterminismRecipe | None = None) -> int:
    """Run the determinism proof as a CI gate.

    Returns 0 if the proof passes (BIT_DETERMINISTIC or SKIPPED),
    1 if it fails (NON_DETERMINISTIC or FAILED).

    Args:
        recipe: the training recipe. If None, a default minimal
            recipe is used (LightGBM, 100-row synthetic dataset,
            seed=42).

    Returns:
        0 on success, 1 on failure.
    """
    import json
    import pathlib

    if recipe is None:
        import random

        rng = random.Random(42)
        rows = ["feature_1,feature_2,feature_3,label\n"]
        for _ in range(100):
            f1 = rng.gauss(0, 1)
            f2 = rng.gauss(0, 1)
            f3 = rng.gauss(0, 1)
            label = 1 if (f1 + f2 + f3 + rng.gauss(0, 0.5)) > 0 else 0
            rows.append(f"{f1:.6f},{f2:.6f},{f3:.6f},{label}\n")
        recipe = DeterminismRecipe(
            model_family="lightgbm",
            random_seed=42,
            search_space={
                "num_leaves": [31],
                "learning_rate": [0.1],
                "max_depth": [6],
                "n_estimators": [50],
                "min_data_in_leaf": [5],
            },
            inline_dataset_csv="".join(rows),
        )

    runner = DeterminismProofRunner()
    receipt = runner.run(recipe)

    print("=" * 70)
    print(f"DETERMINISM PROOF: {receipt.verdict}")
    print("=" * 70)
    print(f"  Run 1 SHA-256: {receipt.run1.sha256}")
    print(f"  Run 2 SHA-256: {receipt.run2.sha256}")
    print(f"  SHA-256 match: {receipt.sha256_match}")
    print(f"  Critical match: {receipt.critical_fields_match}")
    if receipt.run1.error:
        print(f"  Run 1 error: {receipt.run1.error}")
    if receipt.run2.error:
        print(f"  Run 2 error: {receipt.run2.error}")

    # Save receipt
    repo_root = pathlib.Path(__file__).resolve().parent
    for _ in range(4):
        repo_root = repo_root.parent
    receipt_path = repo_root / "reports" / "determinism-proof" / "receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(
            json.loads(receipt.model_dump_json()),
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  Receipt saved to: {receipt_path}")

    if receipt.verdict in (
        DeterminismVerdict.BIT_DETERMINISTIC,
        DeterminismVerdict.SKIPPED,
    ):
        return 0
    return 1
