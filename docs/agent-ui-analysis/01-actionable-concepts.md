# Actionable Concepts

Ideas from the agent designs that could actually be implemented in our current Next.js + Tailwind + Zustand dashboard without a rewrite.

---

## 1. Safety-State Banner (High Value, Low Effort)

**What it is:** A permanent, unhideable ribbon beneath the top bar showing the current safety mode (READ ONLY, NO ORDER PATH, PAPER MODE, etc.). Color-driven by a single source of truth. Rendered in ≥2 places simultaneously (top bar + operator rail footer).

**Why it works for us:**
- We already have a kill switch and risk page
- Paper trading is our default mode — the safety banner makes this architecturally visible
- Prevents the "can this thing route real orders?" confusion

**How to implement:**
- Add a `safetyMode` slice to the existing Zustand store
- States: `READ_ONLY`, `PAPER_MODE`, `LIVE_LOCKED`, `NO_ORDER_PATH`
- Render as a 4px colored stripe + LED dots in the `<AppShell>` topbar
- The risk page already shows exposure — wire the kill-switch state into this banner

**Agent consensus:** All 4 agents made this the #1 non-negotiable. The merged version calls it "architecturally enforced, not a footer disclaimer."

---

## 2. Source Health as a First-Class Visual Layer (High Value, Medium Effort)

**What it is:** Every data point carries source, freshness, and confidence metadata. Source health is visible inline, not in a settings page.

**Why it works for us:**
- We already have `/data/coverage`, OpenBB health endpoints, and datasource registry
- The `/health/openbb` endpoint already tracks health history
- Our `docs/datasources.md` already routes providers by safety tier

**How to implement:**
- Add `sourceConfidence` badges next to price/news data in Markets and Predictions pages
- Badge format: `[● ALPACA-IEX | CONF: MED]` with colored LED dot
- Extend existing `datasource` API responses to include `freshness`, `coverage`, `confidence` fields
- Add a compact "Source Health Mini-Stack" to the Markets page sidebar

**Agent consensus:** All 4 agents made source health a top-level concern. GPT 5.5 added the most detail on badge format.

---

## 3. Structured AI Operator Rail (High Value, Medium Effort)

**What it is:** A fixed 5-section panel replacing a chat-style AI copilot:
1. **Current Focus** — echoes cockpit state (symbols, feed, interval, mode)
2. **Detected** — bulleted observations with glyphs (fresh headlines, missing coverage, model artifacts)
3. **Why It Matters** — 2-3 sentence interpretation, purple-tinted, badged `AI INTERPRETATION`
4. **Suggested Next Checks** — numbered clickable actions (e.g., "Expand bar window", "Fetch OpenBB quote")
5. **Risk Caveats** — fixed disclaimers

**Why it works for us:**
- We already have predictions, news impact, and research tools
- The "Why It Matters" section is essentially what `/news/impact` already does — just needs structured presentation
- "Suggested Next Checks" maps directly to existing API actions (expand window → change interval, fetch OpenBB → research endpoint)
- Architecturally prevents hallucinated advice — the rail is populated from structured `DetectedCondition[]` arrays, not LLM free-text

**How to implement:**
- Add an `<OperatorRail>` component to the right side of the Overview or Markets page
- Populate from existing API data: predictions (confidence), news (freshness), coverage (gaps)
- "Why It Matters" can be template-driven from existing data (no LLM needed for v1)
- "Suggested Checks" are wired to existing UI actions (interval change, research fetch, etc.)

**Agent consensus:** All 4 agents rejected chat-box AI in favor of structured sections. Opus had the most detailed spec.

---

## 4. Progressive Disclosure L1→L4 (Medium Value, Low Effort)

**What it is:** Every evidence item has 4 depth levels:
- **L1: Summary** — headline, health, confidence
- **L2: Evidence** — extracted data points
- **L3: Raw Payload** — formatted JSON
- **L4: Debug** — millisecond timing, API endpoints hit

**Why it works for us:**
- We already return raw API data from our endpoints
- The research page already shows OpenBB/Exa results
- Adding depth selectors to existing cards is a small UX enhancement

**How to implement:**
- Add a `[L1][L2][L3][L4]` tab bar to evidence cards on the Predictions and Research pages
- L1/L2 are rendered from existing API response shapes
- L3 shows the raw JSON response (already available from TanStack Query cache)
- L4 shows timing data (add `X-Response-Time` header to API responses)
- L3/L4 auto-collapse after 30s of inactivity (per Opus spec)

---

## 5. Market Pulse Timeline (Medium Value, Medium Effort)

**What it is:** A chronological evidence stream showing events (bar arrived, news detected, model generated, risk check) with source and confidence annotations.

**Why it works for us:**
- We already have an activity feed on the Overview page
- Adding source badges and event-type glyphs is an enhancement, not a rewrite
- The timeline concept binds naturally to our existing WebSocket events

**How to implement:**
- Enhance the existing activity feed with structured event types and source chips
- Add event glyphs: `▮` bar, `◆` news, `◈` model, `⚠` risk
- Each row: timestamp · symbol · event type · source · confidence · summary
- Filter by symbol, source, event type

---

## 6. Confidence Rings / Visual Confidence Indicators (Low Value, Low Effort)

**What it is:** Model/prediction nodes show a circular arc (0-100%) representing confidence score.

**Why it works for us:**
- Predictions page already shows confidence as a number
- A visual ring is a small enhancement to existing prediction tiles

**How to implement:**
- Add an SVG ring component around prediction cards
- Arc length = confidence percentage
- Color: purple for AI/model, amber for experimental

---

## 7. Keyboard Shortcuts (Low Value, Low Effort)

**What it is:** Single-key shortcuts for cockpit zones: `G` graph, `T` timeline, `O` operator rail, `R` run scan, `/` command palette.

**Why it works for us:**
- We already have `⌘K` command palette with mnemonics
- Adding a few more single-key shortcuts is trivial

**How to implement:**
- Extend existing command palette hook with zone shortcuts
- `R` = refresh/re-fetch current data
- `G` = focus graph/constellation (when built)
- `O` = toggle operator rail
