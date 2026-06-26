# Research — Fincept Cutting-Edge Quant/ML Reference Database

This directory is the team's curated reference for cutting-edge quant/ML trading research, repos, models, architectures, benchmarks, vendors, and events. It exists to make every Tier decision in `Sisyphus_Quant_ML_Deep_Dive.md` and `Sisyphus_Ultra_Report.md` evidence-based.

## Start here

- [`INDEX.md`](./INDEX.md) — master table of contents, by category and by Sisyphus tier
- [`_meta/ROADMAP_MAPPING.md`](./_meta/ROADMAP_MAPPING.md) — Sisyphus gap → research entry map (the most useful file)
- [`_meta/SOURCES.md`](./_meta/SOURCES.md) — where to mine for new entries
- [`_meta/RELEVANCE_SCORING.md`](./_meta/RELEVANCE_SCORING.md) — how to rate an entry
- [`_meta/ANTI_CURATION.md`](./_meta/ANTI_CURATION.md) — what NOT to add
- [`_meta/ENTRY_TEMPLATE.md`](./_meta/ENTRY_TEMPLATE.md) — schema for new entries

## How to use

### "I'm about to implement a Tier Q2 task."
Open `_meta/ROADMAP_MAPPING.md`. Find the row. Read the top three entries. Write the spec task with the references inline.

### "A regression in production."
Open `INDEX.md`, filter by tag (e.g., `calibration`, `drift`). Read the `Why we care` and `How to apply` sections.

### "A new idea in standup."
Search `INDEX.md` for the relevant tag. If a TimesFM entry already exists, read it. If not, decide whether to add.

### "I'm reviewing a PR."
Open `INDEX.md` and confirm the cited references exist with `relevance: high` or `medium`.

### "I'm preparing a Tier X+ checkpoint review."
Open `_meta/ROADMAP_MAPPING.md`. For every Tier Q3 row, confirm at least one `high`-relevance entry exists.

## How to add a new entry

1. Pick a category (`papers/`, `repos/`, `models/`, `architectures/`, `benchmarks/`, `vendors/`, `events/`). For `papers/`, also pick a year.
2. Use the file naming convention: `kebab-case-author-or-topic.md` (e.g., `platt-scaling.md`, `qlib-microsoft.md`).
3. Copy the frontmatter and body template from `_meta/ENTRY_TEMPLATE.md`.
4. Fill in every frontmatter field. Use `Unknown` if a field genuinely doesn't apply.
5. Write the body. The most important sections: `Why we care` (specific to a Sisyphus gap) and `How to apply` (concrete code suggestion).
6. Update `INDEX.md` (add a row).
7. Update `_meta/ROADMAP_MAPPING.md` (add the entry to the relevant rows).
8. Append a line to `UPDATE_LOG.md`.

## What is in this database right now

- 21 seed entries spanning papers, repos, models, architectures, benchmarks, and events
- Every Tier Q0 / Q1 / Q2 gap in `Sisyphus_Quant_ML_Deep_Dive.md` has at least one `high`-relevance entry
- Several Tier Q3 / Q4 gaps are intentionally uncovered (Q3 covered where research is mature; Q4 deferred)
- Open entry needs: see `_meta/ROADMAP_MAPPING.md` "Open entries needed" section

## Maintenance

- `high` entries reviewed every 6 months; `medium` and `low` every 12 months.
- New entries added continuously as the research lead finds them.
- The `last_reviewed` field in every entry tracks the most recent review.
- `UPDATE_LOG.md` is the audit trail.

## Companion docs

- [`Sisyphus_Quant_ML_Deep_Dive.md`](../Sisyphus_Quant_ML_Deep_Dive.md) — the Sisyphus tier gaps that this database is designed to support
- [`Sisyphus_Ultra_Report.md`](../Sisyphus_Ultra_Report.md) — the system-wide audit
- [`spec/EDGE_ROADMAP.md`](../spec/EDGE_ROADMAP.md) — the strategic thesis
- [`docs/RESEARCH_PLAN.md`](../docs/RESEARCH_PLAN.md) — the explanation of this database (companion document)
