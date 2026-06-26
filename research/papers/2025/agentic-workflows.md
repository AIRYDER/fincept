---
title: "Agentic Workflows for Financial Research: An Architecture Pattern"
authors: ["Weng, Lilian"]
affiliation: "Independent / ex-OpenAI"
source: "https://lilianweng.github.io/posts/2023/06/23/agent/"
date: "2023-06-23"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q3.1", "none"]
tags: ["agent", "agentic-workflow", "llm", "orchestration", "pattern"]
license: "Unknown"
effort_to_apply: "M"
adoption_risk: "medium"
---

## TL;DR
Lilian Weng's canonical blog post on agentic workflows. The taxonomy: (1) ReAct (Reason + Act), an interleaved pattern where the LLM thinks, acts, observes, and repeats; (2) Plan-and-Execute, where the LLM first plans, then executes; (3) Reflexion, where the LLM reflects on its previous actions; (4) BabyAGI, task-driven autonomous agents. The post is the reference for designing agentic LLM applications.

## Why we care
Sisyphus Tier Q3.1 (multi-agent debate) and the in-tree `news_impact_agent` are *agentic* LLM applications. Weng's taxonomy is the design reference: ReAct for simple tool use, Plan-and-Execute for multi-step research, Reflexion for self-improving agents, BabyAGI for autonomous task execution. The Fincept team should pick a pattern and stick with it; the choice has big implications for reliability and cost.

## Key ideas
- ReAct: the LLM interleaves thinking, acting (tool call), and observing (tool result). The pattern is simple but can fail if the LLM loops.
- Plan-and-Execute: the LLM first generates a plan, then executes each step. The plan is human-readable.
- Reflexion: the LLM reflects on its previous actions and uses the reflection to improve. Self-improving.
- BabyAGI: the LLM autonomously generates and executes tasks, creating new tasks as needed. Most ambitious; most failure-prone.

## How to apply to Fincept
1. Pick a single pattern for the Fincept LLM agents. ReAct is the safest starting point.
2. The `news_impact_agent` already uses a ReAct-like loop (LLM + tool use); document the pattern explicitly.
3. For more complex research (e.g., a "research agent" that finds new alpha sources), use Plan-and-Execute.

## Caveats
- All agentic patterns can fail in non-obvious ways. The team should expect ~10-20% of agent runs to fail and design for that.
- LLM cost: Plan-and-Execute uses 1-2 LLM calls per step; BabyAGI can use 10+. Budget accordingly.
- Self-improving agents (Reflexion, BabyAGI) need careful evaluation: the LLM's "improvement" can be illusory.

## Related entries
- `research/repos/langchain.md` (the framework)
- `research/papers/2024/openai-function-calling.md` (function-calling)
- `research/papers/2025/du-multi-agent-debate.md` (multi-agent)

## References
- Weng, *LLM Powered Autonomous Agents* (2023, blog)
- Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models* (2023, ICLR)
- Shinn et al., *Reflexion: Language Agents with Verbal Reinforcement Learning* (2023, NeurIPS)
