# Anti-Curation — What NOT to Add

These are common research-database traps. We do not add:

## HFT / sub-millisecond latency papers
EDGE_ROADMAP §3 explicitly forbids it. The reason is not that HFT research is bad; it is that we have chosen a different game (low-latency mid-frequency).

## Generic "how to backtest" tutorials
Backtester mechanics are well-served by `services/backtester/`. We capture the *methodology behind the mechanics* (walk-forward, look-ahead audit, deflated Sharpe), not the mechanics themselves.

## Cloud-vendor ML whitepapers
Except where they specifically document an architecture we might adopt (e.g., feature stores, online inference at scale).

## Twitter / Reddit signal research
EDGE_ROADMAP §3: signal-to-noise too low; LLM cost too high.

## Sentiment from images / video
Same.

## Pure RL for portfolio allocation
EDGE_ROADMAP §3: sample-inefficient and unstable. We may capture RL papers as `relevance: low` with a clear "we are not building this" caveat, because the research is informative about what *doesn't* work.

## "1000 features" trap papers
Multiple-comparison noise; the volume of features is not a research problem.

## Outdated repos
If a repo has had no commit in 12 months and no clear successor, prefer the successor.

## Marketing whitepapers dressed as research
If a paper has no reproducible methodology, no eval section, and no author affiliation beyond the vendor, it is marketing.

## Hype-cycle blog posts
"X is the future of trading" is not research; it is journalism. We capture the underlying *paper* when there is one.

## Twitter threads
No matter how insightful. Quote the original paper; do not quote the thread.

## Pre-publication "AI-generated" content
Specifically: anything where the methodology is "use a large language model to summarize the literature." That is itself not literature.

## Cult-of-personality content
Specifically: posts by named individuals that have no methodology. The individual may be right or wrong, but the post is not research.

## Discredited work
When a paper has been formally retracted or informally discredited by the community, do not add it even if it was influential. Cite the discrediting work; do not cite the original.

## Papers with no author affiliation
Anonymous papers are not research. Skip.

## Papers more than 20 years old
Unless they are foundational (Jegadeesh-Titman 1993, Platt 1999, Kelly 1956) or specifically relevant to a Sisyphus tier gap, do not add. The field has moved on; old papers are rarely the right reference.

## Promotional repos with stars bought
Check the star history before trusting a repo's "popularity."
