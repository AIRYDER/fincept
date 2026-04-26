# Risk Register

Likelihood (L) and Impact (I) on a 1–5 scale. Score = L × I. Review monthly.

## Top risks

| ID | Risk | L | I | Score | Owner | Mitigation |
|---|---|---|---|---|---|---|
| R-01 | Scope creep back toward full blueprint (FPGA, Qt6, multi-agent hierarchy) before MVP ships | 5 | 5 | 25 | Tech lead | Written scope gate; ADR-0001..0005 locked; monthly steering review flags any diversion |
| R-02 | Hiring — can't fill quant + ML + platform roles in 8 weeks | 4 | 4 | 16 | CEO | Start sourcing before Phase 0; budget for contractors; partner with specialized recruiters |
| R-03 | Market data licensing cost blindside when adding equities | 5 | 3 | 15 | CFO | Stay on crypto + free EOD until a clear alpha justifies paid feeds; get quotes from Polygon/Databento in Phase 0 |
| R-04 | No alpha — strategies don't beat baseline after costs | 3 | 5 | 15 | Head of quant | Accept early; pivot to research platform (Bet C) or execution-for-others if strategies fail |
| R-05 | Timescale hits scale wall earlier than expected | 2 | 4 | 8 | Platform lead | Benchmark at 1B rows before committing; fallback plan to ClickHouse documented |
| R-06 | Exchange API change breaks ingestor in production | 4 | 3 | 12 | Data lead | Version-pinned adapter tests; synthetic-data replay harness; feed-down alerts |
| R-07 | Regulatory complexity when going live on equities | 3 | 5 | 15 | Compliance (TBH) | Stay crypto-only until compliance role filled; engage external counsel before FIX work starts |
| R-08 | Security breach of exchange API keys | 2 | 5 | 10 | Security lead (TBH) | HSM-backed keys from Phase 5; withdrawal permissions disabled on all keys; IP allowlist |
| R-09 | Model overfitting passes internal review, loses money live | 4 | 4 | 16 | Head of quant | Shadow-deploy ≥4 weeks; walk-forward mandatory; circuit breakers on live P&L |
| R-10 | Python/GIL becomes real bottleneck for strategy runner | 3 | 3 | 9 | Backend lead | One-strategy-per-process design avoids GIL at the runner level; Rust rewrite path documented |
| R-11 | Single-engineer knowledge silos (ingestor, OMS) | 4 | 3 | 12 | Tech lead | Mandatory pair reviews on critical paths; rotation in Phase 3 onward |
| R-12 | Founder attention splits across Fincept + other projects | 3 | 4 | 12 | CEO | Explicit time commitment in charter; delegate tech-lead role formally |

## Retired / not-a-risk

- "Sub-100μs latency unachievable" — not a risk, we explicitly rejected this target in the MVP (see ROADMAP §3).
- "FPGA development timeline" — removed from scope; no risk.
