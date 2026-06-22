"""
TDD tests for quant_foundry.budget (TASK-0901: budget guard before heavy jobs).

The budget guard enforces a hard monthly spending ceiling. GPU spend must
fail closed, exactly like the JWT runtime guard — a job that would exceed the
ceiling is rejected before it starts, not after it has already incurred cost.

Acceptance criteria from NEXT_STEPS_PLAN:
- Add budget guard before heavy jobs.
- (Module list + stop all optional modules are already implemented by
  TASK-0203; this task adds the budget guard + the runtime plan document.)

Design:
- ``BudgetGuard`` tracks cumulative monthly spend in a JSONL file (durable
  across restarts).
- ``check_and_reserve(amount_cents, job_type)`` returns a ``BudgetDecision``
  with ``allowed=True/False``. If allowed, the amount is reserved (added to
  the monthly total). If not allowed, the job is rejected before it starts.
- ``record_spend(amount_cents, job_type)`` records actual spend (e.g. after a
  RunPod job completes and the real cost is known). This can adjust a prior
  reservation up or down.
- The monthly ceiling is configurable via env (``QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS``).
- A global kill switch (``QUANT_FOUNDRY_BUDGET_KILL_SWITCH``) blocks ALL jobs
  regardless of remaining budget — for manual emergency stops.

File-disjoint from all active builders. New module, no imports of settlement /
dossier / tournament / gateway / outbox / inbox.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from quant_foundry.budget import BudgetDecision, BudgetGuard

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def budget_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Clean temp directory for the budget ledger."""
    d = tmp_path / "budget"
    d.mkdir()
    return d


@pytest.fixture
def guard(budget_dir: pathlib.Path) -> BudgetGuard:
    """BudgetGuard with a 1000-cent ($10) monthly ceiling."""
    return BudgetGuard(
        base_dir=budget_dir,
        monthly_budget_cents=1000,
        kill_switch_enabled=False,
    )


def _current_year_month() -> str:
    """Return the current YYYY-MM string."""
    t = time.gmtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


# --------------------------------------------------------------------------- #
# Basic guard behavior                                                         #
# --------------------------------------------------------------------------- #


class TestBudgetGuardBasic:
    def test_job_within_budget_is_allowed(self, guard: BudgetGuard):
        decision = guard.check_and_reserve(amount_cents=500, job_type="training")
        assert isinstance(decision, BudgetDecision)
        assert decision.allowed is True
        assert decision.remaining_cents == 500
        assert decision.monthly_budget_cents == 1000
        assert decision.spent_cents == 500

    def test_job_exceeding_budget_is_rejected(self, guard: BudgetGuard):
        decision = guard.check_and_reserve(amount_cents=1500, job_type="training")
        assert decision.allowed is False
        assert decision.remaining_cents == 1000
        assert decision.spent_cents == 0
        assert "exceeds monthly budget" in decision.reason

    def test_cumulative_spend_is_tracked(self, guard: BudgetGuard):
        d1 = guard.check_and_reserve(amount_cents=300, job_type="training")
        assert d1.allowed is True
        d2 = guard.check_and_reserve(amount_cents=400, job_type="inference")
        assert d2.allowed is True
        assert d2.spent_cents == 700
        assert d2.remaining_cents == 300

    def test_third_job_that_exceeds_is_rejected(self, guard: BudgetGuard):
        guard.check_and_reserve(amount_cents=400, job_type="training")
        guard.check_and_reserve(amount_cents=400, job_type="training")
        d3 = guard.check_and_reserve(amount_cents=300, job_type="training")
        assert d3.allowed is False
        assert d3.spent_cents == 800
        assert d3.remaining_cents == 200
        assert "exceeds monthly budget" in d3.reason

    def test_zero_amount_is_allowed(self, guard: BudgetGuard):
        decision = guard.check_and_reserve(amount_cents=0, job_type="mock")
        assert decision.allowed is True
        assert decision.spent_cents == 0

    def test_negative_amount_raises(self, guard: BudgetGuard):
        with pytest.raises(ValueError):
            guard.check_and_reserve(amount_cents=-1, job_type="training")


# --------------------------------------------------------------------------- #
# Kill switch                                                                  #
# --------------------------------------------------------------------------- #


class TestBudgetKillSwitch:
    def test_kill_switch_blocks_all_jobs(self, budget_dir: pathlib.Path):
        guard = BudgetGuard(
            base_dir=budget_dir,
            monthly_budget_cents=10_000,
            kill_switch_enabled=True,
        )
        decision = guard.check_and_reserve(amount_cents=100, job_type="training")
        assert decision.allowed is False
        assert "kill switch is active" in decision.reason

    def test_kill_switch_can_be_toggled(self, guard: BudgetGuard):
        guard.set_kill_switch(True)
        d1 = guard.check_and_reserve(amount_cents=100, job_type="training")
        assert d1.allowed is False
        guard.set_kill_switch(False)
        d2 = guard.check_and_reserve(amount_cents=100, job_type="training")
        assert d2.allowed is True


# --------------------------------------------------------------------------- #
# Durability (restart-safe)                                                    #
# --------------------------------------------------------------------------- #


class TestBudgetDurability:
    def test_spend_survives_restart(self, budget_dir: pathlib.Path):
        g1 = BudgetGuard(base_dir=budget_dir, monthly_budget_cents=1000)
        g1.check_and_reserve(amount_cents=600, job_type="training")
        # Simulate a restart by creating a new guard pointing at the same dir.
        g2 = BudgetGuard(base_dir=budget_dir, monthly_budget_cents=1000)
        decision = g2.check_and_reserve(amount_cents=500, job_type="training")
        assert decision.allowed is False
        assert decision.spent_cents == 600
        assert "exceeds monthly budget" in decision.reason

    def test_spend_resets_across_months(self, budget_dir: pathlib.Path):
        """Spend from a previous month does NOT count against the current month."""
        g = BudgetGuard(base_dir=budget_dir, monthly_budget_cents=1000)
        # Record spend in a previous month.
        g.record_spend(amount_cents=800, job_type="training", year_month="2025-01")
        # Current month should still have the full budget.
        decision = g.check_and_reserve(amount_cents=900, job_type="training")
        assert decision.allowed is True
        assert decision.spent_cents == 900


# --------------------------------------------------------------------------- #
# Record spend (actual cost adjustment)                                        #
# --------------------------------------------------------------------------- #


class TestRecordSpend:
    def test_record_spend_increases_total(self, guard: BudgetGuard):
        guard.check_and_reserve(amount_cents=200, job_type="training")
        guard.record_spend(amount_cents=100, job_type="training")
        spend = guard.get_monthly_spend()
        assert spend == 300

    def test_record_spend_for_different_job_type(self, guard: BudgetGuard):
        guard.check_and_reserve(amount_cents=200, job_type="training")
        guard.record_spend(amount_cents=50, job_type="inference")
        spend = guard.get_monthly_spend()
        assert spend == 250

    def test_record_spend_for_specific_month(self, guard: BudgetGuard):
        guard.record_spend(amount_cents=500, job_type="training", year_month="2025-06")
        assert guard.get_monthly_spend(year_month="2025-06") == 500
        assert guard.get_monthly_spend() == 0  # current month unaffected


# --------------------------------------------------------------------------- #
# Read API                                                                     #
# --------------------------------------------------------------------------- #


class TestBudgetReadApi:
    def test_get_monthly_spend_returns_zero_initially(self, guard: BudgetGuard):
        assert guard.get_monthly_spend() == 0

    def test_get_monthly_spend_after_reservation(self, guard: BudgetGuard):
        guard.check_and_reserve(amount_cents=300, job_type="training")
        assert guard.get_monthly_spend() == 300

    def test_get_budget_summary(self, guard: BudgetGuard):
        guard.check_and_reserve(amount_cents=400, job_type="training")
        summary = guard.get_summary()
        assert summary["monthly_budget_cents"] == 1000
        assert summary["spent_cents"] == 400
        assert summary["remaining_cents"] == 600
        assert summary["kill_switch_enabled"] is False
        assert summary["year_month"] == _current_year_month()


# --------------------------------------------------------------------------- #
# Edge cases                                                                   #
# --------------------------------------------------------------------------- #


class TestBudgetEdgeCases:
    def test_exact_budget_is_allowed(self, guard: BudgetGuard):
        """A job that exactly exhausts the budget is allowed (not rejected)."""
        decision = guard.check_and_reserve(amount_cents=1000, job_type="training")
        assert decision.allowed is True
        assert decision.remaining_cents == 0

    def test_one_cent_over_budget_is_rejected(self, guard: BudgetGuard):
        decision = guard.check_and_reserve(amount_cents=1001, job_type="training")
        assert decision.allowed is False

    def test_zero_budget_blocks_all_paid_jobs(self, budget_dir: pathlib.Path):
        guard = BudgetGuard(
            base_dir=budget_dir,
            monthly_budget_cents=0,
            kill_switch_enabled=False,
        )
        decision = guard.check_and_reserve(amount_cents=1, job_type="training")
        assert decision.allowed is False
        # But zero-cost jobs are still allowed.
        d0 = guard.check_and_reserve(amount_cents=0, job_type="mock")
        assert d0.allowed is True

    def test_job_type_recorded_in_decision(self, guard: BudgetGuard):
        decision = guard.check_and_reserve(amount_cents=100, job_type="inference")
        assert decision.job_type == "inference"
