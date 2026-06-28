# Media-Sentiment-Price-Impact Dataset System

## Comprehensive Technical Report

**Project:** Fincept Terminal — Quant Foundry
**Subsystem:** `quant_foundry.modules` — Modular A/B-testable dataset system
**Goal:** Analyze the correlation between social media sentiment, news sentiment (by type), and stock prices via abnormal returns
**Status:** All 4 phases complete — 1086 tests passing, 0 failures
**Date:** 2026-06-28

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Module Registry](#3-module-registry)
4. [Sentiment Engines](#4-sentiment-engines)
5. [Source Adapters](#5-source-adapters)
6. [Label Computer](#6-label-computer)
7. [Feature Computers](#7-feature-computers)
8. [Universe Selectors](#8-universe-selectors)
9. [Price Joiners](#9-price-joiners)
10. [Dataset Composer](#10-dataset-composer)
11. [Benchmark Harness](#11-benchmark-harness)
12. [Attribution Report](#12-attribution-report)
13. [Comparison Report](#13-comparison-report)
14. [RunPod Integration](#14-runpod-integration)
15. [Environment Variables](#15-environment-variables)
16. [Testing](#16-testing)
17. [Usage Examples](#17-usage-examples)
18. [File Manifest](#18-file-manifest)

---

## 1. Overview

### What This System Does

This system builds datasets that correlate **media sentiment** (from news and social media) with **stock price movements** (via abnormal returns). It answers these research questions:

1. **Which event types have the most price impact?** (regulatory, earnings, macro, etc.)
2. **Which source has higher signal-to-noise?** (news vs social media)
3. **Which sentiment engine is best?** (FinBERT vs 4-LLM ensemble vs naive word-list)
4. **How did media→price response change from 2018 to 2025?**
5. **Which prediction horizon is most predictable?** (1d, 5d, 21d, 63d)

### Design Principles

- **Modular & swappable:** Every component (sentiment engine, source adapter, label computer, feature computer, universe selector, price joiner) is a self-registering module that implements a Protocol interface for its category. Swap any component by changing a single string ID.
- **A/B-testable:** Compose different module combinations, build datasets, train models, compare `deflated_sharpe` / `PBO` from the `ModelDossier`.
- **PIT-correct:** All feature `observed_at <= decision_time`, enforced by `FeatureLakeBuilder`. No look-ahead leakage.
- **Lazy heavy deps:** numpy, polars, torch, transformers, lightgbm, httpx are all imported inside methods, not at module level. The package is importable without any heavy dependencies installed.
- **No SDK dependencies:** All LLM providers and social source adapters use `httpx` directly (no `openai`, `anthropic`, `tweepy`, etc. packages needed).
- **Graceful degradation:** Missing API keys or API errors never crash the pipeline — they return neutral/empty results.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     DatasetComposer                              │
│                                                                  │
│  1. Universe Selector ──→ list of tickers                       │
│  2. Source Adapter    ──→ list of MediaItem                     │
│  3. Sentiment Engine  ──→ list of SentimentResult               │
│  4. Feature Computers ──→ {symbol: {decision_time: features}}   │
│  5. Price Joiner      ──→ asset bars + benchmark bars           │
│  6. Label Computer    ──→ labeled FeatureRowData list           │
│  7. FeatureLakeBuilder──→ parquet + manifest + receipt + quality│
│                                                                  │
│  Output: IngestionResult (drops into RunPod training pipeline)  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                     BenchmarkHarness                             │
│                                                                  │
│  For each BenchmarkConfig:                                       │
│    DatasetComposer.build() ──→ IngestionResult                  │
│    RealLightGBMTrainer.train() ──→ (ArtifactManifest, ModelDossier) │
│                                                                  │
│  Output: list[BenchmarkResult]                                   │
│    → ComparisonReport (ranked by Sharpe/PBO, best by source/sentiment) │
│    → AttributionReport (feature importance by event type/source/year/horizon) │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
MediaItem (source adapter output)
    │
    ▼
SentimentResult (sentiment engine output)
    │
    ▼
FeatureRowData (feature computer output)
    │
    ▼  + PriceBar (price joiner output)
    │
    ▼
Labeled FeatureRowData (label computer output)
    │
    ▼
FeatureLakeBuilder → parquet + DatasetManifest + receipt + quality report
    │
    ▼
IngestionResult → RunPod training pipeline → ModelDossier
```

---

## 3. Module Registry

### Location
`services/quant_foundry/src/quant_foundry/modules/registry.py`

### Purpose
Central singleton registry that holds all available modules. Modules self-register via the `@register_module` decorator at import time. The registry holds module *classes* keyed by `category:module_id:version`.

### Protocol Interfaces

Each module category has a `Protocol` interface that modules implement:

| Category | Protocol | Method |
|---|---|---|
| `sentiment` | `SentimentEngine` | `score(items: list[MediaItem]) → list[SentimentResult]` |
| `source` | `SourceAdapter` | `async fetch(*, symbols, start_ns, end_ns) → list[MediaItem]` |
| `label` | `LabelComputer` | `compute_labels(rows, *, price_bars, benchmark_bars) → list[FeatureRowData]` |
| `feature` | `FeatureComputer` | `compute_features(items, sentiments, *, symbols, start_ns, end_ns) → dict` |
| `universe` | `UniverseSelector` | `select_symbols(*, start_ns, end_ns) → list[str]` |
| `price_join` | `PriceJoiner` | `load_bars(*, symbols, start_ns, end_ns) → tuple[dict, list]` |

### Shared Data Types

**`MediaItem`** — A single media item (news article, social post):
```python
@dataclass(frozen=True)
class MediaItem:
    item_id: str
    source: str              # "newsapi", "stocktwits", "reddit", "x_twitter"
    headline: str
    body: str
    available_at_ns: int     # PIT decision time (nanoseconds)
    symbols: tuple[str, ...] # tickers mentioned
    event_type: str          # 11 news types or "social"
    url: str | None
    language: str
    metadata: dict[str, str] # platform-specific data
```

**`SentimentResult`** — Sentiment score for a media item:
```python
@dataclass(frozen=True)
class SentimentResult:
    item_id: str
    provider: str    # "naive", "finbert", "openai", "anthropic", "xai", "minimax", "llm-ensemble"
    score: float     # [-1, 1] where -1=very bearish, 0=neutral, 1=very bullish
    confidence: float # [0, 1]
```

**`PriceBar`** — A single OHLCV bar:
```python
@dataclass(frozen=True)
class PriceBar:
    symbol: str
    ts_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float
```

**`FeatureRowData`** — A computed feature row:
```python
@dataclass(frozen=True)
class FeatureRowData:
    symbol: str
    decision_time: int          # nanoseconds
    features: dict[str, float]
    label: float | None = None  # set by label computer
```

### Registry API

```python
from quant_foundry.modules import ModuleRegistry, load_all_modules

# Load all modules (call once at startup)
load_all_modules()

registry = ModuleRegistry.instance()

# List all modules by category
registry.list_all()
# → {"sentiment": ["sentiment:finbert:1.0.0", ...], "source": [...], ...}

# List modules in a category
registry.list_by_category("sentiment")

# Get module info
info = registry.get_info("sentiment:finbert:1.0.0")
# → ModuleInfo(module_id="finbert", category="sentiment", version="1.0.0", config={...})

# Instantiate a module with config overrides
mod = registry.create("sentiment:finbert:1.0.0", config={"device": "cuda"})
```

### Registration Decorator

```python
from quant_foundry.modules.registry import register_module

@register_module("sentiment", "my-engine", "1.0.0", default_config={"param": 42})
class MySentimentEngine:
    def __init__(self, config=None):
        self.config = config or {}
    def score(self, items):
        ...
```

---

## 4. Sentiment Engines

### 4.1 Naive Word-List (`sentiment:naive-wordlist:1.0.0`)

**File:** `modules/sentiment/naive_wordlist.py`

**Description:** Zero-dependency sentiment baseline. Scores text by counting positive/negative words. Reuses the same word lists as the existing `data_ingestion/news.py` for consistency.

**Inputs:**
- `items: list[MediaItem]` — media items to score

**Outputs:**
- `list[SentimentResult]` — one per item, `provider="naive"`, `score` in [-1, 1], `confidence` in [0, 1]

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `positive_words` | (built-in list) | Positive sentiment words |
| `negative_words` | (built-in list) | Negative sentiment words |

**Algorithm:** Score = `(pos - neg) / (pos + neg)`. Confidence = `min(1.0, total / 10.0)`.

**Dependencies:** None (pure Python).

---

### 4.2 FinBERT (`sentiment:finbert:1.0.0`)

**File:** `modules/sentiment/finbert.py`

**Description:** Finance-tuned BERT model (`yiyanghkust/finbert-tone`) for sentiment scoring. The default sentiment engine for news text on RunPod GPU workers. Deterministic, cacheable, free.

**Inputs:**
- `items: list[MediaItem]` — media items to score

**Outputs:**
- `list[SentimentResult]` — one per item, `provider="finbert"`, `score` in {-1, 0, 1} mapped from {negative, neutral, positive}, `confidence` = softmax probability

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `model` | `"yiyanghkust/finbert-tone"` | HuggingFace model name |
| `batch_size` | `32` | Inference batch size |
| `device` | `"auto"` | `"auto"`, `"cuda"`, or `"cpu"` |
| `cache_dir` | `None` | Path for disk caching (by item_id) |

**Caching:** When `cache_dir` is set, results are cached to `finbert_sentiment_cache.json` keyed by `item_id`. Re-runs skip cached items — no re-inference cost.

**Dependencies:** `transformers`, `torch` (imported lazily inside `_load_model()`). Raises `ImportError` at score time if missing.

---

### 4.3 OpenAI LLM (`sentiment:llm-openai:1.0.0`)

**File:** `modules/sentiment/llm_openai.py`

**Description:** Scores sentiment using OpenAI's chat completion API. Better at sarcasm/slang than FinBERT. Uses `httpx` directly (no SDK dependency).

**Inputs:**
- `items: list[MediaItem]` — media items to score

**Outputs:**
- `list[SentimentResult]` — one per item, `provider="openai"`, `score` in [-1, 1], `confidence` in [0, 1]

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `model` | `"gpt-4o-mini"` | OpenAI model name |
| `base_url` | `"https://api.openai.com/v1"` | API base URL |
| `timeout` | `30.0` | Request timeout (seconds) |
| `max_tokens` | `100` | Max response tokens |

**Env var:** `OPENAI_API_KEY` (required). Returns neutral (0.0, 0.0) on missing key or API error.

**Prompt:** Asks the model to return JSON `{"score": float, "confidence": float}`. Score is clamped to [-1, 1], confidence to [0, 1].

---

### 4.4 Anthropic LLM (`sentiment:llm-anthropic:1.0.0`)

**File:** `modules/sentiment/llm_anthropic.py`

**Description:** Scores sentiment using Anthropic's Messages API (Claude).

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `model` | `"claude-sonnet-4-20250514"` | Anthropic model name |
| `base_url` | `"https://api.anthropic.com/v1"` | API base URL |
| `api_version` | `"2023-06-01"` | API version header |
| `timeout` | `30.0` | Request timeout (seconds) |
| `max_tokens` | `100` | Max response tokens |

**Env var:** `ANTHROPIC_API_KEY` (required). Returns neutral on missing key or error.

---

### 4.5 xAI Grok LLM (`sentiment:llm-xai:1.0.0`)

**File:** `modules/sentiment/llm_xai.py`

**Description:** Scores sentiment using xAI's Grok chat completion API. Grok is particularly good at social media text (integrated with X/Twitter).

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `model` | `"grok-3-mini"` | xAI model name |
| `base_url` | `"https://api.x.ai/v1"` | API base URL |
| `timeout` | `30.0` | Request timeout (seconds) |
| `max_tokens` | `100` | Max response tokens |

**Env var:** `XAI_API_KEY` (required). Returns neutral on missing key or error.

---

### 4.6 MiniMax LLM (`sentiment:llm-minimax:1.0.0`)

**File:** `modules/sentiment/llm_minimax.py`

**Description:** Scores sentiment using MiniMax's chat completion API. Adds geographic + model diversity to the 4-LLM ensemble.

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `model` | `"MiniMax-Text-01"` | MiniMax model name |
| `base_url` | `"https://api.minimax.chat/v1"` | API base URL |
| `timeout` | `30.0` | Request timeout (seconds) |
| `max_tokens` | `100` | Max response tokens |

**Env var:** `MINIMAX_API_KEY` (required). Returns neutral on missing key or error.

---

### 4.7 LLM Ensemble (`sentiment:llm-ensemble-4:1.0.0`)

**File:** `modules/sentiment/llm_ensemble.py`

**Description:** Aggregates sentiment scores from OpenAI, Anthropic, xAI, and MiniMax into a single ensemble score. The **disagreement** between providers is itself a signal — high disagreement correlates with ambiguous/mixed-reaction posts.

**Inputs:**
- `items: list[MediaItem]` — media items to score

**Outputs:**
- `list[SentimentResult]` — one per item, `provider="llm-ensemble"`, `score` = mean of per-provider scores, `confidence` = mean confidence × agreement (lower std = higher confidence)

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `providers` | `["openai", "anthropic", "xai", "minimax"]` | Which providers to use |
| `aggregation` | `"mean"` | `"mean"` or `"median"` |
| `min_providers` | `2` | Minimum valid results needed (else neutral) |

**Detailed scoring:** `score_detailed()` returns per-provider scores + ensemble std for feature engineering:
```python
detailed = ensemble.score_detailed(items)
# → [{"item_id": "...", "providers": [{"provider": "openai", "score": 0.8, ...}], "ensemble_score": 0.7, "ensemble_std": 0.1}]
```

**Graceful degradation:** If a provider's API key is missing, that provider is silently skipped. If fewer than `min_providers` return valid results, the ensemble returns neutral (0.0, 0.0).

---

## 5. Source Adapters

### 5.1 NewsAPI (`source:newsapi:1.0.0`)

**File:** `modules/sources/newsapi.py`

**Description:** Fetches news articles from NewsAPI.org. Wraps the existing `fetch_newsapi_articles` function. Classifies each article into one of 11 event types using the news-impact-model classifier.

**Inputs:**
- `symbols: list[str]` — tickers to search for
- `start_ns: int` — start time (nanoseconds)
- `end_ns: int` — end time (nanoseconds)

**Outputs:**
- `list[MediaItem]` — one per article, `source="newsapi"`, `event_type` from 11-type classifier

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `query` | `"stock market"` | Search query (overridden by symbols if default) |
| `page_size` | `100` | Results per page |
| `language` | `"en"` | Article language |

**Env var:** `NEWSAPI_KEY` (required by the underlying adapter).

**Event types:** `regulatory`, `earnings`, `guidance`, `macro`, `product`, `security`, `litigation`, `partnership`, `financing`, `m&a`, `general`

---

### 5.2 StockTwits (`source:stocktwits:1.0.0`)

**File:** `modules/sources/stocktwits.py`

**Description:** Fetches messages from StockTwits' public API. **Key feature:** every message has a mandatory cashtag + optional Bullish/Bearish sentiment tag — free human-labeled sentiment ground truth stored in `metadata["stocktwits_sentiment"]`.

**Inputs:**
- `symbols: list[str]` — tickers to search for
- `start_ns: int`, `end_ns: int` — time range

**Outputs:**
- `list[MediaItem]` — one per message, `source="stocktwits"`, `event_type="social"`, `metadata` includes `stocktwits_sentiment` (if tagged), `stocktwits_user`, `stocktwits_msg_id`

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `max_per_symbol` | `30` | Max messages per symbol |
| `timeout` | `30.0` | Request timeout (seconds) |

**Env var:** `STOCKTWITS_CLIENT_ID` (optional, for higher rate limits: 400/hr vs 200/hr).

**API:** `GET https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json`

---

### 5.3 Reddit (`source:reddit:1.0.0`)

**File:** `modules/sources/reddit.py`

**Description:** Fetches posts from financial subreddits (wallstreetbets, stocks, investing, StockMarket) using Reddit's public JSON API (no auth required — just append `.json` to any Reddit URL).

**Inputs:**
- `symbols: list[str]` — tickers to search for (used for filtering)
- `start_ns: int`, `end_ns: int` — time range

**Outputs:**
- `list[MediaItem]` — one per post, `source="reddit"`, `event_type="social"`, `metadata` includes `reddit_subreddit`, `reddit_score`, `reddit_num_comments`, `reddit_upvote_ratio`

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `subreddits` | `["wallstreetbets", "stocks", "investing", "StockMarket"]` | Subreddits to search |
| `limit` | `25` | Posts per subreddit per fetch |
| `timeout` | `30.0` | Request timeout (seconds) |
| `delay_seconds` | `1.0` | Delay between subreddit fetches (rate limiting) |
| `sort` | `"new"` | `"new"`, `"hot"`, or `"top"` |

**Env var:** `REDDIT_USER_AGENT` (optional, but Reddit's API guidelines recommend setting it).

**Filtering:** Posts with no relevant symbols (cashtag or known-symbol match) are filtered out to keep the dataset focused.

---

### 5.4 X/Twitter (`source:x-twitter:1.0.0`)

**File:** `modules/sources/x_twitter.py`

**Description:** Fetches posts from X/Twitter via API v2 using cashtag search (`$AAPL`). X is the highest-volume real-time social signal for finance.

**Inputs:**
- `symbols: list[str]` — tickers to search for
- `start_ns: int`, `end_ns: int` — time range

**Outputs:**
- `list[MediaItem]` — one per tweet, `source="x_twitter"`, `event_type="social"`, `metadata` includes `x_tweet_id`, `x_retweet_count`, `x_reply_count`, `x_like_count`, `x_quote_count`

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `max_results` | `100` | Max results per symbol (API max 100) |
| `timeout` | `30.0` | Request timeout (seconds) |
| `search_mode` | `"recent"` | `"recent"` (7 days) or `"full_archive"` (academic) |

**Env vars:**
- `X_BEARER_TOKEN` (required for recent search)
- `X_ACADEMIC_BEARER_TOKEN` (required for full_archive mode; falls back to `X_BEARER_TOKEN`)

**API:** `GET https://api.twitter.com/2/tweets/search/recent` (or `/all` for full-archive)

---

## 6. Label Computer

### Abnormal Return (`label:abnormal-return:1.0.0`)

**File:** `modules/labels/abnormal_return.py`

**Description:** Computes **abnormal return** labels — the core "how media moves prices" piece. Replaces the existing "subsequent news event" label with actual market response: `abnormal_return = asset_return − β · benchmark_return` at multiple horizons.

**Inputs:**
- `rows: list[FeatureRowData]` — feature rows to label
- `price_bars: dict[str, list[PriceBar]]` — asset OHLCV bars by symbol
- `benchmark_bars: list[PriceBar]` — benchmark OHLCV bars (e.g. SPY)

**Outputs:**
- `list[FeatureRowData]` — labeled rows with `label` set to primary horizon AR, plus extra `ar_<h>d` columns for all horizons. Rows without sufficient price history are dropped.

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `horizon_days` | `[1, 5, 21, 63]` | Horizons in trading days (1d, 1w, 1m, 1q) |
| `primary_horizon` | `5` | Which horizon is the training label |
| `beta_window` | `252` | β estimation window (~1 year) |
| `min_beta_window` | `60` | Minimum bars needed for β estimation |

**Algorithm:**

For each feature row at `(symbol, decision_time)`:
1. Find the asset price at or before `decision_time` (base price).
2. Find the benchmark price at or before `decision_time` (base benchmark).
3. Estimate β from trailing `beta_window` days of log returns ending **before** `decision_time` (no look-ahead):
   - β = Cov(asset_ret, bench_ret) / Var(bench_ret)
4. For each horizon `h`:
   - Find asset price at `decision_time + h·NS_PER_DAY`
   - Find benchmark price at the same future time
   - `abnormal_return[h] = asset_return − β · benchmark_return`
5. Primary label = abnormal return at `primary_horizon` (default +5d).
6. Other horizons added as extra feature columns `ar_1d`, `ar_5d`, `ar_21d`, `ar_63d`.

**PIT correctness:** β is estimated only from bars with `ts_ns < decision_time` (strictly before). Rows without enough price history (no base price, insufficient β window, or no future price at the primary horizon) are dropped.

---

## 7. Feature Computers

### 7.1 Per-Event-Type (`feature:per-event-type:1.0.0`)

**File:** `modules/features/per_event_type.py`

**Description:** Produces one sentiment feature per event type (11 types + social). For each `(symbol, decision_time)`, the feature value is the mean sentiment of all media items for that symbol in a lookback window that are classified as that event type.

**Inputs:**
- `items: list[MediaItem]` — media items
- `sentiments: list[SentimentResult]` — sentiment scores
- `symbols: list[str]`, `start_ns: int`, `end_ns: int`

**Outputs:**
- `dict[str, dict[int, dict[str, float]]]` — `{symbol: {decision_time: {feature_name: value}}}`

**Features produced (13):**
- `sent_regulatory`, `sent_earnings`, `sent_guidance`, `sent_macro`, `sent_product`, `sent_security`, `sent_litigation`, `sent_partnership`, `sent_financing`, `sent_m&a`, `sent_general` — per-event-type mean sentiment
- `sent_mean` — aggregate mean sentiment across all items in window
- `sent_count` — number of items in window

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `lookback_days` | `3` | How many days back to look for media items |

---

### 7.2 Per-Year (`feature:per-year:1.0.0`)

**File:** `modules/features/per_year.py`

**Description:** Adds a `year` column and per-year one-hot features so the model can learn regime-dependent media response. This is what enables the "how did media→price response change from 2018 to 2025" analysis.

**Features produced (9):**
- `year` — the year as a float (2018.0, 2019.0, ...)
- `year_2018` through `year_2025` — one-hot indicators

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `years` | `[2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]` | Years to one-hot encode |

**Note:** This module is a *passthrough* — it doesn't generate new rows, it only annotates existing decision times with year features via `annotate_row()`. The composer applies it as a post-processing step.

---

## 8. Universe Selectors

### S&P 500 (`universe:sp500:1.0.0`)

**File:** `modules/universe/sp500.py`

**Description:** Returns the S&P 500 ticker list (static, current constituents).

**Inputs:**
- `start_ns: int`, `end_ns: int` (ignored — static list)

**Outputs:**
- `list[str]` — sorted ticker symbols

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `max_symbols` | `None` | Limit to first N tickers (useful for testing) |

**Note:** A future version could load a point-in-time S&P 500 membership file for survivorship-bias-free backtesting.

---

## 9. Price Joiners

### Alpaca Bars (`price_join:alpaca-bars:1.0.0`)

**File:** `modules/price_join/alpaca_bars.py`

**Description:** Loads OHLCV bars from parquet files. Expects parquet files produced by `scripts/ingest_bars.py` or the Alpaca adapter. Files may be a single multi-symbol parquet or one parquet per symbol named `<symbol>.parquet`.

**Inputs:**
- `symbols: list[str]` — tickers to load
- `start_ns: int`, `end_ns: int` — time range filter

**Outputs:**
- `tuple[dict[str, list[PriceBar]], list[PriceBar]]` — (asset_bars by symbol, benchmark_bars flat list)

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `bars_dir` | `"data/bars/"` | Directory containing parquet files |
| `benchmark_symbol` | `"SPY"` | Benchmark symbol for abnormal return computation |

**Parquet schema expected:** `symbol, ts_event, open, high, low, close, volume`

---

## 10. Dataset Composer

**File:** `modules/composer.py`

### Purpose

The orchestration layer that wires modules together into a complete dataset-building pipeline. Combines one module from each category, runs them in order, and produces the same `IngestionResult` shape as the existing `data_ingestion` functions — so it drops straight into the RunPod training pipeline.

### Constructor

```python
DatasetComposer(
    universe="universe:sp500:1.0.0",
    source="source:newsapi:1.0.0",
    sentiment="sentiment:finbert:1.0.0",
    features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
    label="label:abnormal-return:1.0.0",
    price_join="price_join:alpaca-bars:1.0.0",
    config={  # optional per-module config overrides
        "universe:sp500:1.0.0": {"max_symbols": 50},
    },
)
```

### `build()` Method

**Inputs:**
| Parameter | Type | Description |
|---|---|---|
| `output_dir` | `Path` | Output directory for artifacts |
| `dataset_id` | `str` | Unique dataset identifier |
| `start_ns` | `int` | Dataset start time (nanoseconds) |
| `end_ns` | `int` | Dataset end time (nanoseconds) |
| `n_folds` | `int` | Number of walk-forward CV folds (default 3) |
| `label_horizon_ns` | `int` | Label horizon (default 5 days) |

**Outputs:** `IngestionResult` containing:
- `parquet_path` — the dataset parquet file
- `manifest_path` — the `DatasetManifest` JSON
- `receipt_path` — tamper-evident receipt
- `quality_path` — quality report JSON
- `manifest` — `DatasetManifest` object
- `quality_report` — quality report object

### Pipeline Steps

1. **Universe:** `universe_mod.select_symbols()` → list of tickers
2. **Source:** `source_mod.fetch(symbols, start, end)` → list of `MediaItem` (async, awaited automatically)
3. **Sentiment:** `sentiment_mod.score(items)` → list of `SentimentResult`
4. **Features:** Each feature module's `compute_features()` → merged `{symbol: {dt: features}}`
5. **Price join:** `price_mod.load_bars(symbols)` → asset + benchmark bars
6. **Build FeatureRowData list** from merged features + per-year annotation
7. **Labels:** `label_mod.compute_labels(rows, price_bars, benchmark)` → labeled rows
8. **Build dataset:** `FeatureLakeBuilder` → parquet + manifest + receipt + quality report

---

## 11. Benchmark Harness

**File:** `modules/benchmark/harness.py`

### Purpose

Runs multiple `BenchmarkConfig` entries (each defining a module combination), builds a dataset for each, trains a model on each using the existing `RealLightGBMTrainer`, and collects `ModelDossier` metrics for comparison.

### BenchmarkConfig

```python
@dataclass(frozen=True)
class BenchmarkConfig:
    name: str                    # human-readable name
    universe: str                # universe module ID
    source: str                  # source module ID
    sentiment: str               # sentiment module ID
    features: list[str]          # feature module IDs
    label: str                   # label module ID
    price_join: str              # price joiner module ID
    start_ns: int                # dataset start time
    end_ns: int                  # dataset end time
    n_folds: int = 3             # walk-forward CV folds
    config: dict = {}            # per-module config overrides
    random_seed: int | None = None
```

### BenchmarkResult

```python
@dataclass
class BenchmarkResult:
    config: BenchmarkConfig
    dataset_id: str
    parquet_path: Path | None
    manifest_path: Path | None
    dossier: ModelDossier | None    # None if failed
    artifact: ArtifactManifest | None
    error: str | None               # None if succeeded
    duration_seconds: float

    @property
    def succeeded(self) -> bool
    @property
    def deflated_sharpe(self) -> float | None
    @property
    def pbo(self) -> float | None
```

### BenchmarkHarness

```python
harness = BenchmarkHarness(
    configs=[config1, config2, ...],
    output_dir=Path("data/benchmarks"),
    deadline_seconds=600,  # per-config training deadline
)

results = harness.run()  # → list[BenchmarkResult]
report_path = harness.write_report(results)  # → combined JSON report
```

**Failure handling:** If one config fails (dataset build or training error), the error is recorded and the harness moves on to the next config. Failed results have `succeeded=False` and `error` set.

**Output files:**
- `{output_dir}/{config.name}/benchmark_result.json` — per-config result
- `{output_dir}/{config.name}/dataset/` — dataset parquet + manifest
- `{output_dir}/benchmark_report.json` — combined report with summary table

---

## 12. Attribution Report

**File:** `modules/benchmark/attribution.py`

### Purpose

Analyzes a trained model's feature importance to answer the core research questions. Groups features by event type, source, sentiment provider, year, and horizon.

### Construction

```python
# From a model + feature names
report = AttributionReport.from_model(
    feature_importances=model.feature_importances_,
    feature_names=feature_names,
)

# From a parquet + model
report = AttributionReport.from_parquet(
    parquet_path=Path("dataset.parquet"),
    model=trained_model,
)
```

### Attribution Groupings

| Method | Returns | Description |
|---|---|---|
| `event_type_attribution()` | `dict[str, float]` | Importance by event type (regulatory, earnings, macro, social, etc.) — sorted by importance |
| `source_attribution()` | `dict[str, float]` | Importance by source category (`"news"` vs `"social"`) |
| `sentiment_provider_attribution()` | `dict[str, float]` | Importance by sentiment provider (naive, finbert, openai, anthropic, xai, minimax, llm_ensemble, sent_mean, sent_count) |
| `year_attribution()` | `dict[str, float]` | Importance by year (2018–2025) — chronological order |
| `horizon_attribution()` | `dict[str, float]` | Importance by horizon (ar_1d, ar_5d, ar_21d, ar_63d) |

### Output

```python
report.write(Path("attribution.json"))  # JSON file
print(report.summary_text())            # human-readable summary
report.to_dict()                        # JSON-compatible dict
```

The JSON report contains: `total_importance`, `event_type`, `source`, `sentiment_provider`, `year`, `horizon`, `top_features` (top 20).

---

## 13. Comparison Report

**File:** `modules/benchmark/comparison.py`

### Purpose

Takes multiple `BenchmarkResult` entries and produces a side-by-side comparison that ranks results and identifies the best module combinations.

### Construction

```python
comparison = ComparisonReport.from_results(results)
```

### Rankings

| Attribute | Description |
|---|---|
| `ranked_by_sharpe` | Results ranked by deflated Sharpe (descending — best first) |
| `ranked_by_pbo` | Results ranked by PBO (ascending — lower is better) |
| `best_by_source` | Best result per source category (`{"newsapi": {...}, "stocktwits": {...}}`) |
| `best_by_sentiment` | Best result per sentiment engine (`{"finbert": {...}, "llm-ensemble-4": {...}}`) |
| `summary_table` | Flat table with all results + key metrics |

### Output

```python
comparison.write(Path("comparison.json"))  # JSON file
print(comparison.summary_text())           # human-readable summary table
comparison.to_dict()                       # JSON-compatible dict
```

---

## 14. RunPod Integration

### Handler Update

**File:** `runpod/quant-foundry-training/handler.py`

A new `ingest_media_sentiment` task has been added to the RunPod handler. This task builds a media-sentiment-price dataset on the worker, writes the parquet + manifest to the network volume, and returns the paths for a subsequent training job to consume.

### Task Input

```json
{
    "task": "ingest_media_sentiment",
    "dataset_id": "media-sentiment-price-2023",
    "start_ns": 1672531200000000000,
    "end_ns": 1704067200000000000,
    "output_dir": "/workspace/datasets/media-sentiment-price-2023",
    "universe_module": "universe:sp500:1.0.0",
    "source_module": "source:newsapi:1.0.0",
    "sentiment_module": "sentiment:finbert:1.0.0",
    "feature_modules": ["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
    "label_module": "label:abnormal-return:1.0.0",
    "price_join_module": "price_join:alpaca-bars:1.0.0",
    "n_folds": 3,
    "config": {}
}
```

All module selection fields are optional (they default to the values shown above).

### Task Output

```json
{
    "task": "ingest_media_sentiment",
    "dataset_id": "media-sentiment-price-2023",
    "parquet_path": "/workspace/datasets/media-sentiment-price-2023/dataset.parquet",
    "manifest_path": "/workspace/datasets/.../manifest.json",
    "receipt_path": "/workspace/datasets/.../receipt.json",
    "quality_path": "/workspace/datasets/.../quality.json",
    "row_count": 12500,
    "manifest_hash": "abc123...",
    "feature_schema_hash": "def456...",
    "label_schema_hash": "ghi789...",
    "status": "ok"
}
```

### Two-Step Workflow

1. **Ingest:** Send an `ingest_media_sentiment` task to build the dataset → get `manifest_path`
2. **Train:** Send a standard training request with `dataset_manifest_ref` pointing at the manifest path → get `ModelDossier`

This separation allows you to build a dataset once and train multiple models on it with different hyperparameters.

---

## 15. Environment Variables

### Required

| Variable | Used By | Description |
|---|---|---|
| `NEWSAPI_KEY` | `source:newsapi` | NewsAPI.org API key |
| `X_BEARER_TOKEN` | `source:x-twitter` | X/Twitter API v2 bearer token (recent search) |

### Optional (for LLM ensemble)

| Variable | Used By | Description |
|---|---|---|
| `OPENAI_API_KEY` | `sentiment:llm-openai` | OpenAI API key |
| `ANTHROPIC_API_KEY` | `sentiment:llm-anthropic` | Anthropic API key |
| `XAI_API_KEY` | `sentiment:llm-xai` | xAI (Grok) API key |
| `MINIMAX_API_KEY` | `sentiment:llm-minimax` | MiniMax API key |

### Optional (for social sources)

| Variable | Used By | Description |
|---|---|---|
| `STOCKTWITS_CLIENT_ID` | `source:stocktwits` | StockTwits client ID (higher rate limits) |
| `REDDIT_USER_AGENT` | `source:reddit` | Reddit API user agent string |
| `X_ACADEMIC_BEARER_TOKEN` | `source:x-twitter` | X/Twitter academic bearer token (full-archive search) |

### Existing (for RunPod)

| Variable | Description |
|---|---|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | For signing callbacks (existing) |

---

## 16. Testing

### Test Files

| File | Tests | Coverage |
|---|---|---|
| `tests/test_modules.py` | 18 | Registry, sentiment, abnormal-return math, β no-lookahead, per-event-type features, per-year features, universe, end-to-end composer build, no-heavy-deps |
| `tests/test_sentiment_modules.py` | 17 | FinBERT, LLM providers, LLM ensemble, RunPod ingestion task validation |
| `tests/test_source_modules.py` | 23 | StockTwits, Reddit, X/Twitter — normalization, symbol extraction, graceful error handling, timestamp parsing |
| `tests/test_benchmark.py` | 17 | BenchmarkHarness, AttributionReport, ComparisonReport — ranking, grouping, JSON output, summary text |

### Test Results

```
1086 passed, 2 skipped, 0 failed in 26.74s
```

The 2 skipped tests are for `onnxruntime` (unrelated to this system — they're in `test_real_inference.py`).

### Running Tests

```bash
# Run all tests for the modules system
uv run pytest services/quant_foundry/tests/test_modules.py \
                 services/quant_foundry/tests/test_sentiment_modules.py \
                 services/quant_foundry/tests/test_source_modules.py \
                 services/quant_foundry/tests/test_benchmark.py -v

# Run the full test suite
uv run pytest services/quant_foundry/tests/ -v
```

---

## 17. Usage Examples

### Example 1: Build a Single Dataset

```python
from pathlib import Path
from quant_foundry.modules import DatasetComposer, load_all_modules

load_all_modules()

composer = DatasetComposer(
    universe="universe:sp500:1.0.0",
    source="source:newsapi:1.0.0",
    sentiment="sentiment:finbert:1.0.0",
    features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
    label="label:abnormal-return:1.0.0",
    price_join="price_join:alpaca-bars:1.0.0",
    config={
        "universe:sp500:1.0.0": {"max_symbols": 50},  # limit for testing
    },
)

result = composer.build(
    output_dir=Path("data/datasets"),
    dataset_id="media-sentiment-price-2023",
    start_ns=1672531200000000000,  # 2023-01-01
    end_ns=1704067200000000000,    # 2024-01-01
    n_folds=3,
)

print(f"Parquet: {result.parquet_path}")
print(f"Manifest: {result.manifest_path}")
print(f"Rows: {result.manifest.row_count}")
```

### Example 2: A/B Test Sentiment Engines

```python
from pathlib import Path
from quant_foundry.modules import (
    BenchmarkConfig, BenchmarkHarness, ComparisonReport,
    load_all_modules,
)

load_all_modules()

common = dict(
    universe="universe:sp500:1.0.0",
    features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
    label="label:abnormal-return:1.0.0",
    price_join="price_join:alpaca-bars:1.0.0",
    start_ns=1672531200000000000,
    end_ns=1704067200000000000,
    config={"universe:sp500:1.0.0": {"max_symbols": 50}},
)

harness = BenchmarkHarness(
    configs=[
        BenchmarkConfig(name="finbert-news", source="source:newsapi:1.0.0",
                        sentiment="sentiment:finbert:1.0.0", **common),
        BenchmarkConfig(name="naive-news", source="source:newsapi:1.0.0",
                        sentiment="sentiment:naive-wordlist:1.0.0", **common),
        BenchmarkConfig(name="llm-social", source="source:stocktwits:1.0.0",
                        sentiment="sentiment:llm-ensemble-4:1.0.0", **common),
    ],
    output_dir=Path("data/benchmarks"),
)

results = harness.run()
report_path = harness.write_report(results)

comparison = ComparisonReport.from_results(results)
comparison.write(Path("data/benchmarks/comparison.json"))
print(comparison.summary_text())
```

### Example 3: Attribution Report for the Winning Model

```python
from quant_foundry.modules import AttributionReport

# After training, extract feature importances from the model
report = AttributionReport.from_model(
    feature_importances=list(model.feature_importances_),
    feature_names=feature_names,
)

report.write(Path("data/benchmarks/attribution.json"))
print(report.summary_text())

# Access specific attributions
print("Event type:", report.event_type_attribution())
print("Source:", report.source_attribution())
print("Year:", report.year_attribution())
print("Horizon:", report.horizon_attribution())
```

### Example 4: RunPod Ingestion Task

```python
# Send to RunPod worker
task_input = {
    "task": "ingest_media_sentiment",
    "dataset_id": "media-sentiment-price-2023",
    "start_ns": 1672531200000000000,
    "end_ns": 1704067200000000000,
    "output_dir": "/workspace/datasets/media-sentiment-price-2023",
    "sentiment_module": "sentiment:finbert:1.0.0",
    "config": {
        "universe:sp500:1.0.0": {"max_symbols": 100},
        "price_join:alpaca-bars:1.0.0": {"bars_dir": "/runpod-volume/bars/"},
    },
}

# Result contains manifest_path → use in a subsequent training job
training_request = {
    "job_id": "train-2023-finbert",
    "dataset_manifest_ref": result["manifest_path"],
    "model_family": "lightgbm",
}
```

### Example 5: List All Available Modules

```python
from quant_foundry.modules import ModuleRegistry, load_all_modules

load_all_modules()
registry = ModuleRegistry.instance()

for category, modules in registry.list_all().items():
    print(f"\n{category} ({len(modules)}):")
    for mod_id in modules:
        info = registry.get_info(mod_id)
        print(f"  {mod_id}")
```

---

## 18. File Manifest

### Module Source Files (29 files)

```
services/quant_foundry/src/quant_foundry/modules/
├── __init__.py                          # Package exports + load_all_modules()
├── registry.py                          # ModuleRegistry + Protocol interfaces + shared types
├── composer.py                          # DatasetComposer — combines modules → IngestionResult
├── sentiment/
│   ├── __init__.py                      # Registers all sentiment modules
│   ├── naive_wordlist.py                # sentiment:naive-wordlist:1.0.0
│   ├── finbert.py                       # sentiment:finbert:1.0.0
│   ├── llm_openai.py                    # sentiment:llm-openai:1.0.0
│   ├── llm_anthropic.py                 # sentiment:llm-anthropic:1.0.0
│   ├── llm_xai.py                       # sentiment:llm-xai:1.0.0
│   ├── llm_minimax.py                   # sentiment:llm-minimax:1.0.0
│   └── llm_ensemble.py                  # sentiment:llm-ensemble-4:1.0.0
├── sources/
│   ├── __init__.py                      # Registers all source modules
│   ├── newsapi.py                       # source:newsapi:1.0.0
│   ├── stocktwits.py                    # source:stocktwits:1.0.0
│   ├── reddit.py                        # source:reddit:1.0.0
│   └── x_twitter.py                     # source:x-twitter:1.0.0
├── labels/
│   ├── __init__.py                      # Registers all label modules
│   └── abnormal_return.py               # label:abnormal-return:1.0.0
├── features/
│   ├── __init__.py                      # Registers all feature modules
│   ├── per_event_type.py                # feature:per-event-type:1.0.0
│   └── per_year.py                      # feature:per-year:1.0.0
├── universe/
│   ├── __init__.py                      # Registers all universe modules
│   └── sp500.py                         # universe:sp500:1.0.0
├── price_join/
│   ├── __init__.py                      # Registers all price-join modules
│   └── alpaca_bars.py                   # price_join:alpaca-bars:1.0.0
└── benchmark/
    ├── __init__.py                      # Registers benchmark classes
    ├── harness.py                       # BenchmarkHarness + BenchmarkConfig + BenchmarkResult
    ├── attribution.py                   # AttributionReport
    └── comparison.py                    # ComparisonReport
```

### Test Files (4 files, 75 tests)

```
services/quant_foundry/tests/
├── test_modules.py                      # 18 tests — Phase 1 (registry, composer, end-to-end)
├── test_sentiment_modules.py            # 17 tests — Phase 2 (FinBERT, LLM providers, ensemble)
├── test_source_modules.py               # 23 tests — Phase 3 (StockTwits, Reddit, X/Twitter)
└── test_benchmark.py                    # 17 tests — Phase 4 (harness, attribution, comparison)
```

### RunPod Handler Update (1 file)

```
runpod/quant-foundry-training/
└── handler.py                           # Added _handle_ingest_media_sentiment() + task dispatch
```

### Module Registry Summary (16 modules across 6 categories)

| Category | Count | Modules |
|---|---|---|
| `sentiment` | 7 | naive-wordlist, finbert, llm-openai, llm-anthropic, llm-xai, llm-minimax, llm-ensemble-4 |
| `source` | 4 | newsapi, stocktwits, reddit, x-twitter |
| `label` | 1 | abnormal-return |
| `feature` | 2 | per-event-type, per-year |
| `universe` | 1 | sp500 |
| `price_join` | 1 | alpaca-bars |
| **Total** | **16** | |

Plus 3 benchmark classes: `BenchmarkHarness`, `AttributionReport`, `ComparisonReport`.

---

## Verification Results

- **Full test suite:** 1086 passed, 2 skipped (onnxruntime, unrelated), 0 failed
- **Module registry:** All 16 modules registered across 6 categories
- **RunPod handler:** `ingest_media_sentiment` task callable, correctly validates required fields
- **All benchmark classes importable:** `BenchmarkConfig`, `BenchmarkHarness`, `BenchmarkResult`, `AttributionReport`, `ComparisonReport`, `DatasetComposer`
- **No heavy deps at module level:** Verified across all 22 module files — numpy, polars, torch, transformers, lightgbm, httpx are all imported lazily inside methods
