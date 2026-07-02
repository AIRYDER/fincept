"""Tests for quant_foundry.rl_shadow_policy (T-13.3).

Covers the RL shadow policy: config/result construction and validation,
the train/eval separator (separation + filtering), the future-label
guard (single + batch), the RLShadowPolicy train/evaluate/save/load
round-trip, promotion-eligibility enforcement, future-label access
validation, and family registration.

All tests run on CPU-only hosts — the policy uses a mocked heuristic so
no torch / gymnasium / stable_baselines3 import is required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from quant_foundry.rl_runtime import (
    CostModel,
    EnvironmentManifest,
    PolicyCheckpoint,
)
from quant_foundry.rl_shadow_policy import (
    FutureLabelGuard,
    RLShadowConfig,
    RLShadowPolicy,
    RLShadowResult,
    TrainEvalSeparator,
    register_rl_shadow_family,
    validate_no_future_label_access,
    validate_promotion_eligibility,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HEX64 = "a" * 64
HEX64_B = "b" * 64
HEX64_C = "c" * 64

TRAIN_START = "2026-01-01T00:00:00+00:00"
TRAIN_END = "2026-02-01T00:00:00+00:00"
EVAL_START = "2026-02-01T00:00:00+00:00"
EVAL_END = "2026-03-01T00:00:00+00:00"


def _make_manifest(
    n_assets: int = 3,
    n_timesteps: int = 5,
    risk_limits: dict[str, float] | None = None,
) -> EnvironmentManifest:
    if risk_limits is None:
        risk_limits = {"max_weight": 0.5, "max_turnover": 1.0}
    return EnvironmentManifest(
        env_id="test-env",
        env_version="1.0.0",
        env_hash=HEX64,
        cost_model_hash=HEX64_B,
        n_assets=n_assets,
        n_timesteps=n_timesteps,
        reward_components=["return", "cost", "drawdown", "turnover"],
        risk_limits=risk_limits,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _make_cost_model() -> CostModel:
    return CostModel(
        model_id="test-cost",
        commission_bps=1.0,
        slippage_bps=0.5,
        market_impact=0.1,
        cost_hash=HEX64_B,
    )


def _make_config(
    *,
    policy_type: str = "ppo",
    n_train_episodes: int = 2,
    n_eval_episodes: int = 2,
    train_start: str = TRAIN_START,
    train_end: str = TRAIN_END,
    eval_start: str = EVAL_START,
    eval_end: str = EVAL_END,
    n_assets: int = 3,
    n_timesteps: int = 5,
    shadow_only: bool = True,
    seed: int = 42,
    max_checkpoint_steps: int = 1000,
) -> RLShadowConfig:
    return RLShadowConfig(
        policy_type=policy_type,
        n_train_episodes=n_train_episodes,
        n_eval_episodes=n_eval_episodes,
        train_start=train_start,
        train_end=train_end,
        eval_start=eval_start,
        eval_end=eval_end,
        env_manifest=_make_manifest(n_assets=n_assets, n_timesteps=n_timesteps),
        cost_model=_make_cost_model(),
        shadow_only=shadow_only,
        seed=seed,
        max_checkpoint_steps=max_checkpoint_steps,
    )


def _make_returns_data(
    n: int = 10, start: str = TRAIN_START, freq_minutes: int = 60 * 24
) -> list[dict]:
    """Build a synthetic returns series with hourly-ish timestamps."""
    base = datetime.fromisoformat(start)
    rows: list[dict] = []
    for i in range(n):
        ts = base.replace(minute=0, second=0, microsecond=0)
        # Add ``i`` days so the series spans the train + eval windows.
        from datetime import timedelta

        ts = ts + timedelta(days=i)
        rows.append(
            {
                "timestamp": ts.isoformat(),
                "returns": [0.001 * (i % 3) for _ in range(3)],
            }
        )
    return rows


def _make_result(
    config: RLShadowConfig | None = None,
    *,
    promotion_eligible: bool = False,
    metrics: dict[str, float] | None = None,
) -> RLShadowResult:
    cfg = config or _make_config()
    return RLShadowResult(
        config=cfg,
        train_reward=0.5,
        eval_reward=0.3,
        n_train_episodes=cfg.n_train_episodes,
        n_eval_episodes=cfg.n_eval_episodes,
        policy_checkpoint=PolicyCheckpoint(
            checkpoint_id="shadow_ppo_42",
            policy_type="ppo",
            policy_hash=HEX64_C,
            env_id=cfg.env_manifest.env_id,
            training_steps=10,
            artifact_path="/tmp/policy.json",
            created_at="2026-01-01T00:00:00+00:00",
        ),
        rollout_logs=["/tmp/log1.json"],
        env_manifest=cfg.env_manifest,
        promotion_eligible=promotion_eligible,
        metrics=metrics if metrics is not None else {"sharpe": 1.0},
        duration_seconds=0.1,
    )


# ---------------------------------------------------------------------------
# RLShadowConfig
# ---------------------------------------------------------------------------


class TestRLShadowConfig:
    def test_default_construction(self) -> None:
        cfg = _make_config()
        assert cfg.policy_type == "ppo"
        assert cfg.n_train_episodes == 2
        assert cfg.n_eval_episodes == 2
        assert cfg.shadow_only is True
        assert cfg.seed == 42
        assert cfg.max_checkpoint_steps == 1000

    def test_frozen(self) -> None:
        cfg = _make_config()
        with pytest.raises(ValidationError):
            cfg.policy_type = "dqn"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            _make_config()  # baseline
            RLShadowConfig(
                policy_type="ppo",
                train_start=TRAIN_START,
                train_end=TRAIN_END,
                eval_start=EVAL_START,
                eval_end=EVAL_END,
                env_manifest=_make_manifest(),
                cost_model=_make_cost_model(),
                extra_field="bad",  # type: ignore[call-arg]
            )

    def test_invalid_policy_type(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(policy_type="invalid")

    def test_all_valid_policy_types(self) -> None:
        for pt in ("ppo", "a2c", "dqn", "random"):
            cfg = _make_config(policy_type=pt)
            assert cfg.policy_type == pt

    def test_train_end_must_be_after_train_start(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(train_start=TRAIN_END, train_end=TRAIN_START)

    def test_eval_end_must_be_after_eval_start(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(eval_start=EVAL_END, eval_end=EVAL_START)

    def test_eval_start_must_be_at_least_train_end(self) -> None:
        # Overlap: eval_start before train_end.
        with pytest.raises(ValidationError):
            _make_config(
                train_start=TRAIN_START,
                train_end="2026-02-15T00:00:00+00:00",
                eval_start="2026-02-01T00:00:00+00:00",
                eval_end=EVAL_END,
            )

    def test_eval_start_equals_train_end_allowed(self) -> None:
        # Boundary: eval_start == train_end is allowed (no overlap).
        cfg = _make_config(eval_start=TRAIN_END)
        assert cfg.eval_start == TRAIN_END

    def test_shadow_only_false_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(shadow_only=False)

    def test_invalid_iso_datetime(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(train_start="not-a-date")

    def test_n_train_episodes_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(n_train_episodes=0)

    def test_n_eval_episodes_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(n_eval_episodes=0)

    def test_max_checkpoint_steps_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(max_checkpoint_steps=0)


# ---------------------------------------------------------------------------
# RLShadowResult
# ---------------------------------------------------------------------------


class TestRLShadowResult:
    def test_default_construction(self) -> None:
        result = _make_result()
        assert result.promotion_eligible is False
        assert result.train_reward == 0.5
        assert result.eval_reward == 0.3
        assert result.policy_checkpoint is not None

    def test_frozen(self) -> None:
        result = _make_result()
        with pytest.raises(ValidationError):
            result.train_reward = 1.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        cfg = _make_config()
        with pytest.raises(ValidationError):
            RLShadowResult(
                config=cfg,
                train_reward=0.5,
                eval_reward=0.3,
                n_train_episodes=1,
                n_eval_episodes=1,
                env_manifest=cfg.env_manifest,
                duration_seconds=0.1,
                extra_field="bad",  # type: ignore[call-arg]
            )

    def test_promotion_eligible_true_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_result(promotion_eligible=True)

    def test_n_train_episodes_must_be_positive(self) -> None:
        cfg = _make_config()
        with pytest.raises(ValidationError):
            RLShadowResult(
                config=cfg,
                train_reward=0.5,
                eval_reward=0.3,
                n_train_episodes=0,
                n_eval_episodes=1,
                env_manifest=cfg.env_manifest,
                duration_seconds=0.1,
            )

    def test_optional_checkpoint_none_allowed(self) -> None:
        cfg = _make_config()
        result = RLShadowResult(
            config=cfg,
            train_reward=0.5,
            eval_reward=0.3,
            n_train_episodes=1,
            n_eval_episodes=1,
            policy_checkpoint=None,
            rollout_logs=[],
            env_manifest=cfg.env_manifest,
            promotion_eligible=False,
            metrics={},
            duration_seconds=0.1,
        )
        assert result.policy_checkpoint is None
        assert result.rollout_logs == []


# ---------------------------------------------------------------------------
# TrainEvalSeparator
# ---------------------------------------------------------------------------


class TestTrainEvalSeparator:
    def test_valid_separation(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_START, EVAL_END)
        sep.validate_separation()  # no raise

    def test_overlap_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrainEvalSeparator(
                TRAIN_START,
                "2026-02-15T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
                EVAL_END,
            )

    def test_eval_start_equals_train_end_allowed(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, TRAIN_END, EVAL_END)
        sep.validate_separation()

    def test_train_end_before_train_start_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrainEvalSeparator(TRAIN_END, TRAIN_START, EVAL_START, EVAL_END)

    def test_eval_end_before_eval_start_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_END, EVAL_START)

    def test_is_in_train_period(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_START, EVAL_END)
        assert sep.is_in_train_period("2026-01-15T00:00:00+00:00") is True
        assert sep.is_in_train_period(TRAIN_START) is True
        # train_end is exclusive.
        assert sep.is_in_train_period(TRAIN_END) is False
        assert sep.is_in_train_period("2026-02-15T00:00:00+00:00") is False

    def test_is_in_eval_period(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_START, EVAL_END)
        assert sep.is_in_eval_period("2026-02-15T00:00:00+00:00") is True
        assert sep.is_in_eval_period(EVAL_START) is True
        # eval_end is exclusive.
        assert sep.is_in_eval_period(EVAL_END) is False
        assert sep.is_in_eval_period("2026-01-15T00:00:00+00:00") is False

    def test_get_train_data(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_START, EVAL_END)
        data = [
            {"timestamp": "2026-01-15T00:00:00+00:00", "v": 1},
            {"timestamp": "2026-02-15T00:00:00+00:00", "v": 2},
            {"timestamp": "2026-01-05T00:00:00+00:00", "v": 3},
        ]
        train = sep.get_train_data(data)
        assert len(train) == 2
        assert {r["v"] for r in train} == {1, 3}

    def test_get_eval_data(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_START, EVAL_END)
        data = [
            {"timestamp": "2026-01-15T00:00:00+00:00", "v": 1},
            {"timestamp": "2026-02-15T00:00:00+00:00", "v": 2},
            {"timestamp": "2026-02-25T00:00:00+00:00", "v": 3},
        ]
        ev = sep.get_eval_data(data)
        assert len(ev) == 2
        assert {r["v"] for r in ev} == {2, 3}

    def test_custom_timestamp_field(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_START, EVAL_END)
        data = [{"ts": "2026-01-15T00:00:00+00:00", "v": 1}]
        train = sep.get_train_data(data, timestamp_field="ts")
        assert len(train) == 1

    def test_invalid_timestamp_raises(self) -> None:
        sep = TrainEvalSeparator(TRAIN_START, TRAIN_END, EVAL_START, EVAL_END)
        with pytest.raises(ValueError):
            sep.is_in_train_period("not-a-date")


# ---------------------------------------------------------------------------
# FutureLabelGuard
# ---------------------------------------------------------------------------


class TestFutureLabelGuard:
    def test_horizon_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            FutureLabelGuard(horizon=0)

    def test_safe_access(self) -> None:
        guard = FutureLabelGuard(horizon=1)
        assert guard.check_reward_access(
            "2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"
        ) is True

    def test_label_equals_reward_time_safe(self) -> None:
        guard = FutureLabelGuard(horizon=1)
        assert guard.check_reward_access(
            "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"
        ) is True

    def test_future_label_rejected(self) -> None:
        guard = FutureLabelGuard(horizon=1)
        with pytest.raises(ValueError):
            guard.check_reward_access(
                "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"
            )

    def test_batch_no_leakage(self) -> None:
        guard = FutureLabelGuard(horizon=1)
        rewards = [0.1, 0.2, 0.3]
        labels = [0.0, 0.1, 0.2]
        reward_times = [
            "2026-01-03T00:00:00+00:00",
            "2026-01-04T00:00:00+00:00",
            "2026-01-05T00:00:00+00:00",
        ]
        label_times = [
            "2026-01-02T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
            "2026-01-04T00:00:00+00:00",
        ]
        assert (
            guard.validate_no_future_leakage(
                rewards, labels, reward_times, label_times
            )
            is True
        )

    def test_batch_future_leakage_rejected(self) -> None:
        guard = FutureLabelGuard(horizon=1)
        rewards = [0.1, 0.2]
        labels = [0.0, 0.1]
        reward_times = [
            "2026-01-01T00:00:00+00:00",
            "2026-01-04T00:00:00+00:00",
        ]
        label_times = [
            "2026-01-02T00:00:00+00:00",  # future label
            "2026-01-03T00:00:00+00:00",
        ]
        with pytest.raises(ValueError):
            guard.validate_no_future_leakage(
                rewards, labels, reward_times, label_times
            )

    def test_batch_length_mismatch(self) -> None:
        guard = FutureLabelGuard(horizon=1)
        with pytest.raises(ValueError):
            guard.validate_no_future_leakage(
                [0.1, 0.2], [0.0], ["t1"], ["t2"]
            )

    def test_batch_empty_safe(self) -> None:
        guard = FutureLabelGuard(horizon=1)
        assert (
            guard.validate_no_future_leakage([], [], [], []) is True
        )


# ---------------------------------------------------------------------------
# RLShadowPolicy — train / evaluate / save / load
# ---------------------------------------------------------------------------


class TestRLShadowPolicy:
    def test_construct(self) -> None:
        cfg = _make_config()
        policy = RLShadowPolicy(cfg)
        assert policy.config is cfg
        assert policy._n_assets == 3

    def test_train_returns_shadow_result(self) -> None:
        cfg = _make_config(
            n_train_episodes=2, n_eval_episodes=2, n_timesteps=4
        )
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        assert isinstance(result, RLShadowResult)
        assert result.promotion_eligible is False
        assert result.n_train_episodes == 2
        assert result.n_eval_episodes == 2
        assert result.policy_checkpoint is not None
        assert result.policy_checkpoint.policy_type == "ppo"
        assert len(result.rollout_logs) == 2
        assert result.duration_seconds >= 0.0

    def test_train_reward_is_float(self) -> None:
        cfg = _make_config(n_train_episodes=1, n_eval_episodes=1, n_timesteps=3)
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        assert isinstance(result.train_reward, float)
        assert isinstance(result.eval_reward, float)

    def test_train_metrics_present(self) -> None:
        cfg = _make_config(n_train_episodes=1, n_eval_episodes=1, n_timesteps=3)
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        for key in ("sharpe", "max_drawdown", "avg_turnover", "total_return"):
            assert key in result.metrics

    def test_evaluate_returns_metrics(self) -> None:
        cfg = _make_config(n_eval_episodes=2, n_timesteps=4)
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        metrics = policy.evaluate(data)
        assert "sharpe" in metrics
        assert "max_drawdown" in metrics
        assert "avg_turnover" in metrics
        assert metrics["n_steps"] >= 0.0

    def test_evaluate_filters_to_eval_period(self) -> None:
        cfg = _make_config(n_eval_episodes=1, n_timesteps=3)
        policy = RLShadowPolicy(cfg)
        # Only train-period data: eval should still run on simulator.
        data = [
            {"timestamp": "2026-01-05T00:00:00+00:00", "returns": [0.0] * 3}
        ]
        metrics = policy.evaluate(data)
        # Eval data is empty but episodes still run on the simulator.
        assert metrics["n_steps"] >= 0.0

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        cfg = _make_config(n_train_episodes=1, n_eval_episodes=1, n_timesteps=3)
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        out = tmp_path / "result.json"
        policy.save_result(result, str(out))
        assert out.exists()
        loaded = policy.load_result(str(out))
        assert loaded == result

    def test_load_result_missing_file(self, tmp_path: Path) -> None:
        cfg = _make_config()
        policy = RLShadowPolicy(cfg)
        with pytest.raises(FileNotFoundError):
            policy.load_result(str(tmp_path / "nope.json"))

    def test_random_policy_type(self) -> None:
        cfg = _make_config(
            policy_type="random", n_train_episodes=1, n_eval_episodes=1, n_timesteps=3
        )
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        assert result.config.policy_type == "random"
        assert result.policy_checkpoint is not None
        assert result.policy_checkpoint.policy_type == "random"

    def test_single_episode(self) -> None:
        cfg = _make_config(
            n_train_episodes=1, n_eval_episodes=1, n_timesteps=2
        )
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        assert result.n_train_episodes == 1
        assert result.n_eval_episodes == 1

    def test_single_asset(self) -> None:
        cfg = _make_config(
            n_assets=1, n_timesteps=3,
            n_train_episodes=1, n_eval_episodes=1,
        )
        # max_weight must allow 1 asset to sum to 1.0.
        cfg = cfg.model_copy(
            update={
                "env_manifest": _make_manifest(
                    n_assets=1, n_timesteps=3, risk_limits={"max_weight": 1.0, "max_turnover": 1.0}
                )
            }
        )
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        assert result.policy_checkpoint is not None

    def test_minimal_data(self) -> None:
        cfg = _make_config(n_train_episodes=1, n_eval_episodes=1, n_timesteps=2)
        policy = RLShadowPolicy(cfg)
        data = [
            {"timestamp": "2026-01-05T00:00:00+00:00", "returns": [0.0] * 3},
            {"timestamp": "2026-02-15T00:00:00+00:00", "returns": [0.0] * 3},
        ]
        result = policy.train(data)
        assert result.promotion_eligible is False

    def test_train_validates_separation_fail_closed(self) -> None:
        # Build a valid config then construct a separator that overlaps.
        cfg = _make_config()
        policy = RLShadowPolicy(cfg)
        # Tamper with the separator to simulate an overlap at runtime.
        policy.separator.eval_start = _parse_dt(TRAIN_START)
        with pytest.raises(ValueError):
            policy.train(_make_returns_data(n=60))


def _parse_dt(ts: str) -> datetime:
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


class TestValidatePromotionEligibility:
    def test_default_not_eligible(self) -> None:
        result = _make_result()
        assert validate_promotion_eligibility(result) is False

    def test_manual_override_eligible(self) -> None:
        result = _make_result()
        assert (
            validate_promotion_eligibility(result, manual_override=True)
            is True
        )

    def test_result_promotion_eligible_always_false(self) -> None:
        result = _make_result()
        assert result.promotion_eligible is False


# ---------------------------------------------------------------------------
# Future-label access validation
# ---------------------------------------------------------------------------


class TestValidateNoFutureLabelAccess:
    def test_safe_result(self) -> None:
        result = _make_result(metrics={"sharpe": 1.0})
        assert validate_no_future_label_access(result) is True

    def test_future_label_access_rejected(self) -> None:
        result = _make_result(metrics={"future_label_access": 1.0})
        with pytest.raises(ValueError):
            validate_no_future_label_access(result)

    def test_empty_metrics_safe(self) -> None:
        result = _make_result(metrics={})
        assert validate_no_future_label_access(result) is True


# ---------------------------------------------------------------------------
# Family registration
# ---------------------------------------------------------------------------


class TestRegisterRLShadowFamily:
    def test_returns_dict(self) -> None:
        spec = register_rl_shadow_family()
        assert isinstance(spec, dict)

    def test_family_id(self) -> None:
        spec = register_rl_shadow_family()
        assert spec["family_id"] == "rl_shadow"

    def test_shadow_only_true(self) -> None:
        spec = register_rl_shadow_family()
        assert spec["shadow_only"] is True

    def test_required_fields_present(self) -> None:
        spec = register_rl_shadow_family()
        for key in (
            "family_id",
            "display_name",
            "version",
            "dataset_shape",
            "objectives",
            "artifact_format",
            "artifact_loader",
            "required_metrics",
            "promotion_eligibility_class",
            "is_baseline_exception",
            "created_at_ns",
        ):
            assert key in spec, f"missing key {key}"

    def test_enforcement_flags(self) -> None:
        spec = register_rl_shadow_family()
        assert spec["enforces_train_eval_separation"] is True
        assert spec["enforces_future_label_guard"] is True

    def test_policy_types(self) -> None:
        spec = register_rl_shadow_family()
        assert set(spec["policy_types"]) == {"ppo", "a2c", "dqn", "random"}

    def test_not_baseline_exception(self) -> None:
        spec = register_rl_shadow_family()
        assert spec["is_baseline_exception"] is False


# ---------------------------------------------------------------------------
# Integration: end-to-end train -> save -> load -> validate
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_end_to_end(self, tmp_path: Path) -> None:
        cfg = _make_config(
            n_train_episodes=2, n_eval_episodes=2, n_timesteps=4
        )
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        # Save / load.
        out = tmp_path / "shadow.json"
        policy.save_result(result, str(out))
        loaded = policy.load_result(str(out))
        assert loaded == result
        # Validate promotion + future-label.
        assert validate_promotion_eligibility(loaded) is False
        assert validate_no_future_label_access(loaded) is True
        # The saved JSON is valid and round-trips.
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["promotion_eligible"] is False
        assert payload["config"]["shadow_only"] is True

    def test_result_json_contains_manifest(self, tmp_path: Path) -> None:
        cfg = _make_config(n_train_episodes=1, n_eval_episodes=1, n_timesteps=3)
        policy = RLShadowPolicy(cfg)
        data = _make_returns_data(n=60)
        result = policy.train(data)
        out = tmp_path / "shadow.json"
        policy.save_result(result, str(out))
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["env_manifest"]["env_id"] == "test-env"
        assert payload["policy_checkpoint"] is not None
        assert len(payload["rollout_logs"]) >= 1
