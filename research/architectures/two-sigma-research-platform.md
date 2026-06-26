---
title: "Two Sigma Research Platform — Public Architecture Notes"
authors: ["Two Sigma Investments (public talks)"]
affiliation: "Two Sigma"
source: "https://www.twosigma.com/talks/"
date: "2024-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["none"]
tags: ["architecture", "industry-reference", "research-platform", "factor-research"]
license: "Unknown"
effort_to_apply: "XL"
adoption_risk: "low"
---

## TL;DR
Two Sigma is a quantitative hedge fund with a research platform widely regarded as a reference design. The public talks and job descriptions describe the architecture: a central feature store, a factor-research workbench (notebook-driven), an alpha evaluation framework with strict statistical-significance testing, and a portfolio construction layer with risk budgeting. The platform emphasizes reproducibility and auditability; every alpha has a research note, a backtest, a deflated Sharpe p-value, and a deployment record.

## Why we care
Two Sigma is the closest analog to what Fincept is building at production scale. The architectural patterns (centralized feature store, notebook-driven research, statistical-significance gating, risk budgeting) are worth studying even if the implementations are proprietary. The right move is to read the public talks, identify which patterns transfer, and write down the ones we want to adopt.

## Key ideas
- Centralized feature store: every research project draws from the same canonical feature set. No "alpha-specific features."
- Notebook-driven research: every alpha is born in a notebook, with a research note describing the hypothesis, the eval, and the limitations.
- Deflated Sharpe gating: no alpha ships without a deflated Sharpe p-value below 0.05 and an explicit operator review.
- Risk budgeting: every alpha is allocated a risk budget (vol target, max drawdown, concentration). The budget is enforced by the OMS, not the alpha.
- Reproducibility: every backtest is reproducible from a versioned code commit and a pinned data snapshot. The audit trail is the system.

## How to apply to Fincept
1. Adopt the "deflated Sharpe gating" pattern: the candidate-gate policy should require a DSR p-value below 0.05. (See `research/papers/2026/lopez-de-prado-deflated-sharpe.md`.)
2. Adopt the "centralized feature store" pattern: the in-tree `services/features/` already implements this. Verify that every agent reads from the same store.
3. Adopt the "research note" pattern: every model dossier should have a `research_note` field with the hypothesis, eval, and limitations. The Sisyphus Tier Q1.1 calibration dossier is the start.
4. Adopt the "risk budgeting" pattern: Tier Q2.2 vol targeting is the foundation; the per-strategy risk budget is the next step.

## Caveats
- Two Sigma's scale is two orders of magnitude larger than Fincept's. Some patterns (e.g., multi-tenant feature stores, petabyte-scale data) do not transfer.
- The public talks are marketing as much as architecture. Read them critically.
- Two Sigma is in the EDGE_ROADMAP §3 "what NOT to build" implicit list — they have won the game Fincept is playing. The relevant lesson is "this is what good looks like at the top of the field"; the relevant *adoption* is to study the architecture, not to copy it.

## Related entries
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (the gate policy)
- `research/papers/2026/moreira-muir-volatility-managed.md` (the vol target)
- `research/architectures/qlib-design.md` (the closest open-source analog)

## References
- https://www.twosigma.com/talks/ (public lectures and conference talks)
- The "Advances in Financial Machine Learning" book references Two Sigma's research style
- Multiple industry job descriptions (linkedin, glassdoor) describe the platform's shape
