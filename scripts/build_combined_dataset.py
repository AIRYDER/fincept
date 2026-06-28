"""Build combined dataset: technical features + sentiment features + calendar features.

This script:
1. Fetches yfinance bars for 20 symbols + SPY + VIX (10 years)
2. Computes 18 existing technical features
3. Computes 9 calendar/regime features (full period)
4. Computes 6 interaction features (full period)
5. Fetches news from NewsAPI (last 30 days, all 20 symbols)
6. Fetches StockTwits messages (all 20 symbols, free API)
7. Scores sentiment with 4-LLM ensemble (OpenAI + Anthropic + xAI + MiniMax)
8. Computes 13 per-event-type sentiment features (last 30 days, 0.0 for historical)
9. Merges everything into a combined parquet (~46 features)
10. Uploads to RunPod network volume via S3

Output: data/datasets/combined/combined_dataset.parquet
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any, Sequence

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

# --- API Keys (read from env vars or .env file) ---
# Keys should be set in environment or a local .env file (not committed)
_ENV_FILE = _REPO_ROOT / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

NS_PER_DAY = 86_400_000_000_000

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "JPM", "V", "JNJ",
    "WMT", "PG", "UNH", "HD", "MA",
    "DIS", "BAC", "XOM", "KO", "PEP",
]

# Existing 18 technical features
TECHNICAL_FEATURES = (
    "ret_1d", "ret_5d", "ret_10d", "ret_20d", "mom_10d",
    "vol_20d", "vol_60d", "vol_ratio", "vol_regime",
    "rsi_14", "bb_position", "price_vs_sma50", "price_vs_sma200",
    "atr_ratio", "spy_corr_20d", "spy_beta_60d",
    "vix_level", "vix_change_5d",
)

# New calendar features (9)
CALENDAR_FEATURES = (
    "day_of_week", "day_of_month", "month", "quarter",
    "is_month_end", "is_quarter_end", "year",
    "days_since_start", "trend_normalized",
)

# New interaction features (6)
INTERACTION_FEATURES = (
    "rsi_x_vol_ratio", "mom_x_vol_regime",
    "ret_5d_x_bb_position", "vol_20d_x_vix",
    "price_vs_sma50_x_spy_corr", "atr_x_vol_60d",
)

# Sentiment features (13) — per event type + aggregate
SENTIMENT_FEATURES = (
    "sent_regulatory", "sent_earnings", "sent_guidance", "sent_macro",
    "sent_product", "sent_security", "sent_litigation", "sent_partnership",
    "sent_financing", "sent_ma", "sent_general", "sent_mean", "sent_count",
)

ALL_FEATURES = TECHNICAL_FEATURES + CALENDAR_FEATURES + INTERACTION_FEATURES + SENTIMENT_FEATURES


# ─── 1. Fetch yfinance bars ─────────────────────────────────────────────

def fetch_yfinance_bars(symbols: list[str], years: int) -> dict[str, list[dict]]:
    import yfinance as yf
    from datetime import datetime, timezone

    end = datetime.now(timezone.utc)
    start = datetime(end.year - years, end.month, end.day, tzinfo=timezone.utc)

    bars_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        print(f"  Fetching {sym:6s}...", end=" ", flush=True)
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
            if df.empty:
                print("NO DATA")
                continue
            bars = []
            for idx, row in df.iterrows():
                ts_ns = int(idx.tz_convert("UTC").value)
                bars.append({
                    "ts_event": ts_ns,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                })
            bars_by_symbol[sym] = bars
            print(f"{len(bars)} bars")
        except Exception as exc:
            print(f"ERROR: {exc}")
    return bars_by_symbol


# ─── 2. Technical features (reuse from train_deep_real_model.py) ────────

def compute_rsi(close: list[float], period: int = 14) -> list[float]:
    import numpy as np
    n = len(close)
    rsi = np.full(n, 50.0)
    if n < period + 1:
        return rsi.tolist()
    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return rsi.tolist()


def compute_sma(close: list[float], period: int) -> list[float]:
    import numpy as np
    n = len(close)
    sma = np.zeros(n)
    for i in range(period - 1, n):
        sma[i] = float(np.mean(close[i - period + 1: i + 1]))
    return sma.tolist()


def compute_atr(high: list[float], low: list[float], close: list[float], period: int = 14) -> list[float]:
    import numpy as np
    n = len(close)
    atr = np.zeros(n)
    if n < period + 1:
        return atr.tolist()
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    atr[period] = float(np.mean(tr[1:period + 1]))
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr.tolist()


def compute_technical_features(
    bars: list[dict],
    spy_bars: list[dict] | None,
    vix_bars: list[dict] | None,
    label_horizon_days: int,
) -> list[dict[str, float]]:
    import numpy as np

    n = len(bars)
    if n < 205 + label_horizon_days:
        return []

    close = np.array([b["close"] for b in bars], dtype=np.float64)
    high = np.array([b["high"] for b in bars], dtype=np.float64)
    low = np.array([b["low"] for b in bars], dtype=np.float64)
    volume = np.array([b["volume"] for b in bars], dtype=np.float64)
    ts = np.array([b["ts_event"] for b in bars], dtype=np.int64)

    log_close = np.log(close)
    log_ret = np.zeros(n, dtype=np.float64)
    log_ret[1:] = np.diff(log_close)

    ret_1d = log_ret.copy()
    ret_5d = np.zeros(n)
    ret_10d = np.zeros(n)
    ret_20d = np.zeros(n)
    mom_10d = np.zeros(n)
    for i in range(n):
        if i >= 5:
            ret_5d[i] = log_close[i] - log_close[i - 5]
        if i >= 10:
            ret_10d[i] = log_close[i] - log_close[i - 10]
            mom_10d[i] = close[i] / close[i - 10] - 1.0
        if i >= 20:
            ret_20d[i] = log_close[i] - log_close[i - 20]

    vol_20d = np.zeros(n)
    vol_60d = np.zeros(n)
    for i in range(n):
        if i >= 19:
            vol_20d[i] = float(np.std(log_ret[i - 19: i + 1], ddof=0))
        if i >= 59:
            vol_60d[i] = float(np.std(log_ret[i - 59: i + 1], ddof=0))

    vol_mean_20 = np.zeros(n)
    for i in range(n):
        if i >= 19:
            vol_mean_20[i] = float(np.mean(volume[i - 19: i + 1]))
    vol_ratio = np.where(vol_mean_20 > 0, volume / np.where(vol_mean_20 > 0, vol_mean_20, 1.0), 1.0)
    vol_regime = np.where(vol_60d > 0, vol_20d / np.where(vol_60d > 0, vol_60d, 1.0), 1.0)

    rsi = np.array(compute_rsi(close.tolist(), 14))
    sma20 = np.array(compute_sma(close.tolist(), 20))
    std20 = np.zeros(n)
    for i in range(19, n):
        std20[i] = float(np.std(close[i - 19: i + 1], ddof=0))
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    bb_width = upper - lower
    bb_position = np.where(bb_width > 0, (close - lower) / np.where(bb_width > 0, bb_width, 1.0), 0.5)

    sma50 = np.array(compute_sma(close.tolist(), 50))
    sma200 = np.array(compute_sma(close.tolist(), 200))
    price_vs_sma50 = np.where(np.isfinite(sma50) & (sma50 > 0), close / sma50 - 1.0, 0.0)
    price_vs_sma200 = np.where(np.isfinite(sma200) & (sma200 > 0), close / sma200 - 1.0, 0.0)

    atr = np.array(compute_atr(high.tolist(), low.tolist(), close.tolist(), 14))
    atr_ratio = np.array(atr) / np.where(close > 0, close, 1.0)

    # Cross-asset: SPY correlation + beta
    spy_corr = np.zeros(n)
    spy_beta = np.zeros(n)
    if spy_bars is not None and len(spy_bars) > 60:
        spy_ts = {b["ts_event"]: b["close"] for b in spy_bars}
        spy_close = []
        spy_ret = []
        for i in range(n):
            t = ts[i]
            if t in spy_ts:
                spy_close.append(spy_ts[t])
            else:
                spy_close.append(0.0)
        spy_close_arr = np.array(spy_close, dtype=np.float64)
        spy_log_ret = np.zeros(n)
        valid = spy_close_arr > 0
        if valid.sum() > 1:
            spy_log_ret[valid] = np.diff(np.log(spy_close_arr[valid]), prepend=np.log(spy_close_arr[valid][0]))
        for i in range(60, n):
            if valid[i - 19: i + 1].sum() >= 15:
                cov = np.cov(log_ret[i - 19: i + 1], spy_log_ret[i - 19: i + 1])[0, 1]
                var = np.var(spy_log_ret[i - 19: i + 1])
                if var > 0:
                    spy_corr[i] = float(cov / (np.std(log_ret[i - 19: i + 1]) * np.std(spy_log_ret[i - 19: i + 1]) + 1e-10))
                    spy_beta[i] = float(cov / var)

    # VIX features
    vix_level = np.zeros(n)
    vix_change_5d = np.zeros(n)
    if vix_bars is not None and len(vix_bars) > 5:
        vix_ts = {b["ts_event"]: b["close"] for b in vix_bars}
        vix_vals = []
        for i in range(n):
            t = ts[i]
            if t in vix_ts:
                vix_vals.append(vix_ts[t])
            else:
                vix_vals.append(0.0)
        vix_arr = np.array(vix_vals, dtype=np.float64)
        vix_level = vix_arr
        for i in range(5, n):
            if vix_arr[i - 5] > 0:
                vix_change_5d[i] = (vix_arr[i] / vix_arr[i - 5] - 1.0)

    # --- Calendar features ---
    day_of_week = np.zeros(n)
    day_of_month = np.zeros(n)
    month = np.zeros(n)
    quarter = np.zeros(n)
    is_month_end = np.zeros(n)
    is_quarter_end = np.zeros(n)
    year_arr = np.zeros(n)
    days_since_start = np.zeros(n)
    first_ts = ts[0]

    for i in range(n):
        d = dt.datetime.fromtimestamp(ts[i] / 1e9, tz=dt.timezone.utc)
        day_of_week[i] = float(d.weekday())  # 0=Monday, 6=Sunday
        day_of_month[i] = float(d.day)
        month[i] = float(d.month)
        quarter[i] = float((d.month - 1) // 3 + 1)
        year_arr[i] = float(d.year)
        # Is this the last trading day of the month/quarter?
        if i + 1 < n:
            d_next = dt.datetime.fromtimestamp(ts[i + 1] / 1e9, tz=dt.timezone.utc)
            if d_next.month != d.month:
                is_month_end[i] = 1.0
            if (d_next.month - 1) // 3 != (d.month - 1) // 3:
                is_quarter_end[i] = 1.0
        days_since_start[i] = (ts[i] - first_ts) / NS_PER_DAY
    trend_normalized = days_since_start / max(days_since_start.max(), 1.0)

    # --- Interaction features ---
    rsi_x_vol_ratio = rsi / 100.0 * vol_ratio
    mom_x_vol_regime = mom_10d * vol_regime
    ret_5d_x_bb_position = ret_5d * bb_position
    vol_20d_x_vix = vol_20d * vix_level
    price_vs_sma50_x_spy_corr = price_vs_sma50 * spy_corr
    atr_x_vol_60d = atr_ratio * vol_60d

    # --- Label: binary forward return ---
    horizon_ns = label_horizon_days * NS_PER_DAY
    label = np.zeros(n)
    for i in range(n):
        target_ts = ts[i] + horizon_ns
        # Find the closest bar at or after target_ts
        future_idx = np.searchsorted(ts, target_ts)
        if future_idx < n:
            label[i] = 1.0 if close[future_idx] > close[i] else 0.0

    # --- Build rows ---
    rows = []
    for i in range(204, n - label_horizon_days):  # Skip warmup + label horizon
        row = {
            "decision_time": int(ts[i]),
            "ret_1d": float(ret_1d[i]),
            "ret_5d": float(ret_5d[i]),
            "ret_10d": float(ret_10d[i]),
            "ret_20d": float(ret_20d[i]),
            "mom_10d": float(mom_10d[i]),
            "vol_20d": float(vol_20d[i]),
            "vol_60d": float(vol_60d[i]),
            "vol_ratio": float(vol_ratio[i]),
            "vol_regime": float(vol_regime[i]),
            "rsi_14": float(rsi[i]),
            "bb_position": float(bb_position[i]),
            "price_vs_sma50": float(price_vs_sma50[i]),
            "price_vs_sma200": float(price_vs_sma200[i]),
            "atr_ratio": float(atr_ratio[i]),
            "spy_corr_20d": float(spy_corr[i]),
            "spy_beta_60d": float(spy_beta[i]),
            "vix_level": float(vix_level[i]),
            "vix_change_5d": float(vix_change_5d[i]),
            # Calendar
            "day_of_week": float(day_of_week[i]),
            "day_of_month": float(day_of_month[i]),
            "month": float(month[i]),
            "quarter": float(quarter[i]),
            "is_month_end": float(is_month_end[i]),
            "is_quarter_end": float(is_quarter_end[i]),
            "year": float(year_arr[i]),
            "days_since_start": float(days_since_start[i]),
            "trend_normalized": float(trend_normalized[i]),
            # Interactions
            "rsi_x_vol_ratio": float(rsi_x_vol_ratio[i]),
            "mom_x_vol_regime": float(mom_x_vol_regime[i]),
            "ret_5d_x_bb_position": float(ret_5d_x_bb_position[i]),
            "vol_20d_x_vix": float(vol_20d_x_vix[i]),
            "price_vs_sma50_x_spy_corr": float(price_vs_sma50_x_spy_corr[i]),
            "atr_x_vol_60d": float(atr_x_vol_60d[i]),
            # Sentiment (filled later, 0.0 for now)
            "sent_regulatory": 0.0,
            "sent_earnings": 0.0,
            "sent_guidance": 0.0,
            "sent_macro": 0.0,
            "sent_product": 0.0,
            "sent_security": 0.0,
            "sent_litigation": 0.0,
            "sent_partnership": 0.0,
            "sent_financing": 0.0,
            "sent_ma": 0.0,
            "sent_general": 0.0,
            "sent_mean": 0.0,
            "sent_count": 0.0,
            # Label
            "label": float(label[i]),
        }
        rows.append(row)
    return rows


# ─── 3. Fetch news from NewsAPI ─────────────────────────────────────────

def fetch_newsapi_articles(symbols: list[str], days_back: int = 30) -> list[dict]:
    """Fetch news articles from NewsAPI for the given symbols.

    Fetches in 5-day chunks to ensure coverage across the full date range
    (NewsAPI returns most recent first, so a single request only gets the
    latest articles).
    """
    import httpx

    key = os.environ["NEWSAPI_KEY"]
    end_date = dt.datetime.now(dt.timezone.utc)
    start_date = end_date - dt.timedelta(days=days_back)

    all_articles = []
    for sym in symbols:
        print(f"  NewsAPI {sym:6s}...", end=" ", flush=True)
        sym_count = 0
        # Fetch in 5-day chunks, oldest first
        chunk_start = start_date
        while chunk_start < end_date:
            chunk_end = min(chunk_start + dt.timedelta(days=5), end_date)
            try:
                r = httpx.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": f"${sym} OR {sym} stock",
                        "from": chunk_start.strftime("%Y-%m-%d"),
                        "to": chunk_end.strftime("%Y-%m-%d"),
                        "language": "en",
                        "sortBy": "publishedAt",
                        "pageSize": 50,
                        "apiKey": key,
                    },
                    timeout=30,
                )
                if r.status_code == 200:
                    articles = r.json().get("articles", [])
                    for a in articles:
                        all_articles.append({
                            "symbol": sym,
                            "headline": a.get("title") or "",
                            "body": (a.get("description") or "") + " " + (a.get("content") or ""),
                            "source": (a.get("source") or {}).get("name", "newsapi"),
                            "url": a.get("url"),
                            "published_at": a.get("publishedAt") or "",
                        })
                        sym_count += 1
                else:
                    pass  # Rate limited or error, skip this chunk
            except Exception:
                pass
            chunk_start = chunk_end
            time.sleep(0.5)  # Rate limit between chunks
        print(f"{sym_count} articles")

    print(f"  Total articles: {len(all_articles)}")
    return all_articles


# ─── 4. Fetch StockTwits messages ───────────────────────────────────────

def fetch_stocktwits_messages(symbols: list[str]) -> list[dict]:
    """Fetch messages from StockTwits public API."""
    import httpx

    all_msgs = []
    for sym in symbols:
        print(f"  StockTwits {sym:6s}...", end=" ", flush=True)
        try:
            r = httpx.get(
                f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json",
                params={"limit": 30},
                timeout=30,
            )
            if r.status_code == 200:
                msgs = r.json().get("messages", [])
                for m in msgs:
                    all_msgs.append({
                        "symbol": sym,
                        "headline": m.get("body", ""),
                        "body": "",
                        "source": "stocktwits",
                        "url": None,
                        "published_at": m.get("created_at", ""),
                        "sentiment": m.get("entities", {}).get("sentiment", {}).get("basic", None),
                    })
                print(f"{len(msgs)} msgs")
            else:
                print(f"HTTP {r.status_code}")
        except Exception as exc:
            print(f"ERROR: {exc}")
        time.sleep(0.5)

    print(f"  Total messages: {len(all_msgs)}")
    return all_msgs


# ─── 5. Score sentiment with LLM ensemble ───────────────────────────────

def score_sentiment_llm(headline: str, body: str, provider: str) -> tuple[float, float]:
    """Score sentiment using a single LLM provider. Returns (score, confidence)."""
    import httpx

    text = f"{headline}\n{body}"[:500]
    prompt = f'Analyze the financial sentiment of this text. Return JSON: {{"score": float, "confidence": float}}. Score: -1=very bearish, 0=neutral, 1=very bullish.\n\nText: "{text}"'

    try:
        if provider == "openai":
            r = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0,
                },
                timeout=30,
            )
            content = r.json()["choices"][0]["message"]["content"]
        elif provider == "anthropic":
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            content = r.json()["content"][0]["text"]
        elif provider == "xai":
            r = httpx.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"},
                json={
                    "model": "grok-3-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0,
                },
                timeout=30,
            )
            content = r.json()["choices"][0]["message"]["content"]
        elif provider == "minimax":
            r = httpx.post(
                "https://api.minimax.chat/v1/text/chatcompletion_v2",
                headers={"Authorization": f"Bearer {os.environ['MINIMAX_API_KEY']}"},
                json={
                    "model": "MiniMax-Text-01",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0,
                },
                timeout=30,
            )
            content = r.json()["choices"][0]["message"]["content"]
        else:
            return 0.0, 0.0

        # Parse JSON from response
        import re
        json_match = re.search(r'\{[^}]+\}', content)
        if json_match:
            data = json.loads(json_match.group())
            score = max(-1.0, min(1.0, float(data.get("score", 0.0))))
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
            return score, confidence
        return 0.0, 0.0
    except Exception:
        return 0.0, 0.0


def score_sentiment_ensemble(headline: str, body: str) -> tuple[float, float, float]:
    """Score sentiment using 4-LLM ensemble. Returns (ensemble_score, confidence, disagreement)."""
    providers = ["openai", "anthropic", "xai", "minimax"]
    scores = []
    confidences = []

    for provider in providers:
        score, conf = score_sentiment_llm(headline, body, provider)
        if conf > 0:
            scores.append(score)
            confidences.append(conf)
        time.sleep(0.2)

    if len(scores) >= 2:
        import numpy as np
        ensemble_score = float(np.mean(scores))
        ensemble_std = float(np.std(scores))
        ensemble_conf = float(np.mean(confidences)) * (1.0 - ensemble_std)
        return ensemble_score, ensemble_conf, ensemble_std
    return 0.0, 0.0, 0.0


# ─── 6. Classify event type ─────────────────────────────────────────────

EVENT_KEYWORDS = {
    "regulatory": ["regulator", "regulation", "fda", "sec", "doj", "antitrust", "ban", "compliance", "fine", "penalty"],
    "earnings": ["earnings", "revenue", "eps", "quarter", "q1", "q2", "q3", "q4", "beat", "miss", "guidance"],
    "guidance": ["guidance", "outlook", "forecast", "raise", "lower", "revised", "expectations"],
    "macro": ["fed", "interest rate", "inflation", "cpi", "gdp", "unemployment", "recession", "treasury", "yield"],
    "product": ["launch", "product", "release", "update", "feature", " unveil", "announce", "debut"],
    "security": ["breach", "hack", "cyber", "vulnerability", "security", "data leak", "ransomware"],
    "litigation": ["lawsuit", "sue", "sued", "court", "judge", "ruling", "verdict", "settlement", "patent"],
    "partnership": ["partner", "partnership", "collaboration", "deal", "agreement", "joint venture", "alliance"],
    "financing": ["offering", "debt", "bond", "loan", "credit", "raise capital", "dilution", "buyback", "repurchase"],
    "m&a": ["acquire", "acquisition", "merger", "merge", "buyout", "takeover", "bid", "buy out"],
}

def classify_event(headline: str, body: str) -> str:
    text = (headline + " " + body).lower()
    best_type = "general"
    best_score = 0
    for event_type, keywords in EVENT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_type = event_type
    return best_type


# ─── 7. Compute sentiment features ──────────────────────────────────────

def compute_sentiment_features(
    articles: list[dict],
    messages: list[dict],
    symbols: list[str],
    all_rows: list[dict],
    lookback_days: int = 3,
    llm_subset: int = 0,
) -> dict[str, dict[int, dict[str, float]]]:
    """Compute per-event-type sentiment features for each (symbol, decision_time).

    Uses naive wordlist for bulk scoring and LLM ensemble for a subset.
    Returns: {symbol: {decision_time_ns: {feature_name: value}}}
    """
    print(f"\n  Scoring sentiment for {len(articles)} articles + {len(messages)} messages...")

    # Naive wordlist sentiment (instant, no API calls)
    POSITIVE_WORDS = {
        "beat", "beats", "surpass", "exceed", "strong", "growth", "grow", "growing",
        "profit", "profitable", "rally", "surge", "jump", "soar", "gain", "gains",
        "bullish", "optimistic", "positive", "upgrade", "upgraded", "buy", "outperform",
        "raise", "raised", "boost", "boosted", "record", "high", "breakthrough",
        "innovate", "innovation", "launch", "win", "wins", "deal", "partnership",
        "approve", "approval", "clear", "cleared", "success", "successful",
    }
    NEGATIVE_WORDS = {
        "miss", "misses", "fall", "falls", "drop", "drops", "decline", "declining",
        "loss", "losses", "bearish", "pessimistic", "negative", "downgrade", "downgraded",
        "sell", "underperform", "cut", "cuts", "reduced", "lower", "weak", "weakness",
        "fear", "fears", "concern", "concerns", "risk", "risky", "threat", "threaten",
        "lawsuit", "sued", "sue", "investigation", "probe", "fraud", "scandal",
        "recall", "defect", "breach", "hack", "cyber", "attack", "crash", "plunge",
        "tumble", "slump", "default", "bankrupt", "bankruptcy", "halt", "halted",
    }

    def naive_score(text: str) -> tuple[float, float]:
        words = set(text.lower().split())
        pos = sum(1 for w in POSITIVE_WORDS if w in words)
        neg = sum(1 for w in NEGATIVE_WORDS if w in words)
        total = pos + neg
        if total == 0:
            return 0.0, 0.0
        score = (pos - neg) / total
        confidence = min(1.0, total / 10.0)
        return score, confidence

    # Score all items
    scored_items = []  # list of (symbol, ts_ns, event_type, score, confidence)

    # Score articles with naive wordlist (bulk) + LLM ensemble (subset)
    for i, article in enumerate(articles):
        if i % 200 == 0:
            print(f"    Articles: {i}/{len(articles)}...", flush=True)
        headline = article["headline"] or ""
        body = article.get("body", "") or ""
        text = f"{headline}\n{body}"

        # Use LLM ensemble for first N articles, naive for the rest
        if i < llm_subset:
            score, conf, _ = score_sentiment_ensemble(headline, body)
        else:
            score, conf = naive_score(text)

        event_type = classify_event(headline, body)
        try:
            ts_ns = int(dt.datetime.fromisoformat(article["published_at"].replace("Z", "+00:00")).timestamp() * 1e9)
        except Exception:
            continue
        scored_items.append((article["symbol"], ts_ns, event_type, score, conf))

    # Score StockTwits messages (use human sentiment if available)
    for msg in messages:
        human_sent = msg.get("sentiment")
        if human_sent == "Bullish":
            score, conf = 0.8, 0.9
        elif human_sent == "Bearish":
            score, conf = -0.8, 0.9
        else:
            score, conf = naive_score(msg["headline"])
        event_type = "social"
        try:
            ts_ns = int(dt.datetime.fromisoformat(msg["published_at"].replace("Z", "+00:00")).timestamp() * 1e9)
        except Exception:
            continue
        scored_items.append((msg["symbol"], ts_ns, event_type, score, conf))

    print(f"  Scored {len(scored_items)} items total")
    print(f"    LLM-scored: {min(llm_subset, len(articles))}")
    print(f"    Naive-scored: {max(0, len(articles) - llm_subset) + len(messages)}")

    # Debug: check timestamp ranges
    if scored_items:
        min_ts = min(t for _, t, _, _, _ in scored_items)
        max_ts = max(t for _, t, _, _, _ in scored_items)
        print(f"  Article timestamp range: {dt.datetime.fromtimestamp(min_ts/1e9, tz=dt.timezone.utc)} to {dt.datetime.fromtimestamp(max_ts/1e9, tz=dt.timezone.utc)}")
    if all_rows:
        min_dt = min(r["decision_time"] for r in all_rows)
        max_dt = max(r["decision_time"] for r in all_rows)
        print(f"  Decision time range: {dt.datetime.fromtimestamp(min_dt/1e9, tz=dt.timezone.utc)} to {dt.datetime.fromtimestamp(max_dt/1e9, tz=dt.timezone.utc)}")

    # Build lookup: {symbol: [(ts_ns, event_type, score, conf), ...]}
    items_by_symbol: dict[str, list] = {}
    for sym, ts_ns, event_type, score, conf in scored_items:
        items_by_symbol.setdefault(sym, []).append((ts_ns, event_type, score, conf))

    # For each (symbol, decision_time), compute sentiment features from lookback window
    import numpy as np
    lookback_ns = lookback_days * NS_PER_DAY
    result: dict[str, dict[int, dict[str, float]]] = {}

    # Get all unique decision times
    all_dt_set = set()
    for row in all_rows:
        all_dt_set.add(row["decision_time"])

    total_matches = 0
    for sym in symbols:
        sym_items = items_by_symbol.get(sym, [])
        result[sym] = {}
        sym_matches = 0
        for dt_ns in all_dt_set:
            window_start = dt_ns - lookback_ns
            window_items = [(t, et, s, c) for t, et, s, c in sym_items if window_start <= t <= dt_ns]
            if not window_items:
                continue
            sym_matches += 1

            features = {f: 0.0 for f in SENTIMENT_FEATURES}
            event_scores: dict[str, list[float]] = {}
            all_scores = []
            for _, et, s, c in window_items:
                event_scores.setdefault(et, []).append(s * c)
                all_scores.append(s * c)

            for et, scores in event_scores.items():
                feat_name = f"sent_{et.replace('&', 'a')}"
                if feat_name in features:
                    features[feat_name] = float(np.mean(scores)) if scores else 0.0

            features["sent_mean"] = float(np.mean(all_scores)) if all_scores else 0.0
            features["sent_count"] = float(len(all_scores))
            result[sym][dt_ns] = features
        total_matches += sym_matches
        if sym_items:
            print(f"    {sym}: {len(sym_items)} items, {sym_matches} decision-time matches")

    print(f"  Total decision-time matches: {total_matches}")

    return result


# ─── 8. Main pipeline ───────────────────────────────────────────────────

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build combined dataset with sentiment features.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--label-horizon-days", type=int, default=5)
    parser.add_argument("--news-days-back", type=int, default=30)
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print("=" * 70)
    print("COMBINED DATASET BUILDER: Technical + Sentiment + Calendar Features")
    print("=" * 70)
    print(f"  Symbols: {symbols}")
    print(f"  Years: {args.years}")
    print(f"  Label horizon: {args.label_horizon_days}d")
    print(f"  News lookback: {args.news_days_back}d")
    print(f"  Total features: {len(ALL_FEATURES)}")
    print(f"    Technical: {len(TECHNICAL_FEATURES)}")
    print(f"    Calendar: {len(CALENDAR_FEATURES)}")
    print(f"    Interaction: {len(INTERACTION_FEATURES)}")
    print(f"    Sentiment: {len(SENTIMENT_FEATURES)}")

    # 1. Fetch bars
    print(f"\n{'='*70}")
    print("STEP 1: FETCH YFINANCE BARS")
    print("="*70)
    import numpy as np  # needed by compute_sentiment_features
    fetch_symbols = symbols + ["SPY", "^VIX"]
    bars_by_symbol = fetch_yfinance_bars(fetch_symbols, args.years)
    spy_bars = bars_by_symbol.pop("SPY", None)
    vix_bars = bars_by_symbol.pop("^VIX", None)
    total_bars = sum(len(b) for b in bars_by_symbol.values())
    print(f"\n  Total: {total_bars} bars, {len(bars_by_symbol)} symbols")

    # 2. Compute technical + calendar + interaction features
    print(f"\n{'='*70}")
    print("STEP 2: COMPUTE TECHNICAL + CALENDAR + INTERACTION FEATURES")
    print("="*70)
    all_rows = []
    for sym, bars in bars_by_symbol.items():
        print(f"  {sym:6s}...", end=" ", flush=True)
        rows = compute_technical_features(bars, spy_bars, vix_bars, args.label_horizon_days)
        for r in rows:
            r["__symbol"] = sym
        all_rows.extend(rows)
        print(f"{len(rows)} rows")
    print(f"\n  Total rows: {len(all_rows)}")

    # 3. Fetch news + social
    print(f"\n{'='*70}")
    print("STEP 3: FETCH NEWS + SOCIAL MEDIA")
    print("="*70)
    articles = fetch_newsapi_articles(symbols, days_back=args.news_days_back)
    messages = fetch_stocktwits_messages(symbols)

    # 4. Score sentiment + compute features
    print(f"\n{'='*70}")
    print("STEP 4: SCORE SENTIMENT + COMPUTE SENTIMENT FEATURES")
    print("="*70)
    sentiment_features = compute_sentiment_features(articles, messages, symbols, all_rows)

    # 5. Merge sentiment features into all_rows
    print(f"\n{'='*70}")
    print("STEP 5: MERGE SENTIMENT FEATURES")
    print("="*70)
    n_with_sentiment = 0
    for row in all_rows:
        sym = row["__symbol"]
        dt_ns = row["decision_time"]
        sym_sent = sentiment_features.get(sym, {})
        sent = sym_sent.get(dt_ns)
        if sent:
            for feat_name in SENTIMENT_FEATURES:
                row[feat_name] = sent.get(feat_name, 0.0)
            n_with_sentiment += 1
    print(f"  Rows with sentiment: {n_with_sentiment}/{len(all_rows)} ({n_with_sentiment/len(all_rows)*100:.1f}%)")

    # 6. Write parquet
    print(f"\n{'='*70}")
    print("STEP 6: WRITE COMBINED PARQUET")
    print("="*70)
    import polars as pl

    dataset_dir = _REPO_ROOT / "data" / "datasets" / "combined"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = dataset_dir / "combined_dataset.parquet"

    # Build columns (include symbol for downstream merging)
    columns = {"decision_time": [int(r["decision_time"]) for r in all_rows]}
    columns["symbol"] = [str(r.get("__symbol", "")) for r in all_rows]
    for name in ALL_FEATURES:
        columns[name] = [float(r.get(name, 0.0)) for r in all_rows]
    columns["label"] = [float(r["label"]) for r in all_rows]

    df = pl.DataFrame(columns).sort("decision_time")
    df.write_parquet(str(parquet_path))

    print(f"  Parquet: {parquet_path}")
    print(f"  Rows: {len(all_rows)}")
    print(f"  Features: {len(ALL_FEATURES)}")
    print(f"  Size: {parquet_path.stat().st_size / 1024 / 1024:.2f} MB")

    # Also write as CSV for RunPod upload
    csv_path = dataset_dir / "combined_dataset.csv"
    df.write_csv(str(csv_path))
    print(f"  CSV: {csv_path}")
    print(f"  CSV size: {csv_path.stat().st_size / 1024 / 1024:.2f} MB")

    # 7. Summary
    print(f"\n{'='*70}")
    print("DATASET SUMMARY")
    print("="*70)
    labels = [r["label"] for r in all_rows]
    n_up = sum(1 for l in labels if l == 1.0)
    print(f"  Rows: {len(all_rows)}")
    print(f"  Features: {len(ALL_FEATURES)}")
    print(f"  Label balance: {n_up} up / {len(labels) - n_up} down ({n_up/len(labels)*100:.1f}% up)")
    print(f"  Rows with sentiment: {n_with_sentiment} ({n_with_sentiment/len(all_rows)*100:.1f}%)")
    print(f"  Articles fetched: {len(articles)}")
    print(f"  Messages fetched: {len(messages)}")

    print(f"\n{'='*70}")
    print("COMBINED DATASET READY FOR TRAINING")
    print("="*70)
    print(f"  Parquet: {parquet_path}")
    print(f"  CSV:     {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
