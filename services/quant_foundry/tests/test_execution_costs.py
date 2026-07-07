"""Tests for ``quant_foundry.execution_costs`` (Tier 2.5).

Tests verify:
- Cost model is frozen + extra='forbid'.
- apply_training_costs: round-trip cost on position changes, borrow
  costs for shorts, no cost when position is flat.
- compute_cost_aware_metrics: gross vs net Sharpe, drawdown, win rate.
- Net Sharpe is always <= gross Sharpe (costs can only hurt).
- Turnover and total_cost_bps are computed correctly.
- Default cost model matches settlement default (5/3/0 bps).
- Edge cases: empty returns, mismatched lengths, single sample.
"""

from __future__ import annotations

import math

import pytest

from quant_foundry.execution_costs import (
    DEFAULT_TRAINING_COST_MODEL,
    CostAwareMetrics,
    TrainingCostModel,
    apply_training_costs,
    compute_cost_aware_metrics,
)


class TestTrainingCostModel:
    def test_default_matches_settlement(self) -> None:
        """Default training cost model matches settlement default."""
        assert DEFAULT_TRAINING_COST_MODEL.fee_bps == 5.0
        assert DEFAULT_TRAINING_COST_MODEL.spread_bps == 3.0
        assert DEFAULT_TRAINING_COST_MODEL.slippage_bps == 0.0

    def test_round_trip_bps(self) -> None:
        """Round-trip bps is fee + spread + slippage."""
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=2)
        assert model.round_trip_bps == 17.0

    def test_round_trip_fraction(self) -> None:
        """Round-trip fraction is bps / 10000."""
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        assert model.round_trip_fraction == pytest.approx(20.0 / 10_000.0)

    def test_frozen(self) -> None:
        """Cost model is immutable."""
        model = TrainingCostModel()
        with pytest.raises(Exception):
            model.fee_bps = 100  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        """Unknown fields are rejected."""
        with pytest.raises(Exception):
            TrainingCostModel(unknown_field=1)  # type: ignore[call-arg]

    def test_negative_bps_rejected(self) -> None:
        """Negative cost figures are rejected."""
        with pytest.raises(Exception):
            TrainingCostModel(fee_bps=-1.0)

    def test_empty_version_rejected(self) -> None:
        """Empty version string is rejected."""
        with pytest.raises(Exception):
            TrainingCostModel(version="")


class TestApplyTrainingCosts:
    def test_no_cost_when_flat(self) -> None:
        """No cost when position is 0 (flat)."""
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        net = apply_training_costs(
            [0.001, 0.002, 0.001],
            [0.0, 0.0, 0.0],
            model,
        )
        assert net == pytest.approx([0.001, 0.002, 0.001])

    def test_round_trip_cost_on_entry(self) -> None:
        """Half round-trip cost applied on position entry from flat."""
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        # round_trip = 20 bps = 0.002; entry from flat = half = 10 bps = 0.001
        net = apply_training_costs(
            [0.01, 0.01],
            [1.0, 1.0],  # enter long on bar 0, hold
            model,
        )
        # Bar 0: entry cost = half round-trip = 10 bps = 0.001
        assert net[0] == pytest.approx(0.01 - 0.001)
        # Bar 1: no change, no cost
        assert net[1] == pytest.approx(0.01)

    def test_round_trip_cost_on_exit(self) -> None:
        """Half round-trip cost applied on position exit to flat."""
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        net = apply_training_costs(
            [0.01, 0.01, 0.01],
            [1.0, 1.0, 0.0],  # enter, hold, exit
            model,
        )
        # Bar 0: entry cost = half round-trip = 10 bps
        assert net[0] == pytest.approx(0.01 - 0.001)
        # Bar 1: no change
        assert net[1] == pytest.approx(0.01)
        # Bar 2: exit cost = half round-trip = 10 bps
        assert net[2] == pytest.approx(0.01 - 0.001)

    def test_cost_on_position_flip(self) -> None:
        """Full round-trip cost on position flip (long to short)."""
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        net = apply_training_costs(
            [0.01, 0.01],
            [1.0, -1.0],  # long to short = full change of 2
            model,
        )
        # Bar 0: entry cost (change = 1, cost = 20bps * 0.5 = 10bps)
        assert net[0] == pytest.approx(0.01 - 0.001)
        # Bar 1: flip cost (change = 2, cost = 20bps * 1.0 = 20bps)
        assert net[1] == pytest.approx(0.01 - 0.002)

    def test_borrow_cost_for_shorts(self) -> None:
        """Borrow cost applied to short positions."""
        model = TrainingCostModel(
            fee_bps=0, spread_bps=0, slippage_bps=0,
            borrow_bps_per_day=10.0,
        )
        net = apply_training_costs(
            [0.01, 0.01],
            [-1.0, -1.0],
            model,
            holding_days=1,
        )
        # Bar 0: entry cost = 0 (no fee/spread/slippage), borrow = 10bps * 1 = 0.001
        assert net[0] == pytest.approx(0.01 - 0.001)
        # Bar 1: no position change, borrow = 10bps * 1 = 0.001
        assert net[1] == pytest.approx(0.01 - 0.001)

    def test_no_borrow_for_longs(self) -> None:
        """Borrow cost not applied to long positions."""
        model = TrainingCostModel(
            fee_bps=0, spread_bps=0, slippage_bps=0,
            borrow_bps_per_day=10.0,
        )
        net = apply_training_costs(
            [0.01, 0.01],
            [1.0, 1.0],
            model,
            holding_days=1,
        )
        # No entry cost, no borrow for longs
        assert net[0] == pytest.approx(0.01)
        assert net[1] == pytest.approx(0.01)

    def test_length_mismatch_raises(self) -> None:
        """Length mismatch raises ValueError."""
        model = TrainingCostModel()
        with pytest.raises(ValueError, match="length mismatch"):
            apply_training_costs([0.01, 0.02], [1.0], model)

    def test_empty_returns_raises(self) -> None:
        """Empty returns raises ValueError."""
        model = TrainingCostModel()
        with pytest.raises(ValueError, match="non-empty"):
            apply_training_costs([], [], model)

    def test_costs_symmetric(self) -> None:
        """Costs apply to winning and losing trades symmetrically."""
        model = TrainingCostModel(fee_bps=10, spread_bps=0, slippage_bps=0)
        # Winner and loser with same position change
        # Entry from flat = half round-trip = 5 bps = 0.0005
        net_win = apply_training_costs([0.01], [1.0], model)
        net_lose = apply_training_costs([-0.01], [1.0], model)
        # Both pay the same cost (5 bps = 0.0005)
        assert net_win[0] == pytest.approx(0.01 - 0.0005)
        assert net_lose[0] == pytest.approx(-0.01 - 0.0005)


class TestComputeCostAwareMetrics:
    def test_net_sharpe_le_gross_sharpe(self) -> None:
        """Net Sharpe is always <= gross Sharpe (costs can only hurt)."""
        gross = [0.001, -0.002, 0.003, 0.001, -0.001, 0.002, 0.001, -0.001]
        positions = [1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, -1.0]
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=math.sqrt(252),
        )
        assert metrics.sharpe_net <= metrics.sharpe_gross

    def test_gross_and_net_computed(self) -> None:
        """Both gross and net metrics are computed."""
        gross = [0.001, 0.002, -0.001, 0.003, 0.001]
        positions = [1.0, 1.0, 1.0, 1.0, 1.0]
        model = TrainingCostModel(fee_bps=5, spread_bps=3, slippage_bps=0)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        assert metrics.sharpe_gross != 0.0
        assert metrics.sharpe_net != 0.0
        assert metrics.sharpe_net < metrics.sharpe_gross

    def test_turnover_computed(self) -> None:
        """Turnover is fraction of periods with position changes."""
        # 1 entry + 1 exit in 5 periods = 2/5 = 0.4
        gross = [0.001, 0.002, 0.001, 0.001, 0.001]
        positions = [1.0, 1.0, 1.0, 1.0, 0.0]
        model = TrainingCostModel(fee_bps=5, spread_bps=3, slippage_bps=0)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        assert metrics.turnover == pytest.approx(2.0 / 5.0)

    def test_total_cost_bps(self) -> None:
        """Total cost bps = round_trip_bps * turnover."""
        gross = [0.001, 0.002, 0.001, 0.001, 0.001]
        positions = [1.0, 1.0, 1.0, 1.0, 0.0]
        model = TrainingCostModel(fee_bps=5, spread_bps=3, slippage_bps=0)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        # round_trip = 8 bps, turnover = 2/5 = 0.4
        assert metrics.total_cost_bps == pytest.approx(8.0 * 0.4)

    def test_cost_model_version_recorded(self) -> None:
        """Cost model version is recorded in the metrics."""
        gross = [0.001, 0.002]
        positions = [1.0, 1.0]
        model = TrainingCostModel(version="v2.test")
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        assert metrics.cost_model_version == "v2.test"

    def test_max_drawdown_net_worse(self) -> None:
        """Net max drawdown is worse (more negative) than gross."""
        gross = [0.001, -0.005, 0.001, 0.001]
        positions = [1.0, 1.0, 1.0, 1.0]
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        assert metrics.max_drawdown_net <= metrics.max_drawdown_gross

    def test_win_rate_net_le_gross(self) -> None:
        """Net win rate is <= gross win rate (costs make losers of marginal winners)."""
        # Marginal winners that become losers after costs
        gross = [0.001, 0.0001, 0.001, 0.0001]
        positions = [1.0, 1.0, 1.0, 1.0]
        model = TrainingCostModel(fee_bps=10, spread_bps=5, slippage_bps=5)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        assert metrics.win_rate_net <= metrics.win_rate_gross

    def test_mean_return_net_lower(self) -> None:
        """Net mean return is lower than gross."""
        gross = [0.001, 0.002, 0.001, 0.001]
        positions = [1.0, 1.0, 1.0, 1.0]
        model = TrainingCostModel(fee_bps=5, spread_bps=3, slippage_bps=0)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        assert metrics.mean_return_net < metrics.mean_return_gross

    def test_result_is_frozen(self) -> None:
        """CostAwareMetrics is immutable."""
        gross = [0.001, 0.002]
        positions = [1.0, 1.0]
        model = TrainingCostModel()
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        with pytest.raises(Exception):
            metrics.sharpe_net = 999  # type: ignore[misc]

    def test_no_cost_model(self) -> None:
        """Zero-cost model produces net == gross."""
        model = TrainingCostModel(
            fee_bps=0, spread_bps=0, slippage_bps=0, borrow_bps_per_day=0,
        )
        gross = [0.001, -0.002, 0.003, 0.001]
        positions = [1.0, -1.0, 1.0, -1.0]
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=1.0,
        )
        assert metrics.sharpe_net == pytest.approx(metrics.sharpe_gross)
        assert metrics.max_drawdown_net == pytest.approx(metrics.max_drawdown_gross)
        assert metrics.win_rate_net == pytest.approx(metrics.win_rate_gross)

    def test_length_mismatch_raises(self) -> None:
        """Length mismatch raises ValueError."""
        model = TrainingCostModel()
        with pytest.raises(ValueError):
            compute_cost_aware_metrics(
                [0.001, 0.002], [1.0], model, ann_factor=1.0,
            )

    def test_empty_returns_raises(self) -> None:
        """Empty returns raises ValueError."""
        model = TrainingCostModel()
        with pytest.raises(ValueError):
            compute_cost_aware_metrics([], [], model, ann_factor=1.0)

    def test_sharpe_769_fix(self) -> None:
        """The Sharpe-769 artifact: frictionless Sharpe is implausibly
        high, but net Sharpe is much lower after costs.

        This is the core Tier 2.5 fix: a frictionless Sharpe of 769
        should be reduced to a realistic level after applying costs.
        """
        # Simulate the A7 canary: high-frequency, tiny consistent wins
        # with very low variance → frictionless Sharpe explodes.
        # 1000 bars with tiny positive return + minimal noise.
        import random
        rng = random.Random(42)
        gross = [rng.gauss(0.0001, 0.00001) for _ in range(1000)]
        positions = [1.0] * 1000
        # With ann_factor = sqrt(525600) ~ 725 (1-minute bars)
        model = TrainingCostModel(fee_bps=5, spread_bps=3, slippage_bps=0)
        metrics = compute_cost_aware_metrics(
            gross, positions, model, ann_factor=math.sqrt(525_600),
        )
        # Gross Sharpe will be extremely high (low variance + high ann)
        # Net Sharpe should be lower after the entry cost
        assert metrics.sharpe_gross > 100  # frictionless is implausible
        assert metrics.sharpe_net < metrics.sharpe_gross
        # The net Sharpe is reduced by the entry cost
        # (with 1 turn in 1000 bars, the reduction is modest but real)
        assert metrics.sharpe_net < metrics.sharpe_gross * 0.9
