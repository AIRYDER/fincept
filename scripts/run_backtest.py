"""
scripts/run_backtest.py - CLI entrypoint for the backtester.

Usage::

  uv run python scripts/run_backtest.py \\
      --bars data/synth_ohlcv.parquet \\
      --strategy ma_crossover \\
      --strategy-params '{"fast": 5, "slow": 30, "per_symbol_notional": 10000}' \\
      --starting-cash 100000 \\
      --freq 1m

The script writes a run directory under ``reports/backtests/<run_id>/``
with ``run.json`` (manifest) and ``report.json`` (full equity curve +
trades).  The ``run.json`` is the file the API's ``GET /backtest/runs``
endpoint walks.

Exit codes:
  0   run completed; report written
  1   bad args / missing parquet / unknown strategy
  2   runtime error during simulation (printed traceback)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import traceback
from decimal import Decimal

from backtester.runner import REPORTS_ROOT, run_backtest
from fincept_core.schemas import AssetClass, Venue


def _parse_decimal(s: str) -> Decimal:
    try:
        return Decimal(s)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}") from exc


def _parse_strategy_params(raw: str | None) -> dict[str, object]:
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"--strategy-params must be valid JSON; got {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--strategy-params must be a JSON object (key/value map)")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_backtest")
    parser.add_argument(
        "--bars",
        required=True,
        help="Path to a parquet with columns: symbol, ts_event, open, high, low, close, volume.",
    )
    parser.add_argument(
        "--strategy",
        default="ma_crossover",
        help="Registered strategy key.  See backtester.strategies.STRATEGY_REGISTRY.",
    )
    parser.add_argument(
        "--strategy-params",
        default=None,
        help="JSON object of constructor kwargs for the strategy. "
        'Example: \'{"fast": 5, "slow": 30, "per_symbol_notional": 10000}\'',
    )
    parser.add_argument(
        "--starting-cash",
        type=_parse_decimal,
        default=Decimal("100000"),
        help="Starting NAV in USD (default 100000).",
    )
    parser.add_argument(
        "--freq",
        default="1m",
        help="Bar frequency: 1m | 5m | 15m | 1h | 1d.  Used for Sharpe annualization.",
    )
    parser.add_argument(
        "--venue",
        default=str(Venue.PAPER),
        help=f"Venue tag stamped on every bar/order. Default: {Venue.PAPER}.",
    )
    parser.add_argument(
        "--asset-class",
        default=str(AssetClass.CRYPTO_SPOT),
        help=f"AssetClass tag for bars.  Default: {AssetClass.CRYPTO_SPOT}.",
    )
    parser.add_argument(
        "--reports-root",
        default=str(REPORTS_ROOT),
        help="Directory under which to write the run subdir.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do NOT write the run directory; just print summary + exit.",
    )
    args = parser.parse_args(argv)

    bars_path = pathlib.Path(args.bars)
    if not bars_path.exists():
        print(f"ERROR: bars parquet not found: {bars_path}", file=sys.stderr)
        return 1

    try:
        strategy_params = _parse_strategy_params(args.strategy_params)
    except argparse.ArgumentTypeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        venue = Venue(args.venue)
    except ValueError:
        print(
            f"ERROR: unknown venue {args.venue!r}; valid: {[v.value for v in Venue]}",
            file=sys.stderr,
        )
        return 1
    try:
        asset_class = AssetClass(args.asset_class)
    except ValueError:
        print(
            f"ERROR: unknown asset_class {args.asset_class!r}; "
            f"valid: {[a.value for a in AssetClass]}",
            file=sys.stderr,
        )
        return 1

    try:
        result = asyncio.run(
            run_backtest(
                parquet_path=bars_path,
                strategy_name=args.strategy,
                strategy_params=strategy_params,
                starting_cash=args.starting_cash,
                venue=venue,
                asset_class=asset_class,
                freq=args.freq,
                persist=not args.no_persist,
                reports_root=pathlib.Path(args.reports_root),
            )
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 2

    r = result.report
    print(f"run_id        : {result.run_id}")
    if result.run_dir is not None:
        print(f"run_dir       : {result.run_dir}")
    print(f"strategy      : {args.strategy}")
    print(f"n_bars        : {r.n_bars}")
    print(f"n_fills       : {r.n_fills}")
    print(f"starting_cash : {r.starting_cash:,.2f} USD")
    print(f"final_equity  : {r.final_equity:,.2f} USD")
    print(f"total_return  : {r.total_return_pct:+.2f}%")
    sharpe_str = f"{r.sharpe:.2f}" if r.sharpe is not None else "n/a"
    print(f"sharpe        : {sharpe_str}")
    print(f"max_drawdown  : {r.max_drawdown_pct:.2f}%")
    print(f"fees_paid     : {r.fees_paid_total:,.2f} USD")
    if r.per_symbol:
        print("per_symbol    :")
        for ps in r.per_symbol:
            print(
                f"  {ps.symbol:10s} fills={ps.fills:4d} "
                f"notional={ps.notional_traded:,.0f} "
                f"fees={ps.fees_paid:.2f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
