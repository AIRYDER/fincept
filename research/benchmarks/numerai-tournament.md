---
title: "Numerai Tournament — Live ML Competition with Real Capital"
authors: ["Numerai"]
affiliation: "Numerai"
source: "https://numer.ai/tournament"
date: "2015-01-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "medium"
tier_mapping: ["Q1.1", "Q3"]
tags: ["benchmark", "live-competition", "stake-weighted", "feature-erasures", "meta-model"]
license: "Proprietary"
effort_to_apply: "M"
adoption_risk: "low"
---

## TL;DR
Numerai is a weekly ML competition where participants predict stock returns on a fully obfuscated, regularized feature set. Submissions are scored against a meta-model that the company maintains; the top performers stake NMR (Numeraire) cryptocurrency on their predictions and are paid out proportionally. The competition has been running since 2015 and is the longest-running live ML finance competition.

## Why we care
The competition is a free, public benchmark for "can I make money with a model on real-world financial data?" The data is heavily obfuscated (you cannot engineer features on the original semantics), but the *eval* is honest: stake-weighted return. Tier Q1.1 of the Sisyphus Quant/ML Deep Dive calls for an honest eval; the Numerai leaderboard is one of the few public, live, stake-weighted evals.

## Key ideas
- The data: 119 features per row, fully obfuscated, on a weekly cadence. Targets are 4-class (most-up, up, down, most-down) or 2-class (up/down).
- The meta-model: Numerai combines submissions from all participants into a meta-model that is itself the prediction they stake on. This is "the wisdom of crowds meets quantitative finance."
- The payout: stake-weighted, with correlation to the meta-model as the metric. Participants are paid in NMR.
- The interesting design choice: feature obfuscation prevents overfitting to specific stocks or sectors. The features are *regularized*: the company adds noise and erasures to prevent the model from latching onto spurious signals.
- The "consensus" lesson: the meta-model is more accurate than any individual submission. This is the strongest public evidence for *consensus* in quant ML — exactly the pattern Fincept's orchestrator is designed to exploit.

## How to apply to Fincept
1. NOT a wholesale adoption. The data is obfuscated; you cannot port the model.
2. Borrow the *eval discipline*: a stake-weighted, meta-model-aggregated metric is the right way to measure "is this alpha real?" The DSR-p-value gate in `Sisyphus_Quant_ML_Deep_Dive.md` Tier Q0.3 is the Fincept equivalent.
3. The obfuscation lesson: features that latched onto spurious signals in 2015 are still producing false-positive AUCs in 2024. PIT-correct features and the proposed feature-reconciliation work (Sisyphus Tier Q0.1) are the right answer.
4. Top solutions: a public leaderboard with disclosed stake. Useful for "what does the state of the art look like?"

## Caveats
- The data is proprietary; you can only use it through the tournament. No porting.
- The target is obfuscated; the eval is the only honest signal. Don't read too much into the specific numbers.
- The competition is "live" in the sense that it runs every week; the data is 20+ years of obfuscated history. The signal decays over time.
- The meta-model is proprietary. We don't know exactly how it's constructed.

## Related entries
- `research/papers/2026/lopez-de-prado-deflated-sharpe.md` (the gate policy)
- `research/papers/2025/qlib-architecture.md` (the Qlib alpha evaluator is similar in spirit)

## References
- https://numer.ai/tournament
- Numerai whitepaper: https://docs.numer.ai/tournament/learn
- Top participant writeups: see the leaderboard "Profile" pages
