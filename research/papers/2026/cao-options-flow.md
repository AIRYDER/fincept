---
title: "Option Trading Activity and Stock Returns"
authors: ["Cao, Charles", "Chen, Zhiwu", "Griffin, John M."]
affiliation: "Penn State / UC Davis / UT Austin"
source: "https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2005.00765.x"
date: "2005-12-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3"]
tags: ["options-flow", "unusual-options-activity", "signaling", "informed-trading"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The authors show that put-call order imbalance in the equity options market is a strong predictor of subsequent stock returns. When informed traders anticipate negative news, they buy puts (or sell calls); the resulting put-call imbalance predicts a 5-10% return drop over the next 2-4 weeks. The effect is robust across sub-periods, controls for known factors, and is concentrated in stocks with the most options activity. The paper is the canonical reference for "options flow as a signal."

## Why we care
Sisyphus Tier Q3 (X+) Task 080 calls for an "Options-flow agent." Fincept is currently crypto-only, but the architecture should anticipate adding equity coverage. The options-flow agent would be the first non-crypto signal source. The Cao et al. paper is the reference architecture for the agent: read the CBOE/OPRA put-call imbalance data daily, compute the imbalance, and emit a `Prediction` per stock with a 2-4 week horizon.

## Key ideas
- Put-call order imbalance: `PCB = (put_volume - call_volume) / (put_volume + call_volume)`.
- The signal: `PCB > 0.2` (more puts bought than calls) predicts a negative return over the next 2-4 weeks.
- The mechanism: informed traders use options for leverage and limited downside; their activity is observable in the options market before it shows up in stock prices.
- Capacity: the effect is robust for liquid, large-cap stocks; less robust for small-caps and illiquid names.
- Data: CBOE LiveVol, OPRA-derived feeds, or paid vendor (Bloomberg, Refinitiv).

## How to apply to Fincept
1. (Future) Add `services/agents/options_flow/` package per spec Task 080.
2. The agent subscribes to a daily put-call imbalance feed, computes the per-stock PCB, and emits a `Prediction` with horizon 2-4 weeks.
3. The orchestrator consensus weights this agent's predictions the same as any other.
4. The candidate-gate policy should require a paper-spine replay with a Sharpe > 0 over a 1-year OOS window before the options-flow agent is promoted.

## Caveats
- The paper is on US equity. Crypto options are a different market (Binance options, Deribit). The mechanism may or may not transfer.
- Data cost: CBOE LiveVol is $1000+/month; OPRA-derived data is more expensive. This is a Phase X+ investment, not Tier Q2.
- The paper is from 2005; the options market has changed. Recent studies (Hu, 2018; Pan & Poteshman, 2006) confirm the effect but with smaller magnitudes.

## Related entries
- `research/papers/2025/qlib-architecture.md` (Qlib has a basic options signal reference)
- `research/architectures/qlib-design.md` (alpha evaluator applies to options flow)
- EDGE_ROADMAP §2 X+ Task 080

## References
- Cao, Chen, Griffin, *Option Trading Activity and Stock Returns* (2005, Journal of Finance)
- Pan & Poteshman, *The Information in Option Volume for Future Stock Prices* (2006, RFS)
- Hu, *Does the Put-call Ratio Predict Stock Returns?* (2018, JFM)
