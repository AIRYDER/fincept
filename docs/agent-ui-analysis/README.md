# Agent UI Analysis — Signal Cockpit Concepts

**Source:** `FINCEPTNEWUIFUSIUON.pdf` (73 pages, 4 AI agents: Gemini 3.1 Pro, Opus 4.7, GPT 5.5, Merged/Fused)

**Purpose:** Extract concepts that could actually work in our system. This is not a build plan — it's a salvage operation.

**Current implementation status:** The dashboard now includes a
`/signal-cockpit-demo` route for experimenting with the cockpit concept, plus
production-oriented surfaces for predictions, news labs, reconciliation, models,
and the AI portfolio builder. Treat this folder as the design rationale behind
those experiments, not as the authoritative product spec.

---

## What the agents built

All four agents converged on the same core concept: a **Signal Cockpit** — a single-screen, evidence-based research environment where symbols, news, models, sources, and risk caveats are rendered as connected nodes in a force-directed graph, replacing the conventional multi-page dashboard.

The aesthetic target: Teenage Engineering hardware precision + Nothing OS glyph minimalism + Bloomberg density.

---

## Folder contents

| File | What it covers |
| ------ | --------------- |
| `01-actionable-concepts.md` | Ideas we could actually implement with our current stack |
| `02-data-shapes-and-tokens.md` | TypeScript interfaces, color tokens, typography specs we can adopt |
| `03-safety-system-design.md` | The safety-state architecture (the strongest idea across all agents) |
| `04-operator-rail-pattern.md` | Structured AI copilot pattern (not chat) — fixed sections, no free-text |
| `05-evidence-stack-pattern.md` | Progressive disclosure L1→L4 for data provenance |
| `06-not-worth-it.md` | What to skip and why |
| `07-agent-comparison.md` | Where the 4 agents agreed vs. diverged |

---

## TL;DR — Top 5 things worth taking

1. **Safety-state banner** — Always-visible READ ONLY / NO ORDER PATH indicator, derived from a single source of truth, rendered in ≥2 places simultaneously. Directly applicable to our paper-trading context.
2. **Source health as a first-class layer** — We already have `/data/coverage` and OpenBB health. Making source status, freshness, and confidence visible on every data point (not buried in settings) is practical.
3. **Structured AI Operator Rail** — Fixed 5-section copilot (Focus, Detected, Why It Matters, Suggested Checks, Risk Caveats). Architecturally prevents hallucinated advice. Better than a chat box for a trading terminal.
4. **Progressive disclosure (L1→L4)** — Summary → Evidence → Raw Payload → Debug Trace. We already have API data; this is a UX pattern for surfacing it at the right depth.
5. **Semantic color token system** — Strict color grammar (cyan=verified, amber=experimental, red=critical, purple=AI, gray=inactive). We already use green/red/amber but lack the rigor and the AI/purple semantic.

---

## Applied in the dashboard

| Concept | Current surface | Status |
| --------- | ----------------- | -------- |
| Signal Cockpit | `/signal-cockpit-demo` | Prototype/demo only |
| Structured AI rail | Portfolio builder report flow and future cockpit rail | Partially applied |
| Evidence stack | Research/news impact surfaces | Partially applied |
| Source health | Datasource registry, OpenBB health, coverage concepts | Partially applied |
| Semantic AI/safety color | Dashboard tokens and page-specific badges | Needs stricter enforcement |

## Near-term UI priorities

1. Keep AI output structured into fixed sections instead of free-form chat.
2. Show provider/source freshness beside research and model-derived claims.
3. Keep `/portfolio-builder` usable on realistic model names such as GPT-5.5 and Claude Opus 4.7.
4. Avoid making `/signal-cockpit-demo` look production-authoritative until the data, risk, and order-path boundaries are proven.
5. Promote safety state and no-live-order assumptions into persistent UI chrome before adding more autonomous actions.
