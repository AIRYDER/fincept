---
title: "FinGPT — Open-Source Financial LLM"
authors: ["Yang, Hongyang (Cindy)", "Liu, Xiao-Yang, et al."]
affiliation: "Columbia University / AI4Finance Foundation"
source: "https://github.com/AI4Finance-Foundation/FinGPT"
date: "2023-09-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q3"]
tags: ["llm", "fintech", "sentiment", "rag", "fine-tuning"]
license: "MIT"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
FinGPT is a family of open-source financial LLMs, built on Llama 2/3 and other base models, fine-tuned for financial sentiment, news classification, and RAG over financial documents. The repo provides training scripts, multiple model checkpoints (small and large), and a set of notebooks demonstrating common use cases. The companion project FinGPT-Forecaster is a LangChain-style "LLM agent" that can call tools and produce structured predictions.

## Why we care
EDGE_ROADMAP §2 Tier X+ Task 086 calls for a multi-agent LLM debate (bull/bear/judge). FinGPT-Forecaster is the closest open-source reference for what an LLM-based trading agent looks like, and the repo's sentiment-classification checkpoints are drop-in alternatives to the hand-rolled `services/agents/sentiment_agent/llm.py`. The right move is to evaluate FinGPT against the in-tree sentiment agent before adopting.

## Key ideas
- The repo's `fingpt/FinGPT_Forecaster` directory shows a LangChain-style agent that takes (news, market state) → (price direction, confidence) using an LLM with tool calls.
- The `fingpt/FinGPT_Sentiment_Analysis_v3` checkpoint is a Llama-2-7B fine-tuned for financial sentiment; competitive with closed-source APIs on the standard benchmarks.
- RAG over financial documents: 10-Ks, earnings calls, FOMC minutes. The pipeline is well-documented.
- Cost: even the small checkpoints are 7B parameters; inference is non-trivial. The 13B checkpoint is recommended for serious work; the 7B is for prototyping.

## How to apply to Fincept
1. NOT a wholesale replacement for the in-tree `sentiment_agent`. The in-tree agent is simpler and more auditable; FinGPT is more capable but heavier.
2. A potential replacement for the LLM call inside the `sentiment_agent` if the existing implementation underperforms. The eval is F1 on a held-out news corpus.
3. The FinGPT-Forecaster agent design is informative for the Tier Q3 multi-agent debate task. Borrow the LangChain-style tool-calling pattern.

## Caveats
- LLM cost vs marginal alpha (EDGE_ROADMAP principle 3) is the central concern. A 7B model call per news article is expensive; measure carefully.
- The FinGPT checkpoints are not production-grade. They are research baselines.
- The repo has not been updated to Llama 3 as of 2024. Some of the dependencies are stale.
- The sentiment-classification checkpoints assume English news. Multi-language support is incomplete.

## Related entries
- `research/papers/2026/deep-momentum-lim.md` (the LSTM alternative for cross-section)
- EDGE_ROADMAP Tier X+ Task 086 (multi-agent debate)

## References
- https://github.com/AI4Finance-Foundation/FinGPT
- Yang et al., *FinGPT: Open-Source Financial Large Language Models* (2023, arXiv:2306.06031)
- Yang et al., *FinGPT-Forecaster* (2024, AI4Finance blog series)
