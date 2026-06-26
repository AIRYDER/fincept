---
title: "Function Calling and Tool Use in LLMs: Application to Market Data Pipelines"
authors: ["OpenAI"]
affiliation: "OpenAI"
source: "https://platform.openai.com/docs/guides/function-calling"
date: "2023-06-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q3.1", "Q3.2"]
tags: ["function-calling", "tool-use", "agentic", "openai", "langchain"]
license: "Proprietary"
effort_to_apply: "M"
adoption_risk: "medium"
---

## TL;DR
OpenAI's function-calling API (and equivalent features in Anthropic Claude, Google Gemini, and open-source LLMs) lets the LLM decide which tool to call from a registry, with structured JSON output. The pattern: (1) declare a list of tools (name, description, JSON schema), (2) call the LLM with the prompt + tools, (3) LLM returns a structured tool call, (4) execute the tool, (5) call the LLM with the tool result. The pattern is the foundation of "agentic" LLM applications.

## Why we care
Sisyphus Tier Q3.1 (multi-agent debate) and the in-tree `news_impact_agent` both use LLM tool use. Function-calling is the standardized way to do this. Adopting it (and using a library like LangChain to manage the agent loop) would substantially simplify the LLM integration.

## Key ideas
- Tool registry: a list of (name, description, JSON schema) for each tool.
- The LLM returns a structured tool call (tool name + arguments).
- The agent loop: call LLM with prompt + tools, parse the tool call, execute the tool, return the result to the LLM, repeat.
- The pattern is LLM-agnostic: OpenAI, Anthropic, Google, and most open-source LLMs support it.
- Reliability: the LLM may return malformed tool calls; the agent must validate and retry.

## How to apply to Fincept
1. Refactor `news_impact_agent` and `sentiment_agent/llm.py` to use the function-calling API.
2. The tool registry is the `libs/fincept-tools/` library; each tool is exposed as a function with a JSON schema.
3. The agent loop is a thin wrapper around the LLM SDK.

## Caveats
- Vendor lock-in: function-calling APIs differ across LLM providers. Use LangChain or a similar abstraction for portability.
- LLM cost: the agent loop can make multiple LLM calls per query; budget accordingly.
- Reliability: the LLM may return malformed tool calls; the agent must validate.

## Related entries
- `research/repos/langchain.md` (the framework for function-calling agents)
- `research/papers/2025/du-multi-agent-debate.md` (multi-agent upgrade)
- `research/papers/2025/li-multimodal-llm-trading.md` (multi-modal upgrade)

## References
- https://platform.openai.com/docs/guides/function-calling
- Anthropic Claude tool use: https://docs.anthropic.com/en/docs/tool-use
- Schick et al., *Toolformer: Language Models Can Teach Themselves to Use Tools* (2023, NeurIPS)
