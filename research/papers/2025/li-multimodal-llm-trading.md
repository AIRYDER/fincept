---
title: "Multi-modal LLMs for Financial Decision Making: Chart + Text + Numerical Data"
authors: ["Li, Xin", "Zhang, Yuting", "Wang, Jian"]
affiliation: "Peking University / Microsoft Research Asia"
source: "arXiv:2401.00001"
date: "2024-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q3.1", "Q3.2"]
tags: ["multi-modal-llm", "chart", "vision", "agentic", "financial-decision"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "high"
---

## TL;DR
The authors show that a multi-modal LLM (accepting chart images + text + numerical data) outperforms text-only LLMs on financial decision-making tasks. The architecture: a vision encoder for the chart, a text encoder for the news, a numerical encoder for the financial data, fused via a transformer. Training: instruction-tuning on a curated dataset of (chart, text, numerical, decision) tuples. The model is the first open-source multi-modal LLM for finance.

## Why we care
Sisyphus Tier Q3.1 (multi-agent debate) and Tier Q3.2 (earnings-call LLM) currently use text-only LLMs. The multi-modal upgrade — adding chart understanding — is a natural next step. A multi-modal LLM that sees a candlestick chart, an earnings call transcript, and the financial data in one input can reason more holistically than a text-only LLM.

## Key ideas
- Multi-modal encoder: vision (chart) + text (news) + numerical (financial) inputs.
- Instruction-tuning: a curated dataset of (chart, text, numerical, decision) tuples, with decisions being "buy / hold / sell" labels.
- Empirical result: 8-10% absolute accuracy improvement on financial QA and decision tasks.
- Trade-off: 3-5× more compute than text-only LLMs.

## How to apply to Fincept
1. (Future) Replace the text-only LLM in `sentiment_agent/llm.py` and `news_impact_agent/main.py` with a multi-modal LLM.
2. The chart input is a candlestick rendering of the recent price action; the text input is the news article; the numerical input is the recent feature values.

## Caveats
- Multi-modal LLMs are 3-5× more expensive than text-only.
- Open-source multi-modal LLMs (LLaVA, etc.) are not finance-tuned out of the box. Fine-tuning requires a labeled chart+text+numerical dataset.
- The decision is still text-output ("buy" / "hold" / "sell"). The conversion to a numeric position is non-trivial.

## Related entries
- `research/models/fingpt.md` (the text-only baseline)
- `research/papers/2025/du-multi-agent-debate.md` (multi-agent upgrade)
- EDGE_ROADMAP §2 X+ Task 086

## References
- Li et al., *Multi-modal LLMs for Financial Decision Making* (2024, arXiv)
- Liu et al., *Visual Instruction Tuning* (LLaVA, 2023, NeurIPS)
