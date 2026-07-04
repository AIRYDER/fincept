"""
quant_foundry.ui.model_tournament_view — Model Tournament View (T-UI.3).

A pure-Python view layer that renders model tournament comparisons for
operators. It shows baseline vs challenger metrics as a formatted text /
markdown table so operators can compare model families **without reading raw
JSON**.

Design invariants (non-negotiable, fail-closed):

- **No external UI dependencies.** Everything is rendered as plain text /
  markdown strings — no Rich, no Textual, no terminal coupling. This keeps
  the module importable in headless / CI environments and trivially testable.
- **The UI never inflates confidence.**
  :func:`validate_no_inflated_confidence` enforces the promotion hierarchy:

      shadow_eligible -> live_eligible -> promotion_eligible

  A model that claims ``live_eligible`` without ``shadow_eligible``, or
  ``promotion_eligible`` without ``live_eligible``, is rejected with a
  ``ValueError``. There is no code path in this module that silently flips
  an eligibility flag on.
- **All Pydantic models are ``frozen=True`` + ``extra='forbid'``** — no
  mutation, no surprise fields. This matches the audit-integrity invariant
  used across the rest of ``quant_foundry``.
- **Best-metric highlighting is computed, not stored.** The ``[*]`` marker
  in :meth:`TournamentView.render` is derived from the rows passed in, so a
  stale "best" badge can never be persisted.

Public surface:

  - :class:`TournamentViewConfig`
  - :class:`TournamentRow`
  - :class:`TournamentView`
  - :func:`format_metric`
  - :func:`format_delta`
  - :func:`format_eligibility`
  - :func:`find_best_in_column`
  - :func:`validate_no_inflated_confidence`
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Metrics that are "higher is better" (reward / quality metrics).
_HIGHER_IS_BETTER: frozenset[str] = frozenset(
    {
        "sharpe_ratio",
        "cost_adjusted_return",
        "ndcg",
        "map_score",
        "deflated_score",
    }
)

#: Metrics that are "lower is better" (error / risk metrics).
_LOWER_IS_BETTER: frozenset[str] = frozenset(
    {
        "mse",
        "max_drawdown",
        "calibration_ece",
    }
)

#: All sortable metric keys accepted by :class:`TournamentViewConfig`.
_SORTABLE_METRICS: frozenset[str] = frozenset(
    {"deflated_score", "cost_adjusted_return", "mse", "sharpe_ratio"}
)

#: Sort orders accepted by :class:`TournamentViewConfig`.
_SORT_ORDERS: frozenset[str] = frozenset({"asc", "desc"})

#: Sentinel used when a metric value is missing / ``None``.
_NULL_SENTINEL: str = "—"

#: Improvement marker used by :func:`format_delta`.
_IMPROVE_MARKER: str = "▲"

#: Degradation marker used by :func:`format_delta`.
_DEGRADE_MARKER: str = "▼"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TournamentViewConfig(BaseModel):
    """Configuration for :class:`TournamentView`.

    Controls which columns are rendered, how many rows are shown, and how
    rows are sorted. The model is frozen and forbids extra fields so a stale
    config cannot be silently mutated mid-render.

    Attributes:
        show_calibration: show the Expected Calibration Error column.
        show_cost_adjusted_return: show the cost-adjusted return column.
        show_drawdown: show the max drawdown column.
        show_rank_metrics: show the NDCG / mAP rank-metric columns.
        show_trial_count: show the trial-count column.
        show_deflated_score: show the deflated (PBO-adjusted) score column.
        show_shadow_live_eligibility: show the shadow / live / promotion
            eligibility badges.
        max_rows: maximum number of rows rendered by
            :meth:`TournamentView.render` (must be >= 1).
        sort_by: metric to sort rows by. One of ``deflated_score``,
            ``cost_adjusted_return``, ``mse``, ``sharpe_ratio``.
        sort_order: ``asc`` or ``desc``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    show_calibration: bool = True
    show_cost_adjusted_return: bool = True
    show_drawdown: bool = True
    show_rank_metrics: bool = True
    show_trial_count: bool = True
    show_deflated_score: bool = True
    show_shadow_live_eligibility: bool = True
    max_rows: int = Field(default=50, ge=1)
    sort_by: str = "deflated_score"
    sort_order: str = "desc"

    @field_validator("sort_by")
    @classmethod
    def _validate_sort_by(cls, v: str) -> str:
        """Validate that ``sort_by`` is a supported metric key."""
        if v not in _SORTABLE_METRICS:
            raise ValueError(f"sort_by must be one of {sorted(_SORTABLE_METRICS)}, got {v!r}")
        return v

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, v: str) -> str:
        """Validate that ``sort_order`` is ``asc`` or ``desc``."""
        if v not in _SORT_ORDERS:
            raise ValueError(f"sort_order must be one of {sorted(_SORT_ORDERS)}, got {v!r}")
        return v


class TournamentRow(BaseModel):
    """A single model's tournament metrics, ready for rendering.

    All metric fields are optional (``float | None``) so a row can represent
    a model that has not yet produced a given metric (e.g. a baseline that
    was never calibrated). The eligibility flags are required and are
    validated for confidence integrity by
    :func:`validate_no_inflated_confidence`.

    Attributes:
        model_id: unique model identifier.
        model_family: model family name (e.g. ``xgboost``, ``patchtst``).
        is_baseline: ``True`` if this row is the tournament baseline.
        mse: mean squared error (lower is better).
        sharpe_ratio: annualised Sharpe ratio (higher is better).
        cost_adjusted_return: return net of trading costs (higher is better).
        max_drawdown: maximum drawdown (lower is better).
        calibration_ece: Expected Calibration Error (lower is better).
        ndcg: Normalised Discounted Cumulative Gain (higher is better).
        map_score: mean Average Precision (higher is better).
        trial_count: number of optuna / sweep trials run.
        deflated_score: deflated (PBO-adjusted) score (higher is better).
        shadow_eligible: eligible for shadow deployment.
        live_eligible: eligible for live deployment.
        promotion_eligible: eligible for promotion to production.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    model_family: str
    is_baseline: bool
    mse: float | None = None
    sharpe_ratio: float | None = None
    cost_adjusted_return: float | None = None
    max_drawdown: float | None = None
    calibration_ece: float | None = None
    ndcg: float | None = None
    map_score: float | None = None
    trial_count: int | None = None
    deflated_score: float | None = None
    shadow_eligible: bool = False
    live_eligible: bool = False
    promotion_eligible: bool = False


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_metric(value: float | None, higher_is_better: bool = True) -> str:
    """Format a metric value for display.

    Returns the value formatted to 4 decimal places, or ``"—"`` if the value
    is ``None``. The ``higher_is_better`` flag is accepted for API symmetry
    with :func:`format_delta` and does not change the numeric formatting —
    it is documented so callers remember the metric's polarity.

    Args:
        value: the metric value, or ``None`` if missing.
        higher_is_better: whether higher values are better for this metric.

    Returns:
        A 4-decimal-place string (e.g. ``"0.1234"``) or ``"—"``.

    Examples:
        >>> format_metric(0.123456789)
        '0.1235'
        >>> format_metric(None)
        '—'
        >>> format_metric(1.5, higher_is_better=False)
        '1.5000'
    """
    if value is None:
        return _NULL_SENTINEL
    return f"{float(value):.4f}"


def format_delta(
    baseline: float | None,
    challenger: float | None,
    higher_is_better: bool = True,
) -> str:
    """Format the delta between a baseline and challenger metric.

    The delta is ``challenger - baseline``. An improvement (in the direction
    of "better") is marked with ``"▲"`` and a degradation with ``"▼"``. If
    either value is ``None`` the sentinel ``"—"`` is returned — the UI must
    never fabricate a delta from missing data.

    Args:
        baseline: the baseline metric value, or ``None``.
        challenger: the challenger metric value, or ``None``.
        higher_is_better: whether higher values are better for this metric.

    Returns:
        A formatted delta string such as ``"+0.0123 ▲"`` or ``"-0.0456 ▼"``,
        or ``"—"`` if either input is ``None``. A zero delta is reported as
        ``"+0.0000 ▬"`` (neutral).

    Examples:
        >>> format_delta(0.10, 0.12, higher_is_better=True)
        '+0.0200 ▲'
        >>> format_delta(0.10, 0.08, higher_is_better=True)
        '-0.0200 ▼'
        >>> format_delta(0.10, 0.08, higher_is_better=False)
        '-0.0200 ▲'
        >>> format_delta(None, 0.08)
        '—'
    """
    if baseline is None or challenger is None:
        return _NULL_SENTINEL
    delta = float(challenger) - float(baseline)
    # Determine whether the delta is an improvement. For "higher is better"
    # metrics a positive delta is an improvement; for "lower is better"
    # metrics a negative delta is an improvement.
    if delta > 0:
        improved = higher_is_better
    elif delta < 0:
        improved = not higher_is_better
    else:
        # Exactly zero — neither improvement nor degradation.
        return f"{delta:+.4f} \u25ac"
    marker = _IMPROVE_MARKER if improved else _DEGRADE_MARKER
    return f"{delta:+.4f} {marker}"


def format_eligibility(shadow: bool, live: bool, promotion: bool) -> str:
    """Format the combined eligibility badge for a model.

    The badge lists every eligibility tier the model has earned, in the
    canonical order ``SHADOW`` -> ``LIVE`` -> ``PROMO``. A model with no
    eligibility returns ``"[NONE]"``.

    Args:
        shadow: shadow-eligible flag.
        live: live-eligible flag.
        promotion: promotion-eligible flag.

    Returns:
        A badge string such as ``"[SHADOW+LIVE+PROMO]"`` or ``"[SHADOW]"``.

    Examples:
        >>> format_eligibility(True, True, True)
        '[SHADOW+LIVE+PROMO]'
        >>> format_eligibility(True, False, False)
        '[SHADOW]'
        >>> format_eligibility(False, False, False)
        '[NONE]'
    """
    parts: list[str] = []
    if shadow:
        parts.append("SHADOW")
    if live:
        parts.append("LIVE")
    if promotion:
        parts.append("PROMO")
    if not parts:
        return "[NONE]"
    return "[" + "+".join(parts) + "]"


# ---------------------------------------------------------------------------
# Best-metric + confidence-integrity helpers
# ---------------------------------------------------------------------------


def find_best_in_column(
    rows: list[TournamentRow],
    metric: str,
    higher_is_better: bool = True,
) -> str | None:
    """Return the ``model_id`` with the best value for ``metric``.

    "Best" is determined by ``higher_is_better``: for higher-is-better
    metrics the maximum value wins; for lower-is-better metrics the minimum
    value wins. Rows where the metric is ``None`` are skipped. Ties are
    broken by the order rows appear in ``rows`` (first wins) so the result
    is deterministic.

    Args:
        rows: the tournament rows to search.
        metric: the attribute name on :class:`TournamentRow` to compare.
        higher_is_better: whether higher values are better for this metric.

    Returns:
        The ``model_id`` of the best row, or ``None`` if no row has a
        non-``None`` value for ``metric`` (or ``rows`` is empty).
    """
    best_id: str | None = None
    best_value: float | None = None
    for row in rows:
        value = getattr(row, metric, None)
        if value is None:
            continue
        value = float(value)
        if best_value is None:
            best_value = value
            best_id = row.model_id
            continue
        if higher_is_better:
            if value > best_value:
                best_value = value
                best_id = row.model_id
        else:
            if value < best_value:
                best_value = value
                best_id = row.model_id
    return best_id


def validate_no_inflated_confidence(row: TournamentRow) -> bool:
    """Validate that a row's eligibility flags are not inflated.

    The promotion hierarchy is::

        shadow_eligible -> live_eligible -> promotion_eligible

    A model may not claim ``live_eligible`` without ``shadow_eligible``, and
    may not claim ``promotion_eligible`` without ``live_eligible``. This is
    the fail-closed confidence-integrity check: the UI must never render a
    model as more deployable than its earned tiers justify.

    Args:
        row: the :class:`TournamentRow` to validate.

    Returns:
        ``True`` if the row's eligibility is honest.

    Raises:
        ValueError: if ``live_eligible`` is set without ``shadow_eligible``,
            or ``promotion_eligible`` is set without ``live_eligible``.

    Examples:
        >>> from quant_foundry.ui.model_tournament_view import TournamentRow
        >>> row = TournamentRow(
        ...     model_id="m1", model_family="xgb", is_baseline=True,
        ...     shadow_eligible=True, live_eligible=True,
        ...     promotion_eligible=True,
        ... )
        >>> validate_no_inflated_confidence(row)
        True
    """
    if row.live_eligible and not row.shadow_eligible:
        raise ValueError(
            f"model {row.model_id!r} claims live_eligible=True without "
            "shadow_eligible=True — confidence is inflated"
        )
    if row.promotion_eligible and not row.live_eligible:
        raise ValueError(
            f"model {row.model_id!r} claims promotion_eligible=True without "
            "live_eligible=True — confidence is inflated"
        )
    return True


# ---------------------------------------------------------------------------
# TournamentView
# ---------------------------------------------------------------------------


class TournamentView:
    """Render model tournament comparisons for operators.

    The view is a pure presentation layer: it never mutates rows, never
    fabricates metrics, and never inflates eligibility. All rendering is
    plain text / markdown so it works in headless and CI environments.

    Args:
        config: the :class:`TournamentViewConfig` controlling columns,
            row limits and sort order.
    """

    def __init__(self, config: TournamentViewConfig) -> None:
        self.config = config

    # -- filtering -------------------------------------------------------

    def filter_challengers(self, rows: list[TournamentRow]) -> list[TournamentRow]:
        """Return only the non-baseline (challenger) rows.

        Args:
            rows: the full tournament row list.

        Returns:
            A new list containing only rows with ``is_baseline=False``,
            preserving the original order.
        """
        return [r for r in rows if not r.is_baseline]

    def filter_promotion_eligible(self, rows: list[TournamentRow]) -> list[TournamentRow]:
        """Return only the promotion-eligible rows.

        Args:
            rows: the full tournament row list.

        Returns:
            A new list containing only rows with
            ``promotion_eligible=True``, preserving the original order.
        """
        return [r for r in rows if r.promotion_eligible]

    # -- sorting ---------------------------------------------------------

    def sort_rows(self, rows: list[TournamentRow]) -> list[TournamentRow]:
        """Sort rows by the configured metric and order.

        Rows with a ``None`` value for the sort metric are placed last
        regardless of sort order, so they never displace a model that
        actually has a metric. The sort is stable (Python's ``sorted`` is
        stable), so ties preserve the input order.

        Args:
            rows: the rows to sort.

        Returns:
            A new sorted list; the input list is not mutated.
        """
        metric = self.config.sort_by
        higher_is_better = metric in _HIGHER_IS_BETTER
        descending = self.config.sort_order == "desc"

        def _key(row: TournamentRow) -> tuple[int, float]:
            value = getattr(row, metric, None)
            if value is None:
                # None values always sort last. The sentinel group is 1 for
                # None and 0 for present values, so present values come
                # first regardless of ascending/descending.
                return (1, 0.0)
            return (0, float(value))

        # For descending we negate the float portion of the key so that
        # present values sort high->low while None values stay last.
        if descending:
            return sorted(
                rows,
                key=lambda r: (
                    (r is None, 0.0)
                    if getattr(r, metric, None) is None
                    else (False, -float(getattr(r, metric)))
                ),
            )

        return sorted(rows, key=_key)

    # -- best-metric map -------------------------------------------------

    def _best_map(self, rows: list[TournamentRow]) -> dict[str, str | None]:
        """Compute the best ``model_id`` for each rendered metric column."""
        best: dict[str, str | None] = {}
        if self.config.show_cost_adjusted_return:
            best["cost_adjusted_return"] = find_best_in_column(
                rows, "cost_adjusted_return", higher_is_better=True
            )
        if self.config.show_drawdown:
            best["max_drawdown"] = find_best_in_column(rows, "max_drawdown", higher_is_better=False)
        if self.config.show_calibration:
            best["calibration_ece"] = find_best_in_column(
                rows, "calibration_ece", higher_is_better=False
            )
        if self.config.show_rank_metrics:
            best["ndcg"] = find_best_in_column(rows, "ndcg", higher_is_better=True)
            best["map_score"] = find_best_in_column(rows, "map_score", higher_is_better=True)
        if self.config.show_deflated_score:
            best["deflated_score"] = find_best_in_column(
                rows, "deflated_score", higher_is_better=True
            )
        # mse and sharpe are always shown.
        best["mse"] = find_best_in_column(rows, "mse", higher_is_better=False)
        best["sharpe_ratio"] = find_best_in_column(rows, "sharpe_ratio", higher_is_better=True)
        return best

    # -- render ----------------------------------------------------------

    def render(self, rows: list[TournamentRow]) -> str:
        """Render a comparison table of baseline vs challenger models.

        Baseline rows are marked ``[BASELINE]`` and challenger rows
        ``[CHALLENGER]``. The best value in each numeric column is marked
        with ``[*]``. At most :attr:`TournamentViewConfig.max_rows` rows are
        rendered; rows are sorted via :meth:`sort_rows` before truncation.

        Args:
            rows: the tournament rows to render.

        Returns:
            A formatted markdown-style table string. An empty input
            produces a ``"(no models)"`` placeholder so the operator always
            sees something.
        """
        if not rows:
            return "(no models)"

        sorted_rows = self.sort_rows(rows)
        truncated = sorted_rows[: self.config.max_rows]
        best = self._best_map(truncated)

        headers: list[str] = ["Model", "Family", "Role"]
        if self.config.show_cost_adjusted_return:
            headers.append("CostAdjRet")
        if self.config.show_drawdown:
            headers.append("MaxDD")
        if self.config.show_calibration:
            headers.append("ECE")
        if self.config.show_rank_metrics:
            headers.append("NDCG")
            headers.append("mAP")
        if self.config.show_trial_count:
            headers.append("Trials")
        if self.config.show_deflated_score:
            headers.append("Deflated")
        if self.config.show_shadow_live_eligibility:
            headers.append("Eligibility")
        # mse and sharpe are always shown.
        headers = ["Model", "Family", "Role", "MSE", "Sharpe"] + headers[3:]

        lines: list[str] = []
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join("---" for _ in headers) + "|")

        for row in truncated:
            role = "[BASELINE]" if row.is_baseline else "[CHALLENGER]"
            cells: list[str] = [row.model_id, row.model_family, role]

            mse_cell = format_metric(row.mse, higher_is_better=False)
            if best.get("mse") == row.model_id and row.mse is not None:
                mse_cell += " [*]"
            cells.append(mse_cell)

            sharpe_cell = format_metric(row.sharpe_ratio, higher_is_better=True)
            if best.get("sharpe_ratio") == row.model_id and row.sharpe_ratio is not None:
                sharpe_cell += " [*]"
            cells.append(sharpe_cell)

            if self.config.show_cost_adjusted_return:
                cell = format_metric(row.cost_adjusted_return, higher_is_better=True)
                if (
                    best.get("cost_adjusted_return") == row.model_id
                    and row.cost_adjusted_return is not None
                ):
                    cell += " [*]"
                cells.append(cell)

            if self.config.show_drawdown:
                cell = format_metric(row.max_drawdown, higher_is_better=False)
                if best.get("max_drawdown") == row.model_id and row.max_drawdown is not None:
                    cell += " [*]"
                cells.append(cell)

            if self.config.show_calibration:
                cell = format_metric(row.calibration_ece, higher_is_better=False)
                if best.get("calibration_ece") == row.model_id and row.calibration_ece is not None:
                    cell += " [*]"
                cells.append(cell)

            if self.config.show_rank_metrics:
                cell = format_metric(row.ndcg, higher_is_better=True)
                if best.get("ndcg") == row.model_id and row.ndcg is not None:
                    cell += " [*]"
                cells.append(cell)

                cell = format_metric(row.map_score, higher_is_better=True)
                if best.get("map_score") == row.model_id and row.map_score is not None:
                    cell += " [*]"
                cells.append(cell)

            if self.config.show_trial_count:
                cells.append(
                    str(row.trial_count) if row.trial_count is not None else _NULL_SENTINEL
                )

            if self.config.show_deflated_score:
                cell = format_metric(row.deflated_score, higher_is_better=True)
                if best.get("deflated_score") == row.model_id and row.deflated_score is not None:
                    cell += " [*]"
                cells.append(cell)

            if self.config.show_shadow_live_eligibility:
                cells.append(
                    format_eligibility(
                        row.shadow_eligible,
                        row.live_eligible,
                        row.promotion_eligible,
                    )
                )

            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines)

    # -- render_summary --------------------------------------------------

    def render_summary(self, rows: list[TournamentRow]) -> str:
        """Render a tournament summary.

        The summary reports: total model count, baseline vs challenger
        counts, the best challenger (by the configured sort metric), and
        the count of promotion-eligible models.

        Args:
            rows: the tournament rows to summarise.

        Returns:
            A multi-line summary string.
        """
        total = len(rows)
        baselines = [r for r in rows if r.is_baseline]
        challengers = self.filter_challengers(rows)
        promo = self.filter_promotion_eligible(rows)

        lines: list[str] = []
        lines.append("=== Tournament Summary ===")
        lines.append(f"Total models: {total}")
        lines.append(f"Baselines: {len(baselines)}")
        lines.append(f"Challengers: {len(challengers)}")
        lines.append(f"Promotion-eligible: {len(promo)}")

        if challengers:
            sorted_challengers = self.sort_rows(challengers)
            best_challenger = sorted_challengers[0]
            metric = self.config.sort_by
            value = getattr(best_challenger, metric, None)
            value_str = format_metric(value, higher_is_better=metric in _HIGHER_IS_BETTER)
            lines.append(
                f"Best challenger: {best_challenger.model_id} "
                f"({best_challenger.model_family}) "
                f"{metric}={value_str}"
            )
        else:
            lines.append("Best challenger: (none)")

        return "\n".join(lines)

    # -- render_model_detail --------------------------------------------

    def render_model_detail(self, row: TournamentRow) -> str:
        """Render a detailed view of a single model's metrics.

        Args:
            row: the :class:`TournamentRow` to detail.

        Returns:
            A multi-line detail string listing every metric and the model's
            eligibility badge.
        """
        role = "BASELINE" if row.is_baseline else "CHALLENGER"
        lines: list[str] = []
        lines.append(f"=== Model Detail: {row.model_id} ===")
        lines.append(f"Family: {row.model_family}")
        lines.append(f"Role: {role}")
        lines.append(f"MSE: {format_metric(row.mse, higher_is_better=False)}")
        lines.append(f"Sharpe: {format_metric(row.sharpe_ratio, higher_is_better=True)}")
        if self.config.show_cost_adjusted_return:
            lines.append(
                f"Cost-Adjusted Return: {format_metric(row.cost_adjusted_return, higher_is_better=True)}"
            )
        if self.config.show_drawdown:
            lines.append(f"Max Drawdown: {format_metric(row.max_drawdown, higher_is_better=False)}")
        if self.config.show_calibration:
            lines.append(
                f"Calibration ECE: {format_metric(row.calibration_ece, higher_is_better=False)}"
            )
        if self.config.show_rank_metrics:
            lines.append(f"NDCG: {format_metric(row.ndcg, higher_is_better=True)}")
            lines.append(f"mAP: {format_metric(row.map_score, higher_is_better=True)}")
        if self.config.show_trial_count:
            trials = str(row.trial_count) if row.trial_count is not None else _NULL_SENTINEL
            lines.append(f"Trials: {trials}")
        if self.config.show_deflated_score:
            lines.append(
                f"Deflated Score: {format_metric(row.deflated_score, higher_is_better=True)}"
            )
        if self.config.show_shadow_live_eligibility:
            lines.append(
                f"Eligibility: {format_eligibility(row.shadow_eligible, row.live_eligible, row.promotion_eligible)}"
            )
        return "\n".join(lines)

    # -- render_comparison ----------------------------------------------

    def render_comparison(self, baseline: TournamentRow, challenger: TournamentRow) -> str:
        """Render a side-by-side comparison of a baseline vs a challenger.

        For each metric a delta is computed via :func:`format_delta` so the
        operator can see at a glance whether the challenger improves on the
        baseline. Missing metrics (``None`` on either side) yield ``"—"``
        rather than a fabricated delta.

        Args:
            baseline: the baseline :class:`TournamentRow`.
            challenger: the challenger :class:`TournamentRow`.

        Returns:
            A multi-line comparison string with baseline, challenger and
            delta columns.
        """
        lines: list[str] = []
        lines.append(f"=== Comparison: {baseline.model_id} vs {challenger.model_id} ===")
        lines.append(f"{'Metric':<22} {'Baseline':>12} {'Challenger':>12} {'Delta':>14}")
        lines.append("-" * 62)

        def _row_line(
            label: str,
            b: float | None,
            c: float | None,
            higher_is_better: bool,
        ) -> str:
            b_str = format_metric(b, higher_is_better=higher_is_better)
            c_str = format_metric(c, higher_is_better=higher_is_better)
            d_str = format_delta(b, c, higher_is_better=higher_is_better)
            return f"{label:<22} {b_str:>12} {c_str:>12} {d_str:>14}"

        lines.append(_row_line("MSE", baseline.mse, challenger.mse, higher_is_better=False))
        lines.append(
            _row_line(
                "Sharpe",
                baseline.sharpe_ratio,
                challenger.sharpe_ratio,
                higher_is_better=True,
            )
        )
        if self.config.show_cost_adjusted_return:
            lines.append(
                _row_line(
                    "CostAdjRet",
                    baseline.cost_adjusted_return,
                    challenger.cost_adjusted_return,
                    higher_is_better=True,
                )
            )
        if self.config.show_drawdown:
            lines.append(
                _row_line(
                    "MaxDD",
                    baseline.max_drawdown,
                    challenger.max_drawdown,
                    higher_is_better=False,
                )
            )
        if self.config.show_calibration:
            lines.append(
                _row_line(
                    "ECE",
                    baseline.calibration_ece,
                    challenger.calibration_ece,
                    higher_is_better=False,
                )
            )
        if self.config.show_rank_metrics:
            lines.append(
                _row_line(
                    "NDCG",
                    baseline.ndcg,
                    challenger.ndcg,
                    higher_is_better=True,
                )
            )
            lines.append(
                _row_line(
                    "mAP",
                    baseline.map_score,
                    challenger.map_score,
                    higher_is_better=True,
                )
            )
        if self.config.show_deflated_score:
            lines.append(
                _row_line(
                    "Deflated",
                    baseline.deflated_score,
                    challenger.deflated_score,
                    higher_is_better=True,
                )
            )

        lines.append("-" * 62)
        if self.config.show_shadow_live_eligibility:
            lines.append(
                f"Baseline eligibility:    {format_eligibility(baseline.shadow_eligible, baseline.live_eligible, baseline.promotion_eligible)}"
            )
            lines.append(
                f"Challenger eligibility:  {format_eligibility(challenger.shadow_eligible, challenger.live_eligible, challenger.promotion_eligible)}"
            )
        return "\n".join(lines)
