# Phase D · Data Spine — Agent Prompts

**Tasks:** TASK-010, TASK-011, TASK-012, TASK-013, TASK-014, TASK-015, TASK-016, TASK-017
**Checkpoint:** 24-hour soak ingesting ≥5 crypto pairs with zero dropped messages; <100 ms WS-recv-to-DB-commit latency at p99; offline backfill reproduces live features bit-exact.

---

## Phase kickoff

```text
You are now implementing the Data Spine. Every downstream layer (features, agents, OMS) consumes what you produce. A single dropped message becomes a corrupted feature becomes a wrong trade.

PHASE-SPECIFIC RULES (read before any task):

1. PRECISION. Every price, size, fee is `decimal.Decimal`. Never `float`. Never. The moment you see a float in code touching market data, the code is broken.

2. TIMESTAMPS. ts_event is the venue's clock (when did the trade actually happen). ts_recv is our wall clock (when did we receive it). Both are integer nanoseconds. Backtests join on ts_event. Latency SLOs are computed as ts_recv - ts_event. Mixing them silently is a class-of-bug we treat as critical.

3. SYMBOLS. Canonical form is "BTC-USD", "ETH-USD". Adapters convert venue-native ("BTCUSDT" on Binance, "BTC-USD" on Coinbase, "XBT/USD" on Kraken) to canonical. Downstream code never sees venue-native.

4. CONNECTIONS. Every WebSocket adapter MUST:
   - reconnect with exponential backoff + jitter (start 0.5s, cap 30s).
   - detect sequence gaps and trigger snapshot resync.
   - emit heartbeat alarms (no message in N seconds → mark stale, alert).
   - close cleanly on SIGTERM (drain in-flight before exit).

5. NO BLOCKING I/O. Async everywhere. No `requests.get()`. No `time.sleep()` (use `asyncio.sleep`). One blocking call inside an async loop kills throughput.

6. BATCH WRITES. DB inserts go through `fincept_db.ticks.batch_insert_*` with COPY. Per-row INSERT will not meet the throughput SLO.

7. PUBLISH BEFORE PERSIST. The Redis Stream is the source of truth for downstream consumers. Publish to Redis FIRST, then batch-flush to Timescale. If the DB is slow we still produce signals; if Redis is slow we have a real problem and should alert.

CONTEXT TO LOAD:
- spec/CONTRACTS.md §2 (market data events) — the events you produce.
- spec/CONTRACTS.md §6 (stream names) — where you publish.
- libs/fincept-core (TASK-002) — schemas, clock, ids you import.
- libs/fincept-bus (TASK-003) — Producer / Consumer.
- libs/fincept-db (TASK-004) — batch_insert helpers.
- spec/ARCHITECTURE.md "the two clocks" section.

WHEN STUCK:
- Venue API differs from spec? Document the deviation in a comment, normalize to canonical schema, move on. Spec wins outwardly; adapter handles the venue's mess.
- Test needs Redis/Postgres? Mark with @pytest.mark.integration and require docker compose to be up.
- Can't reach venue in test? Snapshot a fixture (recorded WS messages as JSONL in tests/fixtures/) and replay.

Acknowledge by listing the 7 rules above in your own words. Wait for the first task.
```

---

## TASK-010 prompt — Ingestor base + Binance adapter

```text
Implement TASK-010 from spec/tasks/TASK-010-ingestor-binance.md.

Specific landmines:
- Binance ws ping interval = 15s. Their server times out at ~30s of silence. websockets lib has built-in ping if you set ping_interval=15, ping_timeout=10. Use that.
- Binance trade msg field "T" is event time in milliseconds. Multiply by 1_000_000 for ns.
- Binance trade field "m" = is_buyer_market_maker. Side from buyer's perspective: m=True → seller side aggressive → side=SELL; m=False → buyer side aggressive → side=BUY.
- depth diff: bids_remove and asks_remove are prices where size went to zero (encoded as quantity "0").
- max_size on websockets connect: depth snapshots on top pairs can be >1MB. Set max_size=2**22 (4MB).
- Symbol: BTCUSDT → BTC-USDT canonical. Suffix list in to_canonical: try USDT, USDC, USD, BTC, ETH in that order (longest match wins).

Append spec/tasks/TASK-010-ingestor-binance.md and implement.

Verification:
  uv run pytest services/ingestor
  uv run python -m ingestor.main &  # background
  sleep 30
  redis-cli XLEN md.trades  # must be > 0

Stop the background process before declaring done.
```

---

## TASK-011 prompt — Coinbase adapter

```text
Implement TASK-011 — Coinbase Advanced Trade WebSocket adapter.

Mirror the structure of services/ingestor/binance.py. Inherit from VenueAdapter base. Differences:

- URL: wss://advanced-trade-ws.coinbase.com
- Subscribe message format: {"type":"subscribe","channels":["matches","level2"],"product_ids":[...]}
- Product IDs are already in "BTC-USD" form — symbol is canonical natively.
- Message types you handle: "match" (trade), "l2update" (book delta).
- Timestamps in ISO 8601 strings — convert via fincept_core.clock.iso_to_ns.
- Sequence in "sequence" field on "match", "sequence_num" on "l2update".

Author spec/tasks/TASK-011-coinbase.md from the spec/PROMPTS.md template before implementing. Reuse base.py and writer.py from TASK-010 — do not fork them.

Verification: same pattern as TASK-010 but with COINBASE in the venue field. Check `redis-cli XINFO STREAM md.trades | head` shows entries with venue=coinbase mixed in.
```

---

## TASK-012 prompt — Kraken adapter

```text
Implement TASK-012 — Kraken WebSocket v2 adapter.

Differences from Binance/Coinbase:
- URL: wss://ws.kraken.com/v2
- Subscribe: {"method":"subscribe","params":{"channel":"trade","symbol":["BTC/USD",...]}}
- Symbol native: "BTC/USD" → canonical "BTC-USD" (replace / with -).
- Trade timestamps in RFC 3339 with microsecond precision.
- Order book channel "book" with depth=10 default; request depth=100 for full L2.

Author spec/tasks/TASK-012-kraken.md, implement, verify same way.

After this task, services/ingestor/main.py should spawn all three adapters concurrently. Update main.py to use asyncio.TaskGroup (Python 3.11+) and gather them.
```

---

## TASK-013 prompt — EOD equity loader

```text
Implement TASK-013 — daily equity OHLCV loader.

This is a scheduled job, NOT a streaming process. Triggered by jobs/daily_eod_load.py at market close.

Files:
- services/ingestor/src/ingestor/eod_equity.py — async function load_eod(symbols: list[str], date: datetime.date) -> int (count loaded).
- Uses yfinance (free) by default. Optional Polygon.io if POLYGON_API_KEY set (better data quality).
- Writes to bars_1d hypertable via fincept_db.bars.insert_bars.

Specific landmines:
- yfinance is sync — wrap calls in asyncio.to_thread to keep the event loop free.
- yfinance occasionally returns NaN for OHLCV — drop those rows, log a warning.
- Idempotency: if a bar for (symbol, ts_event, freq) exists, ON CONFLICT DO UPDATE (the latest pull wins).
- Universe: load symbols from settings.universe filtered by AssetClass.EQUITY (you'll need to extend the universe model to carry asset class — add it as TASK-013 scope; update contracts in CONTRACTS.md only with leadership approval).
- Dividend / split adjustments: yfinance auto-adjusts; Polygon does not. Document which mode you're in.

Author spec/tasks/TASK-013-eod-equity.md, implement, verify by running:

  uv run python -m ingestor.eod_equity --symbols AAPL,MSFT --date 2025-01-15
  psql ... -c "SELECT count(*) FROM bars_1d WHERE symbol IN ('AAPL','MSFT');"
```

---

## TASK-014 prompt — Quality monitor + supervised reconnect

```text
Implement TASK-014 — wraps the existing adapters with quality monitoring and supervised reconnect.

Files:
- services/ingestor/src/ingestor/quality.py (already exists from TASK-010 — extend, don't rewrite).
- services/ingestor/src/ingestor/supervisor.py — new. Wraps an adapter in a retry loop with exponential backoff + jitter.

Quality metrics to expose via OpenTelemetry:
- ingestor.feed.latency_ns (histogram, labels: venue, symbol)
- ingestor.feed.gaps_total (counter, labels: venue, symbol)
- ingestor.feed.cross_spread_total (counter, labels: venue, symbol) — fired when bid > ask (data corruption)
- ingestor.feed.staleness_seconds (gauge, labels: venue) — seconds since last message

Supervisor behavior:
- on_disconnect: log warning, sleep min(30, 0.5 * 2^attempts) + random.uniform(0, 1) jitter, try reconnect.
- after 10 consecutive failures: emit critical alert, keep trying with capped backoff.
- on SIGTERM: cancel reconnect, drain in-flight events, exit clean.

Author spec/tasks/TASK-014-quality.md, implement, verify with a chaos test:

  # script that kills the WebSocket every 60 seconds for 10 minutes
  python tests/chaos/kill_ws.py
  # ingestor must reconnect each time, no panic, no leaked connections

Update services/ingestor/src/ingestor/main.py to wrap each adapter in supervisor.run().
```

---

## TASK-015 prompt — Reserved (already mapped to EOD equity in TASK-013)

```text
Skip — TASK-015 was reserved in BUILD_ORDER.md but the equity loader was implemented under TASK-013. Update spec/BUILD_ORDER.md to mark 015 as merged into 013.
```

---

## TASK-016 prompt — Feature transforms

```text
Implement TASK-016 — pure-function feature transforms operating on Polars DataFrames.

Files:
- services/features/src/features/transforms/price.py — returns, log_returns, momentum (multi-window).
- services/features/src/features/transforms/volatility.py — realized_vol, parkinson_vol, garman_klass_vol.
- services/features/src/features/transforms/microstructure.py — book_imbalance, spread_bps, trade_intensity, vpin.
- services/features/src/features/transforms/cross.py — beta, rolling_correlation, z_score_cross_sectional.

Specific landmines:
- All functions take Polars DataFrame and return Polars DataFrame with new columns appended. No mutation of input.
- Use Polars expressions (over, rolling_mean, ewm_mean) — much faster than groupby+apply.
- For windowed features, accept a `min_periods` arg; emit Null until enough history.
- VPIN (Easley/López de Prado): bucket trades by volume, classify each bucket as buy/sell, VPIN = |V_buy - V_sell| / (V_buy + V_sell). Window-of-buckets (default 50).
- Returns must use log returns by default for additivity. Provide simple-return variant separately.

Author spec/tasks/TASK-016-features-transforms.md. Tests: each transform tested with a small fabricated DataFrame and known expected output.

Verification:
  uv run pytest services/features/tests/transforms/

Performance target: 1M rows × 10 features in < 2 seconds on a laptop.
```

---

## TASK-017 prompt — Feature store + PIT joins

```text
Implement TASK-017 — online + offline feature store with point-in-time-correct joins.

This is the most subtle task in Phase D. Get PIT wrong and every backtest result lies.

Files:
- services/features/src/features/store.py — OnlineStore (Redis hashes, <10ms reads), OfflineStore (Timescale).
- services/features/src/features/pit.py — point-in-time joins for training data.
- services/features/src/features/online.py — worker that consumes md.bars.1m, computes features, writes to OnlineStore + OfflineStore.

OnlineStore contract:
- key = "feat:{symbol}:{name}", value = "{value};{ts_event_ns}".
- get_row(symbol, names) returns dict {name: value} ONLY if every (symbol, name) was updated within the last 5 minutes; else returns Nones for stale features.
- Why: stale features in production = silent garbage in.

OfflineStore + PIT join contract:
- Features are stored with their ts_event (when the input data was timestamped) and ts_compute (when we calculated them).
- PIT join: given a label timestamp t, only join features where ts_compute <= t. NEVER ts_event <= t — that's the leakage trap. Compute time is what was actually available.
- Test: train a model on PIT-joined features, manually inject a future leakage feature, verify validation accuracy DROPS (not improves) under PIT — proves PIT is enforced.

Author spec/tasks/TASK-017-feature-store.md. This task warrants extra-strict review.

Verification:
  uv run pytest services/features/tests/test_pit.py -v

The PIT-leakage regression test is the most important test in the entire codebase.
```

---

## Phase D exit verification

```text
Run the Phase D checkpoint validation:

1. Start the full stack:
   make dev
   uv run python -m ingestor.main &       # background
   uv run python -m features.online &     # background

2. Soak test (24 hours real time, OR 1 hour minimum for staging acceptance):
   - Watch `docker stats` — ingestor RSS must stabilize (no memory leak).
   - Watch Grafana dashboard ingestion.feed.latency_ns p99 — must stay below 100ms.
   - Watch ingestion.feed.gaps_total — must stay at 0 across the soak window.
   - Watch ingestion.feed.cross_spread_total — must stay at 0.

3. Forcibly kill the ingestor process. Restart. Verify it recovers:
   - No duplicate trades in DB (ON CONFLICT enforced).
   - No gap reported in quality.gaps after restart (it should re-snapshot the book).

4. PIT verification:
   uv run pytest services/features/tests/test_pit.py::test_pit_blocks_leakage -v
   # MUST be green. If red, Phase D is NOT complete.

5. Backfill reproducibility:
   uv run python -m features.offline --symbol BTC-USD --start <yesterday> --end <today>
   # Compare a row from the backfill to the same row produced live during the soak.
   # Bit-exact match required.

6. Stop background processes cleanly. Verify they exit within 5 seconds of SIGTERM.

If all six pass, declare Phase D COMPLETE. Mark tasks 010–017 as [x] in spec/BUILD_ORDER.md. Add "Checkpoint D: passed YYYY-MM-DD" note. Proceed to spec/prompts/phase-B-backtesting.md.

If any fail, do NOT advance. Backtests built on a leaky data spine produce models that lose money in production.
```
