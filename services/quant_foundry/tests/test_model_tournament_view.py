"""Tests for quant_foundry.ui.model_tournament_view (T-UI.3).

Covers:
- TournamentViewConfig construction + validation
- TournamentRow construction
- TournamentView.render (all columns, partial columns, baseline/challenger markers)
- render_summary (counts, best challenger)
- render_model_detail
- render_comparison (deltas, improvements, degradations)
- filter_challengers, filter_promotion_eligible
- sort_rows (by various metrics, asc/desc, None handling)
- format_metric, format_delta, format_eligibility
- find_best_in_column
- validate_no_inflated_confidence (honest + inflated)
- edge cases: empty list, single model, all baselines, no promotion-eligible
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from quant_foundry.ui.model_tournament_view import (
    TournamentRow,
    TournamentView,
    TournamentViewConfig,
    find_best_in_column,
    format_delta,
    format_eligibility,
    format_metric,
    validate_no_inflated_confidence,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _baseline(
    model_id: str = "baseline",
    *,
    mse: float | None = 0.10,
    sharpe: float | None = 1.2,
    cost_adj: float | None = 0.05,
    drawdown: float | None = -0.15,
    ece: float | None = 0.03,
    ndcg: float | None = 0.7,
    map_score: float | None = 0.6,
    trials: int | None = 10,
    deflated: float | None = 0.4,
    shadow: bool = True,
    live: bool = False,
    promo: bool = False,
) -> TournamentRow:
    return TournamentRow(
        model_id=model_id,
        model_family="xgboost",
        is_baseline=True,
        mse=mse,
        sharpe_ratio=sharpe,
        cost_adjusted_return=cost_adj,
        max_drawdown=drawdown,
        calibration_ece=ece,
        ndcg=ndcg,
        map_score=map_score,
        trial_count=trials,
        deflated_score=deflated,
        shadow_eligible=shadow,
        live_eligible=live,
        promotion_eligible=promo,
    )


def _challenger(
    model_id: str = "challenger",
    *,
    mse: float | None = 0.08,
    sharpe: float | None = 1.5,
    cost_adj: float | None = 0.07,
    drawdown: float | None = -0.10,
    ece: float | None = 0.02,
    ndcg: float | None = 0.8,
    map_score: float | None = 0.65,
    trials: int | None = 20,
    deflated: float | None = 0.5,
    shadow: bool = True,
    live: bool = True,
    promo: bool = True,
) -> TournamentRow:
    return TournamentRow(
        model_id=model_id,
        model_family="patchtst",
        is_baseline=False,
        mse=mse,
        sharpe_ratio=sharpe,
        cost_adjusted_return=cost_adj,
        max_drawdown=drawdown,
        calibration_ece=ece,
        ndcg=ndcg,
        map_score=map_score,
        trial_count=trials,
        deflated_score=deflated,
        shadow_eligible=shadow,
        live_eligible=live,
        promotion_eligible=promo,
    )


# ---------------------------------------------------------------------------
# TournamentViewConfig
# ---------------------------------------------------------------------------


class TestTournamentViewConfig:
    def test_defaults(self) -> None:
        cfg = TournamentViewConfig()
        assert cfg.show_calibration is True
        assert cfg.show_cost_adjusted_return is True
        assert cfg.show_drawdown is True
        assert cfg.show_rank_metrics is True
        assert cfg.show_trial_count is True
        assert cfg.show_deflated_score is True
        assert cfg.show_shadow_live_eligibility is True
        assert cfg.max_rows == 50
        assert cfg.sort_by == "deflated_score"
        assert cfg.sort_order == "desc"

    def test_custom_values(self) -> None:
        cfg = TournamentViewConfig(max_rows=10, sort_by="mse", sort_order="asc")
        assert cfg.max_rows == 10
        assert cfg.sort_by == "mse"
        assert cfg.sort_order == "asc"

    def test_frozen(self) -> None:
        cfg = TournamentViewConfig()
        with pytest.raises(ValidationError):
            cfg.max_rows = 5  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            TournamentViewConfig(unknown_field=1)  # type: ignore[call-arg]

    def test_max_rows_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            TournamentViewConfig(max_rows=0)
        with pytest.raises(ValidationError):
            TournamentViewConfig(max_rows=-1)

    def test_invalid_sort_by(self) -> None:
        with pytest.raises(ValidationError):
            TournamentViewConfig(sort_by="bogus")

    def test_invalid_sort_order(self) -> None:
        with pytest.raises(ValidationError):
            TournamentViewConfig(sort_order="bogus")

    def test_all_sortable_metrics_accepted(self) -> None:
        for m in ("deflated_score", "cost_adjusted_return", "mse", "sharpe_ratio"):
            TournamentViewConfig(sort_by=m)


# ---------------------------------------------------------------------------
# TournamentRow
# ---------------------------------------------------------------------------


class TestTournamentRow:
    def test_construction_full(self) -> None:
        row = _challenger()
        assert row.model_id == "challenger"
        assert row.model_family == "patchtst"
        assert row.is_baseline is False
        assert row.mse == 0.08
        assert row.sharpe_ratio == 1.5
        assert row.cost_adjusted_return == 0.07
        assert row.max_drawdown == -0.10
        assert row.calibration_ece == 0.02
        assert row.ndcg == 0.8
        assert row.map_score == 0.65
        assert row.trial_count == 20
        assert row.deflated_score == 0.5
        assert row.shadow_eligible is True
        assert row.live_eligible is True
        assert row.promotion_eligible is True

    def test_construction_minimal(self) -> None:
        row = TournamentRow(model_id="m", model_family="f", is_baseline=True)
        assert row.mse is None
        assert row.sharpe_ratio is None
        assert row.shadow_eligible is False
        assert row.live_eligible is False
        assert row.promotion_eligible is False

    def test_frozen(self) -> None:
        row = _baseline()
        with pytest.raises(ValidationError):
            row.mse = 0.5  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            TournamentRow(
                model_id="m",
                model_family="f",
                is_baseline=True,
                bogus=1,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# format_metric
# ---------------------------------------------------------------------------


class TestFormatMetric:
    def test_value_formatted_4dp(self) -> None:
        assert format_metric(0.123456789) == "0.1235"

    def test_none_returns_sentinel(self) -> None:
        assert format_metric(None) == "—"

    def test_higher_is_better_flag_ignored_for_format(self) -> None:
        assert format_metric(1.5, higher_is_better=False) == "1.5000"

    def test_negative_value(self) -> None:
        assert format_metric(-0.0001) == "-0.0001"

    def test_zero(self) -> None:
        assert format_metric(0.0) == "0.0000"


# ---------------------------------------------------------------------------
# format_delta
# ---------------------------------------------------------------------------


class TestFormatDelta:
    def test_improvement_higher_is_better(self) -> None:
        # challenger higher than baseline, higher is better -> improvement
        assert format_delta(0.10, 0.12, higher_is_better=True) == "+0.0200 ▲"

    def test_degradation_higher_is_better(self) -> None:
        assert format_delta(0.10, 0.08, higher_is_better=True) == "-0.0200 ▼"

    def test_improvement_lower_is_better(self) -> None:
        # challenger lower than baseline, lower is better -> improvement
        assert format_delta(0.10, 0.08, higher_is_better=False) == "-0.0200 ▲"

    def test_degradation_lower_is_better(self) -> None:
        assert format_delta(0.10, 0.12, higher_is_better=False) == "+0.0200 ▼"

    def test_either_none_returns_sentinel(self) -> None:
        assert format_delta(None, 0.08) == "—"
        assert format_delta(0.10, None) == "—"
        assert format_delta(None, None) == "—"

    def test_zero_delta_neutral(self) -> None:
        result = format_delta(0.10, 0.10)
        assert "0.0000" in result
        assert "▲" not in result
        assert "▼" not in result


# ---------------------------------------------------------------------------
# format_eligibility
# ---------------------------------------------------------------------------


class TestFormatEligibility:
    def test_all_three(self) -> None:
        assert format_eligibility(True, True, True) == "[SHADOW+LIVE+PROMO]"

    def test_shadow_only(self) -> None:
        assert format_eligibility(True, False, False) == "[SHADOW]"

    def test_shadow_live(self) -> None:
        assert format_eligibility(True, True, False) == "[SHADOW+LIVE]"

    def test_none(self) -> None:
        assert format_eligibility(False, False, False) == "[NONE]"


# ---------------------------------------------------------------------------
# find_best_in_column
# ---------------------------------------------------------------------------


class TestFindBestInColumn:
    def test_higher_is_better(self) -> None:
        rows = [_baseline(deflated=0.4), _challenger(deflated=0.5)]
        assert find_best_in_column(rows, "deflated_score", True) == "challenger"

    def test_lower_is_better(self) -> None:
        rows = [_baseline(mse=0.10), _challenger(mse=0.08)]
        assert find_best_in_column(rows, "mse", False) == "challenger"

    def test_all_none_returns_none(self) -> None:
        rows = [
            TournamentRow(model_id="a", model_family="f", is_baseline=True),
            TournamentRow(model_id="b", model_family="f", is_baseline=False),
        ]
        assert find_best_in_column(rows, "mse", False) is None

    def test_empty_rows_returns_none(self) -> None:
        assert find_best_in_column([], "mse", False) is None

    def test_tie_first_wins(self) -> None:
        rows = [_baseline(model_id="first", mse=0.08), _challenger(model_id="second", mse=0.08)]
        assert find_best_in_column(rows, "mse", False) == "first"

    def test_skips_none_values(self) -> None:
        rows = [
            TournamentRow(model_id="a", model_family="f", is_baseline=True, mse=None),
            _challenger(model_id="b", mse=0.08),
        ]
        assert find_best_in_column(rows, "mse", False) == "b"


# ---------------------------------------------------------------------------
# validate_no_inflated_confidence
# ---------------------------------------------------------------------------


class TestValidateNoInflatedConfidence:
    def test_honest_all_false(self) -> None:
        row = TournamentRow(model_id="m", model_family="f", is_baseline=True)
        assert validate_no_inflated_confidence(row) is True

    def test_honest_full_hierarchy(self) -> None:
        row = TournamentRow(
            model_id="m",
            model_family="f",
            is_baseline=False,
            shadow_eligible=True,
            live_eligible=True,
            promotion_eligible=True,
        )
        assert validate_no_inflated_confidence(row) is True

    def test_honest_shadow_only(self) -> None:
        row = TournamentRow(
            model_id="m",
            model_family="f",
            is_baseline=False,
            shadow_eligible=True,
        )
        assert validate_no_inflated_confidence(row) is True

    def test_inflated_live_without_shadow(self) -> None:
        row = TournamentRow(
            model_id="m",
            model_family="f",
            is_baseline=False,
            shadow_eligible=False,
            live_eligible=True,
        )
        with pytest.raises(ValueError, match="live_eligible"):
            validate_no_inflated_confidence(row)

    def test_inflated_promo_without_live(self) -> None:
        row = TournamentRow(
            model_id="m",
            model_family="f",
            is_baseline=False,
            shadow_eligible=True,
            live_eligible=False,
            promotion_eligible=True,
        )
        with pytest.raises(ValueError, match="promotion_eligible"):
            validate_no_inflated_confidence(row)


# ---------------------------------------------------------------------------
# TournamentView filtering
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_filter_challengers(self) -> None:
        rows = [_baseline(), _challenger(), _baseline("b2"), _challenger("c2")]
        view = TournamentView(TournamentViewConfig())
        result = view.filter_challengers(rows)
        assert [r.model_id for r in result] == ["challenger", "c2"]

    def test_filter_challengers_empty(self) -> None:
        view = TournamentView(TournamentViewConfig())
        assert view.filter_challengers([]) == []

    def test_filter_challengers_all_baselines(self) -> None:
        rows = [_baseline(), _baseline("b2")]
        view = TournamentView(TournamentViewConfig())
        assert view.filter_challengers(rows) == []

    def test_filter_promotion_eligible(self) -> None:
        rows = [
            _baseline(promo=False),
            _challenger(promo=True),
            _challenger("c2", promo=False),
        ]
        view = TournamentView(TournamentViewConfig())
        result = view.filter_promotion_eligible(rows)
        assert [r.model_id for r in result] == ["challenger"]

    def test_filter_promotion_eligible_none(self) -> None:
        rows = [_baseline(), _challenger(promo=False)]
        view = TournamentView(TournamentViewConfig())
        assert view.filter_promotion_eligible(rows) == []


# ---------------------------------------------------------------------------
# TournamentView.sort_rows
# ---------------------------------------------------------------------------


class TestSortRows:
    def test_sort_descending_deflated(self) -> None:
        rows = [_baseline(deflated=0.4), _challenger(deflated=0.5)]
        view = TournamentView(TournamentViewConfig(sort_by="deflated_score", sort_order="desc"))
        result = view.sort_rows(rows)
        assert result[0].model_id == "challenger"

    def test_sort_ascending_deflated(self) -> None:
        rows = [_baseline(deflated=0.4), _challenger(deflated=0.5)]
        view = TournamentView(TournamentViewConfig(sort_by="deflated_score", sort_order="asc"))
        result = view.sort_rows(rows)
        assert result[0].model_id == "baseline"

    def test_sort_by_mse_ascending(self) -> None:
        rows = [_baseline(mse=0.10), _challenger(mse=0.08)]
        view = TournamentView(TournamentViewConfig(sort_by="mse", sort_order="asc"))
        result = view.sort_rows(rows)
        assert result[0].model_id == "challenger"

    def test_sort_by_sharpe_descending(self) -> None:
        rows = [_baseline(sharpe=1.2), _challenger(sharpe=1.5)]
        view = TournamentView(TournamentViewConfig(sort_by="sharpe_ratio", sort_order="desc"))
        result = view.sort_rows(rows)
        assert result[0].model_id == "challenger"

    def test_sort_none_values_last(self) -> None:
        rows = [
            _challenger(model_id="has_val", deflated=0.5),
            TournamentRow(
                model_id="no_val",
                model_family="f",
                is_baseline=True,
                deflated_score=None,
            ),
        ]
        view = TournamentView(TournamentViewConfig(sort_by="deflated_score", sort_order="desc"))
        result = view.sort_rows(rows)
        assert result[-1].model_id == "no_val"

    def test_sort_does_not_mutate_input(self) -> None:
        rows = [_baseline(deflated=0.4), _challenger(deflated=0.5)]
        original = list(rows)
        view = TournamentView(TournamentViewConfig())
        view.sort_rows(rows)
        assert rows == original


# ---------------------------------------------------------------------------
# TournamentView.render
# ---------------------------------------------------------------------------


class TestRender:
    def test_empty_returns_placeholder(self) -> None:
        view = TournamentView(TournamentViewConfig())
        assert view.render([]) == "(no models)"

    def test_baseline_and_challenger_markers(self) -> None:
        rows = [_baseline(), _challenger()]
        view = TournamentView(TournamentViewConfig())
        out = view.render(rows)
        assert "[BASELINE]" in out
        assert "[CHALLENGER]" in out

    def test_all_columns_present(self) -> None:
        rows = [_baseline(), _challenger()]
        view = TournamentView(TournamentViewConfig())
        out = view.render(rows)
        for col in (
            "Model",
            "Family",
            "Role",
            "MSE",
            "Sharpe",
            "CostAdjRet",
            "MaxDD",
            "ECE",
            "NDCG",
            "mAP",
            "Trials",
            "Deflated",
            "Eligibility",
        ):
            assert col in out, f"missing column {col}"

    def test_partial_columns(self) -> None:
        rows = [_baseline(), _challenger()]
        cfg = TournamentViewConfig(
            show_calibration=False,
            show_cost_adjusted_return=False,
            show_drawdown=False,
            show_rank_metrics=False,
            show_trial_count=False,
            show_deflated_score=False,
            show_shadow_live_eligibility=False,
        )
        view = TournamentView(cfg)
        out = view.render(rows)
        assert "ECE" not in out
        assert "CostAdjRet" not in out
        assert "MaxDD" not in out
        assert "NDCG" not in out
        assert "Trials" not in out
        assert "Deflated" not in out
        assert "Eligibility" not in out
        # MSE and Sharpe always shown
        assert "MSE" in out
        assert "Sharpe" in out

    def test_best_marker_highlighted(self) -> None:
        rows = [_baseline(mse=0.10), _challenger(mse=0.08)]
        view = TournamentView(TournamentViewConfig(sort_by="mse", sort_order="asc"))
        out = view.render(rows)
        # challenger has lower (better) mse -> should have [*]
        assert "[*]" in out

    def test_max_rows_truncation(self) -> None:
        rows = [
            TournamentRow(
                model_id=f"m{i}",
                model_family="f",
                is_baseline=False,
                deflated_score=float(i),
            )
            for i in range(10)
        ]
        view = TournamentView(TournamentViewConfig(max_rows=3))
        out = view.render(rows)
        # Only 3 data rows + header + separator
        data_lines = [
            l
            for l in out.split("\n")
            if l.startswith("| ") and "[BASELINE]" not in l and "[CHALLENGER]" in l
        ]
        assert len(data_lines) == 3

    def test_single_model(self) -> None:
        rows = [_baseline()]
        view = TournamentView(TournamentViewConfig())
        out = view.render(rows)
        assert "[BASELINE]" in out
        assert "baseline" in out


# ---------------------------------------------------------------------------
# render_summary
# ---------------------------------------------------------------------------


class TestRenderSummary:
    def test_counts(self) -> None:
        rows = [_baseline(), _challenger(), _challenger("c2", promo=False)]
        view = TournamentView(TournamentViewConfig())
        out = view.render_summary(rows)
        assert "Total models: 3" in out
        assert "Baselines: 1" in out
        assert "Challengers: 2" in out
        assert "Promotion-eligible: 1" in out

    def test_best_challenger(self) -> None:
        rows = [_baseline(), _challenger(deflated=0.5), _challenger("c2", deflated=0.3)]
        view = TournamentView(TournamentViewConfig(sort_by="deflated_score", sort_order="desc"))
        out = view.render_summary(rows)
        assert "Best challenger: challenger" in out

    def test_no_challengers(self) -> None:
        rows = [_baseline()]
        view = TournamentView(TournamentViewConfig())
        out = view.render_summary(rows)
        assert "Best challenger: (none)" in out

    def test_empty(self) -> None:
        view = TournamentView(TournamentViewConfig())
        out = view.render_summary([])
        assert "Total models: 0" in out


# ---------------------------------------------------------------------------
# render_model_detail
# ---------------------------------------------------------------------------


class TestRenderModelDetail:
    def test_full_detail(self) -> None:
        row = _challenger()
        view = TournamentView(TournamentViewConfig())
        out = view.render_model_detail(row)
        assert "challenger" in out
        assert "patchtst" in out
        assert "CHALLENGER" in out
        assert "MSE:" in out
        assert "Sharpe:" in out
        assert "Eligibility:" in out

    def test_baseline_role(self) -> None:
        row = _baseline()
        view = TournamentView(TournamentViewConfig())
        out = view.render_model_detail(row)
        assert "BASELINE" in out

    def test_none_metric_shown_as_sentinel(self) -> None:
        row = TournamentRow(model_id="m", model_family="f", is_baseline=True)
        view = TournamentView(TournamentViewConfig())
        out = view.render_model_detail(row)
        assert "—" in out


# ---------------------------------------------------------------------------
# render_comparison
# ---------------------------------------------------------------------------


class TestRenderComparison:
    def test_comparison_has_both_models(self) -> None:
        b = _baseline()
        c = _challenger()
        view = TournamentView(TournamentViewConfig())
        out = view.render_comparison(b, c)
        assert "baseline" in out
        assert "challenger" in out

    def test_comparison_shows_deltas(self) -> None:
        b = _baseline(mse=0.10)
        c = _challenger(mse=0.08)
        view = TournamentView(TournamentViewConfig())
        out = view.render_comparison(b, c)
        assert "Delta" in out
        # lower is better for mse, challenger lower -> improvement
        assert "▲" in out

    def test_comparison_degradation(self) -> None:
        b = _baseline(sharpe=1.5)
        c = _challenger(sharpe=1.2)
        view = TournamentView(TournamentViewConfig())
        out = view.render_comparison(b, c)
        # higher is better for sharpe, challenger lower -> degradation
        assert "▼" in out

    def test_comparison_none_delta_sentinel(self) -> None:
        b = _baseline(mse=None)
        c = _challenger(mse=0.08)
        view = TournamentView(TournamentViewConfig())
        out = view.render_comparison(b, c)
        assert "—" in out

    def test_comparison_eligibility_shown(self) -> None:
        b = _baseline()
        c = _challenger()
        view = TournamentView(TournamentViewConfig())
        out = view.render_comparison(b, c)
        assert "Baseline eligibility:" in out
        assert "Challenger eligibility:" in out


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_all_baselines_render(self) -> None:
        rows = [_baseline(), _baseline("b2")]
        view = TournamentView(TournamentViewConfig())
        out = view.render(rows)
        assert out.count("[BASELINE]") == 2
        assert "[CHALLENGER]" not in out

    def test_no_promotion_eligible_summary(self) -> None:
        rows = [_baseline(promo=False), _challenger(promo=False)]
        view = TournamentView(TournamentViewConfig())
        out = view.render_summary(rows)
        assert "Promotion-eligible: 0" in out

    def test_render_single_baseline(self) -> None:
        view = TournamentView(TournamentViewConfig())
        out = view.render([_baseline()])
        assert "[BASELINE]" in out

    def test_render_all_none_metrics(self) -> None:
        rows = [
            TournamentRow(model_id="m1", model_family="f", is_baseline=True),
            TournamentRow(model_id="m2", model_family="f", is_baseline=False),
        ]
        view = TournamentView(TournamentViewConfig())
        out = view.render(rows)
        # No best marker since all metrics None
        assert "[*]" not in out
        assert "—" in out
