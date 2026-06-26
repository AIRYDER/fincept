# Sources — Where to Mine for Research

## Primary sources (must monitor, weekly or monthly)

| Source | What it gives | Cadence | URL |
|---|---|---|---|
| arXiv q-fin.ST | Statistical finance (calibration, microstructure, factor models) | weekly | https://arxiv.org/list/q-fin.ST/recent |
| arXiv q-fin.TR | Trading & market microstructure | weekly | https://arxiv.org/list/q-fin.TR/recent |
| arXiv q-fin.GN | General finance (methodology) | weekly | https://arxiv.org/list/q-fin.GN/recent |
| arXiv q-fin.PR | Pricing (option, vol models) | monthly | https://arxiv.org/list/q-fin.PR/recent |
| arXiv cs.LG | ML methodology (may transfer) | monthly | https://arxiv.org/list/cs.LG/recent |
| arXiv stat.ML | Statistical ML (calibration, drift, online learning) | monthly | https://arxiv.org/list/stat.ML/recent |
| ICAIF proceedings | Top venue for AI in finance | yearly | https://ai-finance.org/conference |
| NeurIPS finance workshops | Cutting-edge ML × finance | yearly | https://neurips.cc/Conferences/2024/ |
| KDD finance workshops | Applied data science × finance | yearly | https://kdd.org/ |
| SSRN Quantitative Finance | Pre-publication drafts | monthly | https://papers.ssrn.com/sol3/Jeljour.cfm?form_name=filter_abstract&cfm_abstract_id=1500000 |
| GitHub Trending | New repos worth evaluating | weekly | https://github.com/trending/python?since=weekly |
| Microsoft Qlib releases | Reference open-source platform | per release | https://github.com/microsoft/qlib/releases |
| Numerai tournament | Live ML competition | weekly | https://numer.ai/tournament |
| WorldQuant BRAIN | Alphas and methodology | monthly | https://platform.worldquantbrain.com/ |
| Hudson & Thames blog | Quant research, well-documented | per post | https://hudsonthames.org/ |
| OpenBB Discord | Operator-facing discussion | per release | https://discord.gg/openbb |

## Secondary sources (check quarterly)

- *Journal of Portfolio Management* — https://www.iijournals.com/jpm
- *Quantitative Finance* — https://www.tandfonline.com/toc/rquf20/current
- *Journal of Financial Econometrics* — https://academic.oup.com/jfec
- *Bloomberg Quant Research* — https://www.bloomberg.com/professional/ (subscription)
- Two Sigma Talks — https://www.twosigma.com/talks/
- Jane Street F Sharp puzzles + writeups — https://www.janestreet.com/puzzles/
- Hudson River Trading open-source repos — https://github.com/hudson-and-thames
- Citadel AI Research — https://www.citadel.com/careers/ (occasional public posts)
- Alpha-Architect blog — https://alphaarchitect.com/
- PyQuant News — https://www.pyquantnews.com/

## Search queries (use these in arXiv / Google Scholar / Semantic Scholar)

- `online learning` + `finance`
- `cross-sectional momentum` + `deep learning`
- `volatility targeting` + `portfolio`
- `Kelly criterion` + `correlated assets`
- `look-ahead bias` + `backtest`
- `walk-forward` + `cross-validation`
- `path signature` + `trading`
- `rough volatility`
- `neural SDE` + `hedging`
- `multi-agent LLM` + `trading`
- `conformal prediction` + `portfolio`
- `distributionally robust optimization` + `finance`
- `drawdown-constrained` + `portfolio`
- `time-series foundation model` + `forecasting`
- `online portfolio selection` (OLPS algorithms)
- `temporal point process` + `order book`
- `LOB` + `Transformer`
- `neural bandit` + `portfolio`
- `Hawkes process` + `limit order book`
- `order flow toxicity`
- `VPIN` + `volume-synchronized probability of informed trading`
- `interpretable ML` + `finance`
- `causal inference` + `factor model`

## What NOT to add (per `ANTI_CURATION.md`)

- HFT / sub-millisecond latency papers (EDGE_ROADMAP §3 forbids it)
- Generic "how to backtest" tutorials (backtester mechanics live in `services/backtester/`)
- Cloud-vendor ML whitepapers (except where they document an architecture)
- Twitter / Reddit signal research (EDGE_ROADMAP §3: signal-to-noise too low)
- Sentiment from images / video (token cost vs alpha currently terrible)
- Pure RL for portfolio allocation (EDGE_ROADMAP §3: sample-inefficient and unstable)
- "1000 features" trap papers (multiple-comparison noise)
- Outdated repos (no commit in 12 months, no clear successor)
- Marketing whitepapers dressed as research
- Hype-cycle blog posts ("X is the future of trading")
