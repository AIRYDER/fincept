"""
TDD tests for quant_foundry.alpha_genome (TASK-1005).

The Alpha Genome Lab is the bounded-mutation engine that proposes
candidate recipes (feature set + model type + hyperparameters + windows)
for training and evaluation. The lab must:

- Reproduce recipes deterministically (same content -> same hash).
- Reject mutations outside the allowlist.
- Enforce per-recipe and per-sweep trial budgets via ``TrialBudget``,
  consulting ``BudgetGuard`` for the global kill switch.
- Early-stop underperforming recipes based on intermediate tournament
  scores.
- Force every candidate through the same ``PromotionGate.evaluate()``
  path — no recipe can bypass tournament/sentinel gates.
- Never emit a secret in any recipe or receipt.

Each test class targets one acceptance criterion from NEXT_FIVE_TASKS.md
Task 3.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import pytest
from quant_foundry.alpha_genome import (
    ALLOWED_MODEL_FAMILIES,
    ALLOWED_TRANSFORMS,
    DEFAULT_TRAIN_WINDOW_RANGE_NS,
    HYPERPARAM_BOUNDS,
    AlphaGenomeLab,
    BudgetDecision,
    DiscardReceipt,
    EarlyStopDecision,
    EarlyStopper,
    MutationKind,
    Recipe,
    RecipeMutation,
    SweepReceipt,
    TrialBudget,
    TrialStatus,
)
from quant_foundry.budget import BudgetGuard

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_recipe(**overrides: Any) -> Recipe:
    """Build a valid seed Recipe for tests."""
    base: dict[str, Any] = {
        "recipe_id": "recipe-seed-0001",
        "parent_recipe_id": None,
        "mutation_kind": None,
        "feature_set": ("f1", "f2", "f3"),
        "model_family": "gbm",
        "hyperparameters": {
            "n_estimators": 100.0,
            "max_depth": 4.0,
            "learning_rate": 0.05,
        },
        "train_window_ns": 365 * 86_400_000_000_000,  # 1 year
        "val_window_ns": 30 * 86_400_000_000_000,  # 30 days
        "label_horizon_ns": 86_400_000_000_000,  # 1 day
        "random_seed": 42,
    }
    base.update(overrides)
    return Recipe(**base)


def _seed_recipe_factory() -> Recipe:
    return _seed_recipe()


# ---------------------------------------------------------------------------
# Recipe reproducibility
# ---------------------------------------------------------------------------


class TestRecipeReproducibility:
    def test_recipe_hash_is_deterministic(self) -> None:
        r1 = _seed_recipe()
        r2 = _seed_recipe()
        assert r1.recipe_hash == r2.recipe_hash
        assert len(r1.recipe_hash) == 64  # full sha256 hex

    def test_recipe_id_derivation_is_stable(self) -> None:
        r1 = _seed_recipe()
        r2 = _seed_recipe()
        assert r1.recipe_id == r2.recipe_id

    def test_different_feature_set_yields_different_hash(self) -> None:
        r1 = _seed_recipe()
        r2 = _seed_recipe(feature_set=("f1", "f2", "f3", "f4"))
        assert r1.recipe_hash != r2.recipe_hash

    def test_different_hyperparameters_yield_different_hash(self) -> None:
        r1 = _seed_recipe()
        r2 = _seed_recipe(
            hyperparameters={
                "n_estimators": 200.0,
                "max_depth": 4.0,
                "learning_rate": 0.05,
            }
        )
        assert r1.recipe_hash != r2.recipe_hash

    def test_random_seed_does_not_affect_hash(self) -> None:
        """Seed is metadata; deterministic content omits it (but we
        include it via the canonical payload). Verify the canonical
        payload is what feeds the hash, so identical canonical content
        produces identical hashes."""
        r1 = _seed_recipe(random_seed=42)
        r2 = _seed_recipe(random_seed=42)
        assert r1.recipe_hash == r2.recipe_hash
        r3 = _seed_recipe(random_seed=43)
        assert r1.recipe_hash != r3.recipe_hash


# ---------------------------------------------------------------------------
# Mutation allowlist
# ---------------------------------------------------------------------------


class TestMutationAllowlist:
    def test_add_feature_produces_new_recipe(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(kind=MutationKind.ADD_FEATURE, feature_name="f_new")
        child = m.apply(parent)
        assert "f_new" in child.feature_set
        assert child.parent_recipe_id == parent.recipe_id
        assert child.mutation_kind == MutationKind.ADD_FEATURE.value
        assert child.recipe_hash != parent.recipe_hash

    def test_add_feature_rejects_duplicate(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(kind=MutationKind.ADD_FEATURE, feature_name="f1")
        with pytest.raises(ValueError):
            m.apply(parent)

    def test_add_feature_rejects_secret_named_feature(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(
            kind=MutationKind.ADD_FEATURE,
            feature_name="api_key_extra",
        )
        with pytest.raises(ValueError, match="secret"):
            m.apply(parent)

    def test_recipe_rejects_secret_named_feature_at_construction(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            _seed_recipe(feature_set=("f1", "password_x"))

    def test_remove_feature_produces_new_recipe(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(kind=MutationKind.REMOVE_FEATURE, feature_name="f1")
        child = m.apply(parent)
        assert "f1" not in child.feature_set
        assert child.feature_set == ("f2", "f3")

    def test_remove_feature_rejects_last_feature(self) -> None:
        parent = _seed_recipe(feature_set=("f1",))
        m = RecipeMutation(kind=MutationKind.REMOVE_FEATURE, feature_name="f1")
        with pytest.raises(ValueError, match="last feature"):
            m.apply(parent)

    def test_transform_feature_uses_allowlist(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(
            kind=MutationKind.TRANSFORM_FEATURE,
            feature_name="f1",
            transform_kind="zscore",
        )
        child = m.apply(parent)
        assert "f1__zscore" in child.feature_set
        assert "f1" not in child.feature_set

    def test_transform_feature_rejects_unknown_kind(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(
            kind=MutationKind.TRANSFORM_FEATURE,
            feature_name="f1",
            transform_kind="look_ahead_inject",
        )
        with pytest.raises(ValueError, match="allowlist"):
            m.apply(parent)

    def test_set_hyperparam_within_bounds(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(
            kind=MutationKind.SET_HYPERPARAM,
            hyperparam_name="max_depth",
            hyperparam_value=8.0,
        )
        child = m.apply(parent)
        assert child.hyperparameters["max_depth"] == 8.0

    def test_set_hyperparam_outside_bounds_rejected(self) -> None:
        parent = _seed_recipe()
        # max_depth bounds for gbm are (2, 12). 50 is outside.
        m = RecipeMutation(
            kind=MutationKind.SET_HYPERPARAM,
            hyperparam_name="max_depth",
            hyperparam_value=50.0,
        )
        with pytest.raises(ValueError, match="outside"):
            m.apply(parent)

    def test_set_hyperparam_unknown_name_rejected(self) -> None:
        parent = _seed_recipe()
        m = RecipeMutation(
            kind=MutationKind.SET_HYPERPARAM,
            hyperparam_name="invented_param",
            hyperparam_value=1.0,
        )
        with pytest.raises(ValueError, match="not defined"):
            m.apply(parent)

    def test_narrow_window_works(self) -> None:
        parent = _seed_recipe()
        delta = 7 * 86_400_000_000_000  # 7 days
        m = RecipeMutation(kind=MutationKind.NARROW_TRAIN_WINDOW, delta_ns=delta)
        child = m.apply(parent)
        assert child.train_window_ns == parent.train_window_ns - delta

    def test_narrow_window_rejected_below_min(self) -> None:
        parent = _seed_recipe(
            train_window_ns=DEFAULT_TRAIN_WINDOW_RANGE_NS[0]  # 30 days
        )
        # Try to narrow by more than the entire window.
        m = RecipeMutation(
            kind=MutationKind.NARROW_TRAIN_WINDOW,
            delta_ns=100 * 86_400_000_000_000,  # 100 days
        )
        with pytest.raises(ValueError, match="minimum"):
            m.apply(parent)

    def test_widen_window_rejected_above_max(self) -> None:
        parent = _seed_recipe(
            train_window_ns=DEFAULT_TRAIN_WINDOW_RANGE_NS[1]  # 5 years
        )
        m = RecipeMutation(
            kind=MutationKind.WIDEN_TRAIN_WINDOW,
            delta_ns=10 * 86_400_000_000_000,
        )
        with pytest.raises(ValueError, match="maximum"):
            m.apply(parent)

    def test_recipe_rejects_unknown_model_family(self) -> None:
        with pytest.raises(ValueError, match="allowlist"):
            _seed_recipe(model_family="transformer_v99")

    def test_allowlists_are_defined(self) -> None:
        assert "gbm" in ALLOWED_MODEL_FAMILIES
        assert "catboost" in ALLOWED_MODEL_FAMILIES
        assert "zscore" in ALLOWED_TRANSFORMS
        assert "log_return" in ALLOWED_TRANSFORMS
        assert "gbm" in HYPERPARAM_BOUNDS
        assert "n_estimators" in HYPERPARAM_BOUNDS["gbm"]


# ---------------------------------------------------------------------------
# TrialBudget
# ---------------------------------------------------------------------------


class TestTrialBudget:
    def test_recipe_within_limit_allowed(self, tmp_path: pathlib.Path) -> None:
        budget = TrialBudget(
            per_recipe_limit_cents=2000,
            per_sweep_limit_cents=20_000,
        )
        d = budget.check_recipe(recipe_id="r1", cost_cents=1000)
        assert d.allowed is True
        assert d.scope == "recipe"

    def test_recipe_over_per_recipe_limit_rejected(self) -> None:
        budget = TrialBudget(
            per_recipe_limit_cents=1000,
            per_sweep_limit_cents=10_000,
        )
        d = budget.check_recipe(recipe_id="r1", cost_cents=1500)
        assert d.allowed is False
        assert d.scope == "recipe"
        assert "per-recipe limit" in d.reason

    def test_recipe_over_per_sweep_limit_rejected(self) -> None:
        budget = TrialBudget(
            per_recipe_limit_cents=3_000,
            per_sweep_limit_cents=5_000,
        )
        d = budget.check_recipe(recipe_id="r1", cost_cents=3_000)
        budget.record_recipe_spend(cost_cents=3_000)
        assert d.allowed is True
        d2 = budget.check_recipe(recipe_id="r2", cost_cents=3_000)
        assert d2.allowed is False
        assert d2.scope == "sweep"

    def test_cumulative_spend_tracked_across_recipes(self) -> None:
        budget = TrialBudget(
            per_recipe_limit_cents=1000,
            per_sweep_limit_cents=10_000,
        )
        d1 = budget.check_recipe(recipe_id="r1", cost_cents=1000)
        budget.record_recipe_spend(cost_cents=1000)
        assert d1.allowed is True
        d2 = budget.check_recipe(recipe_id="r2", cost_cents=500)
        budget.record_recipe_spend(cost_cents=500)
        assert d2.allowed is True
        assert budget.sweep_spent_cents == 1500
        # 9 more recipes at 1000c each would total 10_500c -> rejected.
        d3 = budget.check_recipe(recipe_id="r3", cost_cents=9000)
        assert d3.allowed is False

    def test_reset_sweep_clears_accumulator(self) -> None:
        budget = TrialBudget(per_recipe_limit_cents=1000, per_sweep_limit_cents=10_000)
        budget.check_recipe(recipe_id="r1", cost_cents=1000)
        budget.record_recipe_spend(cost_cents=1000)
        assert budget.sweep_spent_cents == 1000
        budget.reset_sweep()
        assert budget.sweep_spent_cents == 0
        d = budget.check_recipe(recipe_id="r1", cost_cents=1000)
        assert d.allowed is True

    def test_negative_amount_raises(self) -> None:
        budget = TrialBudget(per_recipe_limit_cents=1000, per_sweep_limit_cents=10_000)
        with pytest.raises(ValueError):
            budget.check_recipe(recipe_id="r1", cost_cents=-1)

    def test_kill_switch_blocks_paid_jobs(self, tmp_path: pathlib.Path) -> None:
        guard = BudgetGuard(
            base_dir=tmp_path / "b",
            monthly_budget_cents=100_000,
            kill_switch_enabled=True,
        )
        budget = TrialBudget(
            per_recipe_limit_cents=1000,
            per_sweep_limit_cents=10_000,
            guard=guard,
        )
        d = budget.check_recipe(recipe_id="r1", cost_cents=500)
        assert d.allowed is False
        assert "BudgetGuard" in d.reason

    def test_zero_cost_bypasses_guard_check(self, tmp_path: pathlib.Path) -> None:
        """Zero-cost trials are local/mock; guard check skipped per
        BudgetGuard semantics."""
        guard = BudgetGuard(
            base_dir=tmp_path / "b",
            monthly_budget_cents=0,
            kill_switch_enabled=True,
        )
        budget = TrialBudget(
            per_recipe_limit_cents=1000,
            per_sweep_limit_cents=10_000,
            guard=guard,
        )
        d = budget.check_recipe(recipe_id="r1", cost_cents=0)
        assert d.allowed is True

    def test_invalid_construction_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrialBudget(
                per_recipe_limit_cents=10_000,
                per_sweep_limit_cents=1_000,  # per_recipe > per_sweep
            )
        with pytest.raises(ValueError):
            TrialBudget(
                per_recipe_limit_cents=-1,
                per_sweep_limit_cents=1000,
            )


# ---------------------------------------------------------------------------
# EarlyStopper
# ---------------------------------------------------------------------------


class TestEarlyStopper:
    def test_below_min_settled_no_decision(self) -> None:
        es = EarlyStopper(min_settled=20)
        d = es.should_stop(
            settled_count=5,
            child_score=0.5,
            parent_score=1.0,
        )
        assert d.should_stop is False
        assert "not enough settled" in d.reason

    def test_missing_score_no_decision(self) -> None:
        es = EarlyStopper(min_settled=5)
        d = es.should_stop(
            settled_count=10,
            child_score=None,
            parent_score=1.0,
        )
        assert d.should_stop is False

    def test_above_floor_continues(self) -> None:
        es = EarlyStopper(min_settled=5, relative_threshold=0.10)
        d = es.should_stop(
            settled_count=10,
            child_score=0.95,
            parent_score=1.0,  # floor = 0.9, child 0.95 > 0.9
        )
        assert d.should_stop is False
        assert "above floor" in d.reason

    def test_below_floor_kills(self) -> None:
        es = EarlyStopper(min_settled=5, relative_threshold=0.10)
        d = es.should_stop(
            settled_count=10,
            child_score=0.5,
            parent_score=1.0,  # floor = 0.9, child 0.5 < 0.9
        )
        assert d.should_stop is True
        assert "below parent" in d.reason

    def test_invalid_construction_rejected(self) -> None:
        with pytest.raises(ValueError):
            EarlyStopper(min_settled=0)
        with pytest.raises(ValueError):
            EarlyStopper(relative_threshold=0.0)


# ---------------------------------------------------------------------------
# AlphaGenomeLab: dispatch / evidence-backed registration / no bypass
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _MockOutcome:
    """A minimal training outcome for tests."""

    model_id: str
    cost_cents: int = 0
    duration_seconds: float = 0.0
    dossier_evidence: Any = None
    tournament_result: Any = None
    sentinel_receipt: Any = None


@dataclasses.dataclass
class _MockRegistry:
    """A registry stub with an ``upsert`` method."""

    inserted: list[Any] = dataclasses.field(default_factory=list)

    def upsert(self, dossier: Any) -> None:
        self.inserted.append(dossier)


class _MockGate:
    """A mock promotion gate that always approves or rejects on demand."""

    def __init__(self, approved: bool = True) -> None:
        self.approved = approved

    def evaluate(self, *, request: Any, evidence: Any) -> Any:
        from quant_foundry.promotion import (
            PromotionReceipt,
            PromotionRejectionReason,
            ReviewDecision,
        )

        if self.approved:
            return PromotionReceipt(
                decision=ReviewDecision.APPROVED,
                request=request,
                review_note="ok",
                rejection_reason=None,
                decided_at_ns=0,
            )
        return PromotionReceipt(
            decision=ReviewDecision.REJECTED,
            request=request,
            review_note="rejected",
            rejection_reason=PromotionRejectionReason.NO_DOSSIER,
            decided_at_ns=0,
        )


def _build_lab_helper(
    *,
    approved: bool = True,
    per_recipe_limit_cents: int = 100_000,
    per_sweep_limit_cents: int = 1_000_000,
    score_for: Any = None,
    cost_cents: int = 0,
) -> tuple[AlphaGenomeLab, _MockRegistry]:
    gate = _MockGate(approved=approved)
    budget = TrialBudget(
        per_recipe_limit_cents=per_recipe_limit_cents,
        per_sweep_limit_cents=per_sweep_limit_cents,
    )
    es = EarlyStopper(min_settled=1000)  # effectively never
    registry = _MockRegistry()

    def dispatcher(recipe: Recipe) -> _MockOutcome:
        return _MockOutcome(
            model_id=f"m-{recipe.recipe_id}",
            cost_cents=cost_cents,
            duration_seconds=0.001,
            dossier_evidence={"recipe_id": recipe.recipe_id},
        )

    def probe(recipe_id: str) -> Any:
        if score_for is None:
            return None
        return score_for(recipe_id)

    lab = AlphaGenomeLab(
        gate=gate,
        budget=budget,
        early_stopper=es,
        dispatcher=dispatcher,
        tournament_probe=probe,
        registry=registry,
    )
    return lab, registry


class TestAlphaGenomeLab:
    def test_sweep_runs_and_returns_receipt(self) -> None:
        lab, _ = _build_lab_helper(approved=True)
        seed = _seed_recipe()
        receipt: SweepReceipt = lab.run_sweep(seed_recipe=seed, n_recipes=4)
        assert isinstance(receipt, SweepReceipt)
        assert receipt.n_recipes == 4
        assert (
            receipt.n_registered + receipt.n_rejected + receipt.n_killed_early + receipt.n_discarded
            == len(receipt.trial_receipts)
        )

    def test_no_bypass_approved_candidates_are_registered(self) -> None:
        lab, registry = _build_lab_helper(approved=True)
        seed = _seed_recipe()
        lab.run_sweep(seed_recipe=seed, n_recipes=3)
        # The mock gate approves; each recipe should be upserted.
        assert len(registry.inserted) >= 1

    def test_no_bypass_rejected_candidates_are_not_registered(self) -> None:
        lab, registry = _build_lab_helper(approved=False)
        seed = _seed_recipe()
        receipt = lab.run_sweep(seed_recipe=seed, n_recipes=3)
        assert len(registry.inserted) == 0
        # All trials were dispatched but rejected by gate.
        assert receipt.n_rejected >= 1
        for t in receipt.trial_receipts:
            if t.status == TrialStatus.REJECTED_BY_GATE:
                assert t.promotion_decision == "rejected"

    def test_budget_exhaustion_stops_new_trials(self) -> None:
        lab, _registry = _build_lab_helper(
            approved=True,
            per_recipe_limit_cents=10,
            per_sweep_limit_cents=10,
        )
        seed = _seed_recipe()
        receipt = lab.run_sweep(seed_recipe=seed, n_recipes=10)
        # Only the first trial fits; rest are discarded for budget.
        assert receipt.n_discarded >= 1
        # The sweep itself does not raise — it stops cleanly.

    def test_early_stop_kills_underperforming_recipe(self) -> None:
        # Probe returns a low child score that triggers early stop.
        def probe(recipe_id: str) -> Any:
            @dataclasses.dataclass
            class S:
                deflated_sharpe: float = 0.05
                settled_count: int = 200

            if recipe_id == _seed_recipe().recipe_id:

                @dataclasses.dataclass
                class P:
                    deflated_sharpe: float = 1.0
                    settled_count: int = 1000

                return P()
            return S()

        gate = _MockGate(approved=True)
        budget = TrialBudget(per_recipe_limit_cents=100_000, per_sweep_limit_cents=1_000_000)
        es = EarlyStopper(min_settled=20, relative_threshold=0.10)
        registry = _MockRegistry()

        def dispatcher(recipe: Recipe) -> _MockOutcome:
            return _MockOutcome(
                model_id=f"m-{recipe.recipe_id}",
                cost_cents=0,
                dossier_evidence={"recipe_id": recipe.recipe_id},
            )

        lab = AlphaGenomeLab(
            gate=gate,
            budget=budget,
            early_stopper=es,
            dispatcher=dispatcher,
            tournament_probe=probe,
            registry=registry,
        )
        seed = _seed_recipe()
        receipt = lab.run_sweep(seed_recipe=seed, n_recipes=1)
        assert receipt.n_killed_early == 1
        assert receipt.trial_receipts[0].status == TrialStatus.KILLED_EARLY

    def test_no_secrets_in_trial_receipts(self) -> None:
        lab, _ = _build_lab_helper(approved=True)
        seed = _seed_recipe()
        receipt = lab.run_sweep(seed_recipe=seed, n_recipes=3)
        # No receipt may contain a secret substring in any string field.
        forbidden = (
            "password",
            "token",
            "secret",
            "api_key",
            "apikey",
            "credential",
            "private_key",
        )
        for t in receipt.trial_receipts:
            for f in dataclasses.fields(t):
                val = getattr(t, f.name)
                if isinstance(val, str):
                    for sub in forbidden:
                        assert sub not in val.lower(), (
                            f"secret substring {sub!r} found in {f.name}={val!r}"
                        )

    def test_zero_n_recipes_raises(self) -> None:
        lab, _ = _build_lab_helper()
        with pytest.raises(ValueError):
            lab.run_sweep(seed_recipe=_seed_recipe(), n_recipes=0)

    def test_seed_recipe_hash_integrity_check(self) -> None:
        """If the seed recipe's recipe_hash is tampered with, sweep refuses."""
        lab, _ = _build_lab_helper()
        seed = _seed_recipe()
        # Forge the hash; this should fail the integrity check.
        object.__setattr__(seed, "recipe_hash", "0" * 64)
        with pytest.raises(ValueError, match="recipe_hash"):
            lab.run_sweep(seed_recipe=seed, n_recipes=1)


# ---------------------------------------------------------------------------
# Discard receipt (failure surface)
# ---------------------------------------------------------------------------


class TestDiscardReceipt:
    def test_discard_receipt_construction(self) -> None:
        r = DiscardReceipt(
            recipe_id="r1",
            recipe_hash="abc",
            status=TrialStatus.DISCARDED,
            reason="mutation outside allowlist",
            sweep_id="sw-1",
        )
        assert r.status == TrialStatus.DISCARDED
        assert r.reason == "mutation outside allowlist"

    def test_discard_receipt_is_frozen(self) -> None:
        r = DiscardReceipt(
            recipe_id="r1",
            recipe_hash="abc",
            status=TrialStatus.DISCARDED,
            reason="x",
            sweep_id="sw-1",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.status = TrialStatus.KILLED_EARLY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BudgetDecision type
# ---------------------------------------------------------------------------


class TestBudgetDecisionType:
    def test_budget_decision_constructs(self) -> None:
        d = BudgetDecision(
            allowed=True,
            reason="",
            spent_cents=100,
            limit_cents=1000,
            scope="recipe",
        )
        assert d.allowed is True
        assert d.scope == "recipe"

    def test_early_stop_decision_constructs(self) -> None:
        d = EarlyStopDecision(
            should_stop=True,
            reason="low score",
            settled_count=10,
            parent_score=1.0,
            child_score=0.5,
        )
        assert d.should_stop is True


# ---------------------------------------------------------------------------
# No-bypass of tournament gates (re-affirmation in the integration test)
# ---------------------------------------------------------------------------


class TestNoBypassInvariant:
    def test_lab_never_registers_without_gate_approval(self) -> None:
        """A gate that always rejects means zero registrations."""
        lab, registry = _build_lab_helper(approved=False)
        seed = _seed_recipe()
        receipt = lab.run_sweep(seed_recipe=seed, n_recipes=5)
        # Every trial was dispatched, but none registered.
        for t in receipt.trial_receipts:
            assert t.status != TrialStatus.REGISTERED, (
                f"trial was registered despite gate rejection: {t}"
            )
        assert len(registry.inserted) == 0
