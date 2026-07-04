"""
scripts/run_intraday_walkforward.py — one-shot Alpaca minute-bar ingest
+ walk-forward backtest, the answer to "does the GBM have intraday alpha?"

This script chains :mod:`scripts.ingest_bars` and :mod:`scripts.walk_forward`
with intraday-tuned defaults so a single command takes you from "no data"
to "verdict on the GBM model".  Re-running with the same ``--out-dir``
is idempotent: if the parquet already exists it is reused (skip ingest).

Usage::

  # Set Alpaca creds once (paper account is fine — data is the same feed).
  $env:FINCEPT_ALPACA_API_KEY = "PKXXX..."
  $env:FINCEPT_ALPACA_API_SECRET = "..."

  # Run with defaults: 3 weeks of 1-min bars on SPY/AAPL/MSFT, 5 folds.
  uv run python scripts/run_intraday_walkforward.py

  # Customise window or symbols.
  uv run python scripts/run_intraday_walkforward.py \\
      --symbols SPY,AAPL,MSFT,QQQ --weeks 4 \\
      --out-dir reports/walkforward/intraday_4w

The driver runs walk-forward TWICE on the same parquet:
  1. ``ungated``  — no risk caps; baseline alpha estimate
  2. ``gated``    — same caps as live OMS (per-symbol/gross notional);
                    proves the gate doesn't accidentally kill the alpha

If the gated and ungated reports diverge meaningfully, that's a *signal*
the strategy depended on positions the live OMS would have rejected — a
finding the user would want to know about before flipping to live.

Free-tier caveats:
  - The free Alpaca data plan only serves the IEX feed (~2.5% of US
    consolidated tape volume).  Prices on liquid names (SPY/AAPL/MSFT)
    track the consolidated tape closely so the *signal* is preserved,
    but bar volumes are small \u2014 the realistic cost model will see this
    and apply larger market-impact terms than reality.  Pass
    ``--feed sip`` if you have a paid subscription.
  - Free-tier data has a 15-minute embargo, so the default window ends
    at T-2 days to be safely past it.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, timedelta

# scripts/ aren't packaged; surface the workspace src dirs.
_REPO = pathlib.Path(__file__).resolve().parent.parent
for _src in (
    _REPO / "services" / "backtester" / "src",
    _REPO / "libs" / "fincept-core" / "src",
):
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

# Reuse the existing CLI entrypoints; this keeps a single source of
# truth for ingest pagination / walk-forward orchestration.
from ingest_bars import _alpaca_credentials_or_none  # noqa: E402
from ingest_bars import main as ingest_main  # noqa: E402
from walk_forward import main as walk_forward_main  # noqa: E402

DEFAULT_SYMBOLS = "SPY,AAPL,MSFT"

# Walk-forward defaults tuned for 1-minute bars over ~3 weeks of trading.
# 3 weeks * 5 trading days * 390 minutes = ~5850 bars/symbol.
_WF_DEFAULTS: dict[str, str] = {
    "features": "ret_5m,ret_15m,ret_60m,rv_15m,rv_60m,mom_z_60m,mom_z_240m",
    "horizon_bars": "5",
    "bar_minutes": "1",
    "n_folds": "5",
    "train_min_bars": "2000",
    "val_bars": "500",
    "purge_bars": "15",
    "embargo_bars": "15",
    "freq": "1m",
    "venue": "alpaca",
    "asset_class": "equity",
    "num_boost_round": "100",
    "entry_threshold": "0.0",
    "exit_threshold": "0.0",
}

# Match the live OMS defaults so "gated" really means "what live would do".
_DEFAULT_RISK_PER_SYMBOL = 10_000
_DEFAULT_RISK_GROSS = 50_000


def _trading_day_offset(reference: date, business_days_back: int) -> date:
    """Walk back N business days from ``reference``.  Cheap weekend skip;
    doesn't account for market holidays \u2014 callers add a small buffer."""
    d = reference
    skipped = 0
    while skipped < business_days_back:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            skipped += 1
    return d


def _resolve_dates(*, end: str | None, weeks: int) -> tuple[date, date]:
    """Return ``(start, end)`` honouring ``--end`` if supplied, else
    defaulting to T-2 trading days back to clear the free-tier embargo."""
    end_d = date.fromisoformat(end) if end is not None else _trading_day_offset(date.today(), 2)
    # weeks * 5 business days = days of intraday history.
    start_d = _trading_day_offset(end_d, max(1, weeks) * 5 - 1)
    return start_d, end_d


def _ingest_if_missing(
    *,
    parquet_path: pathlib.Path,
    symbols: str,
    start: date,
    end: date,
    timeframe: str,
    feed: str,
) -> int:
    if parquet_path.exists():
        size_kb = parquet_path.stat().st_size // 1024
        print(
            f"[driver] reusing existing parquet {parquet_path} "
            f"({size_kb} KB) \u2014 delete it to re-ingest"
        )
        return 0
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"[driver] ingest minute bars {symbols} "
        f"{start.isoformat()}..{end.isoformat()} \u2192 {parquet_path}"
    )
    return ingest_main(
        [
            "--source",
            "alpaca",
            "--symbols",
            symbols,
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--timeframe",
            timeframe,
            "--feed",
            feed,
            "--out",
            str(parquet_path),
        ]
    )


def _walk_forward_args(
    *,
    parquet_path: pathlib.Path,
    out_dir: pathlib.Path,
    risk_per_symbol: int | None,
    risk_gross: int | None,
) -> list[str]:
    args = [
        "--bars",
        str(parquet_path),
        "--features",
        _WF_DEFAULTS["features"],
        "--horizon-bars",
        _WF_DEFAULTS["horizon_bars"],
        "--bar-minutes",
        _WF_DEFAULTS["bar_minutes"],
        "--n-folds",
        _WF_DEFAULTS["n_folds"],
        "--train-min-bars",
        _WF_DEFAULTS["train_min_bars"],
        "--val-bars",
        _WF_DEFAULTS["val_bars"],
        "--purge-bars",
        _WF_DEFAULTS["purge_bars"],
        "--embargo-bars",
        _WF_DEFAULTS["embargo_bars"],
        "--freq",
        _WF_DEFAULTS["freq"],
        "--venue",
        _WF_DEFAULTS["venue"],
        "--asset-class",
        _WF_DEFAULTS["asset_class"],
        "--num-boost-round",
        _WF_DEFAULTS["num_boost_round"],
        "--entry-threshold",
        _WF_DEFAULTS["entry_threshold"],
        "--exit-threshold",
        _WF_DEFAULTS["exit_threshold"],
        "--out-dir",
        str(out_dir),
    ]
    if risk_per_symbol is not None:
        args += ["--max-notional-per-symbol", str(risk_per_symbol)]
    if risk_gross is not None:
        args += ["--max-gross-notional", str(risk_gross)]
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_intraday_walkforward",
        description=(
            "Ingest Alpaca minute bars + run gated/ungated walk-forward "
            "backtest. The single command for the 'does GBM have intraday "
            "alpha?' question."
        ),
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f"Comma-separated tickers. Default: {DEFAULT_SYMBOLS}.",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=3,
        help="Number of weeks of trading history. Default: 3.",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="End date YYYY-MM-DD (inclusive). Default: 2 trading days "
        "before today, to clear free-tier 15-min embargo.",
    )
    parser.add_argument(
        "--timeframe",
        default="1Min",
        help="Alpaca bar size. Default: 1Min. Other valid: 5Min, 15Min, 1Hour.",
    )
    parser.add_argument(
        "--feed",
        default="iex",
        choices=("iex", "sip"),
        help="Alpaca feed. iex=free, sip=paid. Default: iex.",
    )
    parser.add_argument(
        "--out-dir",
        default="reports/walkforward/intraday",
        help="Reports directory. The parquet lives in <out-dir>/bars.parquet "
        "and per-run subdirs hold ungated/ and gated/ artifacts.",
    )
    parser.add_argument(
        "--max-notional-per-symbol",
        type=int,
        default=_DEFAULT_RISK_PER_SYMBOL,
        help=f"Risk cap for the gated run. Default: {_DEFAULT_RISK_PER_SYMBOL} "
        "(matches live OMS default).",
    )
    parser.add_argument(
        "--max-gross-notional",
        type=int,
        default=_DEFAULT_RISK_GROSS,
        help=f"Gross notional cap for the gated run. Default: "
        f"{_DEFAULT_RISK_GROSS} (matches live OMS default).",
    )
    parser.add_argument(
        "--skip-gated",
        action="store_true",
        help="Skip the gated walk-forward (ungated only).",
    )
    parser.add_argument(
        "--skip-ungated",
        action="store_true",
        help="Skip the ungated baseline walk-forward (gated only).",
    )
    args = parser.parse_args(argv)

    # Fail fast if creds aren't set so we don't waste time setting up.
    key, secret = _alpaca_credentials_or_none()
    if not key or not secret:
        print(
            "ERROR: Alpaca credentials not set.\n"
            "  Export FINCEPT_ALPACA_API_KEY + FINCEPT_ALPACA_API_SECRET\n"
            "  (or ALPACA_API_KEY + ALPACA_API_SECRET).\n"
            "  Free paper-account keys at https://app.alpaca.markets",
            file=sys.stderr,
        )
        return 2

    if args.skip_gated and args.skip_ungated:
        print("ERROR: --skip-gated and --skip-ungated are mutually exclusive", file=sys.stderr)
        return 1

    out_dir = pathlib.Path(args.out_dir)
    parquet_path = out_dir / "bars.parquet"

    start_d, end_d = _resolve_dates(end=args.end, weeks=args.weeks)
    print(
        f"[driver] window: {start_d.isoformat()} .. {end_d.isoformat()} "
        f"({args.weeks} week(s)) symbols={args.symbols}"
    )

    rc = _ingest_if_missing(
        parquet_path=parquet_path,
        symbols=args.symbols,
        start=start_d,
        end=end_d,
        timeframe=args.timeframe,
        feed=args.feed,
    )
    if rc != 0:
        return rc

    if not args.skip_ungated:
        print()
        print("=" * 78)
        print(" RUN 1/2: ungated walk-forward (baseline alpha estimate)")
        print("=" * 78)
        rc = walk_forward_main(
            _walk_forward_args(
                parquet_path=parquet_path,
                out_dir=out_dir / "ungated",
                risk_per_symbol=None,
                risk_gross=None,
            )
        )
        if rc != 0:
            return rc

    if not args.skip_gated:
        print()
        print("=" * 78)
        print(
            f" RUN 2/2: gated walk-forward "
            f"(per_symbol={args.max_notional_per_symbol}, "
            f"gross={args.max_gross_notional})"
        )
        print("=" * 78)
        rc = walk_forward_main(
            _walk_forward_args(
                parquet_path=parquet_path,
                out_dir=out_dir / "gated",
                risk_per_symbol=args.max_notional_per_symbol,
                risk_gross=args.max_gross_notional,
            )
        )
        if rc != 0:
            return rc

    print()
    print("[driver] done.  Inspect:")
    print(f"  parquet : {parquet_path}")
    if not args.skip_ungated:
        print(f"  ungated : {out_dir / 'ungated' / 'report.json'}")
    if not args.skip_gated:
        print(f"  gated   : {out_dir / 'gated' / 'report.json'}")
    print(
        "Compare oos_total_return_pct / oos_sharpe between the two reports. "
        "If they're close, the live OMS gate doesn't change the verdict; "
        "if gated is materially worse, the strategy was relying on positions "
        "the gate would have rejected."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
