# Edge Roadmap — How This System Plans to Outperform

> Living document. Reviewed at every phase exit. Decisions are reversible; theses are not.

## 1. The thesis (and the brutal truth)

**Goal:** Net-of-cost Sharpe ≥ 1.5 with max drawdown ≤ S&P 500 over rolling 3-year windows, across a multi-asset paper-then-live portfolio (US equity + crypto + selectively options).

**Brutal truth:**

- ~80% of professional active managers fail to beat the S&P over 10+ years, net of fees.
- The base rate for systematic firms reaching durable Sharpe > 1.5 net of cost is low.
- The Phase A–X feature set puts us at "credible mid-frequency multi-asset platform" — *necessary* but not *sufficient* for consistent outperformance.
- The gap from "credible" to "alpha-generating" is the work in this roadmap.

**Where edges actually exist at this scale:**

- Faster reaction to public news (LLM sentiment, transcript analysis).
- Better execution (RL execution, smart routing).
- Alternative data the consensus prices in slowly.
- Behavioral mispricings (momentum after under-reaction, mean-reversion after over-reaction).
- Less-liquid markets where institutional capacity is constrained.
- Multi-asset diversification (crypto and equity decorrelate during specific regimes).
- Options-based asymmetry (cheap convexity around macro events).

**Where edges do NOT exist at this scale (do not build):**

- HFT / sub-millisecond latency. Citadel, Jane Street, Jump have already won this.
- Dark pool / IOI access. Institutional only.
- Information edges from proprietary M&A data. Either illegal or capacity-bound.
- Pure correlation factor-zoo with no causal hypothesis. Crowded; capacity-constrained.
- Twitter/Reddit firehose at scale. Signal-to-noise ratio is brutal; LLM cost dwarfs alpha.

## 2. Tiered roadmap

The Phase X spec ends at "shadow ensemble beats baseline by Sharpe ≥ +0.5." That is a *necessary* gate, not the final destination. Beyond Phase X, there are three tiers ordered by leverage-per-engineer-week.

### Tier 1 — Phase X+ · Profitability layer

Highest leverage-per-effort. Each fits cleanly into existing contracts (`spec/CONTRACTS.md`). Ship before any Tier 2 work begins.

| Addition | Why it adds Sharpe | Marginal cost |
|---|---|---|
| **Options flow agent** | Unusual options activity is one of the few signals retail can buy with documented persistence. | Data feed (CBOE LiveVol or scrape OPRA-derived) + 1 agent. |
| **Earnings call transcript LLM agent** | Management tone + forward-guidance language predicts 3–60 day returns (Loughran-McDonald + modern LLMs). | Reuses `agents/llm_sentiment` infra; new fetcher. |
| **Insider Form 4 + short interest agents** | Open insider purchases (especially clusters) are durable. High SI + positive catalyst = squeeze. Free SEC/FINRA data. | 1 ingestor + 1 agent. |
| **Cross-sectional ranking layer** | The most durable equity edge for 30+ years: long top decile, short bottom decile by composite score. | New step in `services/orchestrator`. |
| **Portfolio-level vol targeting** | Smooths the equity curve and improves Sharpe even with no new alpha. | New layer in `services/risk` above Kelly. |
| **Strategy decay monitor + capacity curves** | Every alpha decays. Without monitoring, allocation continues to dead strategies. Capacity curves prevent over-allocation. | New job + dashboard tile. |
| **Multi-agent LLM debate** | Bull / bear / judge consistently beats single-shot LLM in evals. 3× tokens. | Replaces `llm_loop.py` decision step. |
| **Sector rotation overlay** | Macro-conditioned sector tilts add a medium-frequency layer nearly orthogonal to single-stock alpha. Free. | Macro features + new agent. |
| **Correlation-breakdown alerts** | When "uncorrelated alphas" suddenly correlate, realized vol explodes. Catch the regime shift. | New monitor in `services/risk`. |
| **Liquidity stress test** | Daily: "if I had to exit 50% of book in 1 day, what's the implied slippage?" Caps tail position size. | New job in `services/risk`. |

**Phase X+ checkpoint:** 8-week shadow deployment of (Phase X agents + Phase X+ additions). Required: Sharpe ≥ baseline + 0.7, max drawdown ≤ benchmark, realized vol ≤ portfolio vol target ± 20%, with p < 0.05 via block bootstrap.

### Tier 2 — Phase Y · Differentiation

Bigger investments. Real differentiation from generic retail platforms. Only after Tier 1 is profitable in shadow.

| Addition | Why it matters |
|---|---|
| **On-chain analytics for crypto** | Whale tracking, exchange flows, stablecoin issuance, DeFi TVL, miner reserves. Crypto's equivalent of fundamentals. |
| **Cross-asset macro regime model** | Inflation-up + growth-down + liquidity-tightening are jointly more predictive than any single regime axis. Drives the orchestrator's per-strategy weights. |
| **Tail-risk hedging budget** | Carve 0.5–1% NAV/year for OTM SPX puts. Caps the rare drawdowns that destroy compounding. |
| **Selective alt-data integration** | App downloads (SensorTower), web traffic (SimilarWeb), hiring (Glassdoor). Skip $50k+ vendors initially. |
| **Multi-arm bandit strategy allocator** | Thompson-sampling capital allocator across *strategies* (not signals). Pairs with decay monitor. |
| **Online learning / concept drift** | `river` for incremental updates of GBM and feature normalizers. Avoids all-or-nothing nightly retrain. |
| **L2 microstructure features** | Order-book imbalance, hidden-liquidity inference, trade-flow toxicity. Adds short-horizon edge. |

**Phase Y checkpoint:** 12-week shadow + paper. Outperforms benchmark over 3 distinct macro regimes within the period. Capacity stress test: simulated $10× current AUM does not degrade Sharpe by more than 20%.

### Tier 3 — Phase Z · Research frontier

High variance, durable payoff. Funded by Tier 1+2 alpha. Not a substitute for them.

| Addition | Why it matters |
|---|---|
| **Options strategies as alpha sources** | Vol-harvesting, dispersion, asymmetric event bets. Doubles platform complexity. |
| **Generative scenario simulation** | GAN/diffusion model produces plausible adversarial scenarios for stress-testing. |
| **Graph neural networks** | Supply-chain and customer-supplier relationships. Captures information-flow effects. |
| **Causal inference layer** | Replace "X correlates with Y" with "X causes Y via mechanism Z." Causal edges survive regime shifts. |
| **Federated learning** | If multi-tenant. Train across user accounts without seeing positions. |

**Phase Z checkpoint:** Each module has a published internal whitepaper with reproducible OOS evaluation. No deployment without Phase X+ checkpoint criteria met by that specific module.

## 3. The "do not build" list

Discipline matters more than ambition. The following are common retail-firm traps:

- Sub-millisecond latency / colocation. Latency target stays <100ms signal / <500ms decision.
- Twitter/Reddit firehose. Signal-to-noise too low; LLM cost too high. Sample r/wallstreetbets sentiment extremes cheaply if at all.
- Sentiment from images / video. Token cost vs alpha is currently terrible.
- Pure RL for portfolio allocation. Sample-inefficient and unstable. Stick to mean-variance / risk parity / multi-arm bandit.
- "1000 features" trap. Capacity-bound, mostly noise after multiple-comparison correction. Phase X TASK-066 already captures the productive subset.
- Mass-customized indicators with no causal hypothesis.
- Mass-scrape every news source. Pay for one good vendor or two; quality > coverage.

## 4. Risk and ops items the original spec underweights

These are *not* alpha sources — but their absence destroys realized alpha. They join Phase X+ and Phase H as appropriate.

- **Correlation breakdown monitor** (Phase X+, in `services/risk`).
- **Liquidity stress test** (Phase X+, in `services/risk`).
- **Counterparty exposure dashboard** (Phase H — only meaningful live).
- **Strategy capacity curve** (Phase X+, paired with decay monitor).
- **Reg-T / Reg-SHO monitor** (Phase H — only with margin).
- **Drawdown circuit breakers per strategy** (Phase X+, in `services/risk` — strategy-level kill, not just portfolio-level).

## 5. Decision principles for new alpha proposals

Every proposed addition (whether in this doc or new) gets evaluated against these principles before becoming a TASK spec:

1. **Causal hypothesis.** Why should this work? "Correlation in backtest" is not an answer. Document the economic / behavioral mechanism.
2. **Capacity estimate.** At what AUM does this strategy stop scaling? If <$10M, deprioritize.
3. **Marginal cost vs marginal Sharpe.** Will the data + compute + LLM cost exceed expected alpha? Compute both before building.
4. **Decay rate.** How fast does this alpha get arbitraged away once it's published? Older signals (insider buying, post-earnings drift) decay slower than novel ones.
5. **Orthogonality.** Is this orthogonal to existing alphas? Adding the 5th momentum variant adds capacity-bound noise; adding the 1st cross-asset macro signal adds Sharpe.
6. **Eval suite first.** Before code, define labeled examples + scoring metric. If you can't define the eval, you don't understand the problem.
7. **Shadow before live.** No new alpha source touches order routing for 4+ weeks (8 for LLM-based). Period.

## 6. What success looks like

End-state targets, by phase exit:

| Phase | Net Sharpe | Max DD | Realized Vol | Capacity | Notes |
|---|---|---|---|---|---|
| Phase O exit | ≥ 0.5 | ≤ 25% | unconstrained | $1k paper | Plumbing works. |
| Phase X exit | ≥ 1.0 | ≤ 20% | ≤ 25% | $1k–$10k paper | Cutting edge agents help. |
| Phase X+ exit | ≥ 1.5 | ≤ 15% | targeted ~10–15% | $10k–$100k paper / $1k live | Profitability layer working. |
| Phase Y exit | ≥ 1.7 | ≤ 12% | targeted ~10% | $1M+ live | Differentiation. |
| Phase Z exit | ≥ 2.0 | ≤ 10% | targeted ~10% | $10M+ live | Frontier; durable. |

These are **targets**, not promises. Track honestly; revise when reality disagrees.

## 7. Operating rhythm

- **Quarterly:** edge-roadmap review with full team. Re-rank Tier-2/3 candidates by current data. Retire dead theses.
- **Per phase exit:** update Section 6's actuals vs targets. If actuals miss by >25%, hold rollout and diagnose before adding new alphas.
- **Per new alpha proposal:** pass Section 5's seven principles before becoming a TASK spec.
- **Per dead alpha:** post-mortem doc in `docs/postmortems/alpha-NAME-YYYY-MM.md`. Lessons feed back into Section 5.

## 8. References

- `spec/BUILD_ORDER.md` — task IDs and dependencies for Phases X+, Y, Z.
- `spec/prompts/phase-Xplus.md` — agent prompts to drive Phase X+ implementation.
- `spec/CONTRACTS.md` — canonical types every new alpha must conform to.
- `docs/RISKS.md` — broader risk register including the "alpha thesis fails" risk.
