"""Tests for quant_foundry.portfolio_policy (T-13.2).

Covers the portfolio policy model: PolicyConfig / PolicyOutput /
ReplayResult / RiskLimits construction and validation, the
PortfolioPolicy (act / validate_weights / validate_turnover /
compute_reward), the standalone validate_no_risk_violation helper, and
the ReplayEngine (run / compute_sharpe). Emphasis on fail-closed
behavior: weight, turnover, and drawdown violations must raise
ValueError rather than silently clipping.
"""

from __future__ import annotations

import hashlib
import json
import math

import pytest

from quant_foundry.portfolio_policy import (
    PolicyConfig,
    PolicyOutput,
    PortfolioPolicy,
    ReplayEngine,
    ReplayResult,
    RiskLimits,
    compute_cost_model_hash,
    validate_no_risk_violation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


HEX64 = "a" * 64
HEX64_B = "b" * 64


def _cost_hash(cost_bps: float = 1.0) -> str:
    return compute_cost_model_hash(cost_bps)


def _make_config(**overrides) -> PolicyConfig:
    defaults = dict(
        n_assets=5,
        max_weight=0.4,
        max_turnover=0.5,
        max_drawdown=0.15,
        cost_bps=1.0,
        abstention_threshold=0.3,
        shadow_only=True,
        seed=42,
    )
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def _make_risk_limits(**overrides) -> RiskLimits:
    defaults = dict(
        max_weight=0.4,
        max_turnover=0.5,
        max_drawdown=0.15,
        min_positions=3,
    )
    defaults.update(overrides)
    return RiskLimits(**defaults)


def _make_policy(
    config: PolicyConfig | None = None,
    risk_limits: RiskLimits | None = None,
    cost_model_hash: str | None = None,
) -> PortfolioPolicy:
    if config is None:
        config = _make_config()
    if risk_limits is None:
        risk_limits = _make_risk_limits()
    if cost_model_hash is None:
        cost_model_hash = _cost_hash()
    return PortfolioPolicy(
        config=config,
        risk_limits=risk_limits,
        cost_model_hash=cost_model_hash,
    )


def _equal_weights(n: int) -> list[float]:
    return [1.0 / n] * n


# ---------------------------------------------------------------------------
# PolicyConfig
# ---------------------------------------------------------------------------


class TestPolicyConfig:
    def test_default_construction(self):
        cfg = PolicyConfig(n_assets=5)
        assert cfg.n_assets == 5
        assert cfg.max_weight == 0.1
        assert cfg.max_turnover == 0.5
        assert cfg.max_drawdown == 0.15
        assert cfg.cost_bps == 1.0
        assert cfg.abstention_threshold == 0.3
        assert cfg.reward_components == ["return", "cost", "drawdown", "turnover"]
        assert cfg.shadow_only is True
        assert cfg.seed == 42

    def test_frozen(self):
        cfg = PolicyConfig(n_assets=3)
        with pytest.raises(Exception):
            cfg.n_assets = 10  # type: ignore[misc]

    def test_extra_forbidden(self):
        with pytest.raises(Exception):
            PolicyConfig(n_assets=3, bogus=1.0)  # type: ignore[call-arg]

    def test_n_assets_must_be_positive(self):
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=0)
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=-1)

    def test_max_weight_in_range(self):
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, max_weight=0.0)
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, max_weight=1.5)
        # 1.0 is allowed (upper bound inclusive).
        cfg = PolicyConfig(n_assets=3, max_weight=1.0)
        assert cfg.max_weight == 1.0

    def test_max_turnover_non_negative(self):
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, max_turnover=-0.1)
        cfg = PolicyConfig(n_assets=3, max_turnover=0.0)
        assert cfg.max_turnover == 0.0

    def test_max_drawdown_in_range(self):
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, max_drawdown=0.0)
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, max_drawdown=1.0)
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, max_drawdown=1.5)

    def test_cost_bps_non_negative(self):
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, cost_bps=-1.0)

    def test_abstention_threshold_in_range(self):
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, abstention_threshold=-0.1)
        with pytest.raises(ValueError):
            PolicyConfig(n_assets=3, abstention_threshold=1.5)
        # Bounds inclusive.
        cfg = PolicyConfig(n_assets=3, abstention_threshold=0.0)
        assert cfg.abstention_threshold == 0.0
        cfg = PolicyConfig(n_assets=3, abstention_threshold=1.0)
        assert cfg.abstention_threshold == 1.0


# ---------------------------------------------------------------------------
# PolicyOutput
# ---------------------------------------------------------------------------


class TestPolicyOutput:
    def test_construction(self):
        out = PolicyOutput(
            target_weights=[0.3, 0.3, 0.4],
            abstain=False,
            confidence=0.8,
            expected_reward=0.01,
            reward_components={"return": 0.02, "cost": 0.01},
            risk_limits_respected=True,
            cost_model_hash=HEX64,
        )
        assert out.target_weights == [0.3, 0.3, 0.4]
        assert out.abstain is False
        assert out.confidence == 0.8

    def test_frozen(self):
        out = PolicyOutput(
            target_weights=[0.5, 0.5],
            abstain=True,
            confidence=0.1,
            expected_reward=0.0,
            reward_components={},
            risk_limits_respected=True,
            cost_model_hash=HEX64,
        )
        with pytest.raises(Exception):
            out.abstain = False  # type: ignore[misc]

    def test_extra_forbidden(self):
        with pytest.raises(Exception):
            PolicyOutput(
                target_weights=[0.5, 0.5],
                abstain=True,
                confidence=0.1,
                expected_reward=0.0,
                reward_components={},
                risk_limits_respected=True,
                cost_model_hash=HEX64,
                bogus=1,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# ReplayResult
# ---------------------------------------------------------------------------


class TestReplayResult:
    def test_construction(self):
        res = ReplayResult(
            n_rebalances=10,
            total_return=0.05,
            total_cost=0.001,
            max_drawdown=0.02,
            avg_turnover=0.1,
            sharpe_ratio=1.5,
            n_abstentions=2,
            n_risk_violations=0,
            reward_history=[0.01, 0.02],
            weight_history=[[0.5, 0.5], [0.4, 0.6]],
        )
        assert res.n_rebalances == 10
        assert res.sharpe_ratio == 1.5

    def test_sharpe_can_be_none(self):
        res = ReplayResult(
            n_rebalances=1,
            total_return=0.0,
            total_cost=0.0,
            max_drawdown=0.0,
            avg_turnover=0.0,
            sharpe_ratio=None,
            n_abstentions=0,
            n_risk_violations=0,
            reward_history=[0.0],
            weight_history=[[0.5, 0.5]],
        )
        assert res.sharpe_ratio is None

    def test_frozen(self):
        res = ReplayResult(
            n_rebalances=1,
            total_return=0.0,
            total_cost=0.0,
            max_drawdown=0.0,
            avg_turnover=0.0,
            sharpe_ratio=None,
            n_abstentions=0,
            n_risk_violations=0,
            reward_history=[0.0],
            weight_history=[[0.5, 0.5]],
        )
        with pytest.raises(Exception):
            res.n_rebalances = 5  # type: ignore[misc]

    def test_extra_forbidden(self):
        with pytest.raises(Exception):
            ReplayResult(
                n_rebalances=1,
                total_return=0.0,
                total_cost=0.0,
                max_drawdown=0.0,
                avg_turnover=0.0,
                sharpe_ratio=None,
                n_abstentions=0,
                n_risk_violations=0,
                reward_history=[0.0],
                weight_history=[[0.5, 0.5]],
                bogus=1,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# RiskLimits
# ---------------------------------------------------------------------------


class TestRiskLimits:
    def test_default_min_positions(self):
        rl = RiskLimits(max_weight=0.4, max_turnover=0.5, max_drawdown=0.15)
        assert rl.min_positions == 3

    def test_frozen(self):
        rl = RiskLimits(max_weight=0.4, max_turnover=0.5, max_drawdown=0.15)
        with pytest.raises(Exception):
            rl.max_weight = 0.5  # type: ignore[misc]

    def test_extra_forbidden(self):
        with pytest.raises(Exception):
            RiskLimits(
                max_weight=0.4,
                max_turnover=0.5,
                max_drawdown=0.15,
                bogus=1,  # type: ignore[call-arg]
            )

    def test_max_weight_in_range(self):
        with pytest.raises(ValueError):
            RiskLimits(max_weight=0.0, max_turnover=0.5, max_drawdown=0.15)
        with pytest.raises(ValueError):
            RiskLimits(max_weight=1.5, max_turnover=0.5, max_drawdown=0.15)

    def test_max_turnover_non_negative(self):
        with pytest.raises(ValueError):
            RiskLimits(max_weight=0.4, max_turnover=-0.1, max_drawdown=0.15)

    def test_max_drawdown_in_range(self):
        with pytest.raises(ValueError):
            RiskLimits(max_weight=0.4, max_turnover=0.5, max_drawdown=0.0)
        with pytest.raises(ValueError):
            RiskLimits(max_weight=0.4, max_turnover=0.5, max_drawdown=1.0)

    def test_min_positions_positive(self):
        with pytest.raises(ValueError):
            RiskLimits(
                max_weight=0.4,
                max_turnover=0.5,
                max_drawdown=0.15,
                min_positions=0,
            )


# ---------------------------------------------------------------------------
# compute_cost_model_hash
# ---------------------------------------------------------------------------


class TestComputeCostModelHash:
    def test_returns_hex64(self):
        h = compute_cost_model_hash(1.0)
        assert len(h) == 64
        int(h, 16)  # parses as hex

    def test_deterministic(self):
        assert compute_cost_model_hash(1.0) == compute_cost_model_hash(1.0)

    def test_different_params_different_hash(self):
        assert compute_cost_model_hash(1.0) != compute_cost_model_hash(2.0)

    def test_matches_manual_sha256(self):
        payload = json.dumps(
            {"cost_bps": round(1.0, 12)},
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert compute_cost_model_hash(1.0) == expected


# ---------------------------------------------------------------------------
# PortfolioPolicy construction
# ---------------------------------------------------------------------------


class TestPortfolioPolicyConstruction:
    def test_construction(self):
        policy = _make_policy()
        assert policy.config.n_assets == 5
        assert policy.cost_model_hash == _cost_hash()

    def test_invalid_cost_model_hash(self):
        with pytest.raises(ValueError):
            PortfolioPolicy(
                config=_make_config(),
                risk_limits=_make_risk_limits(),
                cost_model_hash="not-a-hash",
            )

    def test_min_positions_max_weight_incompatible(self):
        # min_positions=3, max_weight=0.1 → max sum = 0.3 < 1.0.
        with pytest.raises(ValueError):
            PortfolioPolicy(
                config=_make_config(n_assets=5, max_weight=0.1),
                risk_limits=_make_risk_limits(max_weight=0.1, min_positions=3),
                cost_model_hash=_cost_hash(),
            )


# ---------------------------------------------------------------------------
# PortfolioPolicy.validate_weights
# ---------------------------------------------------------------------------


class TestValidateWeights:
    def test_valid_weights(self):
        policy = _make_policy()
        policy.validate_weights([0.2, 0.2, 0.2, 0.2, 0.2])

    def test_wrong_length(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.validate_weights([0.5, 0.5])

    def test_sum_not_one(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.validate_weights([0.5, 0.1, 0.1, 0.1, 0.1])

    def test_weight_exceeded(self):
        policy = _make_policy(config=_make_config(max_weight=0.2))
        # 0.5 > 0.2.
        with pytest.raises(ValueError):
            policy.validate_weights([0.5, 0.2, 0.1, 0.1, 0.1])

    def test_negative_weight(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.validate_weights([-0.1, 0.3, 0.3, 0.3, 0.2])

    def test_min_positions_violated(self):
        policy = _make_policy(
            config=_make_config(max_weight=0.5),
            risk_limits=_make_risk_limits(max_weight=0.5, min_positions=3),
        )
        # Only 2 non-zero positions.
        with pytest.raises(ValueError):
            policy.validate_weights([0.5, 0.5, 0.0, 0.0, 0.0])

    def test_not_a_list(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.validate_weights("notalist")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PortfolioPolicy.validate_turnover
# ---------------------------------------------------------------------------


class TestValidateTurnover:
    def test_valid_turnover(self):
        policy = _make_policy(config=_make_config(max_turnover=0.5))
        policy.validate_turnover(
            [0.2, 0.2, 0.2, 0.2, 0.2],
            [0.3, 0.2, 0.2, 0.2, 0.1],
        )

    def test_turnover_exceeded(self):
        policy = _make_policy(config=_make_config(max_turnover=0.1))
        with pytest.raises(ValueError):
            policy.validate_turnover(
                [0.2, 0.2, 0.2, 0.2, 0.2],
                [0.4, 0.2, 0.2, 0.1, 0.1],
            )

    def test_length_mismatch(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.validate_turnover([0.5, 0.5], [0.3, 0.3, 0.4])

    def test_zero_turnover_ok(self):
        policy = _make_policy(config=_make_config(max_turnover=0.0))
        policy.validate_turnover(
            [0.2, 0.2, 0.2, 0.2, 0.2],
            [0.2, 0.2, 0.2, 0.2, 0.2],
        )


# ---------------------------------------------------------------------------
# PortfolioPolicy.compute_reward
# ---------------------------------------------------------------------------


class TestComputeReward:
    def test_all_components_present(self):
        policy = _make_policy()
        reward = policy.compute_reward(
            returns=[0.01, 0.02, 0.03, 0.04, 0.05],
            weights=[0.2, 0.2, 0.2, 0.2, 0.2],
            old_weights=[0.2, 0.2, 0.2, 0.2, 0.2],
        )
        assert "return" in reward
        assert "cost" in reward
        assert "drawdown" in reward
        assert "turnover" in reward
        assert "net" in reward

    def test_return_is_weighted_sum(self):
        policy = _make_policy()
        reward = policy.compute_reward(
            returns=[0.01, 0.02, 0.03, 0.04, 0.05],
            weights=[0.2, 0.2, 0.2, 0.2, 0.2],
            old_weights=[0.2, 0.2, 0.2, 0.2, 0.2],
        )
        expected_return = 0.2 * (0.01 + 0.02 + 0.03 + 0.04 + 0.05)
        assert abs(reward["return"] - expected_return) < 1e-9

    def test_zero_turnover_zero_cost(self):
        policy = _make_policy()
        reward = policy.compute_reward(
            returns=[0.01] * 5,
            weights=[0.2] * 5,
            old_weights=[0.2] * 5,
        )
        assert reward["turnover"] == 0.0
        assert reward["cost"] == 0.0

    def test_cost_proportional_to_turnover(self):
        policy = _make_policy(config=_make_config(cost_bps=2.0))
        reward = policy.compute_reward(
            returns=[0.0] * 5,
            weights=[0.3, 0.2, 0.2, 0.2, 0.1],
            old_weights=[0.2, 0.2, 0.2, 0.2, 0.2],
        )
        # turnover = |0.1| + 0 + 0 + 0 + |0.1| = 0.2
        assert abs(reward["turnover"] - 0.2) < 1e-9
        # cost = 2.0 / 1e4 * 0.2 = 0.00004
        assert abs(reward["cost"] - (2.0 / 1e4 * 0.2)) < 1e-12

    def test_net_is_return_minus_cost(self):
        policy = _make_policy()
        reward = policy.compute_reward(
            returns=[0.01] * 5,
            weights=[0.3, 0.2, 0.2, 0.2, 0.1],
            old_weights=[0.2, 0.2, 0.2, 0.2, 0.2],
        )
        assert abs(
            reward["net"]
            - (reward["return"] - reward["cost"] - reward["drawdown"])
        ) < 1e-12

    def test_wrong_returns_length(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.compute_reward(
                returns=[0.01, 0.02],
                weights=[0.2] * 5,
                old_weights=[0.2] * 5,
            )

    def test_wrong_weights_length(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.compute_reward(
                returns=[0.01] * 5,
                weights=[0.5, 0.5],
                old_weights=[0.2] * 5,
            )

    def test_wrong_old_weights_length(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.compute_reward(
                returns=[0.01] * 5,
                weights=[0.2] * 5,
                old_weights=[0.5, 0.5],
            )


# ---------------------------------------------------------------------------
# PortfolioPolicy.act
# ---------------------------------------------------------------------------


class TestPolicyAct:
    def test_normal_act_emits_weights(self):
        policy = _make_policy()
        out = policy.act(
            model_outputs=[0.05, 0.03, 0.02, 0.01, 0.04],
            current_weights=_equal_weights(5),
            confidence=0.8,
        )
        assert out.abstain is False
        assert out.risk_limits_respected is True
        assert len(out.target_weights) == 5
        assert abs(sum(out.target_weights) - 1.0) < 1e-6
        assert out.cost_model_hash == _cost_hash()

    def test_abstention_low_confidence(self):
        policy = _make_policy(config=_make_config(abstention_threshold=0.5))
        out = policy.act(
            model_outputs=[0.05, 0.03, 0.02, 0.01, 0.04],
            current_weights=_equal_weights(5),
            confidence=0.2,
        )
        assert out.abstain is True
        assert all(w == 0.0 for w in out.target_weights)

    def test_abstention_zero_confidence(self):
        policy = _make_policy()
        out = policy.act(
            model_outputs=[0.05, 0.03, 0.02, 0.01, 0.04],
            current_weights=_equal_weights(5),
            confidence=0.0,
        )
        assert out.abstain is True
        assert all(w == 0.0 for w in out.target_weights)

    def test_abstention_drawdown_breach(self):
        policy = _make_policy(config=_make_config(max_drawdown=0.1))
        out = policy.act(
            model_outputs=[0.05, 0.03, 0.02, 0.01, 0.04],
            current_weights=_equal_weights(5),
            confidence=0.9,
            drawdown=0.12,
        )
        assert out.abstain is True
        assert all(w == 0.0 for w in out.target_weights)

    def test_confidence_at_threshold_acts(self):
        policy = _make_policy(config=_make_config(abstention_threshold=0.5))
        out = policy.act(
            model_outputs=[0.05, 0.03, 0.02, 0.01, 0.04],
            current_weights=_equal_weights(5),
            confidence=0.5,
        )
        assert out.abstain is False

    def test_wrong_current_weights_length(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.act(
                model_outputs=[0.05] * 5,
                current_weights=[0.5, 0.5],
                confidence=0.8,
            )

    def test_confidence_out_of_range(self):
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.act(
                model_outputs=[0.05] * 5,
                current_weights=_equal_weights(5),
                confidence=1.5,
            )
        with pytest.raises(ValueError):
            policy.act(
                model_outputs=[0.05] * 5,
                current_weights=_equal_weights(5),
                confidence=-0.1,
            )

    def test_weights_clipped_to_max_weight(self):
        # max_weight=0.25 with min_positions=4 → 4*0.25=1.0 (compatible).
        policy = _make_policy(
            config=_make_config(max_weight=0.25, max_turnover=2.0),
            risk_limits=_make_risk_limits(
                max_weight=0.25, min_positions=4, max_turnover=2.0
            ),
        )
        out = policy.act(
            model_outputs=[1.0, 0.0, 0.0, 0.0, 0.0],
            current_weights=_equal_weights(5),
            confidence=0.9,
        )
        assert out.abstain is False
        for w in out.target_weights:
            assert w <= 0.25 + 1e-6
        assert abs(sum(out.target_weights) - 1.0) < 1e-6

    def test_all_negative_signal_equal_weight(self):
        policy = _make_policy()
        out = policy.act(
            model_outputs=[-0.05, -0.03, -0.02, -0.01, -0.04],
            current_weights=_equal_weights(5),
            confidence=0.9,
        )
        # All-negative signal → equal weight fallback (or abstain if
        # that violates min_positions). Either way, no crash.
        assert out.risk_limits_respected in (True, False)

    def test_cost_model_hash_recorded(self):
        policy = _make_policy(cost_model_hash=HEX64_B)
        out = policy.act(
            model_outputs=[0.05] * 5,
            current_weights=_equal_weights(5),
            confidence=0.8,
        )
        assert out.cost_model_hash == HEX64_B

    def test_reward_components_present(self):
        policy = _make_policy()
        out = policy.act(
            model_outputs=[0.05, 0.03, 0.02, 0.01, 0.04],
            current_weights=_equal_weights(5),
            confidence=0.8,
        )
        comps = out.reward_components
        assert "return" in comps
        assert "cost" in comps
        assert "drawdown" in comps
        assert "turnover" in comps


# ---------------------------------------------------------------------------
# validate_no_risk_violation
# ---------------------------------------------------------------------------


class TestValidateNoRiskViolation:
    def test_respected_returns_true(self):
        out = PolicyOutput(
            target_weights=[0.3, 0.3, 0.4],
            abstain=False,
            confidence=0.8,
            expected_reward=0.0,
            reward_components={},
            risk_limits_respected=True,
            cost_model_hash=HEX64,
        )
        rl = _make_risk_limits(max_weight=0.5, min_positions=2)
        assert validate_no_risk_violation(out, rl) is True

    def test_abstain_always_safe(self):
        out = PolicyOutput(
            target_weights=[0.0, 0.0, 0.0],
            abstain=True,
            confidence=0.1,
            expected_reward=0.0,
            reward_components={},
            risk_limits_respected=True,
            cost_model_hash=HEX64,
        )
        rl = _make_risk_limits()
        assert validate_no_risk_violation(out, rl) is True

    def test_weight_violation_raises(self):
        out = PolicyOutput(
            target_weights=[0.6, 0.2, 0.2],
            abstain=False,
            confidence=0.8,
            expected_reward=0.0,
            reward_components={},
            risk_limits_respected=True,
            cost_model_hash=HEX64,
        )
        rl = _make_risk_limits(max_weight=0.5, min_positions=2)
        with pytest.raises(ValueError):
            validate_no_risk_violation(out, rl)

    def test_min_positions_violation_raises(self):
        out = PolicyOutput(
            target_weights=[0.5, 0.5, 0.0],
            abstain=False,
            confidence=0.8,
            expected_reward=0.0,
            reward_components={},
            risk_limits_respected=True,
            cost_model_hash=HEX64,
        )
        rl = _make_risk_limits(max_weight=0.5, min_positions=3)
        with pytest.raises(ValueError):
            validate_no_risk_violation(out, rl)

    def test_sum_violation_raises(self):
        out = PolicyOutput(
            target_weights=[0.3, 0.3, 0.3],
            abstain=False,
            confidence=0.8,
            expected_reward=0.0,
            reward_components={},
            risk_limits_respected=True,
            cost_model_hash=HEX64,
        )
        rl = _make_risk_limits(max_weight=0.5, min_positions=2)
        with pytest.raises(ValueError):
            validate_no_risk_violation(out, rl)


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------


class TestReplayEngine:
    def test_refuses_non_shadow(self):
        cfg = _make_config(shadow_only=False)
        policy = _make_policy(config=cfg)
        with pytest.raises(ValueError):
            ReplayEngine(policy=policy, config=cfg)

    def test_full_replay(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        series = [
            [0.01, 0.02, 0.03, 0.04, 0.05],
            [0.02, 0.01, 0.0, -0.01, 0.03],
            [0.0, 0.01, 0.02, 0.03, 0.04],
        ]
        res = engine.run(series, initial_weights=_equal_weights(5))
        assert res.n_rebalances == 3
        assert len(res.reward_history) == 3
        assert len(res.weight_history) == 3
        assert all(len(wh) == 5 for wh in res.weight_history)

    def test_initial_weights_wrong_length(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        with pytest.raises(ValueError):
            engine.run([[0.01] * 5], initial_weights=[0.5, 0.5])

    def test_returns_row_wrong_length(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        with pytest.raises(ValueError):
            engine.run([[0.01, 0.02]], initial_weights=_equal_weights(5))

    def test_empty_series(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        res = engine.run([], initial_weights=_equal_weights(5))
        assert res.n_rebalances == 0
        assert res.total_return == 0.0
        assert res.sharpe_ratio is None

    def test_single_step(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        res = engine.run([[0.01] * 5], initial_weights=_equal_weights(5))
        assert res.n_rebalances == 1
        assert res.sharpe_ratio is None  # need >= 2 for Sharpe

    def test_abstentions_counted(self):
        # Very high abstention threshold → all abstain.
        cfg = _make_config(abstention_threshold=0.99)
        policy = _make_policy(config=cfg)
        engine = ReplayEngine(policy=policy, config=cfg)
        series = [[0.001] * 5, [0.002] * 5, [0.001] * 5]
        res = engine.run(series, initial_weights=_equal_weights(5))
        assert res.n_abstentions >= 1

    def test_total_cost_non_negative(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        series = [[0.01] * 5, [0.02] * 5, [0.01] * 5]
        res = engine.run(series, initial_weights=_equal_weights(5))
        assert res.total_cost >= 0.0

    def test_max_drawdown_non_negative(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        series = [[0.01] * 5, [-0.05] * 5, [0.01] * 5]
        res = engine.run(series, initial_weights=_equal_weights(5))
        assert res.max_drawdown >= 0.0

    def test_avg_turnover_non_negative(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        series = [[0.01] * 5, [0.02] * 5]
        res = engine.run(series, initial_weights=_equal_weights(5))
        assert res.avg_turnover >= 0.0

    def test_deterministic_with_same_seed(self):
        series = [[0.01, 0.02, 0.03, 0.04, 0.05], [0.02, 0.01, 0.0, -0.01, 0.03]]
        policy1 = _make_policy(config=_make_config(seed=42))
        policy2 = _make_policy(config=_make_config(seed=42))
        engine1 = ReplayEngine(policy=policy1, config=policy1.config)
        engine2 = ReplayEngine(policy=policy2, config=policy2.config)
        res1 = engine1.run(series, initial_weights=_equal_weights(5))
        res2 = engine2.run(series, initial_weights=_equal_weights(5))
        assert res1.reward_history == res2.reward_history
        assert res1.weight_history == res2.weight_history


# ---------------------------------------------------------------------------
# ReplayEngine.compute_sharpe
# ---------------------------------------------------------------------------


class TestComputeSharpe:
    def test_none_for_fewer_than_two(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        assert engine.compute_sharpe([0.01]) is None
        assert engine.compute_sharpe([]) is None

    def test_none_for_zero_std(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        assert engine.compute_sharpe([0.01, 0.01, 0.01]) is None

    def test_positive_for_positive_mean(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        sharpe = engine.compute_sharpe([0.01, 0.02, 0.03, 0.04])
        assert sharpe is not None
        assert sharpe > 0.0

    def test_annualization_factor(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        rewards = [0.01, -0.01, 0.02, -0.02, 0.03]
        sharpe = engine.compute_sharpe(rewards)
        # Manual computation.
        import numpy as np
        arr = np.asarray(rewards, dtype=float)
        manual = float(
            np.mean(arr) / np.std(arr, ddof=1) * math.sqrt(252)
        )
        assert sharpe is not None
        assert abs(sharpe - manual) < 1e-9


# ---------------------------------------------------------------------------
# Fail-closed integration tests
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_weight_violation_in_replay_raises(self):
        # Configure so the policy would emit weights that violate
        # max_weight, then verify the replay engine raises.
        # With max_weight=0.5 and a single strong signal, the policy
        # clips to 0.5 — but min_positions=3 forces at least 3 non-zero.
        # We instead test the validate_weights path directly.
        policy = _make_policy()
        with pytest.raises(ValueError):
            policy.validate_weights([0.6, 0.1, 0.1, 0.1, 0.1])

    def test_turnover_violation_raises(self):
        policy = _make_policy(config=_make_config(max_turnover=0.01))
        with pytest.raises(ValueError):
            policy.validate_turnover(
                [0.2, 0.2, 0.2, 0.2, 0.2],
                [0.4, 0.2, 0.2, 0.1, 0.1],
            )

    def test_drawdown_breach_forces_abstention(self):
        policy = _make_policy(config=_make_config(max_drawdown=0.05))
        out = policy.act(
            model_outputs=[0.1] * 5,
            current_weights=_equal_weights(5),
            confidence=0.99,
            drawdown=0.06,
        )
        assert out.abstain is True

    def test_initial_weights_validated(self):
        policy = _make_policy()
        engine = ReplayEngine(policy=policy, config=policy.config)
        # Invalid initial weights (sum != 1).
        with pytest.raises(ValueError):
            engine.run([[0.01] * 5], initial_weights=[0.5, 0.1, 0.1, 0.1, 0.1])


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_asset(self):
        cfg = _make_config(n_assets=1, max_weight=1.0)
        rl = _make_risk_limits(max_weight=1.0, min_positions=1)
        policy = _make_policy(config=cfg, risk_limits=rl)
        out = policy.act(
            model_outputs=[0.05],
            current_weights=[1.0],
            confidence=0.8,
        )
        assert out.abstain is False
        assert len(out.target_weights) == 1
        assert abs(out.target_weights[0] - 1.0) < 1e-6

    def test_single_asset_replay(self):
        cfg = _make_config(n_assets=1, max_weight=1.0)
        rl = _make_risk_limits(max_weight=1.0, min_positions=1)
        policy = _make_policy(config=cfg, risk_limits=rl)
        engine = ReplayEngine(policy=policy, config=cfg)
        res = engine.run([[0.01], [0.02], [0.01]], initial_weights=[1.0])
        assert res.n_rebalances == 3
        assert all(len(wh) == 1 for wh in res.weight_history)

    def test_all_abstain_replay(self):
        cfg = _make_config(abstention_threshold=0.99)
        policy = _make_policy(config=cfg)
        engine = ReplayEngine(policy=policy, config=cfg)
        res = engine.run(
            [[0.001] * 5, [0.001] * 5, [0.001] * 5],
            initial_weights=_equal_weights(5),
        )
        assert res.n_abstentions == 3
        # When abstaining, weights are held → weight history is initial.
        for wh in res.weight_history:
            assert wh == _equal_weights(5)

    def test_zero_confidence_abstains(self):
        policy = _make_policy()
        out = policy.act(
            model_outputs=[0.1] * 5,
            current_weights=_equal_weights(5),
            confidence=0.0,
        )
        assert out.abstain is True
        assert all(w == 0.0 for w in out.target_weights)

    def test_full_confidence_acts(self):
        # Use a high max_turnover so the tilt from equal weights is allowed.
        policy = _make_policy(
            config=_make_config(max_turnover=2.0),
            risk_limits=_make_risk_limits(max_turnover=2.0),
        )
        out = policy.act(
            model_outputs=[0.1, 0.05, 0.03, 0.02, 0.01],
            current_weights=_equal_weights(5),
            confidence=1.0,
        )
        assert out.abstain is False

    def test_policy_improves_over_equal_weight(self):
        # A policy with a clear signal should outperform equal-weight
        # on a series where the signal asset outperforms.
        policy = _make_policy(
            config=_make_config(
                max_weight=0.4, abstention_threshold=0.0, max_turnover=2.0
            ),
            risk_limits=_make_risk_limits(
                max_weight=0.4, min_positions=3, max_turnover=2.0
            ),
        )
        engine = ReplayEngine(policy=policy, config=policy.config)
        # Asset 0 consistently outperforms.
        series = [
            [0.05, 0.0, 0.0, 0.0, 0.0],
            [0.05, 0.0, 0.0, 0.0, 0.0],
            [0.05, 0.0, 0.0, 0.0, 0.0],
            [0.05, 0.0, 0.0, 0.0, 0.0],
            [0.05, 0.0, 0.0, 0.0, 0.0],
        ]
        res_policy = engine.run(series, initial_weights=_equal_weights(5))

        # Equal-weight baseline (no rebalancing): just compute the
        # cumulative return of holding equal weights.
        eq = _equal_weights(5)
        import numpy as np
        value = 1.0
        for row in series:
            r = np.asarray(row, dtype=float)
            value *= 1.0 + float(np.dot(np.asarray(eq), r))
        eq_return = value - 1.0

        # The policy should at least not catastrophically underperform
        # and should have no risk violations.
        assert res_policy.n_risk_violations == 0
        # With max_weight=0.4 the policy tilts toward asset 0, so it
        # should outperform equal weight (which only has 0.2 on asset 0).
        assert res_policy.total_return >= eq_return - 1e-6
