"""
Tests for TASK-0404: Tournament Scoring Skeleton.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `tournament.py`,
`leaderboard.py`, and `significance.py` exist.

Acceptance criteria covered (one or more tests per criterion):
- Two fixture models rank deterministically.
- A model with high ML score but poor cost-adjusted return loses to a simpler
  profitable one.
- A noise/shuffled-label model fails the gate (negative control).
- A model that beats baseline gross but not net-of-cost is blocked.
- Deflated Sharpe and the bootstrap p-value are recorded and shown.
- Stale or insufficient evidence blocks promotion recommendation.
- Tournament output can feed a promotion packet later.

Cross-cutting rigor covered:
- §2: Deflated Sharpe Ratio (discounts for trial count + return non-normality).
- §4: cost governance — ranking is on NET edge, not gross.
- Significance test vs. baseline uses a stationary/block bootstrap (respects
  horizon-overlap autocorrelation), NOT an IID t-test.
"""

from __future__ import annotations

import statistics
from typing import Any

import pytest

# These imports will fail in the red phase (modules do not exist yet).
from quant_foundry.leaderboard import (
    Leaderboard,
    PromotionRecommendation,
    TournamentResult,
)
from quant_foundry.significance import (
    DeflatedSharpeResult,
    deflated_sharpe_ratio,
    stationary_bootstrap_pvalue,
)
from quant_foundry.tournament import (
    BaselineKind,
    ScoreComponent,
    ScoringInput,
    Tournament,
    TournamentStatus,
)

# ---------------------------------------------------------------------------
# Fixtures — deterministic, no randomness in the test inputs themselves.
# ---------------------------------------------------------------------------


def _make_scoring_input(
    model_id: str,
    oos_returns_net: list[float],
    *,
    oos_returns_gross: list[float] | None = None,
    oos_returns_baseline: list[float] | None = None,
    trial_count: int = 1,
    brier: float | None = None,
    calibration_buckets: list[tuple[str, int, int]] | None = None,
    confidence_buckets: list[tuple[str, float, float]] | None = None,
    max_drawdown: float = 0.0,
    turnover: float | None = None,
    feature_availability_ratio: float = 1.0,
    latency_ms: float | None = None,
    capacity_decay_penalty: float = 0.0,
    settled_count: int | None = None,
    last_settled_at_ns: int | None = None,
    now_ns: int = 1_000_000_000,
    stale_threshold_ns: int = 10_000_000_000,
    min_settled_samples: int = 10,
    cost_model_version: str = "cm-v1",
    training_accuracy: float | None = None,
) -> ScoringInput:
    """Build a ScoringInput with sensible defaults for tests."""
    kwargs: dict[str, Any] = {
        "model_id": model_id,
        "oos_returns_net": list(oos_returns_net),
        "oos_returns_gross": list(oos_returns_gross)
        if oos_returns_gross is not None
        else list(oos_returns_net),
        "oos_returns_baseline": list(oos_returns_baseline)
        if oos_returns_baseline is not None
        else [0.0] * len(oos_returns_net),
        "trial_count": trial_count,
        "max_drawdown": max_drawdown,
        "feature_availability_ratio": feature_availability_ratio,
        "capacity_decay_penalty": capacity_decay_penalty,
        "settled_count": settled_count if settled_count is not None else len(oos_returns_net),
        "now_ns": now_ns,
        "stale_threshold_ns": stale_threshold_ns,
        "min_settled_samples": min_settled_samples,
        "cost_model_version": cost_model_version,
    }
    # Only pass optional fields when they have a value (pydantic list/tuple
    # fields with default_factory reject explicit None).
    if brier is not None:
        kwargs["brier"] = brier
    if calibration_buckets is not None:
        kwargs["calibration_buckets"] = calibration_buckets
    if confidence_buckets is not None:
        kwargs["confidence_buckets"] = confidence_buckets
    if turnover is not None:
        kwargs["turnover"] = turnover
    if latency_ms is not None:
        kwargs["latency_ms"] = latency_ms
    if last_settled_at_ns is not None:
        kwargs["last_settled_at_ns"] = last_settled_at_ns
    if training_accuracy is not None:
        kwargs["training_accuracy"] = training_accuracy
    return ScoringInput(**kwargs)


# A simple profitable model: small positive net edge every period.
_PROFITABLE_NET = [0.001, 0.002, 0.001, 0.002, 0.001, 0.002, 0.001, 0.002,
                   0.001, 0.002, 0.001, 0.002, 0.001, 0.002, 0.001, 0.002]

# A high-ML-score model with poor cost-adjusted return: high gross but net is
# near zero / negative because costs eat the edge.
_HIGH_ML_GROSS = [0.005, 0.006, 0.005, 0.006, 0.005, 0.006, 0.005, 0.006,
                  0.005, 0.006, 0.005, 0.006, 0.005, 0.006, 0.005, 0.006]
_HIGH_ML_NET = [0.0001, -0.0001, 0.0001, -0.0001, 0.0001, -0.0001,
                0.0001, -0.0001, 0.0001, -0.0001, 0.0001, -0.0001,
                0.0001, -0.0001, 0.0001, -0.0001]

# Pure noise: shuffled-label model. Net returns are i.i.d. zero-mean noise.
_NOISE_NET = [0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0.002, -0.002,
              0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0.002, -0.002]

# Beats baseline gross but not net: gross positive, net negative (costs dominate).
_GROSS_POS_NET_NEG_GROSS = [0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003,
                            0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003]
_GROSS_POS_NET_NEG_NET = [-0.001, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001,
                          -0.001, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001]


# ===========================================================================
# significance.py — Deflated Sharpe Ratio + stationary bootstrap p-value
# ===========================================================================


class TestDeflatedSharpeRatio:
    """DSR discounts for trial count + return non-normality (rigor §2)."""

    def test_dsr_returns_result_with_dsr_sharpe_and_trial_count(self) -> None:
        result = deflated_sharpe_ratio(
            oos_returns=_PROFITABLE_NET, trial_count=5
        )
        assert isinstance(result, DeflatedSharpeResult)
        assert result.trial_count == 5
        # Raw Sharpe is reported alongside the deflated one.
        assert hasattr(result, "raw_sharpe")
        assert hasattr(result, "deflated_sharpe")
        # DSR <= raw Sharpe always (deflation only discounts).
        assert result.deflated_sharpe <= result.raw_sharpe + 1e-12

    def test_dsr_more_trials_lowers_deflated_sharpe(self) -> None:
        """More trials => more deflation => lower DSR for the same returns."""
        r_few = deflated_sharpe_ratio(_PROFITABLE_NET, trial_count=1)
        r_many = deflated_sharpe_ratio(_PROFITABLE_NET, trial_count=100)
        assert r_many.deflated_sharpe < r_few.deflated_sharpe + 1e-12

    def test_dsr_zero_mean_returns_gives_nonpositive_sharpe(self) -> None:
        r = deflated_sharpe_ratio([0.0, 0.0, 0.0, 0.0], trial_count=1)
        assert r.raw_sharpe == 0.0
        assert r.deflated_sharpe <= 0.0

    def test_dsr_negative_mean_returns_gives_negative_sharpe(self) -> None:
        r = deflated_sharpe_ratio(
            [-0.001, -0.002, -0.001, -0.002], trial_count=1
        )
        assert r.raw_sharpe < 0.0
        assert r.deflated_sharpe < 0.0

    def test_dsr_handles_non_normality(self) -> None:
        """Skewed/fat-tailed returns should produce a non-normality penalty."""
        # Heavy positive outlier in one period.
        fat_tailed = [0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.05,
                      0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.001]
        r = deflated_sharpe_ratio(fat_tailed, trial_count=1)
        assert hasattr(r, "skew")
        assert hasattr(r, "kurtosis")
        # The deflation must account for non-normality (penalty term > 0 when
        # skew/kurtosis are non-trivial).
        assert r.deflated_sharpe <= r.raw_sharpe


class TestStationaryBootstrapPValue:
    """Bootstrap p-value vs. baseline respects autocorrelation (rigor §2)."""

    def test_pvalue_returns_result_with_pvalue_and_trial_count(self) -> None:
        result = stationary_bootstrap_pvalue(
            model_returns=_PROFITABLE_NET,
            baseline_returns=[0.0] * len(_PROFITABLE_NET),
            trial_count=5,
            n_bootstrap=200,
            seed=42,
        )
        assert hasattr(result, "p_value")
        assert hasattr(result, "trial_count")
        assert 0.0 <= result.p_value <= 1.0
        assert result.trial_count == 5

    def test_pvalue_significant_for_clearly_beating_baseline(self) -> None:
        """A model that consistently beats zero should have a small p-value."""
        result = stationary_bootstrap_pvalue(
            model_returns=_PROFITABLE_NET,
            baseline_returns=[0.0] * len(_PROFITABLE_NET),
            trial_count=1,
            n_bootstrap=500,
            seed=42,
        )
        assert result.p_value < 0.05

    def test_pvalue_not_significant_for_noise_vs_baseline(self) -> None:
        """A noise model should NOT show a significant p-value vs zero."""
        result = stationary_bootstrap_pvalue(
            model_returns=_NOISE_NET,
            baseline_returns=[0.0] * len(_NOISE_NET),
            trial_count=1,
            n_bootstrap=500,
            seed=42,
        )
        assert result.p_value > 0.05

    def test_pvalue_deterministic_with_fixed_seed(self) -> None:
        """Same seed + inputs => same p-value (deterministic tests)."""
        r1 = stationary_bootstrap_pvalue(
            _PROFITABLE_NET, [0.0] * len(_PROFITABLE_NET),
            trial_count=1, n_bootstrap=200, seed=7,
        )
        r2 = stationary_bootstrap_pvalue(
            _PROFITABLE_NET, [0.0] * len(_PROFITABLE_NET),
            trial_count=1, n_bootstrap=200, seed=7,
        )
        assert r1.p_value == r2.p_value


# ===========================================================================
# tournament.py — ScoringInput, baselines, scoring, gating
# ===========================================================================


class TestScoringInput:
    """ScoringInput carries the OOS return series + trial count (rigor §2)."""

    def test_scoring_input_carries_oos_series_and_trial_count(self) -> None:
        si = _make_scoring_input("m1", _PROFITABLE_NET, trial_count=3)
        assert si.model_id == "m1"
        assert si.oos_returns_net == _PROFITABLE_NET
        assert si.trial_count == 3
        # The series length must match (bootstrap needs the series).
        assert len(si.oos_returns_net) == len(_PROFITABLE_NET)

    def test_scoring_input_rejects_mismatched_series_lengths(self) -> None:
        with pytest.raises(ValueError):
            ScoringInput(
                model_id="m1",
                oos_returns_net=_PROFITABLE_NET,
                oos_returns_gross=_PROFITABLE_NET[:5],  # wrong length
                oos_returns_baseline=[0.0] * len(_PROFITABLE_NET),
                trial_count=1,
                settled_count=len(_PROFITABLE_NET),
                now_ns=1, stale_threshold_ns=10, min_settled_samples=10,
            )

    def test_scoring_input_rejects_empty_model_id(self) -> None:
        with pytest.raises(ValueError):
            ScoringInput(
                model_id="",
                oos_returns_net=_PROFITABLE_NET,
                oos_returns_gross=_PROFITABLE_NET,
                oos_returns_baseline=[0.0] * len(_PROFITABLE_NET),
                trial_count=1,
                settled_count=len(_PROFITABLE_NET),
                now_ns=1, stale_threshold_ns=10, min_settled_samples=10,
            )


class TestBaselines:
    """Deterministic baseline comparison (zero-skill / persistence / buy-hold)."""

    def test_zero_skill_baseline_is_all_zeros(self) -> None:
        t = Tournament()
        baseline = t.compute_baseline(_PROFITABLE_NET, BaselineKind.ZERO_SKILL)
        assert baseline == [0.0] * len(_PROFITABLE_NET)

    def test_persistence_baseline_is_lag_one(self) -> None:
        """Naive persistence: predict last value (return[t] = return[t-1])."""
        t = Tournament()
        baseline = t.compute_baseline(_PROFITABLE_NET, BaselineKind.PERSISTENCE)
        # persistence[0] is 0 (no prior); persistence[t] = returns[t-1].
        assert baseline[0] == 0.0
        for i in range(1, len(_PROFITABLE_NET)):
            assert baseline[i] == _PROFITABLE_NET[i - 1]

    def test_buy_and_hold_baseline_is_constant_mean(self) -> None:
        """Buy-and-hold: constant return equal to the mean of the model series."""
        t = Tournament()
        baseline = t.compute_baseline(_PROFITABLE_NET, BaselineKind.BUY_AND_HOLD)
        mean_ret = statistics.mean(_PROFITABLE_NET)
        assert all(abs(b - mean_ret) < 1e-12 for b in baseline)


class TestTournamentScoring:
    """Explainable weighted score over the components (rigor §4: net, not gross)."""

    def test_score_components_are_recorded(self) -> None:
        t = Tournament(seed=42, n_bootstrap=200)
        result = t.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=1))
        assert isinstance(result, TournamentResult)
        # Every score component is a named, auditable entry.
        assert len(result.score_components) > 0
        names = {c.name for c in result.score_components}
        # Must include net edge, DSR, calibration, drawdown at minimum.
        assert "net_edge" in names
        assert "deflated_sharpe" in names
        assert "drawdown_penalty" in names

    def test_score_is_deterministic_with_fixed_seed(self) -> None:
        t1 = Tournament(seed=42, n_bootstrap=200)
        t2 = Tournament(seed=42, n_bootstrap=200)
        r1 = t1.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=1))
        r2 = t2.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=1))
        assert r1.total_score == r2.total_score
        assert r1.p_value == r2.p_value

    def test_high_ml_poor_cost_loses_to_simple_profitable(self) -> None:
        """Acceptance: high-ML-score / poor-cost-return loses to simpler profitable."""
        t = Tournament(seed=42, n_bootstrap=200)
        simple = t.score(_make_scoring_input("simple", _PROFITABLE_NET, trial_count=1))
        high_ml = t.score(
            _make_scoring_input(
                "high_ml", _HIGH_ML_NET, oos_returns_gross=_HIGH_ML_GROSS,
                training_accuracy=0.95, trial_count=1,
            )
        )
        # The simpler profitable model must rank higher (higher total_score).
        assert simple.total_score > high_ml.total_score
        # The high-ML model's net edge must be near zero / negative.
        net_edge_high = next(
            c for c in high_ml.score_components if c.name == "net_edge"
        )
        assert net_edge_high.value <= 0.0002

    def test_beats_baseline_gross_but_not_net_is_blocked(self) -> None:
        """Acceptance: beats baseline gross but not net-of-cost is blocked."""
        t = Tournament(seed=42, n_bootstrap=200)
        result = t.score(
            _make_scoring_input(
                "gross_pos_net_neg",
                _GROSS_POS_NET_NEG_NET,
                oos_returns_gross=_GROSS_POS_NET_NEG_GROSS,
                trial_count=1,
            )
        )
        # Net edge is negative => a blocking issue must be raised.
        assert result.status == TournamentStatus.BLOCKED
        blocking_msgs = [b.get("code", "") for b in result.blocking_issues]
        assert any("net_of_cost" in m or "net_edge" in m for m in blocking_msgs)

    def test_noise_model_fails_gate(self) -> None:
        """Acceptance: noise/shuffled-label model fails the gate (negative control)."""
        t = Tournament(seed=42, n_bootstrap=500)
        result = t.score(
            _make_scoring_input("noise", _NOISE_NET, trial_count=10)
        )
        # A noise model must NOT be recommended for promotion.
        assert result.recommendation != PromotionRecommendation.PROMOTE
        # It should be blocked or insufficient-evidence.
        assert result.status in (
            TournamentStatus.BLOCKED,
            TournamentStatus.INSUFFICIENT_EVIDENCE,
        )

    def test_dsr_and_pvalue_recorded_and_shown(self) -> None:
        """Acceptance: Deflated Sharpe and bootstrap p-value are recorded and shown."""
        t = Tournament(seed=42, n_bootstrap=200)
        result = t.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=3))
        assert result.deflated_sharpe is not None
        assert result.p_value is not None
        # Both must appear in the score components (auditable).
        names = {c.name for c in result.score_components}
        assert "deflated_sharpe" in names
        # p_value is recorded on the result (shown to operator).
        assert 0.0 <= result.p_value <= 1.0


class TestTournamentGating:
    """Stale evidence + minimum-settled-sample gate."""

    def test_insufficient_settled_samples_blocks(self) -> None:
        """A model with too few settled predictions is insufficient-evidence."""
        t = Tournament(seed=42, n_bootstrap=200)
        # Only 3 settled samples, min is 10.
        result = t.score(
            _make_scoring_input(
                "m1", [0.001, 0.002, 0.001], settled_count=3, min_settled_samples=10,
            )
        )
        assert result.status == TournamentStatus.INSUFFICIENT_EVIDENCE
        assert result.recommendation == PromotionRecommendation.HOLD

    def test_stale_evidence_blocks_promotion(self) -> None:
        """Stale evidence (last_settled too old) blocks promotion."""
        t = Tournament(seed=42, n_bootstrap=200)
        # last_settled at ns=1, now=100, stale threshold=10 => stale.
        result = t.score(
            _make_scoring_input(
                "m1", _PROFITABLE_NET, trial_count=1,
                last_settled_at_ns=1, now_ns=100, stale_threshold_ns=10,
            )
        )
        assert result.status == TournamentStatus.STALE
        assert result.recommendation != PromotionRecommendation.PROMOTE
        blocking_msgs = [b.get("code", "") for b in result.blocking_issues]
        assert any("stale" in m for m in blocking_msgs)

    def test_fresh_sufficient_evidence_can_be_promoted(self) -> None:
        """A clearly profitable model with fresh, sufficient evidence can be promoted."""
        t = Tournament(seed=42, n_bootstrap=500)
        result = t.score(
            _make_scoring_input(
                "m1", _PROFITABLE_NET, trial_count=1,
                last_settled_at_ns=95, now_ns=100, stale_threshold_ns=10,
                min_settled_samples=10,
            )
        )
        # Must NOT be insufficient or stale.
        assert result.status != TournamentStatus.INSUFFICIENT_EVIDENCE
        assert result.status != TournamentStatus.STALE
        # A clearly profitable model should be recommended for promotion.
        assert result.recommendation == PromotionRecommendation.PROMOTE


class TestTournamentResultShape:
    """Tournament output can feed a promotion packet later (structured)."""

    def test_result_has_all_promotion_packet_fields(self) -> None:
        t = Tournament(seed=42, n_bootstrap=200)
        result = t.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=1))
        # Fields needed by a promotion packet:
        assert hasattr(result, "model_id")
        assert hasattr(result, "total_score")
        assert hasattr(result, "score_components")
        assert hasattr(result, "p_value")
        assert hasattr(result, "deflated_sharpe")
        assert hasattr(result, "blocking_issues")
        assert hasattr(result, "recommendation")
        assert hasattr(result, "status")
        assert hasattr(result, "trial_count")

    def test_score_component_is_auditable(self) -> None:
        """Each ScoreComponent carries name, value, weight, and contribution."""
        t = Tournament(seed=42, n_bootstrap=200)
        result = t.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=1))
        for c in result.score_components:
            assert isinstance(c, ScoreComponent)
            assert c.name
            assert hasattr(c, "value")
            assert hasattr(c, "weight")
            assert hasattr(c, "contribution")

    def test_result_to_dict_is_json_serializable(self) -> None:
        """The result can be serialized to feed a promotion packet."""
        import json

        t = Tournament(seed=42, n_bootstrap=200)
        result = t.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=1))
        d = result.to_dict()
        # Must be JSON serializable (no datetime, no custom objects).
        json.dumps(d)


# ===========================================================================
# leaderboard.py — Leaderboard ranking
# ===========================================================================


class TestLeaderboard:
    """Leaderboard ranks models by tournament score."""

    def test_two_models_rank_deterministically(self) -> None:
        """Acceptance: two fixture models rank deterministically."""
        t = Tournament(seed=42, n_bootstrap=200)
        r_simple = t.score(_make_scoring_input("simple", _PROFITABLE_NET, trial_count=1))
        r_high_ml = t.score(
            _make_scoring_input(
                "high_ml", _HIGH_ML_NET, oos_returns_gross=_HIGH_ML_GROSS,
                training_accuracy=0.95, trial_count=1,
            )
        )
        lb = Leaderboard()
        lb.add(r_simple)
        lb.add(r_high_ml)
        ranked = lb.ranked()
        assert len(ranked) == 2
        # Higher score first.
        assert ranked[0].total_score >= ranked[1].total_score
        # The simpler profitable model must be rank 1.
        assert ranked[0].model_id == "simple"

    def test_leaderboard_rank_order_matches_score_order(self) -> None:
        t = Tournament(seed=42, n_bootstrap=200)
        lb = Leaderboard()
        lb.add(t.score(_make_scoring_input("a", _PROFITABLE_NET, trial_count=1)))
        lb.add(t.score(_make_scoring_input("b", _HIGH_ML_NET, trial_count=1)))
        lb.add(t.score(_make_scoring_input("c", _NOISE_NET, trial_count=1)))
        ranked = lb.ranked()
        # Monotonically non-increasing total_score.
        scores = [r.total_score for r in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_leaderboard_excludes_insufficient_evidence_from_top(self) -> None:
        """Insufficient-evidence models are never ranked above sufficient ones."""
        t = Tournament(seed=42, n_bootstrap=200)
        lb = Leaderboard()
        lb.add(t.score(_make_scoring_input(
            "insufficient", [0.001, 0.002], settled_count=2, min_settled_samples=10,
        )))
        lb.add(t.score(_make_scoring_input("good", _PROFITABLE_NET, trial_count=1)))
        ranked = lb.ranked()
        # The good model must be rank 1.
        assert ranked[0].model_id == "good"
        # The insufficient model must be last (or excluded from promotion ranks).
        assert ranked[-1].model_id == "insufficient"
        assert ranked[-1].status == TournamentStatus.INSUFFICIENT_EVIDENCE

    def test_leaderboard_to_dict_for_promotion_packet(self) -> None:
        """The leaderboard can be serialized to feed a promotion packet."""
        import json

        t = Tournament(seed=42, n_bootstrap=200)
        lb = Leaderboard()
        lb.add(t.score(_make_scoring_input("a", _PROFITABLE_NET, trial_count=1)))
        d = lb.to_dict()
        json.dumps(d)
        assert "ranked" in d
        assert len(d["ranked"]) == 1


# ===========================================================================
# Cross-cutting: no secrets in tournament output
# ===========================================================================


class TestNoSecretsInTournamentOutput:
    """Tournament output must not leak secrets (cross-cutting security)."""

    @pytest.mark.parametrize("secret_field", [
        "api_key", "token", "secret", "password", "broker_account", "credential",
    ])
    def test_scoring_input_has_no_secret_fields(self, secret_field: str) -> None:
        """ScoringInput must not have any secret-named field."""
        si_fields = set(ScoringInput.model_fields.keys())
        assert secret_field not in si_fields

    def test_result_to_dict_has_no_secret_keys(self) -> None:
        t = Tournament(seed=42, n_bootstrap=200)
        result = t.score(_make_scoring_input("m1", _PROFITABLE_NET, trial_count=1))
        d = result.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password",
                        "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
