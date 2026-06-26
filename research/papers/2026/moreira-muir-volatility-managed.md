---
title: "Volatility-Managed Portfolios"
authors: ["Moreira, Alan", "Muir, Tyler"]
affiliation: "Yale School of Management"
source: "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2819114"
date: "2017-01-15"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q2.2", "Q1.3"]
tags: ["volatility-targeting", "portfolio-construction", "risk-management", "sharpe-improvement"]
license: "CC-BY"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
Volatility-managed portfolios scale position sizes inversely to recent realized volatility: when vol is low, take larger positions; when vol is high, take smaller ones. The authors show this single transformation improves Sharpe ratios for many classic strategies (momentum, value, carry) on US equity, Treasury, currency, and commodity markets from 1926–2015. The mechanism is convex: scaling down in volatile periods reduces drawdowns more than it reduces gains.

## Why we care
The current `services/orchestrator/src/orchestrator/allocator.py::target_notional` is a linear function of signal strength: `notional = cap * |direction * confidence|`. There is no volatility targeting. As the Sisyphus Quant/ML Deep Dive (Tier Q2.2) flagged, this caps the system's Sharpe around 1.0 because exposure does not adapt to regime vol. Volatility targeting is the cheapest, highest-leverage Sharpe improvement available — no new alpha source needed, just a wrapper around the existing allocator.

## Key ideas
- Volatility scaling: w_t = c / σ_t where σ_t is recent realized vol (e.g., 20-day rolling).
- The "managed" return: r̃_t = w_t × r_t. This is the return of a portfolio whose dollar exposure scales with inverse vol.
- Mechanism: option-like behavior. In low-vol regimes, you are long gamma; in high-vol regimes, you automatically de-risk.
- Key practical detail: use a vol estimator with bounded sensitivity to jumps. Exponentially-weighted realized vol with a 20–60 day window is standard; Garman-Klass or Yang-Zhang if you have OHLC.
- The "managed" series is, in expectation, lower-variance and higher-Sharpe than the unmanaged series, with the same alpha source.

## How to apply to Fincept
1. Add `services/risk/vol_target.py::scaled_notional(raw_notional, realized_vol, target_vol)`. Signature: `Decimal → Decimal`. Pure function.
2. Wire it into `orchestrator/allocator.py::target_notional` so the returned notional is `scaled_notional(raw, recent_vol, settings.TARGET_PORTFOLIO_VOL)`.
3. Compute realized vol per symbol in the orchestrator (or pull from a `vol_target` service that subscribes to features.online).
4. Add a `TARGET_PORTFOLIO_VOL` setting (default 0.10 annualized, configurable per strategy).
5. Document the behavior change in the dossier (Tier Q1.1) — Sharpe should improve, but max notional per symbol may be hit more often.

## Caveats
- Vol targeting reduces the magnitude of wins as well as losses. In trending low-vol regimes, the unmanaged version can outperform.
- Realized vol estimators are noisy; an underestimate can leave the portfolio over-exposed exactly when a vol spike hits. Use an upper confidence band, not a point estimate.
- For crypto, 24/7 trading means a 20-day window is 20 calendar days, not 20 trading days. Adjust the annualization factor.

## Related entries
- `research/papers/2025/concept-drift-survey-gama.md` (vol regime change is a kind of concept drift)
- `research/architectures/qlib-design.md` (Qlib's portfolio builder uses vol targeting)
- `research/papers/2026/chow-yang-correlated-kelly.md` (Kelly with vol target is the next step after this entry)

## References
- Moreira & Muir, *Volatility-Managed Portfolios* (2017, Journal of Finance)
- Daniel & Moskowitz, *Momentum Crashes* (2016, JF) — same authors' earlier evidence of momentum's vol exposure
- Hocquard, Papageorgiou, Réveillac, *A Constant-Volatility Framework for Managing Tail Risk* (2015)
