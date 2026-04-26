# Phase O · Orchestrator + Risk + OMS — Agent Prompts

**Tasks:** TASK-040 (orchestrator), TASK-041 (risk gate), TASK-042 (risk extras), TASK-043 (real-time VaR), TASK-044 (paper OMS), TASK-045 (portfolio service)
**Checkpoint:** End-to-end paper trading — decision → risk → OMS → fill → position — works for one strategy with full audit trail reconstructable from `ord.*` streams. Risk limits enforced. No duplicate orders on restart.

---

## Phase kickoff

```text
You are now implementing the spine that turns predictions into orders. This is where the system stops "thinking" and starts "doing." Bugs in this phase cost money. Bugs in earlier phases produced wrong predictions; bugs here produce wrong trades.

PHASE-SPECIFIC RULES:

1. SINGLETONS. Orchestrator, Risk, and OMS are singletons. Two running concurrently = double-trades = ruin. Use `fincept_core.leadership.Leader` for elections via Redis. The non-leader instance MUST sit idle until it wins, never publish.

2. EVENT-SOURCED EVERYTHING. Every state change (decision created, risk decision, order accepted, partial fill, position updated) is appended to `ord.*` Redis streams AND to the audit_log table. The current state is always recoverable from the stream. NEVER mutate orders or positions in place without writing the transition.

3. IDEMPOTENCY ON RESTART. After a kill -9, replays must NOT generate duplicate orders. Use idempotency keys: hash(decision_id, side, qty) → if already in audit_log, skip.

4. RISK GATE BLOCKS BY DEFAULT. If risk gate code path errors out (DB unavailable, limit lookup fails), the order is REJECTED — not approved. Fail closed, never open.

5. KILL SWITCH IS SYNCHRONOUS. When `risk:kill_switch` key is set in Redis, every active service must check it before publishing. The check is one Redis GET — keep it fast (<1ms). On kill, services log + cease publishing within 1 second.

6. PAPER MODE IS LAW. Until Phase H Gate, TRADING_MODE=paper. Any code path that would route to a live venue must guard with `if settings.trading_mode == 'live'` AND fail loudly if a live adapter isn't configured. Accidental live submission during dev = immediate firing.

7. ALL MONEY = DECIMAL. Everywhere. Forever. If you see a `float * Decimal` operation, the code is broken — not occasionally, always. The result loses precision and the loss compounds.

CONTEXT TO LOAD:
- spec/CONTRACTS.md §4 (Decision, OrderIntent, Order, Fill), §5 (RiskCheckResult, Position), §6 (stream names — particularly the WORM streams).
- spec/ARCHITECTURE.md "deployment" section (singleton enforcement).
- libs/fincept-core.leadership for the Leader pattern.
- TASK-031 / 032 / 033 — the agents whose Predictions you consume.

WHEN STUCK:
- Two orchestrators running? Check leader election. Both should not return is_leader=True; if they do, the leadership.py CAS is broken.
- Order looks duplicated? Check the audit_log for the same `idempotency_key`. If present twice, the dedup path is bypassed somewhere.
- Risk seems too lax? Run `risk.gate.check` with hand-crafted decisions hitting each limit; verify each fires. Never trust limits without explicit unit tests.
- Position drift between OMS and Portfolio service? They must reconcile via `ord.fills` — both consume the same source of truth. If they diverge, one isn't ack'ing or is double-counting.

Acknowledge by listing the 7 rules. Wait for the first task.
```

---

## TASK-040 prompt — Orchestrator

```text
Implement TASK-040 from spec/tasks/TASK-040-orchestrator.md.

Specific landmines:
- Leader election: orchestrator must NOT publish anything until is_leader=True. The check happens on every loop iteration. Loss of leadership mid-run = immediate halt of publication, then re-attempt to acquire.
- Per-symbol state is in-process memory (dict). On restart, replay the last N minutes of sig.* streams to warm state — otherwise the first decisions after restart are based on no signals. Implement a `warm_up()` step in main.py that consumes the last 5 minutes before going live.
- Confidence floor: don't emit a Decision if consensus.confidence < 0.3. Threshold configurable via env. Below threshold = noise, do not trade.
- Decision dedup: if you just published a Decision for (symbol, side) within the last `min_decision_interval_s` (default 30s), skip. Prevents flapping when signals oscillate.
- Stream consumption: use SEPARATE consumer groups for predict / sentiment / regime so they progress independently. One slow agent doesn't block consumption from others.
- Append decision_id, source_signals to ord.decisions for full traceability. Auditors must be able to reconstruct "why did we trade BTC at 14:32:07 UTC".

Append spec/tasks/TASK-040-orchestrator.md and implement.

Verification:
  uv run pytest services/orchestrator
  # End-to-end: publish synthetic predictions on sig.predict; observe Decision on ord.decisions within 1s.

Singleton test:
  uv run python -m orchestrator.main &
  uv run python -m orchestrator.main &
  sleep 2
  redis-cli GET leader:orchestrator
  # Exactly one of the two processes must hold the lock.
```

---

## TASK-041 prompt — Risk gate (basic)

```text
Implement TASK-041 from spec/tasks/TASK-041-risk-gate.md.

Specific landmines:
- Edge/variance estimates for Kelly: where do they come from? They MUST come from the calibration data of the originating agent (read from MLflow registry once at startup, refresh hourly). Hardcoding edge=0.001 in production = systematic over-sizing.
- Kelly fraction default = 0.5 (half-Kelly). Full Kelly is mathematically optimal but in practice over-sizes due to estimation error in edge. Half-Kelly is industry standard.
- Limits are hierarchical: per-symbol → per-strategy → per-firm. Check all three; reject on first failure with the most specific reason.
- Kill switch: check at the very start of `check()`. If active, return `approved=False, reasons=["kill_switch_active"]` immediately. Don't compute anything else.
- Quantity vs notional: Decision carries USD notional. OrderIntent carries quantity (shares/coins). The conversion uses the CURRENT mid-price from the live-prices service. Do NOT convert with a stale price; if no live price, REJECT the order.
- Idempotency: same decision_id arriving twice → second one rejected with reason="duplicate_decision".

Append spec/tasks/TASK-041-risk-gate.md and implement.

Verification:
  uv run pytest services/risk
  # Each limit (symbol notional, gross notional, daily loss, kill switch) has its own deny test.

Manual integration:
  redis-cli SET kill_switch "drill"
  # Publish a Decision on ord.decisions
  # Risk service must log "risk.denied" with reason="kill_switch_active"; nothing on ord.orders.
  redis-cli DEL kill_switch
```

---

## TASK-042 prompt — Risk gate extensions

```text
Implement TASK-042 — extend the risk gate with concentration, restricted list, and self-trade prevention.

Files (extend existing):
- services/risk/src/risk/concentration.py — concentration limits.
- services/risk/src/risk/restricted.py — restricted symbol list.
- services/risk/src/risk/self_trade.py — self-trade prevention (STP).

Concentration limits:
- max_pct_nav_per_symbol (default 20%): no single symbol > 20% of portfolio NAV.
- max_pct_nav_per_sector (default 40%): no sector > 40%. (Sector mapping from a simple JSON config until proper data provider integration.)
- Pre-trade check: project the post-fill state, compare against limits.

Restricted list:
- Loaded from a CSV or DB table at startup, refreshed every 5 minutes.
- Reasons: regulatory restriction, internal blackout (employee personal trading), counterparty risk.
- Reject any order on a restricted symbol with reason="restricted_<reason_code>".

Self-trade prevention:
- If we already have an open BUY order on BTC-USD, reject a new SELL order on BTC-USD that would cross our own bid. (For paper, we cross internal book; for live, exchange-side STP also helps but we double-check.)
- Implementation: maintain in-memory open-orders map keyed by symbol; check before approval.

Author spec/tasks/TASK-042-risk-extensions.md, implement.

Verification: each new check has a dedicated unit test in tests/test_concentration.py, test_restricted.py, test_self_trade.py.
```

---

## TASK-043 prompt — Real-time VaR

```text
Implement TASK-043 — real-time portfolio VaR.

Files:
- services/risk/src/risk/var.py — historical-simulation and parametric VaR.
- services/risk/src/risk/factor_exposures.py — compute betas to a small factor set.

VaR contracts:
- compute_historical_var(positions, lookback_days=252, conf=0.99) -> Decimal
  Uses 1 yr of historical returns, full-revaluation simulation, 99% percentile worst case.
- compute_parametric_var(positions, conf=0.99) -> Decimal
  Variance-covariance method, faster but assumes normality.

Both update on every position change (consume ord.positions). Cache result; refresh every 60s during market hours.

Factor exposures:
- 5 factors: market beta, size, value, momentum, vol-targeting.
- Beta computed via OLS regression over a rolling 60-day window.
- Used by orchestrator for risk-aware allocation in Phase X (TASK-066).

Specific landmines:
- Historical VaR with too-short window = unstable. With too-long window = stale to current regime. 252 days is conventional; document the choice.
- Parametric VaR underestimates tails. Always show alongside historical VaR; never show parametric alone.
- The `kill_switch_threshold_var` config triggers automatic kill switch if portfolio VaR > X. Default disabled; opt-in via env.

Author spec/tasks/TASK-043-var.md, implement.

Verification:
  uv run pytest services/risk/tests/test_var.py
  # Synthetic 100-day return series with known std → parametric VaR within 1bps of analytical answer.
  # Historical VaR on the same series ≈ parametric VaR (since synthetic is normal).
```

---

## TASK-044 prompt — Paper OMS

```text
Implement TASK-044 from spec/tasks/TASK-044-paper-oms.md.

Specific landmines:
- Singleton enforcement: same Leader pattern as orchestrator and risk. Use role="oms".
- Live prices come from `md.trades` consumer running in parallel with the order processor. Both must be running before any order can be filled. If price for a symbol is missing or older than 5 seconds → REJECT order.
- Latency injection: PaperFiller adds Gaussian latency to the fill timestamp. Default mean=50ms, std=15ms. Make configurable for chaos testing.
- Spread cost: half-spread above mid for BUY market, below for SELL. Configurable per-venue spread (use TASK-021's CostModel).
- ts_event in Fill = order_received_ns + latency_ns. Backtests will join on this; getting it wrong creates a phantom edge.
- Order state machine: enforce strictly. PENDING_NEW → NEW → FILLED. Never PENDING_NEW → FILLED. Use `state.can_transition` before every transition.
- Audit: append every state change to ord.orders stream AND audit_log table. The two are redundant on purpose — Redis is fast for live consumers, DB is durable for compliance.

Append spec/tasks/TASK-044-paper-oms.md and implement.

Verification:
  uv run pytest services/oms
  # State machine transitions, fill simulation, audit logging all unit-tested.

Integration:
  make dev
  uv run python -m ingestor.main &
  uv run python -m oms.main &
  # Manually publish an OrderIntent to ord.orders
  redis-cli XLEN ord.fills
  # Should be > 0 within 100ms.
```

---

## TASK-045 prompt — Portfolio service

```text
Implement TASK-045 — positions + P&L + attribution service.

Files:
- services/portfolio/src/portfolio/main.py — entrypoint (consumes ord.fills, publishes ord.positions).
- services/portfolio/src/portfolio/positions.py — position math.
- services/portfolio/src/portfolio/pnl.py — realized + unrealized P&L marking.
- services/portfolio/src/portfolio/attribution.py — by strategy / symbol / factor.

Position update on fill:
- If new symbol: open position with avg_cost = fill price, quantity = signed.
- If same direction: weighted-average new avg_cost, accumulate quantity.
- If reducing: realized_pnl += (fill_price - avg_cost) * fill_qty * sign(quantity). Quantity decreases.
- If crossing zero: close existing, then open new in opposite direction.

P&L marking:
- Realized: closed positions — fixed at closure.
- Unrealized: open positions, marked at latest mid (from md.trades cache).
- NAV = starting_cash + sum(realized) - sum(fees) + sum(unrealized).
- Publish updated Position to ord.positions on every change.

Attribution:
- by_strategy: sum P&L grouped by strategy_id.
- by_symbol: sum P&L grouped by symbol.
- by_factor: regress strategy returns on factor returns (TASK-043's factor exposures); attribute via (beta_factor * factor_return).

Specific landmines:
- Singleton (role="portfolio").
- Reconcile on startup: replay all of ord.fills to rebuild positions. Slow on large history; consider periodic snapshots in audit_log to bound replay time.
- Different from OMS: OMS knows orders/fills; Portfolio knows positions/P&L. Don't duplicate state — Portfolio derives from fills.

Author spec/tasks/TASK-045-portfolio.md, implement.

Verification:
  uv run pytest services/portfolio
  # Manual reconciliation against a hand-computed scenario in test_e2e_pnl.py.
```

---

## Phase O exit verification

```text
Run the Phase O checkpoint validation:

1. End-to-end paper trade lifecycle:
   make dev
   uv run python -m ingestor.main &
   uv run python -m agents.gbm_predictor.main &
   uv run python -m orchestrator.main &
   uv run python -m risk.main &
   uv run python -m oms.main &
   uv run python -m portfolio.main &
   sleep 600
   # Inspect:
   redis-cli XLEN sig.predict     # > 0
   redis-cli XLEN ord.decisions   # > 0
   redis-cli XLEN ord.orders      # > 0
   redis-cli XLEN ord.fills       # > 0
   redis-cli XLEN ord.positions   # > 0
   # P&L visible via portfolio service. Position quantity matches sum of fills.

2. Singleton enforcement:
   # Start two of each (orchestrator, risk, oms, portfolio) on the same Redis.
   # Verify exactly one of each holds its leader:* lock.
   redis-cli GET leader:orchestrator
   redis-cli GET leader:risk
   redis-cli GET leader:oms
   redis-cli GET leader:portfolio

3. Risk denial paths (each in its own scenario):
   - kill switch (redis-cli SET kill_switch test)
   - per-symbol limit (publish decision exceeding MAX_NOTIONAL_USD_PER_SYMBOL)
   - daily loss (manually set realized_pnl to breach limit)
   - restricted list (add symbol to restricted; submit order)
   - VaR (when TASK-043 enabled)
   # Each must produce a "risk.denied" log with the correct reason and ZERO entries on ord.orders.

4. Idempotency on restart:
   # Submit a Decision; let it process to FILLED.
   # Kill the OMS process mid-fill (force partial state).
   # Restart OMS.
   # Verify NO duplicate orders or fills produced.

5. Audit trail completeness:
   # Pick a Fill from ord.fills.
   # Trace back: Fill.order_id → Order in audit_log → OrderIntent → Decision.decision_id → source Predictions.
   # Every link must be present and timestamp-ordered.

6. Throughput sanity:
   # Synthetic generator publishing 100 Decisions per second.
   # Pipeline must absorb without backpressure for 60 seconds.
   # If queue depth grows, profile and fix before declaring done.

If all six pass, declare Phase O COMPLETE. Mark tasks 040–045 as [x]. Add "Checkpoint O: passed YYYY-MM-DD". Proceed to spec/prompts/phase-U-ui-api.md.

If any fail, especially audit-trail completeness or singleton enforcement, do NOT advance — UI built on a broken backend will showcase broken trades to humans.
```
