# Relevance Scoring

Every entry has a `relevance: high | medium | low` field. The rule:

## `high` — directly addresses a Sisyphus Tier 0 / Q0 / Q1 / Q2 gap
Examples:
- The DSR paper is `high` because it gives the Fincept candidate gate (Tier Q0.3) a defensible statistical test.
- The Moreira-Muir vol-targeting paper is `high` because it gives Tier Q2.2 a one-paragraph implementation.
- The Platt scaling paper is `high` because Tier Q1.1 calibration dossier needs a calibration step.

## `medium` — enables a Tier 3+ feature or is foundational context
Examples:
- The Microsoft Qlib repo is `medium` because it informs Tier Q2.1 and Q2.2 design but is not directly applied.
- The ICAIF conference entry is `medium` because it is a source, not a paper.
- The Two Sigma architecture entry is `medium` because it informs design but does not provide a direct implementation.

## `low` — tangential, already addressed in code, or general background
Examples:
- A copy-paste of a Wikipedia article.
- A "first-principles" explainer that does not add to what's already in `libs/fincept-core`.
- An entry for a Tier Q4 research-frontier feature that is not in scope for the next year.

## Discipline rules

- Default to `medium`. Inflated `high` ratings degrade the signal.
- A `high` entry should be linked from `ROADMAP_MAPPING.md`. If it is not, downgrade to `medium`.
- A `low` entry should explain *why it is kept*. If there is no reason, delete the entry.
- The `relevance` field is updated at every review cycle. It is not permanent.

## Mapping to status

| relevance | status | review cadence |
|---|---|---|
| high | verified | every 6 months |
| medium | verified | every 12 months |
| low | verified | every 12 months |
| any | needs-review | until a human validates |
| any | stale-link | until a human fixes or archives |
| any | archived | no review |
