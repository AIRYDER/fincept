# Roadmap Mapping ? Sisyphus Tier Gaps to Research Entries

This file is the single most useful artifact in `research/`. When a Tier Q task is being implemented, this is the first file to open.

| Sisyphus gap | What we need to do | Research entries (sorted by relevance) |
|---|---|---|
| **Q0.1** Reconcile `gbm_predictor` features with live feature vocabulary | Update `FEATURES`; verify with look-ahead audit | `papers/2025/lopez-de-prado-look-ahead-bias.md`, `papers/2025/qlib-architecture.md` |
| **Q0.2** Extract shared trainer module | Move LightGBM into a shared lib | `papers/2024/platt-scaling.md`, `papers/2025/qlib-architecture.md` |
| **Q0.3** Apply `CandidateGatePolicy` to `gbm_predictor` | Port the gate; add DSR p-value | `papers/2026/lopez-de-prado-deflated-sharpe.md`, `papers/2025/lopez-de-prado-look-ahead-bias.md`, `papers/2025/qlib-architecture.md` |
| **Q0.4** Save `feature_importance.json` in production trainer | Standardize the artifact | `papers/2025/qlib-architecture.md`, `repos/qlib-microsoft.md` |
| **Q1.1** Calibration dossier per promoted model | Add Platt scaling, conformal intervals, DSR, and dossier | `papers/2024/platt-scaling.md`, `papers/2025/vovk-conformal-trading.md`, `papers/2026/lopez-de-prado-deflated-sharpe.md`, `papers/2025/qlib-architecture.md`, `repos/qlib-microsoft.md`, `architectures/qlib-design.md`, `benchmarks/numerai-tournament.md`, `papers/2025/lopez-de-prado-look-ahead-bias.md` |
| **Q1.2** Shadow vs active comparison | Paired test on (symbol, ts_event) | `papers/2025/qlib-architecture.md`, `papers/2024/platt-scaling.md` |
| **Q1.3** Drift detection | ADWIN / conformal / DRO / observability on rolling performance | `papers/2025/concept-drift-survey-gama.md`, `repos/river.md`, `papers/2026/moreira-muir-volatility-managed.md`, `papers/2025/vovk-conformal-trading.md`, `papers/2025/namkoong-distributionally-robust.md`, `papers/2025/chawla-thorp-kelly-2018.md`, `repos/arize-ai.md` |
| **Q1.4** Class-imbalance + horizon-mismatch fix | Update LightGBM params and validate class-weighted loss | `papers/2024/platt-scaling.md`, `papers/2024/zhang-deeplob.md`, `papers/2025/vovk-conformal-trading.md`, `papers/2025/qlib-architecture.md` |
| **Q2.1** Cross-sectional ranking | Rank by signal; long-short; OLPS baselines | `papers/2026/deep-momentum-lim.md`, `papers/2025/jegadeesh-titman-canonical.md`, `papers/2025/li-online-portfolio-selection.md`, `papers/2025/bhojraj-sector-rotation.md`, `architectures/worldquant-brain.md`, `repos/qlib-microsoft.md`, `papers/2025/qlib-architecture.md`, `architectures/qlib-design.md` |
| **Q2.2** Portfolio-level vol targeting | Scale notionals to target vol and calibrated interval width | `papers/2026/moreira-muir-volatility-managed.md`, `papers/2025/vovk-conformal-trading.md`, `repos/qlib-microsoft.md`, `papers/2025/qlib-architecture.md` |
| **Q2.3** Strategy decay monitor | Rolling Sharpe, drawdown, bandit regret, and alert | `papers/2025/concept-drift-survey-gama.md`, `repos/river.md`, `papers/2026/thompson-sampling-bandit.md`, `papers/2024/grossman-zhou-drawdown.md` |
| **Q2.4** Kelly-optimal sizing | Replace linear allocator with Kelly / DRO / drawdown-aware sizing | `papers/2026/chow-yang-correlated-kelly.md`, `papers/2025/chawla-thorp-kelly-2018.md`, `papers/2025/namkoong-distributionally-robust.md`, `papers/2024/grossman-zhou-drawdown.md`, `papers/2025/li-online-portfolio-selection.md`, `papers/2026/lopez-de-prado-deflated-sharpe.md` |
| **Q2.5** Per-strategy and per-symbol confidence thresholds | Add config support | `papers/2026/moreira-muir-volatility-managed.md`, `papers/2025/vovk-conformal-trading.md` |
| **Q3** General cutting-edge alpha expansion | Broad Tier Q3 references whose frontmatter maps to Q3 | `papers/2026/deep-momentum-lim.md`, `papers/2025/concept-drift-survey-gama.md`, `papers/2025/qlib-architecture.md`, `repos/qlib-microsoft.md`, `repos/finrl.md`, `models/timesfm-google.md`, `models/chronos-amazon.md`, `models/lag-llama.md`, `models/fingpt.md`, `architectures/qlib-design.md`, `benchmarks/numerai-tournament.md`, `papers/2026/cao-options-flow.md` |
| **Q3.1** Multi-agent LLM debate | Replace single-shot LLM with debate / agentic tool use | `papers/2025/du-multi-agent-debate.md`, `papers/2025/agentic-workflows.md`, `papers/2024/openai-function-calling.md`, `papers/2025/li-multimodal-llm-trading.md`, `repos/langchain.md`, `models/fingpt.md`, `events/icaif.md` |
| **Q3.2** Earnings-call LLM agent | New transcript and multimodal LLM agent | `papers/2026/llm-transcript-earning.md`, `papers/2025/li-multimodal-llm-trading.md`, `papers/2024/openai-function-calling.md`, `models/fingpt.md` |
| **Q3.3** Insider / short-interest agent | New informed-trading / disclosure agent | `papers/2026/cao-options-flow.md`, `papers/2026/llm-transcript-earning.md`, `models/fingpt.md` |
| **Q3.4** Sector rotation overlay | Macro-conditioned tilts and alpha-platform evaluation | `papers/2025/bhojraj-sector-rotation.md`, `architectures/worldquant-brain.md`, `repos/qlib-microsoft.md` |
| **Q3.5** Correlation breakdown alerts | New monitor | `papers/2025/concept-drift-survey-gama.md`, `repos/river.md` |
| **Q3.6** Liquidity stress test | Daily slippage sim | `papers/2026/moreira-muir-volatility-managed.md`, `repos/qlib-microsoft.md`, `repos/hudson-river-hftbacktest.md` |
| **Q3.7** L2 microstructure features | Order book features and deep LOB models | `papers/2024/zhang-deeplob.md`, `papers/2025/zhang-deeplob-v2-transformer-lob.md`, `papers/2025/nie-patchtst.md`, `benchmarks/kaggle-optiver-trading-at-the-close.md`, `repos/hudson-river-hftbacktest.md` |
| **Q3.8** Online learning / concept drift | `river`-based | `repos/river.md`, `papers/2025/concept-drift-survey-gama.md`, `repos/finrl.md`, `papers/2026/thompson-sampling-bandit.md` |
| **Q3.9** Time-series foundation models | TimesFM / Chronos / Lag-Llama / PatchTST | `models/timesfm-google.md`, `models/chronos-amazon.md`, `models/lag-llama.md`, `papers/2025/nie-patchtst.md` |
| **Q3.10** Multi-arm bandit allocator | Thompson sampling | `papers/2026/thompson-sampling-bandit.md`, `repos/river.md` |
| **Q4** Frontier scoping | Options, GAN/diffusion scenarios, neural SDE, causal inference, path signatures | `papers/2025/lyons-path-signatures.md`, `papers/2024/horvath-neural-sde.md`, `papers/2024/hartford-causal-inference.md`, `papers/2024/kiyavash-generative-scenarios.md`, `papers/2025/diffusion-financial-scenarios.md`, `papers/2024/heston-sabr-vol-models.md` |
| **Q4.1** Options strategies as alpha | Vol harvesting, dispersion, options alpha scoping | `papers/2024/heston-sabr-vol-models.md`, `papers/2024/horvath-neural-sde.md`, `papers/2026/cao-options-flow.md` |
| **Q4.2** Generative scenario simulation | GAN / diffusion stress scenarios | `papers/2024/kiyavash-generative-scenarios.md`, `papers/2025/diffusion-financial-scenarios.md` |
| **Q4.3** Graph neural networks | Supply-chain and graph alpha frontier | `papers/2025/lyons-path-signatures.md` |
| **Q4.4** Causal inference layer | DoWhy / EconML / Deep IV | `papers/2024/hartford-causal-inference.md` |
| **Q4.5** Federated learning | Multi-tenant research frontier | `repos/mlflow.md` |

**Reading the table.** For any Tier gap with `high`-relevance entries, read those first. Tier Q4 entries are scoping references, not next-build commitments.

## Operational / MLOps references (tier_mapping `none` or mixed)

| Topic | Research entries |
|---|---|
| Feature store / feature registry | `repos/feast.md` |
| ML observability / drift monitoring | `repos/arize-ai.md` |
| Experiment tracking / model registry | `repos/mlflow.md` |
| LLM orchestration / agent framework | `repos/langchain.md`, `papers/2025/agentic-workflows.md`, `papers/2024/openai-function-calling.md` |
| Industry infrastructure references | `repos/jane-street-fsharp.md`, `repos/hudson-river-hftbacktest.md`, `architectures/worldquant-brain.md`, `architectures/two-sigma-research-platform.md` |

## Phase 1 frozen entry inventory

The 21 Phase 1 entries are frozen and remain explicitly referenced by the database:

- `papers/2026/lopez-de-prado-deflated-sharpe.md`
- `papers/2026/moreira-muir-volatility-managed.md`
- `papers/2026/deep-momentum-lim.md`
- `papers/2026/chow-yang-correlated-kelly.md`
- `papers/2025/jegadeesh-titman-canonical.md`
- `papers/2025/concept-drift-survey-gama.md`
- `papers/2025/qlib-architecture.md`
- `papers/2024/platt-scaling.md`
- `repos/qlib-microsoft.md`
- `repos/river.md`
- `repos/finrl.md`
- `repos/zipline-reloaded.md`
- `models/timesfm-google.md`
- `models/chronos-amazon.md`
- `models/lag-llama.md`
- `models/fingpt.md`
- `architectures/qlib-design.md`
- `architectures/two-sigma-research-platform.md`
- `benchmarks/kaggle-optiver-trading-at-the-close.md`
- `benchmarks/numerai-tournament.md`
- `events/icaif.md`

## Phase 2 entry inventory

The 30 Phase 2 entries added by the expansion are:

- `papers/2025/lopez-de-prado-look-ahead-bias.md`
- `papers/2025/chawla-thorp-kelly-2018.md`
- `papers/2026/thompson-sampling-bandit.md`
- `papers/2025/vovk-conformal-trading.md`
- `papers/2025/namkoong-distributionally-robust.md`
- `papers/2025/li-online-portfolio-selection.md`
- `papers/2026/cao-options-flow.md`
- `papers/2026/llm-transcript-earning.md`
- `papers/2025/bhojraj-sector-rotation.md`
- `papers/2024/zhang-deeplob.md`
- `papers/2025/du-multi-agent-debate.md`
- `papers/2024/grossman-zhou-drawdown.md`
- `papers/2025/lyons-path-signatures.md`
- `papers/2024/horvath-neural-sde.md`
- `papers/2024/hartford-causal-inference.md`
- `papers/2024/kiyavash-generative-scenarios.md`
- `repos/feast.md`
- `repos/arize-ai.md`
- `repos/mlflow.md`
- `repos/langchain.md`
- `repos/jane-street-fsharp.md`
- `repos/hudson-river-hftbacktest.md`
- `architectures/worldquant-brain.md`
- `papers/2025/zhang-deeplob-v2-transformer-lob.md`
- `papers/2025/nie-patchtst.md`
- `papers/2024/heston-sabr-vol-models.md`
- `papers/2025/li-multimodal-llm-trading.md`
- `papers/2024/openai-function-calling.md`
- `papers/2025/agentic-workflows.md`
- `papers/2025/diffusion-financial-scenarios.md`

**Open entries needed after Phase 2.**

| Topic | Likely entry | Source | Suggested relevance |
|---|---|---|---|
| Dedicated insider / short-interest agent | paper | Cohen, Malloy, Pomorski, *Decoding Inside Information* (2012, JFE) | medium |
| Graph neural networks for cross-asset / supply-chain alpha | paper | Graph ML for financial networks survey | low (Tier Q4) |
| Federated learning for multi-tenant finance | paper | Federated learning in finance survey | low (Tier Q4) |
| Kyle lambda / market impact | paper | Kyle, *Continuous Auctions and Insider Trading* (1985) | medium |
| Hawkes processes for order flow | paper | Bacry et al., *Hawkes Processes in Finance* | medium |
