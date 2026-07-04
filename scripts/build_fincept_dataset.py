"""Build a training dataset from the EXISTING fincept-terminal data pipeline.

Extracts from Redis:
  1. info.enriched stream — 2,298 enriched news events (headline, symbols,
     event_category, source_quality, recency_score, ts_event)
  2. sig.sentiment stream — 29 LLM-scored sentiment signals
  3. news_alpha:example:* — 277 labeled training examples with 24 sentiment
     features + 5m/30m/4h return labels
  4. md.bars.1m — 7,014 1-min bar events (for price features)

Joins them with yfinance daily bars to produce a combined dataset that
leverages the EXISTING information gathering system instead of fetching
news from scratch via NewsAPI.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import redis
import yfinance as yf

_REPO = pathlib.Path(__file__).resolve().parent.parent
_OUTDIR = _REPO / "data" / "datasets" / "fincept_combined"
_OUTDIR.mkdir(parents=True, exist_ok=True)

NS_PER_DAY = 86_400_000_000_000
NS_PER_MIN = 60_000_000_000
NS_PER_HOUR = 3_600_000_000_000


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)


def dt_to_ns(d: datetime) -> int:
    return int(d.timestamp() * 1e9)


# ─── 1. Extract from Redis ────────────────────────────────────────────────


def extract_redis_data() -> dict:
    """Extract all training-relevant data from Redis."""
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)

    # ── info.enriched stream ──
    print("Extracting info.enriched stream...")
    entries = r.xrange("info.enriched")
    info_events = []
    for entry_id, fields in entries:
        payload_raw = fields.get("payload", "")
        if isinstance(payload_raw, bytes):
            payload_raw = payload_raw.decode()
        try:
            event = json.loads(payload_raw)
            p = event.get("payload", event)
            info_events.append(
                {
                    "event_id": p.get("event_id", entry_id),
                    "ts_event": p.get("ts_event", 0),
                    "headline": p.get("headline", ""),
                    "symbols": p.get("symbols", []),
                    "event_category": p.get("event_category", "general"),
                    "source": p.get("source", ""),
                    "source_type": p.get("source_type", ""),
                    "source_quality": p.get("source_quality", 0.5),
                    "recency_score": p.get("recency_score", 1.0),
                    "novelty_score": p.get("novelty_score", 1.0),
                    "entities": p.get("entities", []),
                    "url": p.get("url", ""),
                }
            )
        except Exception:
            continue
    print(f"  {len(info_events)} info events")

    # ── sig.sentiment stream ──
    print("Extracting sig.sentiment stream...")
    entries = r.xrange("sig.sentiment")
    sentiment_signals = []
    for entry_id, fields in entries:
        payload_raw = fields.get("payload", "")
        if isinstance(payload_raw, bytes):
            payload_raw = payload_raw.decode()
        try:
            event = json.loads(payload_raw)
            p = event.get("payload", event)
            sentiment_signals.append(
                {
                    "ts_event": p.get("ts_event", 0),
                    "symbol": p.get("symbol", ""),
                    "score": p.get("score", 0.0),
                    "confidence": p.get("confidence", 0.0),
                    "event_type": p.get("event_type", "general"),
                    "source_url": p.get("source_url", ""),
                    "source_excerpt": p.get("source_excerpt", ""),
                }
            )
        except Exception:
            continue
    print(f"  {len(sentiment_signals)} sentiment signals")

    # ── news_alpha:example:* hashes ──
    print("Extracting news_alpha examples...")
    alpha_keys = [k for k in r.keys("news_alpha:example:*")]
    alpha_examples = []
    for key in alpha_keys:
        data = r.hgetall(key)
        try:
            frame = json.loads(data.get("frame", "{}"))
            vals = frame.get("values", {})
            tags = frame.get("tags", {})
            example = {
                "key": key,
                "symbol": frame.get("symbol", data.get("symbol", "")),
                "ts_event": int(data.get("ts_event", frame.get("ts_event", 0))),
                "freq": frame.get("freq", ""),
                "start_price": float(data.get("start_price", 0)),
                "label_5m_return": float(data.get("label:5m:return", 0)),
                "label_5m_end_price": float(data.get("label:5m:end_price", 0)),
                "label_30m_return": float(data.get("label:30m:return", 0)),
                "label_30m_end_price": float(data.get("label:30m:end_price", 0)),
                "label_4h_return": float(data.get("label:4h:return", 0)),
                "label_4h_end_price": float(data.get("label:4h:end_price", 0)),
                "latest_event_category": tags.get("latest_event_category", ""),
                "dominant_event_category": tags.get("dominant_event_category", ""),
            }
            # Add all sentiment feature values
            for fname, fval in vals.items():
                if fval is not None:
                    example[f"feat_{fname}"] = float(fval)
                else:
                    example[f"feat_{fname}"] = 0.0
            alpha_examples.append(example)
        except Exception:
            continue
    print(f"  {len(alpha_examples)} labeled examples")

    # ── md.bars.1m stream ──
    print("Extracting md.bars.1m stream...")
    entries = r.xrange("md.bars.1m")
    bars = []
    for entry_id, fields in entries:
        payload_raw = fields.get("payload", "")
        if isinstance(payload_raw, bytes):
            payload_raw = payload_raw.decode()
        try:
            bar = json.loads(payload_raw)
            bars.append(
                {
                    "ts_event": bar.get("ts_event", 0),
                    "symbol": bar.get("symbol", ""),
                    "venue": bar.get("venue", ""),
                    "open": float(bar.get("open", 0)),
                    "high": float(bar.get("high", 0)),
                    "low": float(bar.get("low", 0)),
                    "close": float(bar.get("close", 0)),
                    "volume": float(bar.get("volume", 0)),
                    "trades": int(bar.get("trades", 0)),
                    "vwap": float(bar.get("vwap", 0)),
                }
            )
        except Exception:
            continue
    print(f"  {len(bars)} 1-min bars")

    return {
        "info_events": info_events,
        "sentiment_signals": sentiment_signals,
        "alpha_examples": alpha_examples,
        "bars_1m": bars,
    }


# ─── 2. Build event-level dataset from info.enriched + price data ──────────

EVENT_CATEGORY_ONEHOT = [
    "earnings",
    "analyst",
    "general",
    "market_move",
    "partnership",
    "product",
    "macro",
    "regulatory",
    "security",
]


def build_event_dataset(info_events: list[dict], sentiment_signals: list[dict]) -> pd.DataFrame:
    """Build a per-event dataset with sentiment scores and price labels.

    Each row = one news event for one symbol.
    Features: event_category one-hot, source_quality, recency_score,
              novelty_score, sentiment_score, sentiment_confidence,
              hour_of_day, day_of_week.
    Labels: 5d forward return (to be joined with yfinance).
    """
    print("\nBuilding event-level dataset...")

    # Build sentiment lookup by (symbol, approximate ts)
    sent_by_symbol = defaultdict(list)
    for s in sentiment_signals:
        sym = s["symbol"].upper()
        sent_by_symbol[sym].append(s)

    rows = []
    for event in info_events:
        ts = event["ts_event"]
        if ts == 0:
            continue
        dt_event = ns_to_dt(ts)

        # One row per symbol
        for sym in event["symbols"]:
            sym = sym.upper()
            if not sym or sym == "None":
                continue

            # Find matching sentiment signal (within 60 seconds of event)
            matched_sent = None
            for s in sent_by_symbol.get(sym, []):
                if abs(s["ts_event"] - ts) < 60 * NS_PER_MIN:
                    matched_sent = s
                    break

            row = {
                "event_id": event["event_id"],
                "symbol": sym,
                "ts_event_ns": ts,
                "ts_event_dt": dt_event,
                "date": dt_event.strftime("%Y-%m-%d"),
                "headline": event["headline"][:200],
                "event_category": event["event_category"],
                "source": event["source"],
                "source_quality": event["source_quality"],
                "recency_score": event["recency_score"],
                "novelty_score": event["novelty_score"],
                "sentiment_score": matched_sent["score"] if matched_sent else 0.0,
                "sentiment_confidence": matched_sent["confidence"] if matched_sent else 0.0,
                "has_sentiment": 1 if matched_sent else 0,
                "hour_of_day": dt_event.hour,
                "day_of_week": dt_event.weekday(),
                "is_market_hours": 1 if 13 <= dt_event.hour <= 21 else 0,
            }

            # One-hot event category
            for cat in EVENT_CATEGORY_ONEHOT:
                row[f"cat_{cat}"] = 1 if event["event_category"] == cat else 0

            rows.append(row)

    df = pd.DataFrame(rows)
    print(f"  {len(df)} event-symbol rows from {len(info_events)} events")
    print(f"  {df['has_sentiment'].sum()} rows with LLM sentiment scores")
    print(f"  {df['symbol'].nunique()} unique symbols")
    return df


# ─── 3. Join with yfinance for price labels ───────────────────────────────


def add_price_labels(df: pd.DataFrame, horizon_days: int = 5) -> pd.DataFrame:
    """Add forward return labels using yfinance daily bars."""
    print(f"\nAdding {horizon_days}d forward return labels via yfinance...")

    # Get unique symbols
    symbols = sorted(df["symbol"].unique())
    print(f"  Fetching price data for {len(symbols)} symbols...")

    # Fetch yfinance data for all symbols
    price_data = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="2y")
            if len(hist) > 0:
                price_data[sym] = hist
                print(f"    {sym}: {len(hist)} bars")
            else:
                print(f"    {sym}: no data")
        except Exception as e:
            print(f"    {sym}: error {e}")
        time.sleep(0.3)

    # For each event row, find the close on the event date and horizon_days later
    forward_returns = []
    forward_labels = []
    close_at_event = []

    for _, row in df.iterrows():
        sym = row["symbol"]
        event_date = row["date"]

        if sym not in price_data:
            forward_returns.append(np.nan)
            forward_labels.append(np.nan)
            close_at_event.append(np.nan)
            continue

        hist = price_data[sym]
        # Find the close on or before the event date
        event_date_ts = pd.Timestamp(event_date, tz="UTC")
        available_dates = hist.index[hist.index <= event_date_ts]

        if len(available_dates) == 0:
            forward_returns.append(np.nan)
            forward_labels.append(np.nan)
            close_at_event.append(np.nan)
            continue

        entry_date = available_dates[-1]
        entry_close = hist.loc[entry_date, "Close"]

        # Find the close horizon_days later
        future_dates = hist.index[hist.index > entry_date]
        if len(future_dates) < horizon_days:
            forward_returns.append(np.nan)
            forward_labels.append(np.nan)
            close_at_event.append(float(entry_close))
            continue

        exit_date = future_dates[horizon_days - 1]
        exit_close = hist.loc[exit_date, "Close"]

        fwd_ret = (exit_close - entry_close) / entry_close
        forward_returns.append(float(fwd_ret))
        forward_labels.append(1 if fwd_ret > 0 else 0)
        close_at_event.append(float(entry_close))

    df["close_at_event"] = close_at_event
    df[f"forward_return_{horizon_days}d"] = forward_returns
    df[f"forward_label_{horizon_days}d"] = forward_labels

    # Drop rows without price data
    before = len(df)
    df = df.dropna(subset=[f"forward_return_{horizon_days}d"])
    after = len(df)
    print(f"  Dropped {before - after} rows without price data")
    print(f"  Final rows: {after}")
    print(
        f"  Label balance: {(df[f'forward_label_{horizon_days}d'] == 1).sum()} up / "
        f"{(df[f'forward_label_{horizon_days}d'] == 0).sum()} down "
        f"({(df[f'forward_label_{horizon_days}d'] == 1).mean() * 100:.1f}% up)"
    )

    return df


# ─── 4. Build alpha_examples dataset (already labeled) ────────────────────


def build_alpha_dataset(alpha_examples: list[dict]) -> pd.DataFrame:
    """Build a dataset from the already-labeled news_alpha examples."""
    print("\nBuilding alpha_examples dataset...")
    if not alpha_examples:
        print("  No alpha examples found")
        return pd.DataFrame()

    df = pd.DataFrame(alpha_examples)
    # Use 4h return as the main label (most balanced)
    df["label_4h_binary"] = (df["label_4h_return"] > 0).astype(int)
    df["label_30m_binary"] = (df["label_30m_return"] > 0).astype(int)
    df["label_5m_binary"] = (df["label_5m_return"] > 0).astype(int)

    print(f"  {len(df)} examples")
    print(f"  Symbols: {df['symbol'].unique()}")
    print(
        f"  4h label balance: {df['label_4h_binary'].sum()} up / "
        f"{(len(df) - df['label_4h_binary'].sum())} down"
    )
    return df


# ─── 5. Main ──────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 80)
    print("FINCEPT COMBINED DATASET BUILDER (from existing Redis data)")
    print("=" * 80)

    # Extract from Redis
    data = extract_redis_data()

    # Build event-level dataset
    event_df = build_event_dataset(data["info_events"], data["sentiment_signals"])

    # Add price labels
    event_df = add_price_labels(event_df, horizon_days=5)

    # Build alpha examples dataset
    alpha_df = build_alpha_dataset(data["alpha_examples"])

    # Save event dataset
    event_path = _OUTDIR / "event_dataset.parquet"
    event_csv = _OUTDIR / "event_dataset.csv"
    event_df.to_parquet(event_path, index=False)
    event_df.to_csv(event_csv, index=False)
    print(f"\nEvent dataset saved: {event_path}")
    print(f"  Rows: {len(event_df)}")
    print(f"  Features: {len(event_df.columns)}")
    print(f"  Size: {event_path.stat().st_size / 1024 / 1024:.1f} MB")

    # Save alpha dataset
    if len(alpha_df) > 0:
        alpha_path = _OUTDIR / "alpha_dataset.parquet"
        alpha_csv = _OUTDIR / "alpha_dataset.csv"
        alpha_df.to_parquet(alpha_path, index=False)
        alpha_df.to_csv(alpha_csv, index=False)
        print(f"\nAlpha dataset saved: {alpha_path}")
        print(f"  Rows: {len(alpha_df)}")
        print(f"  Features: {len(alpha_df.columns)}")

    # Summary
    print(f"\n{'=' * 80}")
    print("DATASET SUMMARY")
    print(f"{'=' * 80}")
    print(f"  Event dataset:  {len(event_df)} rows, {len(event_df.columns)} features")
    print(f"  Alpha dataset:  {len(alpha_df)} rows, {len(alpha_df.columns)} features")
    print(f"  Info events:    {len(data['info_events'])}")
    print(f"  Sentiment sigs: {len(data['sentiment_signals'])}")
    print(f"  Alpha examples: {len(data['alpha_examples'])}")
    print(f"  1-min bars:     {len(data['bars_1m'])}")

    print(f"\n{'=' * 80}")
    print("FINCEPT COMBINED DATASET READY")
    print(f"{'=' * 80}")
    print(f"  Event Parquet:  {event_path}")
    print(f"  Event CSV:      {event_csv}")
    if len(alpha_df) > 0:
        print(f"  Alpha Parquet:  {alpha_path}")
        print(f"  Alpha CSV:      {alpha_csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
