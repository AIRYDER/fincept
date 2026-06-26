# Entry Template

Every entry is a single Markdown file with YAML frontmatter. The canonical template lives at `research/_meta/ENTRY_TEMPLATE.md` (the worker copies the version in §6.1 of this plan into that file) and is reproduced inline below.

**Schema rules (non-negotiable, enforce in every entry the worker writes):**
- Every entry has all frontmatter fields. Use `Unknown` (not blank) when a field genuinely doesn't apply.
- `status: "verified"` means a human has read it and confirmed the claims. All seed entries the worker writes are `verified` (the planner has done the verification at plan time).
- `relevance` uses the scale in `research/_meta/RELEVANCE_SCORING.md` (see §6.3).
- `tier_mapping` always references a Sisyphus tier ID (Q0.x, Q1.x, Q2.x, Q3.x, Q4.x) or `none` if the entry is foundational/general.
- `last_reviewed` is set to the date the worker writes the entry.

```yaml
---
title: "Paper / Repo / Model Title"
authors: ["Last, First", "Last, First"]
affiliation: "Stanford / Jane Street"
source: "arXiv:2401.12345"
date: "2024-12-15"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q0.1", "Q1.1"]
tags: ["online-learning", "regime-detection", "calibration"]
license: "MIT"
effort_to_apply: "S"
adoption_risk: "low"
---

## TL;DR
2-3 sentences. No jargon. What is this, who wrote it, what does it claim.

## Why we care
- Specific connection to a Sisyphus tier gap.
- What would change in Fincept if we adopted it.

## Key ideas
Main contributions, methods, results. Cite specific numbers when possible.

## How to apply to Fincept
Concrete suggestion: which file, which module, what to change. If the change is non-trivial, link to a draft spec task.

## Caveats
- License, maturity, data requirements, compute.
- What the paper does not prove.

## Related entries
Links to other research/ entries that should be read alongside.

## References
- Original source URL
- Citation
```

**Frontmatter field definitions (worker uses these):**

| Field | Allowed values | Notes |
|---|---|---|
| `title` | string | Exact title of paper/repo/model |
| `authors` | list of strings | `"Last, First"` for papers; `["Project Maintainers"]` for repos/models |
| `affiliation` | string | First author's institution, or repo's primary backer |
| `source` | string | arXiv ID, GitHub URL, HuggingFace URL, or DOI |
| `date` | ISO date | Original publication/release date |
| `added` | ISO date | When added to this DB |
| `last_reviewed` | ISO date | When last verified |
| `status` | `verified` / `needs-review` / `archived` / `stale-link` | Seed entries are `verified` |
| `relevance` | `high` / `medium` / `low` | See §6.3 |
| `tier_mapping` | list of strings | Sisyphus tier IDs, e.g., `["Q1.1", "Q2.1"]` or `["none"]` |
| `tags` | list of strings | Free-form, lowercase, hyphenated |
| `license` | string | `MIT` / `Apache-2.0` / `CC-BY` / `Proprietary` / `Mixed` / `Unknown` |
| `effort_to_apply` | `S` / `M` / `L` / `XL` | Rough size of Fincept-side change. S=≤1 day, M=≤1 week, L=≤1 month, XL=>1 month |
| `adoption_risk` | `low` / `medium` / `high` | What we lose if it doesn't work. High risk = core trading path |

## Full example

```yaml
---
title: "The Deflated Sharpe Ratio: Adjusting for Non-Normal Returns, Selection Bias, and Multiple Testing"
authors: ["Bailey, David H.", "Borwein, Jonathan", "López de Prado, Marcos"]
affiliation: "Lawrence Berkeley National Lab / Dalhousie Univ. / ADIA Lab"
source: "https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf"
date: "2014-12-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "high"
tier_mapping: ["Q0.3", "Q1.1", "Q2.4"]
tags: ["statistical-significance", "backtest", "sharpe-ratio", "multiple-testing", "calibration"]
license: "CC-BY"
effort_to_apply: "S"
adoption_risk: "low"
---

## TL;DR
The Deflated Sharpe Ratio (DSR) extends the standard Sharpe ratio by correcting for three biases common in quantitative finance: non-normal returns, the number of trials (strategy variations) tried, and the length of the backtest. It produces a p-value for whether the observed Sharpe exceeds what would be expected by chance under the null hypothesis of random selection. It is a foundational reference for any system that backtests many strategies and promotes one based on Sharpe.

## Why we care
The current `gbm_predictor/train.py` has no statistical-significance test for a candidate's `best_auc`. `news_alpha_predictor/evaluate.py::CandidateGatePolicy` checks `best_auc >= 0.52` and `best_auc_delta >= 0.0` — both are arbitrary thresholds. Replacing these with a DSR-style test gives the operator a defensible p-value and a more honest "is this model actually better than chance?" answer. The Sisyphus Quant/ML Deep Dive flagged the asymmetry between news_alpha (gated) and gbm (ungated) as Tier Q0.3.

## Key ideas
- Standard Sharpe overstates true performance when returns are non-normal (heavy tails, skew).
- Multiple-testing bias: if you try 100 strategies, the best one will have a high Sharpe by chance alone. The DSR adjusts by estimating the expected maximum Sharpe under the null and the variance of the maximum.
- The deflated Sharpe gives a p-value for the null hypothesis that the true Sharpe is zero. A p-value < 0.05 means the observed Sharpe is statistically significant given the number of trials and the return distribution.
- Formula: DSR ≈ (observed_SR - E[max_SR_under_null]) / std(max_SR_under_null) × z-factor for non-normality.

## How to apply to Fincept
1. Add a `min_p_value` field to `CandidateGatePolicy` (default 0.05).
2. In `services/agents/gbm_predictor/train.py::main`, after the walk-forward summary, compute DSR using the per-fold returns and the number of trials (variants of the same model, e.g., different feature sets, different horizons). Store in `meta.json` as `dsr_p_value`.
3. In `services/agents/news_alpha_predictor/evaluate.py::evaluate_candidate`, add the same DSR check.
4. The promotion endpoint should require `dsr_p_value < 0.05` *or* an explicit operator override logged in promotion history.

## Caveats
- DSR assumes the trials are independent. Correlated trials (e.g., 10 GBM variants with overlapping features) reduce the effective trial count. We should estimate effective trials, not raw trials.
- DSR requires at least 30 return observations to be stable. For very short backtests, use the haircut factor in Bailey & López de Prado's follow-up papers.
- DSR does not address capacity, decay, or regime change. It is a statistical-significance test, not a model-quality test.

## Related entries
- `research/papers/2024/platt-scaling.md` (calibration for the same gate)
- `research/papers/2026/moreira-muir-volatility-managed.md` (vol-targeting shares the multiple-testing concern)
- `research/architectures/qlib-design.md` (Qlib uses DSR as part of its alpha evaluator)

## References
- https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- Bailey & López de Prado, *The Sharpe Ratio Efficient Frontier*, Journal of Portfolio Management (2014)
- López de Prado, *Advances in Financial Machine Learning* (2018), Ch. 13
```
