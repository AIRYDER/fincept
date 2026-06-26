---
title: "Improving Factuality and Reasoning in Language Models through Multiagent Debate"
authors: ["Du, Yilun", "Li, Shuang", "Torralba, Antonio", "Tenenbaum, Joshua B.", "Mordatch, Igor"]
affiliation: "MIT / IBM Research"
source: "arXiv:2305.14325"
date: "2023-05-23"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q3.1"]
tags: ["multi-agent-debate", "llm-reasoning", "factuality", "bull-bear-judge"]
license: "CC-BY"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The authors show that a multi-agent debate (MAD) framework — multiple LLM agents with different prompts debating a position, then a judge LLM aggregating the debate — produces more factual and more rigorous outputs than a single LLM. The debate forces each agent to consider alternative positions; the judge produces the final answer. The paper demonstrates improvements on math (GSM8K), factual reasoning (MATH), and strategic reasoning (Diplomacy) tasks. The 3× token cost is the main downside.

## Why we care
Sisyphus Tier Q3.1 calls for "Multi-agent LLM debate (bull / bear / judge) replacing single-shot in llm_loop.py." Fincept's `services/agents/sentiment_agent/llm.py` and `news_impact_agent` use single-shot LLM calls. The debate framework would improve factuality and reduce hallucination on the news-impact reasoning task. The Du et al. paper is the canonical reference for the framework.

## Key ideas
- Three-agent debate: bull (positive thesis), bear (negative thesis), judge (aggregates).
- Each agent sees the other agents' previous positions; the debate unfolds over multiple rounds.
- The judge LLM is given the full debate transcript and produces the final answer.
- The improvement: ~10% absolute accuracy on math and factual reasoning vs single-shot.
- Cost: 3× tokens (three LLM calls per question). Use sparingly, for the highest-value decisions.

## How to apply to Fincept
1. Add `services/agents/news_impact_agent/debate.py::DebateFramework(bull_prompt, bear_prompt, judge_prompt, n_rounds=2)`.
2. The news_impact_agent uses debate for high-confidence events (e.g., regulatory action, large exchange listing) but single-shot for low-confidence events.
3. The judge output becomes the `Prediction.direction` and `Prediction.confidence`.

## Caveats
- 3× token cost. Estimate: a debate per news event is 3× the cost of a single-shot. For 100 events/day, that's 300 LLM calls vs 100.
- The "judge" can still be wrong; the debate doesn't add *truth*, it adds *red-teaming*.
- The framework assumes a binary bull/bear decomposition. Some news events are nuanced (neither positive nor negative); a 3-agent framework (bull/bear/neutral) may be better.

## Related entries
- `research/models/fingpt.md` (the LLM backbone)
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (the gate policy requires the debate output to be statistically significant)
- EDGE_ROADMAP §2 X+ Task 086

## References
- Du et al., *Improving Factuality and Reasoning in Language Models through Multiagent Debate* (2023, arXiv)
- Liang et al., *Encouraging Divergent Thinking in Large Language Models through Multi-Agent Debate* (2023, arXiv)
- Chan et al., *Chateval: Towards Better LLM-based Evaluators through Adversarial Debate* (2023, arXiv)
