---
title: "Deep IV: A Flexible Approach for Counterfactual Prediction"
authors: ["Hartford, Jason", "Lewis, Greg", "Leyton-Brown, Kevin", "Taddy, Matt"]
affiliation: "University of British Columbia / Amazon"
source: "http://proceedings.mlr.press/v70/hartford17a/hartford17a.pdf"
date: "2017-06-01"
added: "2026-06-22"
last_reviewed: "2026-06-22"
status: "verified"
relevance: "low"
tier_mapping: ["Q4"]
tags: ["causal-inference", "instrumental-variables", "counterfactual-prediction", "deep-learning"]
license: "CC-BY"
effort_to_apply: "L"
adoption_risk: "medium"
---

## TL;DR
The authors show that deep neural networks can be used to estimate the conditional expectation in instrumental variables (IV) regression. The two-stage approach: (1) train a neural net to predict the endogenous variable from the instrument; (2) use the predicted values as features in a second neural net that predicts the outcome. The paper is the canonical reference for "deep IV" — applying deep learning to causal inference. Application to finance: estimating the causal effect of a feature (e.g., news sentiment) on a return, controlling for confounding.

## Why we care
Sisyphus Tier Q4.4 calls for "Causal inference layer (DoWhy / EconML)." The current Fincept system estimates *correlations* between features and returns. Causal inference would let the system estimate the *causal* effect of a feature on a return, controlling for confounding variables. This is Tier Q4 — not in the next-build queue — but a scoping entry for the research lead.

## Key ideas
- The IV framework: a variable Z is an "instrument" for X if (a) Z affects Y only through X, and (b) Z is independent of confounders.
- The two-stage neural IV: stage 1 trains a model to predict X from Z; stage 2 uses the predicted X as a feature for predicting Y.
- The application: a news article is an instrument for stock returns if (a) the article affects the stock only through the price-relevant feature (e.g., sentiment), and (b) the article's publication is independent of other return predictors.
- The result: more accurate counterfactual prediction than naive regression, especially when confounders are present.

## How to apply to Fincept
1. NOT recommended for current implementation. This is a Tier Q4 entry.
2. The Tier Q4 plan: add a causal inference layer above the per-symbol agents. For each feature-return pair, the layer estimates the causal effect using an IV (e.g., lagged values of the feature as the instrument).
3. The agent's confidence is modulated by the strength of the causal effect.

## Caveats
- The IV assumption is strong and not testable. In practice, the "instrument" may be only weakly exogenous.
- The two-stage approach is fragile when the instruments are weak.
- Implementation cost: 1-2 weeks for a research-quality prototype; months for production.

## Related entries
- `research/papers/2025/vovk-conformal-trading.md` (conformal is an alternative uncertainty-quantification framework)
- EDGE_ROADMAP §3 Z Task 103

## References
- Hartford, Lewis, Leyton-Brown, Taddy, *Deep IV: A Flexible Approach for Counterfactual Prediction* (2017, ICML)
- Hartford, Syrgkanis, *Machine Learning and Causality* (2022, tutorial at EC)
- Pearl, *Causality* (2009, Cambridge)
