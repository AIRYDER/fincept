# Phase Z — Research Frontier

> **Strategic intent:** high-variance, durable-payoff research projects. The point of Phase Z is **NOT** ROI within a quarter. The point is durable advantage 18–36 months out. See `spec/EDGE_ROADMAP.md §5` for the research thesis.

**Goal:** Frontier capabilities — options as alpha, generative scenarios, graph neural networks on supply-chain data, causal inference, federated learning. Each module is research-grade and ships only after a published internal whitepaper with reproducible OOS evaluation.

**Checkpoint:** Each Phase Z module has its own internal whitepaper + reproducible OOS evaluation, and individually meets the Phase X+ checkpoint criteria for its scoped contribution before non-zero allocation.

**Funding:** Phase Z is funded by Phase X+ / Y alpha. Do not begin Phase Z if those phases have not produced positive attributable alpha.

## Phase Z — Kickoff

```text
ENTERING PHASE Z — RESEARCH FRONTIER.

Session opener norms apply. Phases F, D, B, A, O, U, X, H, X+, Y complete or in maintenance. Phase Z rules:

1. WHITEPAPER FIRST. Every Phase Z task begins with a 5–10 page internal whitepaper documenting hypothesis, mechanism, prior art (≥10 papers cited), proposed methodology, evaluation plan, expected effect size with confidence interval, and KILL CRITERIA. No code before whitepaper review.
2. KILL CRITERIA ARE NOT NEGOTIABLE. Each whitepaper specifies the result (effect size, p-value, OOS performance) that triggers shipping vs killing the project. If the result misses, the project is killed; the team writes a post-mortem; the artifact (notebook, dataset, code) is archived for future use; we do NOT keep iterating to find a positive result. That is p-hacking.
3. REPRODUCIBLE OOS. The OOS evaluation is on data that did NOT exist at the time of the whitepaper. Train through date T; evaluate on data first available after T. This rules out "I cherry-picked this period" as a critique.
4. INDIVIDUAL X+ CRITERIA. Each Phase Z module must independently meet Phase X+ exit criteria (Sharpe contribution, p-value, orthogonality) at SCOPED capital allocation before any production weight.
5. SHADOW PERIODS ARE ≥ 12 WEEKS. Frontier research has higher noise; longer windows are required.
6. EXTERNAL REVIEW WELCOME. Each whitepaper goes to a senior external reviewer (former quant, academic, peer at another firm) under NDA. Their critique is documented and addressed before code starts.
7. DURABILITY OVER ELEGANCE. Beautiful papers that do not compound advantage by year 3 are failures. Boring projects that compound for 5 years are wins. Pick boring more often than feels right.
8. CONTRACTS ARE STILL IMMUTABLE. New event types ARE allowed in Phase Z (research vehicles), but only via formal RFC; default extension path is via existing tags + audit_log.

CONTEXT: spec/EDGE_ROADMAP.md §5 (mandatory), spec/CONTRACTS.md (extension via RFC).

Tasks: TASK-100..104. Order is operator's choice; tasks are independent. Recommended priority by EDGE_ROADMAP §5: 102 (graph) → 103 (causal) → 100 (options) → 101 (scenarios) → 104 (federated, only if multi-tenant deploy).

Acknowledge by listing the 8 rules. State which Phase Z task you are starting. Confirm the whitepaper exists (NOT just the idea — the actual document). Wait.
```

---

## TASK-100 — Options strategies as alpha sources

```text
TASK-100 — Options as alpha (vol-harvesting, dispersion, asymmetric event).

Hypothesis: Options markets contain THREE durable alpha sources beyond directional speculation: (a) the equity volatility risk premium (selling SPX vol systematically positive-EV), (b) dispersion (index implied vol > weighted constituent implied vol due to correlation premium), (c) asymmetric event-driven plays (gamma-positive entries before known-time-stamped catalysts like CPI/FOMC). Each is well-documented in academic literature; each has practitioner challenges around execution + tail risk.

Files: services/agents/options_alpha/{main,vol_harvest,dispersion,asymmetric_event}.py + services/oms/venue/options.py + libs/fincept-tools/options_pricing.py.

Whitepaper requirements (BEFORE code):
- Vol-harvesting: literature review (Bakshi/Madan, Bondarenko, Israelov + Tummala). Specific strategy: short delta-hedged at-the-money straddles on SPX, weekly rebalance, with vol-targeted notional. Kill criterion: 12-week shadow Sharpe < 0.5.
- Dispersion: short-index-vol + long-constituent-vol correlation trade. Kill criterion: 12-week shadow Sharpe < 0.4.
- Asymmetric event: long gamma + long vega entries 24h before scheduled events; close 4h after. Kill criterion: 8 distinct events with average post-event return < 0.

LANDMINES:
- TAIL RISK IS BIGGER THAN PHASE X+ TAIL HEDGE. Vol harvesting is short vol; one Aug-2015 or Feb-2018 wipes years. Per-strategy hard tail-risk cap = 50% of annual expected return; CIRCUIT BREAKER on VIX > 35.
- HEDGING DRIFT: short-vol delta-hedged is theoretically risk-neutral. Empirically, hedging frequency, transaction costs, jump risk all eat 50–70% of the vol premium. Be conservative on expected returns.
- DISPERSION CAPACITY: small-cap vol illiquidity caps strategy size at $5M-$20M notional. Document the capacity curve.
- EVENT GAMMA: pre-event implied vol is already high. The asymmetric play wins only when vol IS UNDERPRICED relative to realized event move. Verify this empirically per event type, not theoretically.
- LICENSING: real options trading requires options-permission account with broker; sandbox testing only in v1.

DONE WHEN:
- Whitepaper merged + reviewed (≥1 external reviewer).
- pytest green.
- Backtest 2010–2023: vol-harvest Sharpe ≥ 0.7, dispersion Sharpe ≥ 0.5, event-asymmetric N ≥ 12 events with positive average; tail VaR within bounds.
- 12-week shadow on real-time data: at least one of three strategies meets X+ criteria at scoped capital.
- spec/tasks/TASK-100-options-alpha.md authored with whitepaper link + kill criteria.

VERIFY: uv run pytest services/agents/tests/test_options_alpha.py -v
REPORT.
```

---

## TASK-101 — Generative scenario simulation

```text
TASK-101 — Generative scenario simulation (GAN/diffusion adversarial scenarios).

Hypothesis: Backtests + Monte Carlo block bootstrap sample only the OBSERVED return distribution. Generative models (GAN, diffusion, normalizing flows) trained on multi-asset return histories can produce SYNTHETIC scenarios that preserve covariance, cross-asset shock structure, vol clustering, and jump dynamics — including tail events more extreme than any in the training data. Stress-testing against generated scenarios is more rigorous than block bootstrap alone.

Files: services/agents/scenario_gan/{main,trainer,sampler,validator}.py + libs/fincept-tools/scenarios.py.

Whitepaper requirements:
- Literature review (Yoon-Jarrett-van der Schaar TimeGAN, Lopez de Prado generative methods, Tashiro CSDI).
- Specific architecture: TimeGAN trained on (equity, bond, crypto, FX) daily returns; conditional on macro regime (TASK-091).
- Validation: Wasserstein distance, two-sample KS test, autocorrelation function preservation, tail-statistic preservation. NOT just "looks like the data".
- Kill criterion: 5 of 6 statistical tests fail vs holdout.
- Use case: stress test the orchestrator + risk gate against synthetic 1000-day scenarios. Report worst-case 99th-percentile outcomes.

LANDMINES:
- MODE COLLAPSE: GANs notoriously generate a narrow distribution. Validate diversity carefully. Use diffusion or flows if collapse persists.
- LOOK-AHEAD via training signal: training on full history + testing on full history makes the validation circular. Train on rolling window; test on subsequent unseen window.
- TAIL EXTRAPOLATION: a model trained on data without a 1987 / 2008 / 2020 cannot generate one. Don't claim tail safety; claim "stress-tested over distribution similar to training distribution".
- THIS IS RESEARCH SCAFFOLDING, NOT AN ALPHA SOURCE. Do not generate scenarios and trade them. Use scenarios to stress portfolios. Misuse = blow-up.

DONE WHEN:
- Whitepaper merged + reviewed.
- pytest green.
- Generator passes ≥5 of 6 validation statistical tests on holdout.
- Stress-test report: orchestrator + risk gate output against 1000 generated scenarios; worst-case bounded by configured limits.
- spec/tasks/TASK-101-scenario-gan.md authored with whitepaper link + kill criteria.

VERIFY: uv run pytest services/agents/tests/test_scenario_gan.py -v
REPORT.
```

---

## TASK-102 — Graph neural networks (supply-chain + customer-supplier)

```text
TASK-102 — GNN over supply-chain + customer-supplier graphs.

Hypothesis: A firm's price reflects not only its own fundamentals but also the health of its suppliers + customers. Standard ML models miss this because they treat firms as independent. GNNs explicitly model the graph structure. Sources: SEC filings (10-K customer concentration), Factset/Capital IQ supply-chain data, news-extracted relationships. Mechanism: shocks propagate along supply chains 2–6 weeks before being priced.

Files: services/agents/gnn/{main,graph_builder,gnn_model,inference}.py + services/ingestor/supply_chain.py.

Whitepaper requirements:
- Literature review (Cohen-Frazzini "Economic Links and Predictable Returns", Menzly-Ozbas, recent GNN-finance papers Kim+, Wang+).
- Graph: nodes = firms in MVP universe; edges = (a) 10-K customer-concentration (free, EDGAR), (b) news-extracted "supplier of"/"customer of" via TASK-061 LLM, (c) optional Factset (paid, ROI-eval per TASK-093).
- Model: GraphSAGE or GAT on top of standard fundamental + price features.
- Output: per-node prediction emitted as Prediction event_type="gnn_supplychain".
- Kill criterion: GNN Prediction IC ≤ baseline GBM (TASK-031) IC at any horizon. The graph must add information.

LANDMINES:
- GRAPH CONSTRUCTION IS 80% OF THE WORK. 10-K customer-concentration is free but sparse (only top customers > 10% of revenue). News-extracted edges are noisy (LLM hallucinates relationships). Validate edge precision via random sampling + manual review before training.
- DATA-LEAKAGE via graph: if your features include customer firm's price, you've leaked into prediction. Validate strict PIT.
- COLD-START: new IPO has no graph history. Default to zero-edge node; GNN reverts to GBM behavior; flag as low-confidence.
- INTERPRETABILITY: GNN attribution is harder than tree attribution. Use GNNExplainer or SubgraphX for top predictions.
- COMPUTE: ≥5000 firms × 10 years of monthly graphs = nontrivial GPU cost. Budget before starting.

DONE WHEN:
- Whitepaper merged + reviewed.
- pytest green.
- Graph constructed for ≥500 firms in MVP universe with edge precision ≥80% (random sample manual review).
- Walk-forward 5y: GNN Prediction IC > GBM Prediction IC at 60d horizon by ≥0.02.
- 12-week shadow: meets Phase X+ criteria at scoped capital.
- spec/tasks/TASK-102-gnn.md authored with whitepaper link + kill criteria.

VERIFY: uv run pytest services/agents/tests/test_gnn.py -v
REPORT.
```

---

## TASK-103 — Causal inference layer

```text
TASK-103 — Causal inference layer (DoWhy / EconML).

Hypothesis: ML predictions are CORRELATIONAL by construction. Treating them as causal in execution leads to systematic mistakes (e.g., trading on a signal whose information is already in the price; trading on a confound). Causal inference techniques (instrumental variables, regression discontinuity, propensity score, double-ML) ALLOW separation of correlation from causation. Result: cleaner alpha attribution + better counterfactual ("what if we hadn't traded this?") + better model debugging.

Files: services/agents/causal/{main,dowhy_wrapper,counterfactual}.py + services/jobs/causal_attribution.py.

Whitepaper requirements:
- Literature review (Pearl causality, Chernozhukov double-ML, Athey-Imbens, Lopez de Prado on causal in finance).
- Specific applications:
  1. Counterfactual P&L: "what would P&L have been without strategy X?" Requires causal model of strategy interactions.
  2. Confound detection: regress strategy alpha on known factors (size, momentum, value, quality, beta). Residual alpha is what's left after removing confound.
  3. Effect of execution choices: did the venue switch cause better fills, or did markets just calm?
- Kill criterion: causal attribution disagrees with naïve attribution by < 10% over 6 months. (If they agree, causal layer adds no information.)

LANDMINES:
- CAUSAL INFERENCE NEEDS ASSUMPTIONS. Every method requires unverifiable assumptions (no unobserved confounder, monotonicity, parallel trends). State assumptions; assess sensitivity.
- DATA REQUIREMENTS: instrumental variables need an actual instrument (random treatment-affecting, outcome-only-via-treatment). Most finance contexts lack natural instruments. Document carefully.
- THIS IS A DIAGNOSTIC TOOL, NOT AN ALPHA AGENT. Do not generate Predictions from the causal layer; use it to interpret + debug other agents.
- DON'T CLAIM CAUSALITY THE METHOD CANNOT SUPPORT. Most claims will be "consistent with causal X under assumptions Y"; not "X causes Y".

DONE WHEN:
- Whitepaper merged + reviewed.
- pytest green.
- 3 case studies on production strategies: causal vs naïve attribution differs by ≥10% in at least 1; documented why.
- Counterfactual report: monthly auto-generated; published to attribution dashboard.
- spec/tasks/TASK-103-causal.md authored with whitepaper link + kill criteria.

VERIFY: uv run pytest services/agents/tests/test_causal.py -v
REPORT.
```

---

## TASK-104 — Federated learning across tenants

```text
TASK-104 — Federated learning across tenants (CONDITIONAL on multi-tenant deploy).

Hypothesis: If Fincept Terminal is deployed multi-tenant (each customer has own data), federated learning trains shared models on aggregate signal across customers WITHOUT exposing per-customer data. Result: better-than-single-tenant models, while preserving customer privacy + competitive moat.

GATING: This task ONLY runs if a multi-tenant deployment exists with ≥3 customers + signed federation consent. If single-tenant or self-hosted, this task is N/A and should be marked accordingly in BUILD_ORDER.md.

Files: services/agents/fedlearn/{main,coordinator,worker,aggregator}.py + libs/fincept-core/federation.py.

Whitepaper requirements:
- Literature review (McMahan FedAvg, Bonawitz secure aggregation, differential privacy in FL).
- Architecture: each customer trains a local LightGBM (TASK-031) on their data; sends gradients/parameters (NOT data) to central coordinator; coordinator aggregates with differential privacy (epsilon-budgeted); distributes back updated global model.
- Consent: customers OPT-IN to federation. Default = OFF. UI surfaces clearly what is shared.
- Kill criterion: federated model OOS performance ≤ single-tenant model OOS performance. (Federation must add value to justify operational complexity.)

LANDMINES:
- PRIVACY GUARANTEES are subtle. Differential-privacy parameters that look fine can leak information via repeated queries. Hire a privacy expert for review.
- FREE-RIDER RISK: customer with little data benefits from others' data without contributing. Address via reputation/contribution-weighted aggregation.
- LEGAL: cross-jurisdiction data flows have GDPR / CCPA implications even if data isn't shared. Document data flows + obtain customer legal review.
- REGULATORY: in regulated finance contexts (e.g., MiFID), federated models may complicate audit. Consult counsel before live.
- ATTACK SURFACE: malicious customers can poison the federated model. Robust aggregation (median/trimmed-mean, Byzantine-tolerant) required, not naïve averaging.

DONE WHEN:
- Whitepaper merged + reviewed (including external privacy expert).
- pytest green for synthetic 5-tenant simulation: federated model outperforms any single-tenant model on OOS.
- Privacy: differential-privacy budget ε ≤ 1 per training round; total ε accounted across rounds.
- Customer consent flow shipped in UI.
- spec/tasks/TASK-104-fedlearn.md authored with whitepaper link + kill criteria + legal review log.

VERIFY: uv run pytest services/agents/tests/test_fedlearn.py -v
REPORT.
```

---

## Phase Z — Exit verification

```text
PHASE Z EXIT — RESEARCH FRONTIER GATE.

Phase Z does NOT have a single exit checkpoint like F/D/B/A/O/U/X/H/X+/Y. Each Phase Z module exits on its OWN terms.

Per-task exit criteria:
1. Whitepaper merged + ≥1 external reviewer signed off.
2. Reproducible OOS evaluation passes (training-time data + post-training holdout).
3. Module independently meets Phase X+ criteria at scoped capital.
4. Kill criteria explicitly checked; if missed, project killed (NOT iterated to passing).
5. spec/tasks/TASK-1XX.md authored with whitepaper link, kill criteria, and final result documented.
6. mypy --strict clean, pytest green.

Phase Z PORTFOLIO criteria (the system as a whole):
- ≥2 of 5 Phase Z modules ship to non-zero allocation within 18 months of Phase Z kickoff.
- ≥1 Phase Z module's whitepaper is published externally (open source or SSRN) — building recruitment and intellectual moat.
- Phase Z modules contribute ≥10% of total ensemble Sharpe at year 2 of Phase Z.

If <2 modules ship: Phase Z is partially validated. Continue iteratively but slow Phase Z headcount allocation; prioritize maintenance of X+/Y. Frontier research has high failure rate by design — that does NOT mean continue at the same investment.

If ≥1 whitepaper published externally: this is a recruitment + intellectual-moat win independent of P&L. Continue Phase Z investment.

REPORT cadence: quarterly Phase Z review with whitepaper merges, kill events, and P&L attribution.
```
