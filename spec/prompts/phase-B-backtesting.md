# Phase B · Backtesting — Agent Prompts

**Tasks:** TASK-020, TASK-021, TASK-022, TASK-023, TASK-024
**Checkpoint:** Reference MA-crossover strategy reproduces a known Sharpe ratio on 2 yr of BTC 1m bars within 10% of QuantConnect's number; walk-forward IS/OOS split respects PIT.

**Why this phase comes BEFORE live agents:** every alpha claim must be falsifiable. We build the scoreboard first, then the players.

---

## Phase kickoff

```text
You are now implementing the Backtesting layer. Every strategy you ever deploy will be evaluated here first. The backtester is the firm's primary truth-detector — if it lies, capital evaporates.

PHASE-SPECIFIC RULES:

1. DETERMINISM. Same input data + same strategy + same seed → byte-identical blotter. If you can't reproduce a backtest result two days later, you cannot trust any of its conclusions. Use sorted iteration order, fix random seeds at the boundary, log every non-deterministic decision.

2. NO LOOKAHEAD. The backtester replays bars in event-time order. A strategy MUST receive on_bar(bar) where every value in `bar` was knowable at bar.ts_event or earlier. Any feature pulled from a future bar = silent inflation of returns. Tests must inject a fake leakage signal and verify Sharpe regresses (proves PIT works end-to-end).

3. REALISTIC FILLS. Limit orders fill at limit_price (or better) only if (bar.low <= limit <= bar.high). Market orders cross the half-spread. Slippage scales with size/ADV. Fees per venue. NEVER assume "fill at close price" — that's a research-grade shortcut that overstates returns by 30%+.

4. WALK-FORWARD MANDATORY. A single train/test split is research, not validation. Production-bound strategies use rolling-window walk-forward (e.g., train on 12 months, test on 1 month, advance 1 month, repeat). Backtest results must be aggregated across windows.

5. STATISTICAL HYGIENE. Report Sharpe, max drawdown, Calmar, hit rate, profit factor. ALWAYS report sample size + p-value vs random trading. ALWAYS apply Bonferroni or Benjamini-Hochberg if you searched > 1 strategy. Single-strategy "backtest looks great" with no multiple-testing correction is a lie.

6. COSTS ARE NOT OPTIONAL. Every backtest reports gross AND net returns. The headline number is net. Anyone presenting gross returns is hiding something.

CONTEXT TO LOAD:
- spec/CONTRACTS.md §2 (BarEvent), §4 (OrderIntent, Order, Fill), §9 (Strategy interface).
- libs/fincept-db (TASK-004) for read_bars.
- TASK-017's PIT module — backtester reuses pit.py for feature joins during training data assembly.
- spec/ARCHITECTURE.md "the two clocks" — event time vs ingestion time.

WHEN STUCK:
- Result looks too good (Sharpe > 3 on a simple strategy)? Lookahead bias is the prime suspect. Check feature timestamps before celebrating.
- Backtester slow? Profile before optimizing. Polars vectorized > pandas > pure Python loop. Avoid Python loops in the hot path.
- Numbers don't match QuantConnect? Confirm same: data source, fee model, slippage model, period, rebalance frequency. Tiny differences in any → big P&L deltas.

Acknowledge by listing the 6 rules in your own words. Wait for the first task.
```

---

## TASK-020 prompt — Backtester engine + cost model + broker

```text
Implement TASK-020 from spec/tasks/TASK-020-backtester.md.

Specific landmines:
- The Strategy.on_bar callback receives ONE bar at a time. Strategies that need history must maintain it themselves (or query via ctx.get_feature). Do NOT pass the full DataFrame.
- Order fills are computed BEFORE the next bar arrives. Within a bar, the order goes: receive bar → strategy.on_bar(bar) → broker.on_bar(bar) drains pending orders → emit fills → strategy.on_fill(fill) → mark equity.
- Decimal arithmetic only. Decimal('0.1') + Decimal('0.2') == Decimal('0.3'), but 0.1 + 0.2 == 0.30000000000000004. The latter compounded over 1M trades is real money.
- Equity calculation: cash + sum(realized_pnl) + sum(unrealized_pnl_at_bar_close) - sum(fees). Mark unrealized at bar.close, not bar.open.
- Fill price for limit BUY: if bar.low <= limit_price, fill at min(limit_price, bar.open). The min() handles gap-down opens that print below the limit — you'd actually fill at open. (Same logic mirrored for SELL.)
- Random seed: accept a `seed` arg in BacktestEngine.__init__. Default 0. Pass to any np.random / random use inside cost model + broker.

Append spec/tasks/TASK-020-backtester.md and implement.

Verification:
  uv run pytest services/backtester
  # The test_engine_buys_once test must pass with the exact assertion in the spec.

Sanity check: run a buy-and-hold strategy on BTC-USD 1d bars over 2024. Sharpe should be sensible (e.g., 0.8-1.5 depending on period). If you get 5.0, lookahead bias is in.
```

---

## TASK-021 prompt — Cost model refinement

```text
Implement TASK-021 — extend the CostModel skeleton from TASK-020 with venue-specific schedules and borrow costs.

Files (add or extend):
- services/backtester/src/backtester/costs.py — extend CostModel with classmethods for known venues.

Required venue presets:
- CostModel.binance_spot() — maker 0.10%, taker 0.10%, no borrow.
- CostModel.coinbase_advanced() — maker 0.40%, taker 0.60%, no borrow. (use real Advanced Trade fee tiers, default tier).
- CostModel.kraken_spot() — maker 0.16%, taker 0.26%, no borrow.
- CostModel.us_equity_retail() — commission $0.005/share min $1, SEC fee, FINRA TAF, borrow 4% APR for shorts.

Borrow cost model (US equity short):
- Daily charge = (notional * borrow_rate_annual) / 365.
- Charged at end of each calendar day position is held short.
- Add `apply_overnight_borrow(positions: list[Position], date: date) -> Decimal` to CostModel.

Slippage refinement:
- Replace the linear slippage with a square-root market impact: impact_bps = k * sqrt(child_size / ADV).
- k calibration constant tuned per venue (k=10 for crypto, k=15 for liquid US equity, k=30 for small caps).

Author spec/tasks/TASK-021-cost-model.md, implement. Tests must verify each preset against published fee schedules — store them as JSON fixtures.

Verification:
  uv run pytest services/backtester/tests/test_costs.py -v
  # All venue presets parameterized with golden numbers.
```

---

## TASK-022 prompt — Broker simulator partial fills + cancel/replace

```text
Implement TASK-022 — extend SimBroker (already in TASK-020) with partial fills and cancel/replace.

Partial fill model:
- For limit orders, only `min(order.quantity, k * bar.volume)` fills per bar. k=0.05 (we don't take more than 5% of bar volume).
- Multiple bars can each contribute a partial fill until the order is fully filled or canceled.
- Each partial emits a Fill event. Order status transitions to PARTIALLY_FILLED, then FILLED.

Cancel/replace:
- broker.cancel(order_id) — moves order to CANCELED, emits no fill.
- broker.replace(order_id, new_qty=None, new_limit_price=None) — atomic cancel + new order with same order_id family (linked via parent_order_id).

Files: extend services/backtester/src/backtester/broker.py.

Specific landmines:
- Don't double-fill: an order that becomes FILLED via partial accumulation must be removed from open_orders before the next bar.
- Volume-cap: `bar.volume` is total bar volume, not available-to-our-order. Using all of it is unrealistic. Cap at 5% by default; make configurable.
- Limit fills at midpoint of (limit_price, bar.open) for partials when bar.low < limit < bar.open (price moved through limit and continued — we'd fill at the touch).

Author spec/tasks/TASK-022-broker-partials.md, implement, verify with tests that:
1. Submit qty=10 limit, 3 bars each with bar.volume=20 → fills 1+1+1 partials, FILLED on 3rd bar (with k=0.05).
2. Cancel mid-stream — remaining qty does not fill.
3. Replace with new limit — old order canceled, new order accepted.
```

---

## TASK-023 prompt — Walk-forward runner + report

```text
Implement TASK-023 — production-grade strategy evaluation pipeline.

Files:
- services/backtester/src/backtester/walk_forward.py — WalkForwardRunner.
- services/backtester/src/backtester/report.py — generate HTML report (QuantStats integration + custom factor attribution).

WalkForwardRunner contract:
- __init__(strategy_factory: Callable[[], Strategy], data: DataSource, train_months: int, test_months: int, step_months: int, seed: int = 0)
- run() -> WalkForwardResult with per-window metrics + aggregate.
- Per window: fit any strategy params on train, evaluate on test, record OOS results. NEVER look at test results when fitting. Period.

Aggregate metrics:
- Combined OOS equity curve (concatenation of test windows).
- OOS Sharpe (annualized), max drawdown, Calmar, hit rate, profit factor, turnover.
- p-value vs random trading (block bootstrap with N=1000).
- Bonferroni-corrected p-value if more than one strategy variant was tested.

Report:
- HTML with: equity curve, drawdown, monthly returns heatmap, distribution of returns, factor exposures (if a factor model is available), trade list.
- One-page PDF summary for risk-committee review.

Author spec/tasks/TASK-023-walk-forward.md, implement.

Verification:
  uv run python -m backtester.cli walk-forward \
    --strategy reference.ma_crossover \
    --start 2022-01-01 --end 2024-12-31 \
    --train-months 12 --test-months 1 --step-months 1
  # Produces report.html in reports/ directory.

Spot-check: aggregated OOS Sharpe should be lower than in-sample Sharpe. If OOS > IS, walk-forward is broken (lookahead).
```

---

## TASK-024 prompt — SDK Strategy + research CLI

```text
Implement TASK-024 — the public SDK that quantitative researchers import in notebooks.

Files (in libs/fincept-sdk):
- libs/fincept-sdk/src/fincept_sdk/strategy.py — Strategy ABC + StrategyContext (already declared in spec/CONTRACTS.md §9; copy verbatim).
- libs/fincept-sdk/src/fincept_sdk/data.py — get_bars, stream (typed wrappers around services/api or direct DB depending on env).
- libs/fincept-sdk/src/fincept_sdk/universe.py — load_universe(name) -> list[str].
- libs/fincept-sdk/src/fincept_sdk/cli.py — CLI: `fincept backtest <strategy.module:ClassName>`.

Public API stability:
- Whatever you commit here, downstream notebooks will import. Treat libs/fincept-sdk as semver-locked from this point.
- Mark experimental APIs with `_` prefix or @experimental decorator.

Reference strategies (under libs/fincept-sdk/src/fincept_sdk/reference/):
- ma_crossover.py — fast-MA × slow-MA cross.
- mean_reversion.py — Bollinger band reversion.
- pairs_basic.py — single cointegrated pair.

Each reference strategy must include a docstring with expected Sharpe range on a defined benchmark dataset. These act as smoke tests.

Author spec/tasks/TASK-024-sdk-strategy.md, implement.

Verification:
  uv run fincept backtest fincept_sdk.reference.ma_crossover:MACrossover \
    --symbols BTC-USD --start 2023-01-01 --end 2024-12-31
  # Produces a backtest report. Sharpe within docstring's documented range.
```

---

## Phase B exit verification

```text
Run the Phase B checkpoint validation:

1. Reference strategy reproducibility:
   uv run fincept backtest fincept_sdk.reference.ma_crossover:MACrossover \
     --symbols BTC-USD --start 2023-01-01 --end 2024-12-31 --seed 42 > run1.json
   uv run fincept backtest fincept_sdk.reference.ma_crossover:MACrossover \
     --symbols BTC-USD --start 2023-01-01 --end 2024-12-31 --seed 42 > run2.json
   diff run1.json run2.json
   # Must be empty. Determinism gate.

2. PIT enforcement:
   uv run pytest services/backtester/tests/test_no_leakage.py -v
   # The leakage-injection test must fail when the leakage feature is added (proving guard works).

3. Cost model validation:
   uv run pytest services/backtester/tests/test_costs.py -v
   # Each venue preset matches published fee schedule from JSON fixture.

4. Walk-forward output:
   uv run fincept walk-forward fincept_sdk.reference.ma_crossover:MACrossover \
     --symbols BTC-USD --start 2022-01-01 --end 2024-12-31 \
     --train-months 12 --test-months 1 --step-months 1
   # Generates reports/walk_forward_<timestamp>.html. Open it and verify:
   #   - per-window metrics shown
   #   - OOS Sharpe ≤ IS Sharpe (no lookahead)
   #   - p-value vs random shown
   #   - Bonferroni correction if multiple variants

5. Throughput:
   uv run python -m backtester.bench
   # Synthetic 1M bars / 1 strategy / 1 symbol must complete in < 10 seconds on laptop.

If all five pass, declare Phase B COMPLETE. Mark tasks 020–024 as [x] in spec/BUILD_ORDER.md. Add "Checkpoint B: passed YYYY-MM-DD". Proceed to spec/prompts/phase-A-agents.md.

CRITICAL: Phase A and beyond will use this backtester to validate every model. If the backtester is wrong, every Phase A+ Sharpe number is fiction.
```
