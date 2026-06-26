---
title: "Sector Rotation and Stock Returns: A Macro-Conditioned Factor Model"
authors: ["Bhojraj, Sanjeev", "Cremers, K. John P.", "Driessen, John"]
affiliation: "Cornell University / University of Notre Dame / Tilburg University"
source: "https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2007.01230.x"
date: "2007-12-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3.4", "Q2.1"]
tags: ["sector-rotation", "macro-regime", "factor-model", "cross-asset"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
The authors show that a sector-rotation strategy conditioned on macro variables (interest rates, credit spreads, GDP growth) significantly outperforms unconditional sector rotation. The signal: when interest rates are rising, tilt toward value sectors (financials, energy); when rates are falling, tilt toward growth sectors (technology, healthcare). The strategy is implemented as a long-short across sectors and is robust across 1958-2005 US equity.

## Why we care
Sisyphus Tier Q3.4 calls for a "Sector rotation overlay." Fincept is crypto-only, but the strategy generalizes to crypto: when the macro regime is risk-on (low VIX, expanding credit), tilt toward high-beta crypto (altcoins); when risk-off (high VIX, contracting credit), tilt toward low-beta crypto (BTC, stablecoins). The Bhojraj-Cremers-Driessen paper is the academic reference for the macro-conditioned sector rotation.

## Key ideas
- The macro signal: a 3-variable model (interest rate, credit spread, GDP growth) classifies the regime as "expansion" or "contraction."
- The sector tilt: in expansion, overweight cyclical sectors; in contraction, overweight defensive sectors.
- The implementation: long the top-decile of "macro-favored" sectors, short the bottom-decile, rebalance monthly.
- The empirical result: the macro-conditioned rotation produces a Sharpe of ~0.8 vs ~0.5 for unconditional rotation, over 1958-2005.

## How to apply to Fincept
1. Add `services/agents/sector_rotation/` package per spec Task 087.
2. The agent subscribes to a macro data feed (FRED for rates, credit spreads; CoinGecko for crypto market cap), classifies the regime, and emits per-symbol `Prediction`s with a 1-month horizon.
3. The orchestrator consensus combines the rotation agent's predictions with the per-symbol agents (gbm, news_alpha).

## Caveats
- The paper is on US equity sectors. Crypto "sectors" are less well-defined (BTC, ETH, altcoins, DeFi, stablecoins). The macro-conditioned rotation may need adaptation.
- Macro variables lag the regime. A 1-month rebalance is appropriate; daily rebalance adds noise.
- The regime classification is itself a model and has its own look-ahead risk.

## Related entries
- `research/papers/2025/regime-agent-fred.md` (related: the in-tree regime agent)
- `research/papers/2026/deep-momentum-lim.md` (LSTM cross-section is the agent's downstream consumer)
- EDGE_ROADMAP §2 X+ Task 087

## References
- Bhojraj, Cremers, Driessen, *Sector Rotation and Stock Returns* (2007, JFE)
- Kritzman, Page, Turkington, *Regime Shifts: Implications for Dynamic Strategies* (2012, FAJ)
- Ang, Bekaert, *Regime Switching in Asset Allocations* (2002, AFA proceedings)
