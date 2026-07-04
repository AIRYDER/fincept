"""Tests for quant_foundry.shadow_tournament (T-11.3 Shadow Tournament).

Covers:
- FoundationModel enum
- TournamentEntry construction + validation (fail-closed: promotion, empty
  forecasts, missing primary metric)
- TournamentResult construction + validation (duplicate models, winner
  consistency, promotion fail-closed)
- ShadowScorecard construction
- compute_tournament_metrics (mse, mae, crps, pinball_loss)
- settle_predictions
- ShadowTournament.add_entry (duplicate model rejection)
- ShadowTournament.run (synthetic forecasts + actuals, winner determination,
  beats baseline / doesn't beat / ties / all worse)
- ShadowTournament.generate_scorecards
- save/load round-trip
- validate_promotion_eligibility (shadow, override)
- validate_no_live_signal (no live signal, fail-closed)
- Edge cases: single model, single forecast, all models worse than baseline
"""

from __future__ import annotations

import hashlib
import os

import pytest
from pydantic import ValidationError
from quant_foundry.forecast_distribution import (
    ForecastDistributionArtifact,
    TargetTransform,
    compute_forecast_hash,
)
from quant_foundry.shadow_tournament import (
    FoundationModel,
    ShadowScorecard,
    ShadowTournament,
    TournamentEntry,
    TournamentResult,
    compute_tournament_metrics,
    settle_predictions,
    validate_no_live_signal,
    validate_promotion_eligibility,
)

ISO_TS = "2026-01-01T00:00:00+00:00"
ZERO_HASH = hashlib.sha256(b"").hexdigest()
ALT_HASH = hashlib.sha256(b"different").hexdigest()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_forecast(
    artifact_id: str = "art-001",
    model_id: str = "chronos-base",
    median: float = 0.0,
    mean: float | None = None,
    quantiles: dict[float, float] | None = None,
    band_lower: float | None = None,
    band_upper: float | None = None,
    samples: list[float] | None = None,
    symbol: str = "AAPL",
    horizon: int = 1,
) -> ForecastDistributionArtifact:
    """Build a valid ForecastDistributionArtifact with a real hash."""
    if quantiles is None:
        # Build quantiles symmetrically around the median so the band
        # always brackets the median regardless of its value.
        quantiles = {0.05: median - 1.0, 0.5: median, 0.95: median + 1.0}
    if band_lower is None:
        band_lower = quantiles[min(quantiles)]
    if band_upper is None:
        band_upper = quantiles[max(quantiles)]
    if mean is None:
        mean = median
    payload = {
        "artifact_id": artifact_id,
        "model_id": model_id,
        "weight_hash": ZERO_HASH,
        "symbol": symbol,
        "horizon": horizon,
        "target_transform": TargetTransform.LOG_RETURN,
        "mean": mean,
        "median": median,
        "quantiles": quantiles,
        "samples": samples,
        "uncertainty_band_lower": band_lower,
        "uncertainty_band_upper": band_upper,
        "created_at": ISO_TS,
        "artifact_hash": "0" * 64,
    }
    tmp = ForecastDistributionArtifact(**payload)
    h = compute_forecast_hash(tmp)
    return ForecastDistributionArtifact(**{**payload, "artifact_hash": h})


def _make_forecasts(
    n: int,
    prefix: str = "fc",
    model_id: str = "chronos-base",
    medians: list[float] | None = None,
) -> list[ForecastDistributionArtifact]:
    """Build ``n`` valid forecast artifacts."""
    if medians is None:
        medians = [0.0] * n
    return [
        _make_forecast(
            artifact_id=f"{prefix}-{i:03d}",
            model_id=model_id,
            median=medians[i],
        )
        for i in range(n)
    ]


def _entry_payload(**overrides) -> dict:
    """Return a valid TournamentEntry payload."""
    base = {
        "model": FoundationModel.CHRONOS,
        "model_id": "chronos-base",
        "weight_hash": ZERO_HASH,
        "forecasts": [_make_forecast()],
        "metrics": {"mse": 0.1, "mae": 0.2, "crps": 0.15, "pinball_loss": 0.12},
        "promotion_eligible": False,
    }
    base.update(overrides)
    return base


def _result_payload(**overrides) -> dict:
    """Return a valid TournamentResult payload."""
    entry = TournamentEntry(**_entry_payload())
    base = {
        "tournament_id": "tourn-001",
        "dataset_id": "ds-001",
        "entries": [entry],
        "tree_stack_baseline": {"mse": 0.5, "mae": 0.5, "crps": 0.5, "pinball_loss": 0.5},
        "sequence_baseline": {"mse": 0.4, "mae": 0.4, "crps": 0.4, "pinball_loss": 0.4},
        "winner": None,
        "winner_improvement": None,
        "settled": True,
        "created_at": ISO_TS,
        "promotion_eligible": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# FoundationModel enum
# ---------------------------------------------------------------------------


class TestFoundationModel:
    """Tests for the FoundationModel enum."""

    def test_enum_members(self) -> None:
        assert FoundationModel.TIMESFM == "timesfm"
        assert FoundationModel.CHRONOS == "chronos"
        assert FoundationModel.MOIRAI == "moirai"
        assert FoundationModel.LAG_LLM == "lag_llm"

    def test_enum_count(self) -> None:
        assert len(FoundationModel) == 4

    def test_enum_lookup_by_value(self) -> None:
        assert FoundationModel("chronos") is FoundationModel.CHRONOS
        assert FoundationModel("timesfm") is FoundationModel.TIMESFM

    def test_enum_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            FoundationModel("not-a-model")

    def test_enum_is_str(self) -> None:
        assert isinstance(FoundationModel.CHRONOS, str)


# ---------------------------------------------------------------------------
# TournamentEntry
# ---------------------------------------------------------------------------


class TestTournamentEntry:
    """Tests for TournamentEntry construction and validation."""

    def test_valid_entry(self) -> None:
        entry = TournamentEntry(**_entry_payload())
        assert entry.model is FoundationModel.CHRONOS
        assert entry.model_id == "chronos-base"
        assert entry.weight_hash == ZERO_HASH
        assert len(entry.forecasts) == 1
        assert entry.metrics["mse"] == 0.1
        assert entry.promotion_eligible is False

    def test_entry_is_frozen(self) -> None:
        entry = TournamentEntry(**_entry_payload())
        with pytest.raises(ValidationError):
            entry.model_id = "other"  # type: ignore[misc]

    def test_entry_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(surprise="bad"))

    def test_empty_model_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(model_id=""))

    def test_empty_weight_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(weight_hash=""))

    def test_empty_forecasts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(forecasts=[]))

    def test_at_least_one_forecast_required(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(forecasts=[]))

    def test_empty_metrics_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(metrics={}))

    def test_metrics_must_contain_primary(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(metrics={"mae": 0.1}))

    def test_promotion_eligible_true_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentEntry(**_entry_payload(promotion_eligible=True))

    def test_single_forecast_ok(self) -> None:
        entry = TournamentEntry(**_entry_payload())
        assert len(entry.forecasts) == 1

    def test_multiple_forecasts_ok(self) -> None:
        fcs = _make_forecasts(3)
        entry = TournamentEntry(**_entry_payload(forecasts=fcs))
        assert len(entry.forecasts) == 3


# ---------------------------------------------------------------------------
# TournamentResult
# ---------------------------------------------------------------------------


class TestTournamentResult:
    """Tests for TournamentResult construction and validation."""

    def test_valid_result(self) -> None:
        result = TournamentResult(**_result_payload())
        assert result.tournament_id == "tourn-001"
        assert result.dataset_id == "ds-001"
        assert len(result.entries) == 1
        assert result.winner is None
        assert result.winner_improvement is None
        assert result.settled is True
        assert result.promotion_eligible is False

    def test_result_is_frozen(self) -> None:
        result = TournamentResult(**_result_payload())
        with pytest.raises(ValidationError):
            result.tournament_id = "other"  # type: ignore[misc]

    def test_result_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(surprise="bad"))

    def test_empty_tournament_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(tournament_id=""))

    def test_empty_dataset_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(dataset_id=""))

    def test_duplicate_models_rejected(self) -> None:
        e1 = TournamentEntry(**_entry_payload())
        e2 = TournamentEntry(**_entry_payload(model_id="chronos-other"))
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(entries=[e1, e2]))

    def test_distinct_models_ok(self) -> None:
        e1 = TournamentEntry(**_entry_payload(model=FoundationModel.CHRONOS))
        e2 = TournamentEntry(**_entry_payload(model=FoundationModel.MOIRAI, model_id="moirai-base"))
        result = TournamentResult(**_result_payload(entries=[e1, e2]))
        assert len(result.entries) == 2

    def test_empty_entries_ok(self) -> None:
        result = TournamentResult(**_result_payload(entries=[]))
        assert len(result.entries) == 0

    def test_tree_stack_baseline_must_have_primary(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(tree_stack_baseline={"mae": 0.1}))

    def test_sequence_baseline_must_have_primary(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(sequence_baseline={"mae": 0.1}))

    def test_winner_without_improvement_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(
                **_result_payload(winner=FoundationModel.CHRONOS, winner_improvement=None)
            )

    def test_improvement_without_winner_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(winner=None, winner_improvement=-0.1))

    def test_winner_with_improvement_ok(self) -> None:
        result = TournamentResult(
            **_result_payload(winner=FoundationModel.CHRONOS, winner_improvement=-0.1)
        )
        assert result.winner is FoundationModel.CHRONOS
        assert result.winner_improvement == -0.1

    def test_promotion_eligible_true_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TournamentResult(**_result_payload(promotion_eligible=True))


# ---------------------------------------------------------------------------
# ShadowScorecard
# ---------------------------------------------------------------------------


class TestShadowScorecard:
    """Tests for ShadowScorecard construction."""

    def test_valid_scorecard(self) -> None:
        sc = ShadowScorecard(
            tournament_id="tourn-001",
            model=FoundationModel.CHRONOS,
            metric_name="mse",
            value=0.1,
            baseline_value=0.5,
            improvement=-0.4,
            beats_baseline=True,
        )
        assert sc.tournament_id == "tourn-001"
        assert sc.model is FoundationModel.CHRONOS
        assert sc.metric_name == "mse"
        assert sc.value == 0.1
        assert sc.baseline_value == 0.5
        assert sc.improvement == -0.4
        assert sc.beats_baseline is True

    def test_scorecard_is_frozen(self) -> None:
        sc = ShadowScorecard(
            tournament_id="tourn-001",
            model=FoundationModel.CHRONOS,
            metric_name="mse",
            value=0.1,
            baseline_value=0.5,
            improvement=-0.4,
            beats_baseline=True,
        )
        with pytest.raises(ValidationError):
            sc.value = 0.2  # type: ignore[misc]

    def test_scorecard_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ShadowScorecard(
                tournament_id="tourn-001",
                model=FoundationModel.CHRONOS,
                metric_name="mse",
                value=0.1,
                baseline_value=0.5,
                improvement=-0.4,
                beats_baseline=True,
                surprise="bad",
            )

    def test_empty_tournament_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ShadowScorecard(
                tournament_id="",
                model=FoundationModel.CHRONOS,
                metric_name="mse",
                value=0.1,
                baseline_value=0.5,
                improvement=-0.4,
                beats_baseline=True,
            )

    def test_empty_metric_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ShadowScorecard(
                tournament_id="tourn-001",
                model=FoundationModel.CHRONOS,
                metric_name="",
                value=0.1,
                baseline_value=0.5,
                improvement=-0.4,
                beats_baseline=True,
            )


# ---------------------------------------------------------------------------
# compute_tournament_metrics
# ---------------------------------------------------------------------------


class TestComputeTournamentMetrics:
    """Tests for compute_tournament_metrics."""

    def test_perfect_forecast(self) -> None:
        """When median == actual for all, mse and mae are 0."""
        fcs = _make_forecasts(3, medians=[1.0, 2.0, 3.0])
        actuals = [1.0, 2.0, 3.0]
        m = compute_tournament_metrics(fcs, actuals)
        assert m["mse"] == pytest.approx(0.0)
        assert m["mae"] == pytest.approx(0.0)
        assert "crps" in m
        assert "pinball_loss" in m

    def test_mse_value(self) -> None:
        """MSE is the mean of (actual - median)^2."""
        fcs = _make_forecasts(2, medians=[0.0, 0.0])
        actuals = [1.0, -1.0]
        m = compute_tournament_metrics(fcs, actuals)
        assert m["mse"] == pytest.approx(1.0)

    def test_mae_value(self) -> None:
        """MAE is the mean of |actual - median|."""
        fcs = _make_forecasts(2, medians=[0.0, 0.0])
        actuals = [2.0, -1.0]
        m = compute_tournament_metrics(fcs, actuals)
        assert m["mae"] == pytest.approx(1.5)

    def test_crps_nonneg(self) -> None:
        """CRPS is non-negative for reasonable forecasts."""
        fcs = _make_forecasts(2, medians=[0.0, 0.0])
        actuals = [0.5, -0.5]
        m = compute_tournament_metrics(fcs, actuals)
        assert m["crps"] >= 0.0

    def test_pinball_loss_nonneg(self) -> None:
        """Pinball loss is non-negative."""
        fcs = _make_forecasts(2, medians=[0.0, 0.0])
        actuals = [0.5, -0.5]
        m = compute_tournament_metrics(fcs, actuals)
        assert m["pinball_loss"] >= 0.0

    def test_all_four_metrics_present(self) -> None:
        fcs = _make_forecasts(2)
        m = compute_tournament_metrics(fcs, [0.0, 0.0])
        assert set(m.keys()) == {"mse", "mae", "crps", "pinball_loss"}

    def test_empty_forecasts_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_tournament_metrics([], [0.0])

    def test_empty_actuals_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_tournament_metrics(_make_forecasts(1), [])

    def test_mismatched_length_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_tournament_metrics(_make_forecasts(3), [0.0, 1.0])

    def test_non_forecast_rejected(self) -> None:
        with pytest.raises(TypeError):
            compute_tournament_metrics([{"bad": 1}], [0.0])  # type: ignore[arg-type]

    def test_non_number_actual_rejected(self) -> None:
        with pytest.raises(TypeError):
            compute_tournament_metrics(_make_forecasts(1), ["x"])  # type: ignore[list-item]

    def test_single_forecast(self) -> None:
        fcs = _make_forecasts(1, medians=[1.0])
        m = compute_tournament_metrics(fcs, [1.0])
        assert m["mse"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# settle_predictions
# ---------------------------------------------------------------------------


class TestSettlePredictions:
    """Tests for settle_predictions."""

    def test_settlement_records(self) -> None:
        fcs = _make_forecasts(2, medians=[0.0, 0.0])
        records = settle_predictions(fcs, [1.0, -2.0])
        assert len(records) == 2
        assert records[0]["forecast_id"] == "fc-000"
        assert records[0]["actual"] == 1.0
        assert records[0]["error"] == 1.0
        assert records[0]["squared_error"] == 1.0
        assert records[1]["error"] == -2.0
        assert records[1]["squared_error"] == 4.0

    def test_settlement_keys(self) -> None:
        fcs = _make_forecasts(1)
        records = settle_predictions(fcs, [0.0])
        assert set(records[0].keys()) == {
            "forecast_id",
            "actual",
            "error",
            "squared_error",
        }

    def test_settlement_empty_forecasts_rejected(self) -> None:
        with pytest.raises(ValueError):
            settle_predictions([], [0.0])

    def test_settlement_empty_actuals_rejected(self) -> None:
        with pytest.raises(ValueError):
            settle_predictions(_make_forecasts(1), [])

    def test_settlement_mismatched_length_rejected(self) -> None:
        with pytest.raises(ValueError):
            settle_predictions(_make_forecasts(3), [0.0, 1.0])

    def test_settlement_non_forecast_rejected(self) -> None:
        with pytest.raises(TypeError):
            settle_predictions([{"bad": 1}], [0.0])  # type: ignore[arg-type]

    def test_settlement_single(self) -> None:
        fcs = _make_forecasts(1, medians=[2.0])
        records = settle_predictions(fcs, [5.0])
        assert records[0]["error"] == 3.0
        assert records[0]["squared_error"] == 9.0


# ---------------------------------------------------------------------------
# ShadowTournament.add_entry
# ---------------------------------------------------------------------------


class TestShadowTournamentAddEntry:
    """Tests for ShadowTournament.add_entry."""

    def test_add_entry(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        entry = t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(2))
        assert entry.model is FoundationModel.CHRONOS
        assert entry.promotion_eligible is False
        assert len(t.entries) == 1

    def test_add_entry_duplicate_model_rejected(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(1))
        with pytest.raises(ValueError):
            t.add_entry(
                FoundationModel.CHRONOS,
                "chronos-other",
                ALT_HASH,
                _make_forecasts(1),
            )

    def test_add_entry_distinct_models_ok(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(1))
        t.add_entry(FoundationModel.MOIRAI, "moirai-base", ALT_HASH, _make_forecasts(1))
        assert len(t.entries) == 2

    def test_add_entry_invalid_model_type(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        with pytest.raises(TypeError):
            t.add_entry("chronos", "chronos-base", ZERO_HASH, _make_forecasts(1))  # type: ignore[arg-type]

    def test_empty_tournament_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            ShadowTournament("", "ds-001")

    def test_empty_dataset_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            ShadowTournament("tourn-001", "")

    def test_entries_property_returns_copy(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(1))
        e = t.entries
        e.clear()
        assert len(t.entries) == 1


# ---------------------------------------------------------------------------
# ShadowTournament.run
# ---------------------------------------------------------------------------


class TestShadowTournamentRun:
    """Tests for ShadowTournament.run."""

    def test_run_no_entries_raises(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        with pytest.raises(ValueError):
            t.run([0.0], {"mse": 0.5}, {"mse": 0.4})

    def test_run_empty_actuals_raises(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(1))
        with pytest.raises(ValueError):
            t.run([], {"mse": 0.5}, {"mse": 0.4})

    def test_run_forecast_actual_mismatch_raises(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(3))
        with pytest.raises(ValueError):
            t.run([0.0, 1.0], {"mse": 0.5}, {"mse": 0.4})

    def test_run_missing_primary_in_tree_stack(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(1))
        with pytest.raises(ValueError):
            t.run([0.0], {"mae": 0.5}, {"mse": 0.4})

    def test_run_missing_primary_in_sequence(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(FoundationModel.CHRONOS, "chronos-base", ZERO_HASH, _make_forecasts(1))
        with pytest.raises(ValueError):
            t.run([0.0], {"mse": 0.5}, {"mae": 0.4})

    def test_run_winner_beats_both_baselines(self) -> None:
        """Model with mse lower than both baselines wins."""
        t = ShadowTournament("tourn-001", "ds-001")
        # Perfect forecasts → mse = 0
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(3, medians=[1.0, 2.0, 3.0]),
        )
        result = t.run(
            actuals=[1.0, 2.0, 3.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.4},
        )
        assert result.winner is FoundationModel.CHRONOS
        assert result.winner_improvement is not None
        # improvement = 0.0 - min(0.5, 0.4) = -0.4
        assert result.winner_improvement == pytest.approx(-0.4)
        assert result.settled is True
        assert result.promotion_eligible is False

    def test_run_does_not_beat_baseline(self) -> None:
        """Model worse than baselines → no winner."""
        t = ShadowTournament("tourn-001", "ds-001")
        # Forecasts off by a lot → high mse
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(3, medians=[10.0, 10.0, 10.0]),
        )
        result = t.run(
            actuals=[1.0, 2.0, 3.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.4},
        )
        assert result.winner is None
        assert result.winner_improvement is None

    def test_run_tie_with_baseline_no_winner(self) -> None:
        """Model mse exactly equal to a baseline → not strictly lower → no win."""
        t = ShadowTournament("tourn-001", "ds-001")
        # median=0, actual=1 → mse=1.0
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[0.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 1.0},
            sequence_metrics={"mse": 2.0},
        )
        # mse == tree_stack (1.0) → not strictly lower → no winner
        assert result.winner is None

    def test_run_beats_one_not_the_other(self) -> None:
        """Model beats tree-stack but not sequence → no winner."""
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[0.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 2.0},
            sequence_metrics={"mse": 0.5},
        )
        # model mse=1.0 < tree 2.0 but > seq 0.5 → no winner
        assert result.winner is None

    def test_run_all_models_worse_than_baseline(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(2, medians=[10.0, 10.0]),
        )
        t.add_entry(
            FoundationModel.MOIRAI,
            "moirai-base",
            ALT_HASH,
            _make_forecasts(2, medians=[20.0, 20.0]),
        )
        result = t.run(
            actuals=[1.0, 2.0],
            tree_stack_metrics={"mse": 0.1},
            sequence_metrics={"mse": 0.1},
        )
        assert result.winner is None
        assert len(result.entries) == 2

    def test_run_single_model(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[1.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.4},
        )
        assert result.winner is FoundationModel.CHRONOS

    def test_run_best_candidate_among_multiple(self) -> None:
        """Two models beat both baselines; the lower mse wins."""
        t = ShadowTournament("tourn-001", "ds-001")
        # Chronos: perfect → mse 0
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(2, medians=[1.0, 2.0]),
        )
        # Moirai: small error → mse 0.25
        t.add_entry(
            FoundationModel.MOIRAI,
            "moirai-base",
            ALT_HASH,
            _make_forecasts(2, medians=[0.5, 2.5]),
        )
        result = t.run(
            actuals=[1.0, 2.0],
            tree_stack_metrics={"mse": 1.0},
            sequence_metrics={"mse": 1.0},
        )
        assert result.winner is FoundationModel.CHRONOS
        assert result.winner_improvement == pytest.approx(-1.0)

    def test_run_metrics_recomputed(self) -> None:
        """Run recomputes metrics from forecast artifacts."""
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(2, medians=[0.0, 0.0]),
        )
        result = t.run(
            actuals=[1.0, 1.0],
            tree_stack_metrics={"mse": 2.0},
            sequence_metrics={"mse": 2.0},
        )
        assert result.entries[0].metrics["mse"] == pytest.approx(1.0)
        assert result.entries[0].metrics["mae"] == pytest.approx(1.0)

    def test_run_promotion_always_false(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[1.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.4},
        )
        assert result.promotion_eligible is False
        for e in result.entries:
            assert e.promotion_eligible is False


# ---------------------------------------------------------------------------
# ShadowTournament.generate_scorecards
# ---------------------------------------------------------------------------


class TestGenerateScorecards:
    """Tests for ShadowTournament.generate_scorecards."""

    def test_scorecards_per_model_per_metric(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(2, medians=[1.0, 2.0]),
        )
        result = t.run(
            actuals=[1.0, 2.0],
            tree_stack_metrics={"mse": 0.5, "mae": 0.5, "crps": 0.5, "pinball_loss": 0.5},
            sequence_metrics={"mse": 0.4, "mae": 0.4, "crps": 0.4, "pinball_loss": 0.4},
        )
        scs = t.generate_scorecards(result)
        # 1 model × 4 metrics = 4 scorecards
        assert len(scs) == 4
        mse_sc = [s for s in scs if s.metric_name == "mse"][0]
        assert mse_sc.beats_baseline is True
        assert mse_sc.improvement < 0

    def test_scorecards_beats_baseline_false_when_worse(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(2, medians=[10.0, 10.0]),
        )
        result = t.run(
            actuals=[1.0, 2.0],
            tree_stack_metrics={"mse": 0.1},
            sequence_metrics={"mse": 0.1},
        )
        scs = t.generate_scorecards(result)
        mse_sc = [s for s in scs if s.metric_name == "mse"][0]
        assert mse_sc.beats_baseline is False
        assert mse_sc.improvement > 0

    def test_scorecards_uses_best_baseline(self) -> None:
        """Scorecard baseline = min(tree, sequence) for each metric."""
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[1.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.3},
        )
        scs = t.generate_scorecards(result)
        mse_sc = [s for s in scs if s.metric_name == "mse"][0]
        assert mse_sc.baseline_value == pytest.approx(0.3)

    def test_scorecards_multiple_models(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[1.0]),
        )
        t.add_entry(
            FoundationModel.MOIRAI,
            "moirai-base",
            ALT_HASH,
            _make_forecasts(1, medians=[1.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 0.5, "mae": 0.5, "crps": 0.5, "pinball_loss": 0.5},
            sequence_metrics={"mse": 0.4, "mae": 0.4, "crps": 0.4, "pinball_loss": 0.4},
        )
        scs = t.generate_scorecards(result)
        # 2 models × 4 metrics = 8
        assert len(scs) == 8

    def test_scorecards_invalid_result_type(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        with pytest.raises(TypeError):
            t.generate_scorecards("not-a-result")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


class TestSaveLoad:
    """Tests for ShadowTournament.save_result / load_result."""

    def test_save_load_round_trip(self, tmp_path) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(2, medians=[1.0, 2.0]),
        )
        result = t.run(
            actuals=[1.0, 2.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.4},
        )
        path = str(tmp_path / "result.json")
        t.save_result(result, path)
        assert os.path.exists(path)
        loaded = t.load_result(path)
        assert loaded.tournament_id == result.tournament_id
        assert loaded.dataset_id == result.dataset_id
        assert loaded.winner == result.winner
        assert loaded.winner_improvement == pytest.approx(result.winner_improvement or 0.0)
        assert len(loaded.entries) == len(result.entries)
        assert loaded.entries[0].metrics["mse"] == pytest.approx(result.entries[0].metrics["mse"])

    def test_save_creates_parent_dirs(self, tmp_path) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[1.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.4},
        )
        path = str(tmp_path / "nested" / "dir" / "result.json")
        t.save_result(result, path)
        assert os.path.exists(path)

    def test_save_invalid_result_type(self, tmp_path) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        with pytest.raises(TypeError):
            t.save_result("not-a-result", str(tmp_path / "x.json"))  # type: ignore[arg-type]

    def test_save_empty_path_rejected(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        result = TournamentResult(**_result_payload())
        with pytest.raises(ValueError):
            t.save_result(result, "")

    def test_load_empty_path_rejected(self) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        with pytest.raises(ValueError):
            t.load_result("")

    def test_load_missing_file_rejected(self, tmp_path) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        with pytest.raises(ValueError):
            t.load_result(str(tmp_path / "nope.json"))

    def test_save_load_preserves_quantiles(self, tmp_path) -> None:
        t = ShadowTournament("tourn-001", "ds-001")
        t.add_entry(
            FoundationModel.CHRONOS,
            "chronos-base",
            ZERO_HASH,
            _make_forecasts(1, medians=[1.0]),
        )
        result = t.run(
            actuals=[1.0],
            tree_stack_metrics={"mse": 0.5},
            sequence_metrics={"mse": 0.4},
        )
        path = str(tmp_path / "result.json")
        t.save_result(result, path)
        loaded = t.load_result(path)
        orig_q = result.entries[0].forecasts[0].quantiles
        loaded_q = loaded.entries[0].forecasts[0].quantiles
        assert set(orig_q.keys()) == set(loaded_q.keys())
        for k in orig_q:
            assert loaded_q[k] == pytest.approx(orig_q[k])


# ---------------------------------------------------------------------------
# validate_promotion_eligibility
# ---------------------------------------------------------------------------


class TestValidatePromotionEligibility:
    """Tests for validate_promotion_eligibility."""

    def test_shadow_returns_false(self) -> None:
        result = TournamentResult(**_result_payload())
        assert validate_promotion_eligibility(result) is False

    def test_override_returns_true(self) -> None:
        result = TournamentResult(**_result_payload())
        assert validate_promotion_eligibility(result, manual_policy_override=True) is True

    def test_default_no_override(self) -> None:
        result = TournamentResult(**_result_payload())
        assert validate_promotion_eligibility(result, manual_policy_override=False) is False

    def test_invalid_result_type(self) -> None:
        with pytest.raises(TypeError):
            validate_promotion_eligibility("bad")  # type: ignore[arg-type]

    def test_fail_closed_no_autopromote(self) -> None:
        """Even a winning result does not auto-promote."""
        result = TournamentResult(
            **_result_payload(winner=FoundationModel.CHRONOS, winner_improvement=-0.1)
        )
        assert validate_promotion_eligibility(result) is False


# ---------------------------------------------------------------------------
# validate_no_live_signal
# ---------------------------------------------------------------------------


class TestValidateNoLiveSignal:
    """Tests for validate_no_live_signal."""

    def test_no_live_signal_ok(self) -> None:
        result = TournamentResult(**_result_payload())
        assert validate_no_live_signal(result) is True

    def test_invalid_result_type(self) -> None:
        with pytest.raises(TypeError):
            validate_no_live_signal("bad")  # type: ignore[arg-type]

    def test_result_promotion_true_raises(self) -> None:
        # Bypass the model validator by constructing via model_construct.
        result = TournamentResult.model_construct(**_result_payload(promotion_eligible=True))
        with pytest.raises(ValueError):
            validate_no_live_signal(result)

    def test_entry_promotion_true_raises(self) -> None:
        entry = TournamentEntry.model_construct(**_entry_payload(promotion_eligible=True))
        result = TournamentResult.model_construct(**_result_payload(entries=[entry]))
        with pytest.raises(ValueError):
            validate_no_live_signal(result)

    def test_all_entries_checked(self) -> None:
        """If any single entry is promotion-eligible, fail-closed."""
        e1 = TournamentEntry(**_entry_payload(model=FoundationModel.CHRONOS))
        e2_bad = TournamentEntry.model_construct(
            **_entry_payload(
                model=FoundationModel.MOIRAI,
                model_id="moirai-base",
                promotion_eligible=True,
            )
        )
        result = TournamentResult.model_construct(**_result_payload(entries=[e1, e2_bad]))
        with pytest.raises(ValueError):
            validate_no_live_signal(result)
