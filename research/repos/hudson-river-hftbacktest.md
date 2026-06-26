---
title: "Hudson River Trading — hftbacktest: High-Frequency Backtesting in Rust"
authors: ["Hudson River Trading"]
affiliation: "Hudson River Trading"
source: "https://github.com/hudson-and-thames/hftbacktest"
date: "2022-09-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["none"]
tags: ["hudson-river", "hft", "backtester", "rust", "high-frequency"]
license: "MIT"
effort_to_apply: "XL"
adoption_risk: "high"
---

## TL;DR
Hudson River Trading (HRT) open-sourced `hftbacktest`, a high-frequency backtesting framework in Rust. The library implements a queue-position model (each order has a position in the queue) and supports tick-by-tick reconstruction of the limit order book. The reference implementation is for HFT strategy research, but the queue-position model and the L2/L3 replay engine are valuable for any system that ingests and backtests on L2/L3 data. The library is ~10,000 lines of Rust; it is not a Python replacement for Fincept but is a reference for the queue-position model.

## Why we care
EDGE_ROADMAP §3 explicitly says "Sub-millisecond latency / colocation. Citadel, Jane Street, Jump have already won this. Latency target stays <100ms signal / <500ms decision." So Fincept is not building HFT. But the queue-position model from `hftbacktest` is the *correct* way to model partial fills at the limit order book level. The in-tree `services/backtester/src/backtester/broker.py` is simpler (no queue model). The HRT reference is the gold standard for a queue-aware backtester.

## Key ideas
- Queue-position model: when an order is placed at price p, it joins the back of the queue. As new orders arrive at p, the order's queue position decreases. A fill happens when the queue position reaches zero.
- L2/L3 replay: reconstruct the order book at any historical time from the L2/L3 tape.
- Fill model: when an aggressive order arrives, it consumes the queue from the front; the order at the back fills if it's still in the queue when the aggressive order finishes.
- The implementation in Rust: ~10× faster than Python. Not directly applicable to Fincept.

## How to apply to Fincept
1. NOT recommended for direct adoption. Use as a reference.
2. The pattern: a queue-position model is the right way to model partial fills in a backtest.
3. If Fincept ever backtests on L2/L3 crypto data, the in-tree `SimBroker` should be upgraded to a queue-position model. Reference: `hftbacktest`.

## Caveats
- Rust is not Python. Adopting `hftbacktest` would require a Python-Rust bridge.
- The library is HFT-focused. Most of its features are overkill for Fincept's mid-frequency paper trading.
- The queue-position model requires L2/L3 data, which Fincept does not currently ingest (only L1 best bid/ask).

## Related entries
- `research/papers/2024/zhang-deeplob.md` (L2 microstructure)
- `research/repos/jane-street-fsharp.md` (another HFT open-source reference)

## References
- https://github.com/hudson-and-thames/hftbacktest
- https://hudsonthames.org/ (Hudson River's research blog)
