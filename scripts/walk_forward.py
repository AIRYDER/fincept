"""
scripts/walk_forward.py — CLI for the walk-forward backtest harness.

Drives :func:`backtester.walk_forward.walk_forward_backtest`: trains a
GBM per fold (expanding window, OHLCV-derivable features), backtests
each fold's val window out-of-sample, and prints / persists an aggregate
OOS report.  This is the *go/no-go* gate for whether a strategy has
real edge.

Usage::

  # Minute-bar walk-forward (intraday)
  uv run python scripts/walk_forward.py \\
      --bars data/real_eq_intraday.parquet \\
      --features ret_5m,ret_15m,rv_15m,mom_z_60m,mom_z_240m \\
      --horizon-bars 5 --bar-minutes 1 \\
      --n-folds 5 --train-min-bars 2000 --val-bars 500 \\
      --purge-bars 15 --embargo-bars 15 \\
      --asset-class equity --venue alpaca --freq 1m \\
      --out-dir reports/walkforward/intraday

  # Daily-bar walk-forward (use d-suffix features, NOT m-suffix
  # — ``ret_5m`` on daily bars collapses to a 1-bar lookback)
  uv run python scripts/walk_forward.py \\
      --bars data/real_eq_2024h1.parquet \\
      --features ret_1d,ret_5d,rv_5d,mom_z_20d \\
      --horizon-bars 3 --bar-minutes 1440 \\
      --n-folds 5 --train-min-bars 60 --val-bars 20 --purge-bars 3 \\
      --asset-class equity --venue nasdaq --freq 1d \\
      --out-dir reports/walkforward/daily

Bar-minute caveat: ``--bar-minutes`` must match your parquet's bar
size (1, 5, 60, 1440 for 1m / 5m / 1h / daily).  Pick feature suffixes
that match: minute-bar parquets should use ``m`` / ``h`` features,
daily-bar parquets should use ``d`` features so lookback windows are
meaningful.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from decimal import Decimal

# scripts/ aren't packaged; surface the workspace src dirs.
_REPO = pathlib.Path(__file__).resolve().parent.parent
for _src in (
    _REPO / "services" / "backtester" / "src",
    _REPO / "libs" / "fincept-core" / "src",
):
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from backtester.walk_forward import walk_forward_backtest  # noqa: E402
from fincept_core.config import Settings  # noqa: E402
from fincept_core.schemas import AssetClass, Venue  # noqa: E402


def _parse_features(raw: str) -> list[str]:
    items = [s.strip() for s in raw.split(",") if s.strip()]
    if not items:
        raise argparse.ArgumentTypeError("--features must list >= 1 feature")
    return items


def _parse_decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def _format_sharpe(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}"


def _print_summary(report: object) -> None:
    """Print the headline OOS metrics + a per-fold table."""
    r = report  # WalkForwardReport
    print()
    print("=" * 78)
    print(f" walk-forward report  ({r.n_folds} folds, {r.n_oos_bars} OOS bars)")  # type: ignore[attr-defined]
    print("=" * 78)
    print(f"  OOS total return : {_format_pct(r.oos_total_return_pct)}")  # type: ignore[attr-defined]
    print(f"  OOS Sharpe       : {_format_sharpe(r.oos_sharpe)}")  # type: ignore[attr-defined]
    print(f"  OOS max drawdown : {_format_pct(r.oos_max_drawdown_pct)}")  # type: ignore[attr-defined]
    pct_pos_ret = r.pct_folds_positive_return * 100  # type: ignore[attr-defined]
    print(f"  positive folds   : {pct_pos_ret:.0f}% by return", end="")
    if r.pct_folds_positive_sharpe is not None:  # type: ignore[attr-defined]
        pct_pos_sh = r.pct_folds_positive_sharpe * 100  # type: ignore[attr-defined]
        print(f", {pct_pos_sh:.0f}% by Sharpe")
    else:
        print()
    print(
        f"  fold return stats: mean={r.mean_fold_return_pct:+.2f}%  "  # type: ignore[attr-defined]
        f"std={r.std_fold_return_pct:.2f}%"  # type: ignore[attr-defined]
    )
    if r.mean_fold_sharpe is not None:  # type: ignore[attr-defined]
        print(
            f"  fold Sharpe stats: mean={_format_sharpe(r.mean_fold_sharpe)}  "  # type: ignore[attr-defined]
            f"std={r.std_fold_sharpe:.2f}"  # type: ignore[attr-defined]
        )
    print()
    print("  per fold:")
    header = (
        "    k  | train_bars | val_bars | train_rows | n_fills | return  | "
        "sharpe | maxdd"
    )
    print(header)
    print("    " + "-" * (len(header) - 4))
    for f in r.folds:  # type: ignore[attr-defined]
        sharpe_str = _format_sharpe(f.fold_sharpe)
        dd_str = _format_pct(f.fold_max_drawdown_pct)
        print(
            f"    {f.index:<2} | {f.train_bars:>10} | {f.val_bars:>8} | "
            f"{f.train_rows:>10} | {f.n_fills:>7} | "
            f"{_format_pct(f.fold_return_pct):>7} | "
            f"{sharpe_str:>6} | {dd_str:>6}"
        )
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="walk_forward",
        description="Expanding-window walk-forward backtest with GBM per fold.",
    )
    parser.add_argument(
        "--bars", required=True, help="Parquet path (backtester schema)."
    )
    parser.add_argument(
        "--features",
        required=True,
        type=_parse_features,
        help="Comma-separated OHLCV-derivable feature names. Format: "
        "<ret|rv|mom_z>_<N><m|h|d> where m=minutes, h=hours, d=days. "
        "Use m/h on minute-bar parquets and d on daily-bar parquets so "
        "the lookback window doesn't collapse to a single bar.",
    )
    parser.add_argument(
        "--horizon-bars",
        type=int,
        required=True,
        help="Forward horizon (in bars) used to label training rows.",
    )
    parser.add_argument(
        "--bar-minutes",
        type=int,
        required=True,
        help="Length of one bar in minutes (1, 5, 60, 1440).",
    )
    parser.add_argument("--n-folds", type=int, default=5)
    parser.add_argument(
        "--train-min-bars",
        type=int,
        required=True,
        help="Minimum bars in fold-0's training window.",
    )
    parser.add_argument(
        "--val-bars",
        type=int,
        required=True,
        help="Bars per fold's validation window.",
    )
    parser.add_argument("--purge-bars", type=int, default=0)
    parser.add_argument("--embargo-bars", type=int, default=0)
    parser.add_argument(
        "--starting-cash",
        type=_parse_decimal,
        default=Decimal("100000"),
    )
    parser.add_argument(
        "--per-symbol-notional",
        type=_parse_decimal,
        default=Decimal("10000"),
    )
    parser.add_argument(
        "--venue",
        default=str(Venue.PAPER),
        help=f"Venue tag for bars/orders.  Default: {Venue.PAPER}.",
    )
    parser.add_argument(
        "--asset-class",
        default=str(AssetClass.CRYPTO_SPOT),
        help=f"AssetClass tag.  Default: {AssetClass.CRYPTO_SPOT}.",
    )
    parser.add_argument(
        "--freq",
        default="1m",
        help="Bar frequency tag (1m | 5m | 1h | 1d).",
    )
    parser.add_argument("--num-boost-round", type=int, default=100)
    parser.add_argument("--entry-threshold", type=float, default=0.0)
    parser.add_argument("--exit-threshold", type=float, default=0.0)
    parser.add_argument(
        "--max-notional-per-symbol",
        type=int,
        default=None,
        help="Pre-trade risk cap: max |notional in any one symbol| in USD. "
        "When set, every intent runs through the same risk.check_intent "
        "gate the live OMS uses.  Combine with --max-gross-notional to "
        "cap total exposure across symbols.",
    )
    parser.add_argument(
        "--max-gross-notional",
        type=int,
        default=None,
        help="Pre-trade risk cap: max sum-of-|notional| across all symbols.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Where to persist per-fold model artifacts + report.json. "
        "Omit to skip persistence.",
    )
    args = parser.parse_args(argv)

    bars_path = pathlib.Path(args.bars)
    if not bars_path.exists():
        print(f"ERROR: bars parquet not found: {bars_path}", file=sys.stderr)
        return 1

    try:
        venue = Venue(args.venue)
    except ValueError:
        print(f"ERROR: unknown venue {args.venue!r}", file=sys.stderr)
        return 1
    try:
        asset_class = AssetClass(args.asset_class)
    except ValueError:
        print(
            f"ERROR: unknown asset_class {args.asset_class!r}", file=sys.stderr
        )
        return 1

    out_dir = pathlib.Path(args.out_dir) if args.out_dir else None

    risk_settings: Settings | None = None
    if (
        args.max_notional_per_symbol is not None
        or args.max_gross_notional is not None
    ):
        # Build a Settings overriding only the cap fields the user set;
        # leave other fields at their defaults (TRADING_MODE='paper', etc.).
        kwargs: dict[str, int] = {}
        if args.max_notional_per_symbol is not None:
            kwargs["MAX_NOTIONAL_USD_PER_SYMBOL"] = args.max_notional_per_symbol
        if args.max_gross_notional is not None:
            kwargs["MAX_GROSS_NOTIONAL_USD"] = args.max_gross_notional
        risk_settings = Settings(**kwargs)

    try:
        report = asyncio.run(
            walk_forward_backtest(
                parquet_path=bars_path,
                feature_names=args.features,
                horizon_bars=args.horizon_bars,
                bar_minutes=args.bar_minutes,
                n_folds=args.n_folds,
                train_min_bars=args.train_min_bars,
                val_bars=args.val_bars,
                purge_bars=args.purge_bars,
                embargo_bars=args.embargo_bars,
                starting_cash=args.starting_cash,
                per_symbol_notional=args.per_symbol_notional,
                venue=venue,
                asset_class=asset_class,
                freq=args.freq,
                out_dir=out_dir,
                num_boost_round=args.num_boost_round,
                entry_threshold=args.entry_threshold,
                exit_threshold=args.exit_threshold,
                risk_settings=risk_settings,
            )
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _print_summary(report)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / "report.json"
        report_path.write_text(report.model_dump_json(indent=2))
        print(f"  report -> {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
