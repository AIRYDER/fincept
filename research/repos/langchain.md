---
title: "LangChain — Framework for LLM-Powered Applications"
authors: ["LangChain contributors"]
affiliation: "LangChain Inc."
source: "https://github.com/langchain-ai/langchain"
date: "2022-10-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q3.1", "none"]
tags: ["llm-orchestration", "agents", "rag", "tool-use", "prompt-management"]
license: "MIT"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
LangChain is the de facto framework for LLM-powered applications. It provides abstractions for: prompt templates, LLM chains (sequential LLM calls), agents (LLM decides which tools to call), RAG (retrieval-augmented generation), and memory. The framework is opinionated about *how* to compose LLM applications but is LLM-agnostic (works with OpenAI, Anthropic, local models). The `news_impact_agent` work in Fincept is exactly the kind of "LLM agent with tool use" that LangChain was designed for.

## Why we care
Sisyphus Tier Q3.1 (multi-agent debate) and the broader "LLM agent" work in Fincept (`services/agents/news_impact_agent/`, `sentiment_agent/llm.py`) could be substantially simplified by adopting LangChain. The `news_impact_agent` calls LLM with tool use to reason about news impact; LangChain's "agent" abstraction is exactly this pattern.

## Key ideas
- Prompt templates: declarative prompt construction.
- LLM chains: sequential LLM calls with state passing.
- Agents: LLM decides which tools to call from a registry. The agent loop is: (1) call LLM with the prompt and tool list, (2) LLM returns a tool call, (3) execute the tool, (4) call LLM with the result, (5) repeat until the LLM returns a "final answer."
- RAG: combine LLM with a vector database for grounded reasoning.
- Memory: track conversation history.

## How to apply to Fincept
1. (Future) Refactor `news_impact_agent` and `sentiment_agent/llm.py` to use LangChain's agent abstraction.
2. The "tool registry" in LangChain maps to the `fincept-tools` library (`libs/fincept-tools/`).
3. The bull/bear/judge debate pattern (Sisyphus Tier Q3.1) is a multi-agent setup that LangChain supports natively.

## Caveats
- LangChain's API is opinionated; refactoring an existing implementation is a 1-2 week effort.
- The Fincept in-tree tool registry (`fincept-tools`) may need to be wrapped in a LangChain-compatible interface.
- LangChain is moving fast; the API has changed significantly between versions. Pin to a specific version.

## Related entries
- `research/models/fingpt.md` (the LLM backbone)
- `research/papers/2025/du-multi-agent-debate.md` (multi-agent debate)
- EDGE_ROADMAP §2 X+ Task 086 (multi-agent debate)

## References
- https://github.com/langchain-ai/langchain
- https://python.langchain.com/docs/get_started/introduction
- Chase, *LangChain: The Missing Manual* (2023, online)
```

---

## 7. Phase 5 of the expansion — Industry + specialized subfields (6 entries)
