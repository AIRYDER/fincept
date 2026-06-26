---
title: "ACM International Conference on AI in Finance (ICAIF)"
authors: ["ACM (organizer)"]
affiliation: "ACM"
source: "https://ai-finance.org/conference"
date: "2020-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["none"]
tags: ["conference", "venue", "ai-finance", "annual"]
license: "Unknown"
effort_to_apply: "L"
adoption_risk: "low"
---

## TL;DR
ICAIF is the top venue for academic and applied research at the intersection of AI and finance. Founded in 2020, it runs annually (usually November in New York). Papers cover trading, portfolio management, risk, fraud, compliance, and the methodological foundations of ML in finance. The proceedings are published in the ACM Digital Library.

## Why we care
ICAIF is the canonical venue for the kind of research Fincept cares about. Papers presented here are typically well-evaluated, often reproducible, and often directly applicable to a system like Fincept. A quarterly skim of new ICAIF preprints is the highest-leverage activity the research lead can do.

## Key ideas
- Annual conference, single track, peer-reviewed.
- Topics: time-series forecasting, portfolio optimization, risk, fraud, compliance, LLM-for-finance, RL-for-finance, market microstructure, explainability, robustness.
- The proceedings are an excellent "what is the state of the art in [year+1]?" reference.
- The associated workshop series (NeurIPS workshops on AI in finance, KDD workshops) is also worth watching.
- Some ICAIF papers are immediately applicable; some are pure methodology. The papers that transfer to Fincept's roadmap are the trading, portfolio, and risk papers.

## How to apply to Fincept
1. The research lead subscribes to the ICAIF mailing list and skims new preprints quarterly.
2. The Tier X+ roadmap items (multi-agent debate, options flow, sector rotation) all have ICAIF papers. When implementing, search the proceedings.
3. The methodological papers (calibration, robustness, drift detection) are directly applicable to Tier Q1.

## Caveats
- Conference papers are peer-reviewed but not always reproducible. Read the methods section critically.
- Some papers are highly academic with no practical application. Skip these.
- The conference is North-America-centric. European and Asian quant conferences (London Quants, Battle of the Quants) have different flavors.

## Related entries
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (a JPM paper, not ICAIF, but the same community)
- `research/papers/2025/qlib-architecture.md` (Qlib has presented at ICAIF workshops)
- EDGE_ROADMAP §1 (the brutal truth is largely from ICAIF-adjacent research)

## References
- https://ai-finance.org/conference
- ACM Digital Library: https://dl.acm.org/conference/icaif
