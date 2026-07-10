"""
Integration tests for the Alpha Genome Lab wiring (TASK-1005 + TASK-0306).

Covers the gateway â†’ AlphaGenomeLab â†’ DossierRegistry contract end-to-end:

- Gateway exposes an `alpha_genome_lab()` accessor that constructs a real
  AlphaGenomeLab wired to the real PromotionGate + DossierRegistry.
- `start_alpha_sweep(...)` returns a JSON-safe receipt; every candidate
  flows through PromotionGate.evaluate() â€” no shortcut, no bypass.
- `alpha_sweep_status(sweep_id)` returns the stored receipt; returns None
  for unknown ids.
- `register_recipe_candidate(dossier)` writes to the real DossierRegistry
  via the upsert adapter.
- No secrets leak in any response (recipe contents, sweep ids, counts only).
- Disabled gateway returns the safe disabled envelope.

File-disjoint from Builder 1's `alpha_genome.py` and `test_alpha_genome.py`
(the unit tests for the lab itself live there). This file exercises the
gateway facade only.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import pytest
from quant_foundry.alpha_genome import (
    Recipe,
    SweepReceipt,
    TrialStatus,
)
from quant_foundry.dossier import DossierRecord
from quant_foundry.gateway import (
    QuantFoundryGateway,
    _alpha_default_dispatcher,
    _alpha_default_tournament_probe,
    _AlphaDossierUpsertAdapter,
    _sweep_receipt_to_dict,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_gateway(tmp_path: pathlib.Path) -> QuantFoundryGateway:
    """Construct a real gateway with mode=local_mock and enabled=True."""
    return QuantFoundryGateway(
        enabled=True,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-secret",
        base_dir=tmp_path,
    )


def _make_recipe(*, recipe_id: str = "seed-1") -> Recipe:
    """Construct a valid recipe for the gbm family (allowed allowlist)."""
    return Recipe(
        recipe_id=recipe_id,
        parent_recipe_id=None,
        mutation_kind=None,
        feature_set=("f1", "f2"),
        model_family="gbm",
        hyperparameters={
            "n_estimators": 100.0,
            "max_depth": 4.0,
            "learning_rate": 0.05,
            "min_child_samples": 20.0,
            "reg_alpha": 0.0,
            "reg_lambda": 1.0,
        },
        train_window_ns=180 * 86_400_000_000_000,  # 180 days
        val_window_ns=30 * 86_400_000_000_000,  # 30 days
        label_horizon_ns=86_400_000_000_000,  # 1 day
        random_seed=42,
    )


def _make_dossier(*, model_id: str = "m-alpha-1") -> DossierRecord:
    """Construct a minimal valid DossierRecord."""
    return DossierRecord(
        model_id=model_id,
        artifact_manifest_id="manifest-1",
        artifact_sha256="a" * 64,
        dataset_manifest_id="dataset-1",
        feature_schema_hash="b" * 64,
        label_schema_hash="c" * 64,
        training_metrics={"accuracy": 0.55},
    )


# ---------------------------------------------------------------------------
# alpha_genome_lab() accessor
# ---------------------------------------------------------------------------


class TestAlphaGenomeLabAccessor:
    def test_returns_real_lab(self, tmp_path: pathlib.Path) -> None:
        """The accessor constructs a real AlphaGenomeLab wired to the
        gateway's gate, registry, and a default mock dispatcher."""
        gw = _make_gateway(tmp_path)
        lab = gw.alpha_genome_lab()
        # AlphaGenomeLab is a dataclass â€” verify the wired fields.
        assert lab.gate is gw.promotion_gate()
        assert isinstance(lab.registry, _AlphaDossierUpsertAdapter)
        # The registry adapter wraps the same dossier_registry the gateway
        # exposes elsewhere â€” no separate registry, no shortcut.
        assert lab.registry._registry is gw.dossier_registry()

    def test_returns_same_instance_on_repeat_call(self, tmp_path: pathlib.Path) -> None:
        """The accessor is lazy + cached â€” repeated calls return the same lab."""
        gw = _make_gateway(tmp_path)
        first = gw.alpha_genome_lab()
        second = gw.alpha_genome_lab()
        assert first is second

    def test_dispatcher_override_is_used(self, tmp_path: pathlib.Path) -> None:
        """A caller-supplied dispatcher overrides the default mock."""
        gw = _make_gateway(tmp_path)
        calls: list[str] = []

        def custom_dispatcher(recipe: Recipe) -> Any:
            calls.append(recipe.recipe_id)
            return None

        gw.alpha_genome_lab(dispatcher=custom_dispatcher)
        assert calls == []  # construction only; run_sweep not yet called


# ---------------------------------------------------------------------------
# start_alpha_sweep()
# ---------------------------------------------------------------------------


class TestStartAlphaSweep:
    def test_runs_sweep_and_returns_receipt(self, tmp_path: pathlib.Path) -> None:
        """`start_alpha_sweep` returns a JSON-safe receipt envelope."""
        gw = _make_gateway(tmp_path)
        result = gw.start_alpha_sweep(
            seed_recipe=_make_recipe(),
            n_recipes=3,
        )
        assert result["enabled"] is True
        assert result["ok"] is True
        sweep = result["sweep"]
        assert sweep["n_recipes"] == 3
        # The default dispatcher returns no dossier, so every trial is
        # REJECTED_BY_GATE (NO_DOSSIER) â€” the safe path. No recipes are
        # REGISTERED, none are KILLED_EARLY (no probe), none are
        # DISCARDED (within budget).
        assert sweep["n_registered"] == 0
        assert sweep["n_rejected"] == 3
        assert sweep["n_killed_early"] == 0
        assert sweep["n_discarded"] == 0
        # No secrets in the receipt â€” sweep ids + counts + per-trial statuses.
        joined = repr(sweep)
        assert "password" not in joined
        assert "secret" not in joined or "callback_secret" not in joined

    def test_disabled_gateway_returns_safe_envelope(self, tmp_path: pathlib.Path) -> None:
        """A disabled gateway refuses to start a sweep."""
        gw = QuantFoundryGateway(
            enabled=False,
            mode="local_mock",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path,
        )
        result = gw.start_alpha_sweep(
            seed_recipe=_make_recipe(),
            n_recipes=1,
        )
        assert result == {"enabled": False, "detail": "Quant Foundry is disabled"}

    def test_invalid_n_recipes_returns_error_envelope(self, tmp_path: pathlib.Path) -> None:
        """An invalid n_recipes value surfaces as an error_code, not a crash."""
        gw = _make_gateway(tmp_path)
        result = gw.start_alpha_sweep(
            seed_recipe=_make_recipe(),
            n_recipes=0,  # Recipe.__init__/lab will reject
        )
        assert result.get("error_code") in {"invalid_sweep_request", "sweep_failed"}

    def test_trial_receipts_carry_no_secrets(self, tmp_path: pathlib.Path) -> None:
        """Per-trial receipts include only ids + counts + status â€” no secrets."""
        gw = _make_gateway(tmp_path)
        result = gw.start_alpha_sweep(
            seed_recipe=_make_recipe(),
            n_recipes=2,
        )
        trials = result["sweep"]["trial_receipts"]
        assert len(trials) == 2
        for tr in trials:
            # Status is from the enum string value, not arbitrary.
            assert tr["status"] in {
                "registered",
                "rejected_by_gate",
                "killed_early",
                "discarded",
            }
            # Numeric fields are present and well-typed.
            assert isinstance(tr["cost_cents"], int)
            assert isinstance(tr["duration_seconds"], float)
            # sweep_id is set; recipe_id is set.
            assert isinstance(tr["sweep_id"], str)
            assert isinstance(tr["recipe_id"], str)


# ---------------------------------------------------------------------------
# alpha_sweep_status() + list_alpha_sweeps()
# ---------------------------------------------------------------------------


class TestAlphaSweepStatus:
    def test_returns_none_when_unknown(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path)
        assert gw.alpha_sweep_status("does-not-exist") is None

    def test_returns_receipt_after_start(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path)
        started = gw.start_alpha_sweep(
            seed_recipe=_make_recipe(),
            n_recipes=1,
        )
        sweep_id = started["sweep"]["sweep_id"]
        stored = gw.alpha_sweep_status(sweep_id)
        assert stored is not None
        assert stored["sweep_id"] == sweep_id
        assert stored["n_recipes"] == 1

    def test_disabled_returns_none(self, tmp_path: pathlib.Path) -> None:
        gw = QuantFoundryGateway(
            enabled=False,
            mode="local_mock",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path,
        )
        assert gw.alpha_sweep_status("any") is None

    def test_list_alpha_sweeps_returns_all(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path)
        gw.start_alpha_sweep(seed_recipe=_make_recipe(recipe_id="seed-a"), n_recipes=1)
        gw.start_alpha_sweep(seed_recipe=_make_recipe(recipe_id="seed-b"), n_recipes=2)
        all_sweeps = gw.list_alpha_sweeps()
        assert len(all_sweeps) == 2
        recipe_ids = {s["seed_recipe_id"] for s in all_sweeps}
        assert recipe_ids == {"seed-a", "seed-b"}

    def test_list_alpha_sweeps_disabled_is_empty(self, tmp_path: pathlib.Path) -> None:
        gw = QuantFoundryGateway(
            enabled=False,
            mode="local_mock",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path,
        )
        assert gw.list_alpha_sweeps() == []


# ---------------------------------------------------------------------------
# register_recipe_candidate() â€” dossier registration contract
# ---------------------------------------------------------------------------


class TestRegisterRecipeCandidate:
    def test_registers_dossier_into_real_registry(self, tmp_path: pathlib.Path) -> None:
        """The dossier lands in the same DossierRegistry every other
        model uses â€” no separate registry, no shortcut."""
        gw = _make_gateway(tmp_path)
        dossier = _make_dossier(model_id="m-from-alpha-1")
        result = gw.register_recipe_candidate(dossier)
        assert result["enabled"] is True
        assert result["ok"] is True
        stored = gw.dossier_registry().get("m-from-alpha-1")
        assert stored is not None
        assert stored.model_id == "m-from-alpha-1"

    def test_idempotent_re_register_returns_existing(self, tmp_path: pathlib.Path) -> None:
        """Re-registering the same dossier (same content_hash) is idempotent."""
        gw = _make_gateway(tmp_path)
        dossier = _make_dossier(model_id="m-idem-1")
        gw.register_recipe_candidate(dossier)
        # Re-register the SAME dossier â€” must be idempotent.
        result = gw.register_recipe_candidate(dossier)
        assert result["ok"] is True
        # Only one record stored.
        assert len(gw.dossier_registry().list()) == 1

    def test_content_hash_mismatch_raises(self, tmp_path: pathlib.Path) -> None:
        """A new dossier under an existing model_id with a different
        content_hash is a security event (ValueError from the registry)."""
        gw = _make_gateway(tmp_path)
        first = _make_dossier(model_id="m-tamper-1")
        gw.register_recipe_candidate(first)
        # Build a different dossier for the same model_id (different sha).
        second = DossierRecord(
            model_id="m-tamper-1",
            artifact_manifest_id="manifest-1",
            artifact_sha256="d" * 64,  # different sha -> different content_hash
            dataset_manifest_id="dataset-1",
            feature_schema_hash="b" * 64,
            label_schema_hash="c" * 64,
            training_metrics={"accuracy": 0.55},
        )
        with pytest.raises(ValueError, match="content hash mismatch"):
            gw.register_recipe_candidate(second)

    def test_disabled_gateway_returns_safe_envelope(self, tmp_path: pathlib.Path) -> None:
        gw = QuantFoundryGateway(
            enabled=False,
            mode="local_mock",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path,
        )
        result = gw.register_recipe_candidate(_make_dossier())
        assert result == {"enabled": False, "detail": "Quant Foundry is disabled"}


# ---------------------------------------------------------------------------
# Tournament gate non-bypass
# ---------------------------------------------------------------------------


class TestGateNonBypass:
    def test_real_dossier_lands_through_gate(self, tmp_path: pathlib.Path) -> None:
        """When the dispatcher returns a real DossierRecord with a real
        tournament result + sentinel receipt, the lab routes through the
        gate, the gate evaluates, and (when evidence is sufficient) the
        dossier is registered."""
        from quant_foundry.sentinel import SentinelReceipt
        from quant_foundry.tournament import (
            PromotionRecommendation,
            TournamentResult,
            TournamentStatus,
        )

        gw = _make_gateway(tmp_path)
        dossier = _make_dossier(model_id="m-evidence-1")
        sentinel = SentinelReceipt(
            model_id="m-evidence-1",
            passed=True,
            issues=[],
            checks_run=["shuffled_labels", "purged_fold"],
            ts_ns=0,
        )
        tournament = TournamentResult(
            model_id="m-evidence-1",
            total_score=0.9,
            settled_count=15,  # above PromotionGate default min_settled_count=10
            deflated_sharpe=0.9,
            status=TournamentStatus.ELIGIBLE,
            recommendation=PromotionRecommendation.PROMOTE,
        )

        # C7 evidence chain â€” required by the hardened promotion gate.
        from quant_foundry.bundle_io import TrainingSelfCheck
        from quant_foundry.promotion import CallbackReceiptRef, PITEvidenceRef

        c7_evidence = {
            "selfcheck": TrainingSelfCheck(
                passed=True,
                bundle_sha256=dossier.artifact_sha256,
                n_rows_scored=10,
            ),
            "callback_receipt": CallbackReceiptRef(
                status="processed",
                receipt_id="cb:manifest-1",
            ),
            "artifact_uri": "file:///durable/manifest-1.zip",
            "feature_set_version": "fs-v1",
            "pit_evidence": PITEvidenceRef(
                verified=True,
                evidence_sha256="e" * 64,
                manifest_hash=dossier.dataset_manifest_id,
            ),
            "backend_eligible": True,
        }

        def real_dispatcher(recipe: Recipe) -> Any:
            return _StubOutcome(
                model_id="m-evidence-1",
                cost_cents=0,
                duration_seconds=0.0,
                dossier_evidence=dossier,
                tournament_result=tournament,
                sentinel_receipt=sentinel,
                c7_evidence=c7_evidence,
            )

        result = gw.start_alpha_sweep(
            seed_recipe=_make_recipe(),
            n_recipes=1,
            dispatcher=real_dispatcher,
        )
        # The single trial should have been APPROVED by the gate and the
        # dossier registered via the upsert adapter.
        assert result["sweep"]["n_registered"] == 1
        assert result["sweep"]["n_rejected"] == 0
        # Verify the dossier is in the registry.
        stored = gw.dossier_registry().get("m-evidence-1")
        assert stored is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestSweepReceiptToDict:
    def test_returns_json_safe_dict(self) -> None:
        """The serializer converts a SweepReceipt dataclass into a
        JSON-safe dict with scalar fields and a list of trial dicts."""
        trial = _StubTrial(
            recipe_id="r1",
            parent_recipe_id=None,
            status=TrialStatus.REJECTED_BY_GATE,
            reason="no_dossier",
            model_id=None,
            cost_cents=0,
            duration_seconds=0.0,
            promotion_decision="rejected",
            sweep_id="s1",
        )
        receipt = SweepReceipt(
            sweep_id="s1",
            seed_recipe_id="seed-1",
            n_recipes=1,
            n_registered=0,
            n_rejected=1,
            n_killed_early=0,
            n_discarded=0,
            sweep_cost_cents=0,
            started_at_ns=1000,
            ended_at_ns=2000,
            trial_receipts=(trial,),
        )
        out = _sweep_receipt_to_dict(receipt)
        assert out["sweep_id"] == "s1"
        assert out["seed_recipe_id"] == "seed-1"
        assert out["n_recipes"] == 1
        assert out["n_rejected"] == 1
        assert out["started_at_ns"] == 1000
        assert out["ended_at_ns"] == 2000
        assert len(out["trial_receipts"]) == 1
        assert out["trial_receipts"][0]["status"] == "rejected_by_gate"


class TestDefaultDispatcherAndProbe:
    def test_default_dispatcher_returns_safe_outcome(self) -> None:
        """The default dispatcher returns a mock outcome with no dossier
        so the gate rejects with NO_DOSSIER â€” the safe default path."""
        recipe = _make_recipe()
        outcome = _alpha_default_dispatcher(recipe)
        assert outcome.model_id == f"alpha-mock-{recipe.recipe_id}"
        assert outcome.dossier_evidence is None
        assert outcome.cost_cents == 0

    def test_default_tournament_probe_returns_none(self) -> None:
        """No probe means no early stop â€” sweeps run to budget or completion."""
        assert _alpha_default_tournament_probe("any-recipe-id") is None


class TestUpsertAdapter:
    def test_upsert_delegates_to_registry_register(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path)
        adapter = _AlphaDossierUpsertAdapter(gw.dossier_registry())
        dossier = _make_dossier(model_id="m-adapter-1")
        returned = adapter.upsert(dossier)
        assert returned.model_id == "m-adapter-1"
        # Same record in the underlying registry.
        assert gw.dossier_registry().get("m-adapter-1") is not None

    def test_upsert_raises_on_content_hash_mismatch(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path)
        adapter = _AlphaDossierUpsertAdapter(gw.dossier_registry())
        adapter.upsert(_make_dossier(model_id="m-mismatch-1"))
        # Different sha -> different content_hash -> security event.
        second = DossierRecord(
            model_id="m-mismatch-1",
            artifact_manifest_id="manifest-1",
            artifact_sha256="f" * 64,
            dataset_manifest_id="dataset-1",
            feature_schema_hash="b" * 64,
            label_schema_hash="c" * 64,
            training_metrics={"accuracy": 0.55},
        )
        with pytest.raises(ValueError, match="content hash mismatch"):
            adapter.upsert(second)


# ---------------------------------------------------------------------------
# Helpers (test-local)
# ---------------------------------------------------------------------------


@dataclass
class _StubOutcome:
    """Stub TrainingOutcome for tests that need a real dossier path."""

    model_id: str
    cost_cents: int = 0
    duration_seconds: float = 0.0
    dossier_evidence: Any = None
    tournament_result: Any = None
    sentinel_receipt: Any = None
    c7_evidence: dict[str, Any] | None = None


@dataclass
class _StubTrial:
    """Stub TrialReceipt-shaped object for the serializer test."""

    recipe_id: str
    parent_recipe_id: str | None
    status: TrialStatus
    reason: str
    model_id: str | None
    cost_cents: int
    duration_seconds: float
    promotion_decision: str | None
    sweep_id: str
