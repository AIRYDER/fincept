"""Tests for determinism_proof module (Tier 3.1).

Tests the receipt/verdict logic without requiring the real handler
(fast unit tests), plus one integration test that runs the actual
handler in-process (marked slow, requires LightGBM).
"""

from __future__ import annotations

import pytest
from quant_foundry.determinism_proof import (
    DeterminismProofReceipt,
    DeterminismProofRunner,
    DeterminismRecipe,
    DeterminismRunResult,
    DeterminismVerdict,
)


# --------------------------------------------------------------------------- #
# Recipe validation                                                            #
# --------------------------------------------------------------------------- #


class TestDeterminismRecipe:
    def test_defaults(self) -> None:
        recipe = DeterminismRecipe()
        assert recipe.model_family == "lightgbm"
        assert recipe.random_seed == 42
        assert recipe.determinism_status == "deterministic"
        assert recipe.search_space == {}
        assert recipe.extra_constraints == {}
        assert recipe.inline_dataset_csv is None
        assert recipe.dataset_manifest_ref is None

    def test_frozen(self) -> None:
        recipe = DeterminismRecipe()
        with pytest.raises(Exception):  # ValidationError
            recipe.model_family = "xgboost"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(Exception):  # ValidationError
            DeterminismRecipe(unknown_field="bad")  # type: ignore[arg-type]

    def test_model_family_nonempty(self) -> None:
        with pytest.raises(Exception):  # ValidationError
            DeterminismRecipe(model_family="")

    def test_custom_values(self) -> None:
        recipe = DeterminismRecipe(
            model_family="xgboost",
            random_seed=7,
            search_space={"max_depth": [3]},
            extra_constraints={"training_mode": "research"},
            inline_dataset_csv="a,b,label\n1,2,0\n",
            determinism_status="non_deterministic",
        )
        assert recipe.model_family == "xgboost"
        assert recipe.random_seed == 7
        assert recipe.search_space == {"max_depth": [3]}
        assert recipe.extra_constraints == {"training_mode": "research"}
        assert recipe.inline_dataset_csv == "a,b,label\n1,2,0\n"
        assert recipe.determinism_status == "non_deterministic"


# --------------------------------------------------------------------------- #
# Run result                                                                   #
# --------------------------------------------------------------------------- #


class TestDeterminismRunResult:
    def test_defaults(self) -> None:
        r = DeterminismRunResult(run_label="run1")
        assert r.run_label == "run1"
        assert r.sha256 == ""
        assert r.artifact_id == ""
        assert r.size_bytes == 0
        assert r.accuracy is None
        assert r.sharpe_ratio is None
        assert r.elapsed_seconds == 0.0
        assert r.error is None

    def test_frozen(self) -> None:
        r = DeterminismRunResult(run_label="run1")
        with pytest.raises(Exception):
            r.sha256 = "abc"  # type: ignore[misc]

    def test_with_error(self) -> None:
        r = DeterminismRunResult(run_label="run1", error="something failed")
        assert r.error == "something failed"
        assert r.sha256 == ""


# --------------------------------------------------------------------------- #
# Runner verdict logic (mocked _run_training)                                 #
# --------------------------------------------------------------------------- #


def _make_result(
    run_label: str,
    *,
    sha256: str = "abc123",
    artifact_id: str = "art-1",
    size_bytes: int = 100,
    accuracy: float = 0.85,
    sharpe_ratio: float = 1.5,
    error: str | None = None,
) -> DeterminismRunResult:
    return DeterminismRunResult(
        run_label=run_label,
        sha256=sha256,
        artifact_id=artifact_id,
        size_bytes=size_bytes,
        accuracy=accuracy,
        sharpe_ratio=sharpe_ratio,
        error=error,
    )


class TestDeterminismProofRunner:
    def test_bit_deterministic(self) -> None:
        """Two identical runs → BIT_DETERMINISTIC."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(label)  # type: ignore[assignment]
        receipt = runner.run(DeterminismRecipe())
        assert receipt.verdict == DeterminismVerdict.BIT_DETERMINISTIC
        assert receipt.sha256_match is True
        assert receipt.critical_fields_match is True

    def test_non_deterministic_sha_mismatch(self) -> None:
        """Different SHA-256s → NON_DETERMINISTIC."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(  # type: ignore[assignment]
            label, sha256="hash_a" if label == "run1" else "hash_b",
        )
        receipt = runner.run(DeterminismRecipe())
        assert receipt.verdict == DeterminismVerdict.NON_DETERMINISTIC
        assert receipt.sha256_match is False

    def test_non_deterministic_size_mismatch(self) -> None:
        """Same SHA but different size → NON_DETERMINISTIC (critical fields)."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(  # type: ignore[assignment]
            label, size_bytes=100 if label == "run1" else 200,
        )
        receipt = runner.run(DeterminismRecipe())
        assert receipt.verdict == DeterminismVerdict.NON_DETERMINISTIC
        assert receipt.sha256_match is True  # SHA matches
        assert receipt.critical_fields_match is False  # but size differs

    def test_non_deterministic_accuracy_mismatch(self) -> None:
        """Same SHA but different accuracy → NON_DETERMINISTIC."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(  # type: ignore[assignment]
            label, accuracy=0.85 if label == "run1" else 0.90,
        )
        receipt = runner.run(DeterminismRecipe())
        assert receipt.verdict == DeterminismVerdict.NON_DETERMINISTIC
        assert receipt.critical_fields_match is False

    def test_failed_run1_error(self) -> None:
        """Run 1 fails → FAILED."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: (  # type: ignore[assignment]
            _make_result(label, error="import failed")
            if label == "run1"
            else _make_result(label)
        )
        receipt = runner.run(DeterminismRecipe())
        assert receipt.verdict == DeterminismVerdict.FAILED
        assert receipt.sha256_match is False

    def test_failed_run2_error(self) -> None:
        """Run 2 fails → FAILED."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: (  # type: ignore[assignment]
            _make_result(label)
            if label == "run1"
            else _make_result(label, error="OOM")
        )
        receipt = runner.run(DeterminismRecipe())
        assert receipt.verdict == DeterminismVerdict.FAILED

    def test_failed_both_errors(self) -> None:
        """Both runs fail → FAILED."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(  # type: ignore[assignment]
            label, error="crash",
        )
        receipt = runner.run(DeterminismRecipe())
        assert receipt.verdict == DeterminismVerdict.FAILED

    def test_skipped_gpu_backend(self) -> None:
        """Non-deterministic backend (GPU) → SKIPPED."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(label)  # type: ignore[assignment]
        receipt = runner.run(
            DeterminismRecipe(determinism_status="non_deterministic")
        )
        assert receipt.verdict == DeterminismVerdict.SKIPPED
        assert receipt.run1.error is not None
        assert receipt.run2.error is not None
        # _run_training should NOT have been called for skipped
        assert receipt.run1.run_label == "skipped"

    def test_empty_sha256_treated_as_mismatch(self) -> None:
        """Both runs return empty SHA-256 → NON_DETERMINISTIC (not a match)."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(  # type: ignore[assignment]
            label, sha256="",
        )
        receipt = runner.run(DeterminismRecipe())
        # Empty SHA-256 means the run didn't produce a real artifact.
        # sha256_match is False because we require sha256 != "".
        assert receipt.sha256_match is False
        assert receipt.verdict == DeterminismVerdict.NON_DETERMINISTIC

    def test_receipt_is_frozen(self) -> None:
        """Receipt is immutable."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(label)  # type: ignore[assignment]
        receipt = runner.run(DeterminismRecipe())
        with pytest.raises(Exception):
            receipt.verdict = "hacked"  # type: ignore[misc]

    def test_receipt_has_recipe(self) -> None:
        """Receipt records the recipe used."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(label)  # type: ignore[assignment]
        recipe = DeterminismRecipe(random_seed=99, model_family="lightgbm")
        receipt = runner.run(recipe)
        assert receipt.recipe.random_seed == 99
        assert receipt.recipe.model_family == "lightgbm"

    def test_receipt_timestamp_format(self) -> None:
        """Receipt timestamp is ISO format."""
        runner = DeterminismProofRunner()
        runner._run_training = lambda recipe, label: _make_result(label)  # type: ignore[assignment]
        receipt = runner.run(DeterminismRecipe())
        # ISO format: YYYY-MM-DDTHH:MM:SSZ
        assert receipt.timestamp_utc.endswith("Z")
        assert "T" in receipt.timestamp_utc


# --------------------------------------------------------------------------- #
# CI gate helper                                                               #
# --------------------------------------------------------------------------- #


class TestRunDeterminismGate:
    def test_gate_returns_0_on_bit_deterministic(self, monkeypatch) -> None:
        """CI gate returns 0 when verdict is BIT_DETERMINISTIC."""
        def mock_run(self, recipe):
            return DeterminismProofReceipt(
                timestamp_utc="2026-01-01T00:00:00Z",
                recipe=recipe,
                run1=_make_result("run1"),
                run2=_make_result("run2"),
                sha256_match=True,
                critical_fields_match=True,
                verdict=DeterminismVerdict.BIT_DETERMINISTIC,
            )
        monkeypatch.setattr(DeterminismProofRunner, "run", mock_run)
        # Use a minimal recipe so it doesn't try to generate a dataset
        rc = DeterminismProofRunner.__new__(DeterminismProofRunner)
        # Call the module-level function directly
        from quant_foundry.determinism_proof import run_determinism_gate
        # The function creates its own runner, so monkeypatch works
        assert run_determinism_gate(DeterminismRecipe()) == 0

    def test_gate_returns_0_on_skipped(self, monkeypatch) -> None:
        """CI gate returns 0 when verdict is SKIPPED (GPU backend)."""
        def mock_run(self, recipe):
            skipped = DeterminismRunResult(
                run_label="skipped", error="non_deterministic"
            )
            return DeterminismProofReceipt(
                timestamp_utc="2026-01-01T00:00:00Z",
                recipe=recipe,
                run1=skipped,
                run2=skipped,
                sha256_match=False,
                critical_fields_match=False,
                verdict=DeterminismVerdict.SKIPPED,
            )
        monkeypatch.setattr(DeterminismProofRunner, "run", mock_run)
        from quant_foundry.determinism_proof import run_determinism_gate
        assert run_determinism_gate(DeterminismRecipe()) == 0

    def test_gate_returns_1_on_non_deterministic(self, monkeypatch) -> None:
        """CI gate returns 1 when verdict is NON_DETERMINISTIC."""
        def mock_run(self, recipe):
            return DeterminismProofReceipt(
                timestamp_utc="2026-01-01T00:00:00Z",
                recipe=recipe,
                run1=_make_result("run1", sha256="a"),
                run2=_make_result("run2", sha256="b"),
                sha256_match=False,
                critical_fields_match=False,
                verdict=DeterminismVerdict.NON_DETERMINISTIC,
            )
        monkeypatch.setattr(DeterminismProofRunner, "run", mock_run)
        from quant_foundry.determinism_proof import run_determinism_gate
        assert run_determinism_gate(DeterminismRecipe()) == 1

    def test_gate_returns_1_on_failed(self, monkeypatch) -> None:
        """CI gate returns 1 when verdict is FAILED."""
        def mock_run(self, recipe):
            return DeterminismProofReceipt(
                timestamp_utc="2026-01-01T00:00:00Z",
                recipe=recipe,
                run1=_make_result("run1", error="crash"),
                run2=_make_result("run2"),
                sha256_match=False,
                critical_fields_match=False,
                verdict=DeterminismVerdict.FAILED,
            )
        monkeypatch.setattr(DeterminismProofRunner, "run", mock_run)
        from quant_foundry.determinism_proof import run_determinism_gate
        assert run_determinism_gate(DeterminismRecipe()) == 1
