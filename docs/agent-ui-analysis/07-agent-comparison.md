# Agent Comparison

Where the 4 AI agents (Gemini 3.1 Pro, Opus 4.7, GPT 5.5, Merged/Fused) agreed vs. diverged.

---

## Universal Agreement (All 4)

These concepts appeared in every agent's design with near-identical specs:

1. **Safety-state banner** — Always-visible, unhideable, rendered in ≥2 places, color-driven by single source of truth. Every agent made this non-negotiable.

2. **No execution controls** — Zero buy/sell/trade buttons, no order path, no directional language. The cockpit is research-only.

3. **Evidence over recommendation** — Show what is known, from where, how fresh, how confident. Never say "buy."

4. **Source health as first-class layer** — Not hidden in settings. Visible inline on every data point.

5. **Structured AI copilot (not chat)** — Fixed sections, not a chat input. Architecturally prevents hallucinated advice.

6. **Progressive disclosure L1→L4** — Summary → Evidence → Raw Payload → Debug Trace. Every agent specified this depth system.

7. **Signal Constellation Graph** — Force-directed node map as the centerpiece. (We're skipping this for now, but all agents agreed it's the core metaphor.)

8. **Market Pulse Timeline** — Chronological evidence stream with source-lane grouping and event glyphs.

9. **Semantic color system** — Cyan=verified, Amber=experimental, Red=critical, Purple=AI, Gray=inactive. The hex values varied slightly but the semantic map was identical.

10. **Two-font system** — One mono (data/values) + one sans (labels/body). JetBrains Mono + Inter was the most common pairing.

---

## Where They Diverged

### Layout Dimensions

| Aspect | Gemini | Opus | GPT 5.5 | Merged |
|--------|--------|------|---------|--------|
| Left rail width | 15% | 280px | 250-280px | 260px |
| Right rail width | 30% | 360px | 390-460px | ~380px |
| Focus bar height | not specified | 56px | not specified | 56px |
| Safety banner | 4px stripe | 4px stripe | thin stripe | 4px stripe |
| Bottom drawer | pull-up | collapsed 32px | 260-360px | 30% height |

**Takeaway:** The dimensions are close enough that the merged version's specs (260px left, 380px right, 56px top bar, 4px safety stripe) are reasonable defaults.

### Node Visual Forms

| Node Type | Gemini | Opus | GPT 5.5 | Merged |
|-----------|--------|------|---------|--------|
| Symbol | Large circle | Thick ring + dot-matrix core | Circular halo | Large circle + cyan halo |
| News | Diamond | Diamond glyph | Dot-matrix capsule | Dotted capsule |
| Model | Hexagon | Rounded rect + confidence ring | Diamond/ring | Hexagon + confidence ring |
| Source | Hexagon | Hexagon + bar meter | Rounded square | Hexagon |
| Risk | Triangle | Triangle/octagon | Triangle/octagon | Triangle |
| Strategy | Square | Square, dimmed | Hex capsule | Square, dashed border |

**Takeaway:** Symbol=circle, News=diamond/capsule, Model=hexagon+ring, Source=hexagon, Risk=triangle are the consensus. The exact shapes matter less than the visual vocabulary being consistent.

### Color Hex Values

| Semantic | Gemini | Opus | GPT 5.5 | Merged |
|----------|--------|------|---------|--------|
| Void bg | #09090B | #0A0B0D | #07090D | #050505 |
| Panel bg | #121215 | #111317 | #0D1016 | #0D0E12 |
| Verified cyan | #00E599 | #6EE7C7 | #31D0C6 | #00E599 |
| AI purple | #A855F7 | #B79BFF | #A78BFA | #A855F7 |
| Amber warn | #F59E0B | #F2B858 | #F6B848 | #FFB700 |
| Critical red | #EF4444 | #FF5C5C | #FF4D5E | #EF4444 |

**Takeaway:** The semantic map is identical; hex values vary by ±10%. The merged version's values are fine — the important thing is the token system, not the exact hex.

### Graph Engine Recommendation

| Agent | Recommendation |
|-------|---------------|
| Gemini | React Flow or D3.js |
| Opus | PixiJS/Canvas for graph, DOM for rest; d3-force or sigma.js |
| GPT 5.5 | React + Canvas/WebGL (PixiJS or custom), DOM for rest |
| Merged | @xyflow/react (React Flow v12), PixiJS fallback at >500 nodes |

**Takeaway:** React Flow is the consensus for DOM-scale graphs. Canvas fallback is only needed at very high node counts.

---

## Unique Ideas Per Agent

### Gemini Only
- "Run Pulse Scan" as a radar-sweep animation
- Ribbed slider component for interval selection
- Tactile elevated action buttons with illumination

### Opus Only
- 8px base grid unit (all paddings/gaps/radii are multiples of 4px)
- Semantic Zoom (collapse news nodes into `[● 12 HEADLINES]` clusters when zooming out)
- 1-Hop Isolation toggle (dim all non-directly-connected nodes)
- Keyboard accessibility for canvas graph (Tab → zones, G → graph, Arrow keys → node-to-node)
- L3/L4 auto-collapse after 30s of inactivity
- Dot-matrix skeleton loading (not shimmer bars)

### GPT 5.5 Only
- Price/Event Storyboard (compressed narrative strip)
- Graph Toolbelt (VIEW/DEPTH/FILTER/LAYOUT controls inside the graph)
- Layer toggles in left rail ([✓] Sources, [✓] News, [✓] Models, [✓] Risk, [ ] Debug)
- "Undock" glyphs on panels for multi-monitor
- Scrub time range to replay graph state over time
- Shift-click to compare evidence paths between nodes

### Merged Only
- Immutable Color-Lock Rule (semantic colors cannot be overridden by user themes)
- Concrete CSS specs for `.inset-screen` and `.tactile-btn`
- "Caution Tape" motif spec (4px diagonal `#FFB700`/`#1A1200` stripes on model nodes)
- Motion budget: `cubic-bezier(0.1, 0.9, 0.2, 1)`, 100-250ms durations, max 1.4s

---

## Overall Assessment

The agents produced remarkably consistent output. The core ideas (safety banner, structured copilot, evidence stack, source health, semantic colors) are well-validated by the convergence. The visual aesthetic (hardware precision, dark mode, glyph minimalism) is a strong direction even if we don't adopt the full neumorphic treatment.

The main thing the agents got wrong: they all assumed the constellation graph is the immediate priority. For our system, the graph is aspirational — the safety system, operator rail, and evidence stack are the practical wins.
