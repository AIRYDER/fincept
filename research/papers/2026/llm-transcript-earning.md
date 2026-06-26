---
title: "Earnings Call Transcripts as a Source of Alpha: LLMs and the Loughran-McDonald Dictionary"
authors: ["Frankel, Robert", "Jennings, Jared", "Lee, Joshua", "Levine, Casey"]
affiliation: "MIT Sloan / University of Washington / Cornerstone Research"
source: "https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2007.01236.x"
date: "2007-12-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3.2"]
tags: ["earnings-calls", "llm", "nlp", "sentiment", "loughran-mcdonald"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The authors show that the tone and content of earnings call transcripts — particularly the "soft" qualitative language around forward guidance — predicts subsequent stock returns. The effect is robust using a simple Loughran-McDonald (LM) financial dictionary; recent extensions (2023-2024) using LLMs (FinBERT, GPT-4) substantially improve the effect. The signal: a transcript with high "soft" sentiment (forward guidance, hedges) and low "hard" sentiment (specific numbers) predicts positive returns.

## Why we care
Sisyphus Tier Q3.2 calls for an "Earnings-call transcript LLM agent." Fincept is crypto-only, but earnings call transcripts for crypto-adjacent equities (Coinbase, MicroStrategy, the major mining companies) are a high-value signal source. The agent would be the second non-crypto alpha source (after options flow). The Frankel-Jennings-Lee-Levine paper is the academic foundation; the LLM extensions are the modern implementation.

## Key ideas
- Loughran-McDonald dictionary: a financial-specific sentiment dictionary that distinguishes "soft" (forward-looking, hedged) and "hard" (specific, factual) language.
- The signal: high soft / low hard ratio predicts positive returns.
- LLM-based extensions: use FinBERT (finance-specific BERT) or GPT-4 with a structured prompt to extract the same signal.
- Horizon: 3-60 days. The effect is strongest for forward guidance in the prepared remarks and the Q&A.
- Capacity: the effect is robust for liquid, large-cap stocks; less robust for small-caps.

## How to apply to Fincept
1. (Future) Add `services/agents/earnings_call/` package per spec Task 081.
2. The agent subscribes to a daily earnings-call transcript feed (Seeking Alpha, Refinitiv, or a vendor), runs FinBERT or GPT-4, computes the soft/hard ratio, and emits a `Prediction` with horizon 1 month.
3. The orchestrator consensus weights this agent's predictions the same as any other.

## Caveats
- LLM cost: GPT-4 over thousands of transcripts per quarter is expensive. Use FinBERT for the bulk; GPT-4 only for ambiguous cases.
- The paper is on US equity. Crypto-related equities (Coinbase, MicroStrategy) are a smaller universe; the effect may be less robust.
- The "soft" language is correlated with the "hard" language (companies with bad news avoid both). The signal is the *ratio*, not the absolute counts.

## Related entries
- `research/models/fingpt.md` (the LLM backbone)
- `research/papers/2025/qlib-architecture.md` (Qlib has earnings-call-based features)
- EDGE_ROADMAP §2 X+ Task 081

## References
- Frankel, Jennings, Lee, Levine, *Earnings Calls as a Source of Forward-Looking Information* (2007, JFE)
- Loughran & McDonald, *When is a Liability not a Liability? Textual Analysis, Dictionaries, and 10-Ks* (2011, JFE)
- Yang, Liu, Zhong, *FinGPT: Open-Source Financial Large Language Models* (2023, arXiv)
