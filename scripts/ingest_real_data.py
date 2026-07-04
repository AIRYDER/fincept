"""
scripts/ingest_real_data.py — ingest real datasets from vendor APIs.

A real-data companion to ``scripts/build_synthetic_dataset.py``.  Instead of
generating synthetic OHLCV bars, this script calls the vendor API adapters in
``quant_foundry.data_ingestion`` to fetch live data from Alpaca (equity
bars), FRED (macro indicators), and NewsAPI (news events), then runs each
through the same leakage-safe ingestion pipeline (features + labels +
manifest + receipt + quality report).

Three subcommands are supported:

- ``equities`` — fetch OHLCV bars from Alpaca and ingest via
  :func:`ingest_alpaca_equity_bars`.  Requires ``FINCEPT_ALPACA_API_KEY``
  and ``FINCEPT_ALPACA_API_SECRET``.
- ``macro`` — fetch macro series from FRED and ingest via
  :func:`ingest_fred_macro`.  Requires ``FRED_API_KEY``.
- ``news`` — fetch news articles from NewsAPI and ingest via
  :func:`ingest_newsapi_events`.  Requires ``NEWSAPI_KEY``.

The ``all`` option runs all three in sequence.

Each ingestion prints a summary with the dataset_id, parquet path, manifest
path, quality report path, row count, and quality report hash.

Usage::

  uv run python scripts/ingest_real_data.py equities \\
      --output data/datasets/ --symbols AAPL,MSFT,GOOGL,AMZN,SPY

  uv run python scripts/ingest_real_data.py macro \\
      --output data/datasets/ --series GDP,UNRATE,CPIAUCSL,FEDFUNDS,DGS10

  uv run python scripts/ingest_real_data.py news \\
      --output data/datasets/ --query "stock market"

  uv run python scripts/ingest_real_data.py all --output data/datasets/

Heavy dependencies (httpx, polars, numpy) are imported lazily inside the
vendor adapters so this script is importable without them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

# scripts/ are not packaged; make sibling imports work.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Ensure quant_foundry src is importable.
_REPO_ROOT = _SCRIPTS_DIR.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if _QF_SRC.exists() and str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

from quant_foundry.data_ingestion import (  # noqa: E402
    IngestionResult,
    ingest_alpaca_equity_bars,
    ingest_fred_macro,
    ingest_newsapi_events,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default equity symbols for the ``equities`` subcommand.
DEFAULT_EQUITY_SYMBOLS: tuple[str, ...] = ("AAPL", "MSFT", "GOOGL", "AMZN", "SPY")

#: Default FRED series IDs for the ``macro`` subcommand.
DEFAULT_MACRO_SERIES: tuple[str, ...] = (
    "GDP",
    "UNRATE",
    "CPIAUCSL",
    "FEDFUNDS",
    "DGS10",
)

#: Default news query for the ``news`` subcommand.
DEFAULT_NEWS_QUERY = "stock market"

#: Default lookback in trading days for the ``equities`` subcommand (~1 year).
DEFAULT_EQUITY_LOOKBACK_DAYS = 252

#: Default lookback in calendar days for the ``news`` subcommand.
DEFAULT_NEWS_LOOKBACK_DAYS = 30

#: Env var names for vendor credentials (matching the adapter modules).
_ALPACA_KEY_ENV = "FINCEPT_ALPACA_API_KEY"
_ALPACA_SECRET_ENV = "FINCEPT_ALPACA_API_SECRET"
_FRED_KEY_ENV = "FRED_API_KEY"
_NEWSAPI_KEY_ENV = "NEWSAPI_KEY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_list(s: str) -> list[str]:
    """Parse a comma-separated string into a stripped, upper-cased list."""
    return [x.strip().upper() for x in s.split(",") if x.strip()]


def _date_days_ago(days: int) -> str:
    """Return an ISO ``YYYY-MM-DD`` date string *days* before today (UTC)."""
    return (datetime.now(tz=UTC) - timedelta(days=days)).date().isoformat()


def _require_env(env_var: str) -> str:
    """Read a required env var; exit with a clear message if missing."""
    value = os.environ.get(env_var, "")
    if not value:
        raise SystemExit(
            f"missing required environment variable {env_var}; "
            f"set it before running this subcommand.",
        )
    return value


def _print_summary(label: str, result: IngestionResult) -> None:
    """Print a summary of an ingestion run to stdout."""
    row_count = result.quality_report.total_rows
    quality_hash = result.quality_report.quality_hash()
    print(f"[{label}] dataset_id          : {result.manifest.dataset_id}")
    print(f"[{label}] parquet path        : {result.parquet_path}")
    print(f"[{label}] manifest path       : {result.manifest_path}")
    print(f"[{label}] receipt path        : {result.receipt_path}")
    print(f"[{label}] quality report path : {result.quality_path}")
    print(f"[{label}] row count           : {row_count}")
    print(f"[{label}] quality report hash : {quality_hash}")


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def run_equities(args: argparse.Namespace) -> int:
    """Fetch equity bars from Alpaca and ingest them."""
    api_key = _require_env(_ALPACA_KEY_ENV)
    api_secret = _require_env(_ALPACA_SECRET_ENV)

    symbols = _parse_list(args.symbols)
    if not symbols:
        raise SystemExit("--symbols must contain at least one ticker")

    start = args.start_date or _date_days_ago(args.lookback_days)
    end = args.end_date or _date_days_ago(0)

    dataset_id = args.dataset_id or ("real_equities_" + "_".join(symbols[:3]) + f"_{start}_{end}")

    output_dir = pathlib.Path(args.output)
    print(f"[equities] fetching {len(symbols)} symbols from Alpaca ({start}..{end})...")

    result = asyncio.run(
        ingest_alpaca_equity_bars(
            symbols=symbols,
            start=start,
            end=end,
            output_dir=output_dir,
            dataset_id=dataset_id,
            timeframe=args.timeframe,
            label_horizon_days=args.label_horizon_days,
            n_folds=args.n_folds,
            api_key=api_key,
            api_secret=api_secret,
        ),
    )
    _print_summary("equities", result)
    return 0


def run_macro(args: argparse.Namespace) -> int:
    """Fetch macro indicators from FRED and ingest them."""
    api_key = _require_env(_FRED_KEY_ENV)

    series_ids = _parse_list(args.series)
    if not series_ids:
        raise SystemExit("--series must contain at least one FRED series ID")

    start = args.start_date or _date_days_ago(args.lookback_days)
    end = args.end_date or _date_days_ago(0)

    dataset_id = args.dataset_id or ("real_macro_" + "_".join(series_ids[:3]) + f"_{start}_{end}")

    output_dir = pathlib.Path(args.output)
    print(f"[macro] fetching {len(series_ids)} series from FRED ({start}..{end})...")

    result = asyncio.run(
        ingest_fred_macro(
            series_ids=series_ids,
            start=start,
            end=end,
            output_dir=output_dir,
            dataset_id=dataset_id,
            n_folds=args.n_folds,
            api_key=api_key,
        ),
    )
    _print_summary("macro", result)
    return 0


def run_news(args: argparse.Namespace) -> int:
    """Fetch news articles from NewsAPI and ingest them."""
    api_key = _require_env(_NEWSAPI_KEY_ENV)

    start = args.start_date or _date_days_ago(args.lookback_days)
    end = args.end_date or _date_days_ago(0)

    dataset_id = args.dataset_id or (f"real_news_{start}_{end}")

    output_dir = pathlib.Path(args.output)
    print(f"[news] fetching articles for {args.query!r} from NewsAPI ({start}..{end})...")

    result = asyncio.run(
        ingest_newsapi_events(
            query=args.query,
            start=start,
            end=end,
            output_dir=output_dir,
            dataset_id=dataset_id,
            n_folds=args.n_folds,
            api_key=api_key,
        ),
    )
    _print_summary("news", result)
    return 0


def run_all(args: argparse.Namespace) -> int:
    """Run the equities, macro, and news subcommands in sequence."""
    eq_args = argparse.Namespace(
        symbols=args.symbols,
        series=args.series,
        query=args.query,
        output=args.output,
        start_date=args.start_date,
        end_date=args.end_date,
        lookback_days=args.lookback_days,
        timeframe=args.timeframe,
        label_horizon_days=args.label_horizon_days,
        n_folds=args.n_folds,
        dataset_id=None,
    )

    # Equities (skip silently if Alpaca keys are missing).
    if os.environ.get(_ALPACA_KEY_ENV) and os.environ.get(_ALPACA_SECRET_ENV):
        rc = run_equities(eq_args)
        if rc != 0:
            return rc
    else:
        print(f"[all] skipping equities: {_ALPACA_KEY_ENV}/{_ALPACA_SECRET_ENV} not set")

    # Macro (skip silently if FRED key is missing).
    if os.environ.get(_FRED_KEY_ENV):
        rc = run_macro(eq_args)
        if rc != 0:
            return rc
    else:
        print(f"[all] skipping macro: {_FRED_KEY_ENV} not set")

    # News (skip silently if NewsAPI key is missing).
    if os.environ.get(_NEWSAPI_KEY_ENV):
        rc = run_news(eq_args)
        if rc != 0:
            return rc
    else:
        print(f"[all] skipping news: {_NEWSAPI_KEY_ENV} not set")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared by all subcommands."""
    parser.add_argument(
        "--output",
        default="data/datasets/",
        help="Directory to write the dataset artifacts (default: data/datasets/).",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Start date YYYY-MM-DD (default: derived from --lookback-days).",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="End date YYYY-MM-DD (default: today UTC).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Lookback in days used to derive --start-date when not given.",
    )
    parser.add_argument(
        "--n-folds",
        type=int,
        default=3,
        help="Number of walk-forward folds (default: 3).",
    )
    parser.add_argument(
        "--dataset-id",
        default=None,
        help="Dataset ID (default: auto-generated from symbols/series + dates).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest_real_data",
        description=(
            "Ingest real datasets from vendor APIs (Alpaca, FRED, NewsAPI) "
            "into leakage-safe point-in-time datasets with quality reports."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Ingestion subcommand (equities, macro, news, or all).",
    )

    # --- equities --------------------------------------------------------
    eq_parser = subparsers.add_parser(
        "equities",
        help="Fetch OHLCV bars from Alpaca and ingest them.",
    )
    _add_common_args(eq_parser)
    eq_parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_EQUITY_SYMBOLS),
        help=(f"Comma-separated ticker symbols (default: {','.join(DEFAULT_EQUITY_SYMBOLS)})."),
    )
    eq_parser.add_argument(
        "--timeframe",
        default="1Day",
        help="Bar timeframe as accepted by Alpaca (default: 1Day).",
    )
    eq_parser.add_argument(
        "--label-horizon-days",
        type=int,
        default=5,
        help="Forward-return label horizon in days (default: 5).",
    )
    eq_parser.set_defaults(
        lookback_days=DEFAULT_EQUITY_LOOKBACK_DAYS,
        func=run_equities,
    )

    # --- macro -----------------------------------------------------------
    macro_parser = subparsers.add_parser(
        "macro",
        help="Fetch macro indicators from FRED and ingest them.",
    )
    _add_common_args(macro_parser)
    macro_parser.add_argument(
        "--series",
        default=",".join(DEFAULT_MACRO_SERIES),
        help=(f"Comma-separated FRED series IDs (default: {','.join(DEFAULT_MACRO_SERIES)})."),
    )
    macro_parser.set_defaults(
        lookback_days=DEFAULT_EQUITY_LOOKBACK_DAYS,
        func=run_macro,
    )

    # --- news ------------------------------------------------------------
    news_parser = subparsers.add_parser(
        "news",
        help="Fetch news articles from NewsAPI and ingest them.",
    )
    _add_common_args(news_parser)
    news_parser.add_argument(
        "--query",
        default=DEFAULT_NEWS_QUERY,
        help=f"Search query for NewsAPI (default: {DEFAULT_NEWS_QUERY!r}).",
    )
    news_parser.set_defaults(
        lookback_days=DEFAULT_NEWS_LOOKBACK_DAYS,
        func=run_news,
    )

    # --- all -------------------------------------------------------------
    all_parser = subparsers.add_parser(
        "all",
        help="Run equities, macro, and news ingestions in sequence.",
    )
    _add_common_args(all_parser)
    all_parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_EQUITY_SYMBOLS),
        help=f"Comma-separated ticker symbols (default: {','.join(DEFAULT_EQUITY_SYMBOLS)}).",
    )
    all_parser.add_argument(
        "--series",
        default=",".join(DEFAULT_MACRO_SERIES),
        help=f"Comma-separated FRED series IDs (default: {','.join(DEFAULT_MACRO_SERIES)}).",
    )
    all_parser.add_argument(
        "--query",
        default=DEFAULT_NEWS_QUERY,
        help=f"Search query for NewsAPI (default: {DEFAULT_NEWS_QUERY!r}).",
    )
    all_parser.add_argument(
        "--timeframe",
        default="1Day",
        help="Bar timeframe as accepted by Alpaca (default: 1Day).",
    )
    all_parser.add_argument(
        "--label-horizon-days",
        type=int,
        default=5,
        help="Forward-return label horizon in days (default: 5).",
    )
    all_parser.set_defaults(
        lookback_days=DEFAULT_EQUITY_LOOKBACK_DAYS,
        func=run_all,
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
