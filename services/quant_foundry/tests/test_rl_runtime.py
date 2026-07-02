"""Tests for quant_foundry.rl_runtime (T-13.5).

Covers the RL runtime: the Docker image spec, environment manifest, cost
model, rollout record, policy checkpoint, the deterministic market
simulator (reset / step / validate_action / risk-limit enforcement),
the rollout manager (save / load / list / replay log), and the RL
healthcheck.

The test host is CPU-only (torch is installed with the CPU index URL), so
the healthcheck's GPU probe reports unavailable and ``is_healthy()``
returns ``False``; the simulator canary and rollout round-trip probes
themselves run on CPU and are asserted directly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quant_foundry.rl_runtime import (
    CostModel,
    DeterministicMarketSimulator,
    EnvironmentManifest,
    PolicyCheckpoint,
    RLHealthcheck,
    RLImageSpec,
    RolloutManager,
    RolloutRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


HEX64 = "a" * 64
HEX64_B = "b" * 64
HEX64_C = "c" * 64


def _make_manifest(
    n_assets: int = 3,
    n_timesteps: int = 5,
    risk_limits: dict[str, float] | None = None,
    env_hash: str = HEX64,
    cost_model_hash: str = HEX64_B,
) -> EnvironmentManifest:
    if risk_limits is None:
        risk_limits = {"max_weight": 0.5, "max_turnover": 0.5}
    return EnvironmentManifest(
        env_id="test-env",
        env_version="1.0.0",
        env_hash=env_hash,
        cost_model_hash=cost_model_hash,
        n_assets=n_assets,
        n_timesteps=n_timesteps,
        reward_components=["return", "cost", "drawdown", "turnover"],
        risk_limits=risk_limits,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _make_cost_model(cost_hash: str = HEX64_B) -> CostModel:
    return CostModel(
        model_id="test-cost",
        commission_bps=1.0,
        slippage_bps=0.5,
        market_impact=0.1,
        cost_hash=cost_hash,
    )


def _make_rollout(rollout_id: str = "r1") -> RolloutRecord:
    return RolloutRecord(
        rollout_id=rollout_id,
        env_id="test-env",
        policy_hash=HEX64_C,
        n_steps=2,
        actions=[[0.5, 0.3, 0.2], [0.4, 0.4, 0.2]],
        rewards=[0.01, -0.005],
        cumulative_reward=0.005,
        terminated=False,
        truncated=True,
        metadata={"seed": "42"},
    )


# ---------------------------------------------------------------------------
# RLImageSpec
# ---------------------------------------------------------------------------


class TestRLImageSpec:
    def test_defaults(self) -> None:
        spec = RLImageSpec()
        assert spec.image_name == "trainer-gpu-rl"
        assert spec.base_image == "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
        assert spec.python_version == "3.12"
        assert spec.gpu_required is True
        assert spec.supports_checkpoint_resume is True
        assert spec.rollout_cache_dir == "/opt/rollout_cache"

    def test_default_packages(self) -> None:
        spec = RLImageSpec()
        assert "torch==2.1.0" in spec.packages
        assert "numpy>=1.26" in spec.packages
        assert "pandas>=2.1" in spec.packages
        assert "pydantic>=2.7" in spec.packages
        assert "gymnasium>=0.29" in spec.packages
        assert "stable-baselines3>=2.2" in spec.packages

    def test_healthcheck_cmd_references_rl_runtime(self) -> None:
        spec = RLImageSpec()
        assert "RLHealthcheck" in spec.healthcheck_cmd
        assert "rl_runtime" in spec.healthcheck_cmd

    def test_frozen(self) -> None:
        spec = RLImageSpec()
        with pytest.raises(Exception):
            spec.image_name = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            RLImageSpec(unexpected="x")  # type: ignore[call-arg]

    def test_custom_construction(self) -> None:
        spec = RLImageSpec(
            image_name="custom-rl",
            base_image="pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime",
            python_version="3.11",
            packages=["torch==2.2.0", "numpy"],
            gpu_required=False,
            supports_checkpoint_resume=False,
            rollout_cache_dir="/data/cache",
        )
        assert spec.image_name == "custom-rl"
        assert spec.gpu_required is False
        assert spec.supports_checkpoint_resume is False
        assert spec.rollout_cache_dir == "/data/cache"


# ---------------------------------------------------------------------------
# EnvironmentManifest
# ---------------------------------------------------------------------------


class TestEnvironmentManifest:
    def test_valid_construction(self) -> None:
        m = _make_manifest()
        assert m.env_id == "test-env"
        assert m.env_version == "1.0.0"
        assert m.n_assets == 3
        assert m.n_timesteps == 5
        assert m.reward_components == ["return", "cost", "drawdown", "turnover"]
        assert m.risk_limits == {"max_weight": 0.5, "max_turnover": 0.5}

    def test_frozen(self) -> None:
        m = _make_manifest()
        with pytest.raises(Exception):
            m.env_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EnvironmentManifest(
                env_id="x",
                env_version="1.0.0",
                env_hash=HEX64,
                cost_model_hash=HEX64_B,
                n_assets=1,
                n_timesteps=1,
                reward_components=["return"],
                risk_limits={},
                created_at="t",
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_env_hash_must_be_hex64(self) -> None:
        with pytest.raises(Exception):
            _make_manifest(env_hash="nothex")

    def test_cost_model_hash_must_be_hex64(self) -> None:
        with pytest.raises(Exception):
            _make_manifest(cost_model_hash="zz" + "0" * 62)

    def test_env_hash_length(self) -> None:
        with pytest.raises(Exception):
            _make_manifest(env_hash="a" * 63)

    def test_n_assets_minimum(self) -> None:
        with pytest.raises(Exception):
            _make_manifest(n_assets=0)

    def test_n_timesteps_minimum(self) -> None:
        with pytest.raises(Exception):
            _make_manifest(n_timesteps=0)

    def test_single_asset_allowed(self) -> None:
        m = _make_manifest(n_assets=1)
        assert m.n_assets == 1

    def test_single_step_allowed(self) -> None:
        m = _make_manifest(n_timesteps=1)
        assert m.n_timesteps == 1


# ---------------------------------------------------------------------------
# CostModel
# ---------------------------------------------------------------------------


class TestCostModel:
    def test_valid_construction(self) -> None:
        cm = _make_cost_model()
        assert cm.model_id == "test-cost"
        assert cm.commission_bps == 1.0
        assert cm.slippage_bps == 0.5
        assert cm.market_impact == 0.1

    def test_frozen(self) -> None:
        cm = _make_cost_model()
        with pytest.raises(Exception):
            cm.model_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            CostModel(
                model_id="x",
                cost_hash=HEX64_B,
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_negative_commission_rejected(self) -> None:
        with pytest.raises(Exception):
            CostModel(model_id="x", commission_bps=-1.0, cost_hash=HEX64_B)

    def test_negative_slippage_rejected(self) -> None:
        with pytest.raises(Exception):
            CostModel(model_id="x", slippage_bps=-0.1, cost_hash=HEX64_B)

    def test_negative_market_impact_rejected(self) -> None:
        with pytest.raises(Exception):
            CostModel(model_id="x", market_impact=-0.01, cost_hash=HEX64_B)

    def test_zero_costs_allowed(self) -> None:
        cm = CostModel(model_id="zero", cost_hash=HEX64_B)
        assert cm.commission_bps == 0.0
        assert cm.slippage_bps == 0.0
        assert cm.market_impact == 0.0

    def test_cost_hash_must_be_hex64(self) -> None:
        with pytest.raises(Exception):
            CostModel(model_id="x", cost_hash="nothex")

    def test_compute_hash_deterministic(self) -> None:
        h1 = CostModel.compute_hash(1.0, 0.5, 0.1)
        h2 = CostModel.compute_hash(1.0, 0.5, 0.1)
        assert h1 == h2
        assert len(h1) == 64

    def test_compute_hash_differs_for_different_params(self) -> None:
        h1 = CostModel.compute_hash(1.0, 0.5, 0.1)
        h2 = CostModel.compute_hash(2.0, 0.5, 0.1)
        assert h1 != h2


# ---------------------------------------------------------------------------
# RolloutRecord
# ---------------------------------------------------------------------------


class TestRolloutRecord:
    def test_valid_construction(self) -> None:
        r = _make_rollout()
        assert r.rollout_id == "r1"
        assert r.n_steps == 2
        assert len(r.actions) == 2
        assert len(r.rewards) == 2
        assert r.cumulative_reward == 0.005
        assert r.metadata == {"seed": "42"}

    def test_frozen(self) -> None:
        r = _make_rollout()
        with pytest.raises(Exception):
            r.rollout_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            RolloutRecord(
                rollout_id="r",
                env_id="e",
                policy_hash=HEX64_C,
                n_steps=1,
                actions=[[1.0]],
                rewards=[0.0],
                cumulative_reward=0.0,
                terminated=False,
                truncated=False,
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_default_metadata_empty(self) -> None:
        r = RolloutRecord(
            rollout_id="r",
            env_id="e",
            policy_hash=HEX64_C,
            n_steps=1,
            actions=[[1.0]],
            rewards=[0.0],
            cumulative_reward=0.0,
            terminated=False,
            truncated=False,
        )
        assert r.metadata == {}


# ---------------------------------------------------------------------------
# PolicyCheckpoint
# ---------------------------------------------------------------------------


class TestPolicyCheckpoint:
    def test_valid_construction(self) -> None:
        ckpt = PolicyCheckpoint(
            checkpoint_id="c1",
            policy_type="ppo",
            policy_hash=HEX64_C,
            env_id="test-env",
            training_steps=1000,
            artifact_path="/opt/artifacts/c1.pt",
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert ckpt.checkpoint_id == "c1"
        assert ckpt.policy_type == "ppo"
        assert ckpt.training_steps == 1000

    def test_frozen(self) -> None:
        ckpt = PolicyCheckpoint(
            checkpoint_id="c1",
            policy_type="ppo",
            policy_hash=HEX64_C,
            env_id="test-env",
            training_steps=1,
            artifact_path="/x",
            created_at="t",
        )
        with pytest.raises(Exception):
            ckpt.checkpoint_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            PolicyCheckpoint(
                checkpoint_id="c1",
                policy_type="ppo",
                policy_hash=HEX64_C,
                env_id="test-env",
                training_steps=1,
                artifact_path="/x",
                created_at="t",
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_policy_hash_must_be_hex64(self) -> None:
        with pytest.raises(Exception):
            PolicyCheckpoint(
                checkpoint_id="c1",
                policy_type="ppo",
                policy_hash="nothex",
                env_id="test-env",
                training_steps=1,
                artifact_path="/x",
                created_at="t",
            )

    def test_training_steps_minimum(self) -> None:
        with pytest.raises(Exception):
            PolicyCheckpoint(
                checkpoint_id="c1",
                policy_type="ppo",
                policy_hash=HEX64_C,
                env_id="test-env",
                training_steps=-1,
                artifact_path="/x",
                created_at="t",
            )

    def test_zero_training_steps_allowed(self) -> None:
        ckpt = PolicyCheckpoint(
            checkpoint_id="c1",
            policy_type="random",
            policy_hash=HEX64_C,
            env_id="test-env",
            training_steps=0,
            artifact_path="/x",
            created_at="t",
        )
        assert ckpt.training_steps == 0

    @pytest.mark.parametrize("ptype", ["ppo", "a2c", "dqn", "random"])
    def test_policy_types_accepted(self, ptype: str) -> None:
        ckpt = PolicyCheckpoint(
            checkpoint_id="c1",
            policy_type=ptype,
            policy_hash=HEX64_C,
            env_id="test-env",
            training_steps=1,
            artifact_path="/x",
            created_at="t",
        )
        assert ckpt.policy_type == ptype


# ---------------------------------------------------------------------------
# DeterministicMarketSimulator
# ---------------------------------------------------------------------------


class TestDeterministicMarketSimulator:
    def test_reset_returns_observation(self) -> None:
        sim = DeterministicMarketSimulator(_make_manifest(), _make_cost_model())
        obs = sim.reset()
        assert "prices" in obs
        assert "weights" in obs
        assert obs["step"] == 0
        assert obs["portfolio_value"] == 1.0
        assert len(obs["prices"]) == 3

    def test_reset_is_deterministic(self) -> None:
        m = _make_manifest()
        cm = _make_cost_model()
        sim1 = DeterministicMarketSimulator(m, cm, seed=42)
        sim2 = DeterministicMarketSimulator(m, cm, seed=42)
        o1 = sim1.reset()
        o2 = sim2.reset()
        assert o1["prices"] == o2["prices"]

    def test_different_seed_different_prices(self) -> None:
        m = _make_manifest()
        cm = _make_cost_model()
        sim1 = DeterministicMarketSimulator(m, cm, seed=42)
        sim2 = DeterministicMarketSimulator(m, cm, seed=7)
        o1 = sim1.reset()
        o2 = sim2.reset()
        # Initial row is always 1.0; subsequent rows differ.
        assert o1["prices"][0] == o2["prices"][0]
        sim1.step([1 / 3, 1 / 3, 1 / 3])
        sim2.step([1 / 3, 1 / 3, 1 / 3])
        # After stepping, prices diverge for different seeds.
        assert sim1._prices[1] != sim2._prices[1]

    def test_step_returns_five_tuple(self) -> None:
        sim = DeterministicMarketSimulator(_make_manifest(), _make_cost_model())
        sim.reset()
        obs, reward, terminated, truncated, info = sim.step([1 / 3, 1 / 3, 1 / 3])
        assert isinstance(obs, dict)
        assert isinstance(reward, float)
        assert isinstance(terminated, bool)
        assert isinstance(truncated, bool)
        assert isinstance(info, dict)
        assert "portfolio_return" in info
        assert "cost" in info
        assert "turnover" in info

    def test_step_advances_step_counter(self) -> None:
        sim = DeterministicMarketSimulator(_make_manifest(), _make_cost_model())
        sim.reset()
        assert sim._current_step == 0
        sim.step([1 / 3, 1 / 3, 1 / 3])
        assert sim._current_step == 1

    def test_truncated_after_n_timesteps(self) -> None:
        m = _make_manifest(n_assets=2, n_timesteps=2)
        cm = _make_cost_model()
        sim = DeterministicMarketSimulator(m, cm)
        sim.reset()
        sim.step([0.5, 0.5])
        _o, _r, _t, truncated, _info = sim.step([0.5, 0.5])
        assert truncated is True

    def test_validate_action_wrong_length(self) -> None:
        sim = DeterministicMarketSimulator(_make_manifest(), _make_cost_model())
        sim.reset()
        with pytest.raises(ValueError):
            sim.validate_action([0.5, 0.5])  # n_assets=3

    def test_validate_action_sum_not_one(self) -> None:
        sim = DeterministicMarketSimulator(_make_manifest(), _make_cost_model())
        sim.reset()
        with pytest.raises(ValueError):
            sim.validate_action([0.5, 0.3, 0.3])  # sums to 1.1

    def test_validate_action_exceeds_max_weight(self) -> None:
        # max_weight = 0.5
        sim = DeterministicMarketSimulator(_make_manifest(), _make_cost_model())
        sim.reset()
        with pytest.raises(ValueError):
            sim.validate_action([0.6, 0.2, 0.2])

    def test_step_enforces_max_weight_fail_closed(self) -> None:
        sim = DeterministicMarketSimulator(_make_manifest(), _make_cost_model())
        sim.reset()
        with pytest.raises(ValueError):
            sim.step([0.6, 0.2, 0.2])

    def test_step_enforces_max_turnover_fail_closed(self) -> None:
        # max_turnover = 0.5; jump from uniform (~0.33 each) to concentrated.
        m = _make_manifest(n_assets=2, risk_limits={"max_weight": 1.0, "max_turnover": 0.1})
        cm = _make_cost_model()
        sim = DeterministicMarketSimulator(m, cm)
        sim.reset()
        with pytest.raises(ValueError):
            sim.step([1.0, 0.0])  # turnover = ~1.0 > 0.1

    def test_cost_reduces_reward(self) -> None:
        m = _make_manifest(n_assets=2, risk_limits={"max_weight": 1.0, "max_turnover": 2.0})
        cm_zero = CostModel(model_id="zero", cost_hash=HEX64_B)
        cm_cost = CostModel(
            model_id="cost",
            commission_bps=100.0,
            slippage_bps=100.0,
            market_impact=10.0,
            cost_hash=HEX64_C,
        )
        sim_zero = DeterministicMarketSimulator(m, cm_zero, seed=42)
        sim_cost = DeterministicMarketSimulator(m, cm_cost, seed=42)
        sim_zero.reset()
        sim_cost.reset()
        # Non-uniform action so turnover > 0 (initial weights are uniform).
        action = [0.7, 0.3]
        _o0, r0, _t0, _tr0, _i0 = sim_zero.step(action)
        _o1, r1, _t1, _tr1, _i1 = sim_cost.step(action)
        assert r1 < r0  # cost reduces reward

    def test_single_asset_simulator(self) -> None:
        m = _make_manifest(
            n_assets=1,
            n_timesteps=1,
            risk_limits={"max_weight": 1.0, "max_turnover": 1.0},
        )
        cm = _make_cost_model()
        sim = DeterministicMarketSimulator(m, cm)
        obs = sim.reset()
        assert len(obs["prices"]) == 1
        _o, reward, _t, truncated, _info = sim.step([1.0])
        assert isinstance(reward, float)
        assert truncated is True

    def test_single_step_simulator(self) -> None:
        m = _make_manifest(n_assets=2, n_timesteps=1)
        cm = _make_cost_model()
        sim = DeterministicMarketSimulator(m, cm)
        sim.reset()
        _o, _r, _t, truncated, _info = sim.step([0.5, 0.5])
        assert truncated is True

    def test_random_policy_can_roll_out(self) -> None:
        """A 'random' policy that picks uniform weights rolls out fully."""
        m = _make_manifest(n_assets=3, n_timesteps=5)
        cm = _make_cost_model()
        sim = DeterministicMarketSimulator(m, cm, seed=123)
        sim.reset()
        action = [1 / 3, 1 / 3, 1 / 3]
        rewards: list[float] = []
        for _ in range(m.n_timesteps):
            _o, r, _t, truncated, _info = sim.step(action)
            rewards.append(r)
            if truncated:
                break
        assert len(rewards) == m.n_timesteps


# ---------------------------------------------------------------------------
# RolloutManager
# ---------------------------------------------------------------------------


class TestRolloutManager:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path))
        r = _make_rollout("roll-1")
        path = mgr.save_rollout(r)
        assert Path(path).exists()
        loaded = mgr.load_rollout("roll-1")
        assert loaded.rollout_id == r.rollout_id
        assert loaded.actions == r.actions
        assert loaded.rewards == r.rewards

    def test_save_creates_cache_dir(self, tmp_path: Path) -> None:
        cache = tmp_path / "nested" / "cache"
        mgr = RolloutManager(cache_dir=str(cache))
        mgr.save_rollout(_make_rollout("r-x"))
        assert cache.exists()

    def test_list_rollouts_empty(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path))
        assert mgr.list_rollouts() == []

    def test_list_rollouts_returns_ids(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path))
        mgr.save_rollout(_make_rollout("b-rollout"))
        mgr.save_rollout(_make_rollout("a-rollout"))
        ids = mgr.list_rollouts()
        assert ids == ["a-rollout", "b-rollout"]

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            mgr.load_rollout("nope")

    def test_list_rollouts_no_dir(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path / "does-not-exist"))
        assert mgr.list_rollouts() == []

    def test_save_replay_log(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path))
        r = _make_rollout("replay-1")
        replay_path = tmp_path / "replay.json"
        mgr.save_replay_log(r, str(replay_path))
        assert replay_path.exists()
        data = json.loads(replay_path.read_text(encoding="utf-8"))
        assert data["rollout_id"] == "replay-1"
        assert data["n_steps"] == 2
        assert len(data["steps"]) == 2
        assert data["steps"][0]["step"] == 0
        assert data["steps"][1]["cumulative_reward"] == 0.005

    def test_save_replay_log_creates_parent(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path))
        r = _make_rollout("replay-2")
        replay_path = tmp_path / "nested" / "dir" / "replay.json"
        mgr.save_replay_log(r, str(replay_path))
        assert replay_path.exists()

    def test_save_replay_log_cumulative_running(self, tmp_path: Path) -> None:
        mgr = RolloutManager(cache_dir=str(tmp_path))
        r = RolloutRecord(
            rollout_id="cum",
            env_id="e",
            policy_hash=HEX64_C,
            n_steps=3,
            actions=[[1.0], [1.0], [1.0]],
            rewards=[0.1, 0.2, -0.05],
            cumulative_reward=0.25,
            terminated=False,
            truncated=True,
        )
        replay_path = tmp_path / "replay.json"
        mgr.save_replay_log(r, str(replay_path))
        data = json.loads(replay_path.read_text(encoding="utf-8"))
        cum = [s["cumulative_reward"] for s in data["steps"]]
        assert cum == pytest.approx([0.1, 0.3, 0.25])


# ---------------------------------------------------------------------------
# RLHealthcheck
# ---------------------------------------------------------------------------


class TestRLHealthcheck:
    def test_init_default_timeout(self) -> None:
        hc = RLHealthcheck()
        assert hc.timeout_seconds == 60

    def test_init_custom_timeout(self) -> None:
        hc = RLHealthcheck(timeout_seconds=30)
        assert hc.timeout_seconds == 30

    def test_init_non_positive_timeout_rejected(self) -> None:
        with pytest.raises(ValueError):
            RLHealthcheck(timeout_seconds=0)
        with pytest.raises(ValueError):
            RLHealthcheck(timeout_seconds=-1)

    def test_run_returns_status_dict(self) -> None:
        hc = RLHealthcheck()
        status = hc.run()
        assert "healthy" in status
        assert "gpu" in status
        assert "simulator_canary" in status
        assert "rollout_roundtrip" in status
        assert "error" in status
        assert "duration_seconds" in status

    def test_simulator_canary_succeeds_on_cpu(self) -> None:
        hc = RLHealthcheck()
        status = hc.run()
        # The simulator canary does not need a GPU.
        assert status["simulator_canary"] is True

    def test_rollout_roundtrip_succeeds_on_cpu(self) -> None:
        hc = RLHealthcheck()
        status = hc.run()
        assert status["rollout_roundtrip"] is True

    def test_is_healthy_false_on_cpu_only_host(self) -> None:
        hc = RLHealthcheck()
        # On a CPU-only host the GPU is unavailable, so is_healthy is False.
        assert hc.is_healthy() is False

    def test_healthy_false_when_gpu_unavailable(self) -> None:
        hc = RLHealthcheck()
        status = hc.run()
        gpu = status["gpu"]
        if gpu and not gpu.get("available"):
            assert status["healthy"] is False
