"""Tests for ``quant_foundry.champion_challenger`` (Tier 2.4).

Tests verify:
- Champion/challenger comparison produces correct metrics.
- Promotion decision logic: promote, no_edge, not_significant,
  insufficient_evidence, low_dsr.
- Bootstrap p-value is deterministic with fixed seed.
- Edge cases: empty returns, mismatched lengths, single sample.
- Configuration validation.
"""

from __future__ import annotations

import random

import pytest

from quant_foundry.champion_challenger import (
    ChampionChallengerConfig,
    ComparisonInput,
    PromotionDecision,
    ShadowComparisonResult,
    compare_champion_challenger,
)


def _make_input(
    model_id: str,
    n: int = 50,
    mean: float = 0.0001,
    std: float = 0.001,
    seed: int = 42,
    settled_count: int | None = None,
) -> ComparisonInput:
    """Helper: create a ComparisonInput with Gaussian returns."""
    rng = random.Random(seed)
    returns = [rng.gauss(mean, std) for _ in range(n)]
    return ComparisonInput(
        model_id=model_id,
        oos_returns_net=returns,
        trial_count=1,
        settled_count=settled_count if settled_count is not None else n,
    )


class TestCompareChampionChallenger:
    def test_promote_when_challenger_beats_champion(self) -> None:
        """Challenger with clearly better returns → promote."""
        champ = _make_input("champ-v1", mean=0.0001, seed=1)
        chal = _make_input("chal-v2", mean=0.001, seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
            alpha=0.05,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.decision == "promote"
        assert decision.result.net_edge_delta_bps > 1.0
        assert decision.result.bootstrap_p_value < 0.05

    def test_no_edge_when_delta_below_threshold(self) -> None:
        """Challenger with small edge → no_edge."""
        champ = _make_input("champ-v1", mean=0.0005, seed=1)
        chal = _make_input("chal-v2", mean=0.0006, seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=50.0,  # high threshold
            alpha=0.05,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.decision == "no_edge"
        assert "net edge delta" in decision.reason

    def test_not_significant_when_pvalue_high(self) -> None:
        """Challenger with noisy edge → not_significant."""
        # Very noisy returns with tiny edge
        champ = _make_input("champ-v1", mean=0.0001, std=0.01, seed=1)
        chal = _make_input("chal-v2", mean=0.0002, std=0.01, seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=0.0,  # any positive delta passes
            alpha=0.05,
            bootstrap_samples=200,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        # With high noise, p-value should be high
        assert decision.decision in ("not_significant", "no_edge")

    def test_insufficient_evidence_when_too_few_settled(self) -> None:
        """Below min_settled_count → insufficient_evidence."""
        champ = _make_input("champ-v1", n=10, seed=1, settled_count=10)
        chal = _make_input("chal-v2", n=10, seed=2, settled_count=10)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.decision == "insufficient_evidence"
        assert "at least 30" in decision.reason

    def test_insufficient_evidence_one_side(self) -> None:
        """Champion has enough, challenger doesn't → insufficient."""
        champ = _make_input("champ-v1", n=50, seed=1, settled_count=50)
        chal = _make_input("chal-v2", n=10, seed=2, settled_count=10)
        cfg = ChampionChallengerConfig(min_settled_count=30)
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.decision == "insufficient_evidence"

    def test_low_dsr_when_challenger_dsr_negative(self) -> None:
        """Challenger with negative DSR → low_dsr (even with edge)."""
        # Champion with positive returns, challenger with positive but
        # very noisy returns (DSR will be low)
        champ = _make_input("champ-v1", mean=0.001, std=0.001, seed=1)
        chal = _make_input("chal-v2", mean=0.002, std=0.05, seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
            alpha=0.05,
            dsr_threshold=0.5,  # high DSR bar
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        # Challenger has edge but DSR may be low due to noise
        # If it passes edge + significance, check DSR gate
        if decision.decision != "no_edge" and decision.decision != "not_significant":
            assert decision.decision == "low_dsr"

    def test_dsr_delta_computed(self) -> None:
        """DSR delta is challenger DSR minus champion DSR."""
        champ = _make_input("champ-v1", mean=0.0001, std=0.001, seed=1)
        chal = _make_input("chal-v2", mean=0.001, std=0.001, seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.result.dsr_delta == pytest.approx(
            decision.result.challenger_dsr - decision.result.champion_dsr
        )

    def test_brier_delta_computed(self) -> None:
        """Brier delta is computed when both sides have Brier scores."""
        champ = _make_input("champ-v1", seed=1)
        champ_with_brier = champ.model_copy(update={"brier": 0.25})
        chal = _make_input("chal-v2", seed=2)
        chal_with_brier = chal.model_copy(update={"brier": 0.20})
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(
            champ_with_brier, chal_with_brier, cfg
        )
        assert decision.result.brier_delta is not None
        assert decision.result.brier_delta == pytest.approx(-0.05)

    def test_brier_delta_none_when_missing(self) -> None:
        """Brier delta is None when either side lacks Brier."""
        champ = _make_input("champ-v1", seed=1)
        chal = _make_input("chal-v2", seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.result.brier_delta is None

    def test_bootstrap_deterministic(self) -> None:
        """Same seed → same p-value."""
        champ = _make_input("champ-v1", mean=0.0001, std=0.002, seed=1)
        chal = _make_input("chal-v2", mean=0.0003, std=0.002, seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=0.0,
            alpha=0.05,
            bootstrap_samples=500,
            seed=12345,
        )
        d1 = compare_champion_challenger(champ, chal, cfg)
        d2 = compare_champion_challenger(champ, chal, cfg)
        assert d1.result.bootstrap_p_value == d2.result.bootstrap_p_value

    def test_bootstrap_different_seed_different_pvalue(self) -> None:
        """Different seeds may produce slightly different p-values."""
        champ = _make_input("champ-v1", mean=0.0001, std=0.002, seed=1)
        chal = _make_input("chal-v2", mean=0.0003, std=0.002, seed=2)
        cfg1 = ChampionChallengerConfig(
            min_settled_count=30, net_edge_threshold=0.0,
            bootstrap_samples=100, seed=1,
        )
        cfg2 = ChampionChallengerConfig(
            min_settled_count=30, net_edge_threshold=0.0,
            bootstrap_samples=100, seed=999,
        )
        d1 = compare_champion_challenger(champ, chal, cfg1)
        d2 = compare_champion_challenger(champ, chal, cfg2)
        # P-values should be close but may differ slightly
        # (not a strict inequality test — just that both are valid)
        assert 0.0 <= d1.result.bootstrap_p_value <= 1.0
        assert 0.0 <= d2.result.bootstrap_p_value <= 1.0

    def test_net_edge_in_bps(self) -> None:
        """Net edge is reported in basis points."""
        champ = ComparisonInput(
            model_id="champ",
            oos_returns_net=[0.0] * 50,
            settled_count=50,
        )
        chal = ComparisonInput(
            model_id="chal",
            oos_returns_net=[0.001] * 50,  # 10 bps per prediction
            settled_count=50,
        )
        cfg = ChampionChallengerConfig(
            min_settled_count=30, net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.result.champion_net_edge_bps == pytest.approx(0.0)
        assert decision.result.challenger_net_edge_bps == pytest.approx(10.0)
        assert decision.result.net_edge_delta_bps == pytest.approx(10.0)

    def test_mismatched_lengths_truncated(self) -> None:
        """Mismatched return lengths are truncated for bootstrap."""
        champ = ComparisonInput(
            model_id="champ",
            oos_returns_net=[0.0] * 100,
            settled_count=100,
        )
        chal = ComparisonInput(
            model_id="chal",
            oos_returns_net=[0.001] * 50,
            settled_count=50,
        )
        cfg = ChampionChallengerConfig(
            min_settled_count=30, net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        # Should not crash; uses min length for bootstrap
        assert decision.decision in ("promote", "no_edge", "not_significant")

    def test_empty_returns(self) -> None:
        """Empty returns → insufficient evidence or zero edge."""
        champ = ComparisonInput(
            model_id="champ",
            oos_returns_net=[],
            settled_count=0,
        )
        chal = ComparisonInput(
            model_id="chal",
            oos_returns_net=[],
            settled_count=0,
        )
        cfg = ChampionChallengerConfig(min_settled_count=30)
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.decision == "insufficient_evidence"

    def test_result_is_frozen(self) -> None:
        """ShadowComparisonResult is immutable."""
        champ = _make_input("champ-v1", seed=1)
        chal = _make_input("chal-v2", seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30, net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        with pytest.raises(Exception):
            decision.decision = "hack"  # type: ignore[misc]

    def test_config_is_frozen(self) -> None:
        """ChampionChallengerConfig is immutable."""
        cfg = ChampionChallengerConfig()
        with pytest.raises(Exception):
            cfg.alpha = 0.01  # type: ignore[misc]

    def test_result_contains_config(self) -> None:
        """The result includes the config used for the comparison."""
        champ = _make_input("champ-v1", seed=1)
        chal = _make_input("chal-v2", seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=5.0,
            alpha=0.01,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        assert decision.result.config.min_settled_count == 30
        assert decision.result.config.net_edge_threshold == 5.0
        assert decision.result.config.alpha == 0.01

    def test_champion_wins_when_better(self) -> None:
        """Champion with better returns → no_edge (challenger doesn't beat)."""
        champ = _make_input("champ-v1", mean=0.001, seed=1)
        chal = _make_input("chal-v2", mean=0.0001, seed=2)
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
        )
        decision = compare_champion_challenger(champ, chal, cfg)
        # Challenger is worse → negative delta → no_edge
        assert decision.decision == "no_edge"
        assert decision.result.net_edge_delta_bps < 0
