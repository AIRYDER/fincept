---
title: "Heston, SABR, and Rough Volatility: A Practitioner's Guide to Option Pricing Models"
authors: ["Gatheral, Jim"]
affiliation: "Baruch College, CUNY"
source: "https://www.amazon.com/Volatility-Surface-Practitioners-Guide/dp/0471792519"
date: "2006-08-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "low"
tier_mapping: ["Q4"]
tags: ["heston", "sabr", "rough-volatility", "option-pricing", "volatility-surface"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "high"
---

## TL;DR
The practitioner reference for option pricing models beyond Black-Scholes. Gatheral covers the Heston model (stochastic vol with mean reversion), the SABR model (stochastic alpha, beta, rho — used for interest-rate vol), and the rough volatility family (fractional Brownian motion, Bergomi model). The book is the gold standard for understanding the implied volatility surface, the term structure, and the smile/skew. The Heston and SABR models are the workhorses of equity vol trading; rough vol is the cutting edge.

## Why we care
Fincept is paper-trading only; options are out of MVP scope. But the Heston/SABR family is the *correct* approach to volatility modeling, and the in-tree `services/features/transforms/volatility.py` uses classical realized vol estimators. For Tier Q4 (if Fincept ever adds options), the Heston model is the right starting point. The "rough" extension is the modern frontier.

## Key ideas
- The Heston model: `dS/S = μ dt + √v dW_1`, `dv = κ(θ - v) dt + σ√v dW_2` with `dW_1 dW_2 = ρ dt`. Three parameters (κ, θ, σ) plus correlation ρ.
- The SABR model: `dF = α F^β dW_1`, `dα = ν α dW_2` with `dW_1 dW_2 = ρ dt`. The β parameter captures the underlying's behavior (β=0 for normal vol, β=1 for lognormal).
- The Heston and SABR models both produce a closed-form (or semi-closed-form) implied volatility surface.
- Rough volatility: replace the Brownian motion in Heston/SABR with a fractional Brownian motion with Hurst index H < 0.5. Produces a fatter left tail and a steeper at-the-money skew, matching the observed equity vol surface.

## How to apply to Fincept
1. NOT recommended for current implementation. This is a Tier Q4 entry.
2. The in-tree `VolatilityFeatures` are realized vol estimators; they do not produce a vol surface. For a Tier Q4 options implementation, the Heston model is the right tool.
3. For crypto options (Binance, Deribit), the Heston model is the standard.

## Caveats
- The Heston and SABR models are 30+ years old; the "correct" model is still debated. Rough vol is the modern frontier.
- Implementation cost: months (the vol surface fitting is non-trivial).
- This is a Tier Q4 entry. Fincept is paper-trading, not options-trading. The entry is for future-tracking.

## Related entries
- `research/papers/2024/horvath-neural-sde.md` (neural SDEs are the modern alternative)
- EDGE_ROADMAP §3 Z Tier 4 (options alpha)

## References
- Gatheral, *The Volatility Surface: A Practitioner's Guide* (2006, Wiley)
- Heston, *A Closed-Form Solution for Options with Stochastic Volatility* (1993, RFS)
- Bayer, Friz, Gatheral, *Pricing Under Rough Volatility* (2016, Quantitative Finance)
```

---

## 8. Phase 6 of the expansion — Cutting edge (4 entries)

These add 4 cutting-edge entries on LLM agents, multi-modal LLMs, and diffusion scenarios. Most are Tier Q3+ research tracking.
