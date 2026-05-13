# Not Worth It

Concepts from the agent designs that don't make sense for our current system, with reasons.

---

## 1. Signal Constellation Graph (Force-Directed Node Map)

**What it is:** The centerpiece of the agent designs — a force-directed graph where symbols, news, models, sources, and risk are nodes connected by edges. Custom node rendering with confidence rings, caution tape, PCB-trace edges, radar sweep animations.

**Why not:**
- Massive implementation effort for a single visualization
- React Flow + custom nodes + force-directed layout + canvas fallback is weeks of work
- Our current data density doesn't justify it — we have ~5-10 symbols, not 500
- The multi-page dashboard already works for our use case
- Graph visualization is cool but doesn't solve a problem we actually have right now

**Verdict:** Aspirational. Could revisit as a future "Signal Cockpit" page once the core platform is proven, but not now.

---

## 2. Full-Screen Cockpit Replacing All Pages

**What it is:** A single-surface layout that replaces Markets, News, Models, Risk, etc. with one unified cockpit.

**Why not:**
- Complete rewrite of the existing dashboard
- Our current page-based navigation with mnemonics (OV/PS/OR/ST/PR/MK/RK) works
- Users expect distinct pages for distinct workflows
- The cockpit concept works better as an additional view, not a replacement

**Verdict:** The cockpit could be an additional "Signal Cockpit" page, not a replacement for the existing dashboard.

---

## 3. Multi-Monitor Undocking

**What it is:** Panels can "undock" into independent browser windows with synchronized state.

**Why not:**
- Requires `BroadcastChannel` API or shared worker for state sync
- Far from MVP
- Our single-monitor dashboard is sufficient for now

**Verdict:** Nice-to-have for professional desk setups, but not a priority.

---

## 4. Neumorphic Glassmorphism

**What it is:** The layered dark panels with inset shadows, glass tint overlays, and neumorphic depth effects.

**Why not:**
- Accessibility concerns (low contrast on inset surfaces)
- Hard to maintain consistently across components
- The general "dark, dense, technical" aesthetic can be achieved more simply
- Our existing Tailwind dark mode already looks professional

**Verdict:** Take the color tokens and the "hardware feel" direction, but skip the complex neumorphic CSS. Use simple elevation + border highlights instead.

---

## 5. Custom Dot-Matrix Glyphs

**What it is:** Custom SVG/Canvas-drawn dot-matrix text for decorative system labels.

**Why not:**
- Over-engineered for current needs
- Accessibility nightmare (screen readers can't read canvas text)
- The technical aesthetic can be achieved with `JetBrains Mono` + uppercase + tracking

**Verdict:** Use the typography recommendations (mono + uppercase + tracking) but skip custom dot-matrix rendering.

---

## 6. PixiJS Canvas Fallback

**What it is:** When graph nodes exceed 500, switch from DOM-based React Flow to PixiJS canvas rendering.

**Why not:**
- We'll never have 500 simultaneous nodes in our current scope
- Premature optimization
- Adds a complex rendering dependency

**Verdict:** Not needed. If we ever build the constellation graph, React Flow handles our scale fine.

---

## 7. Price/Event Storyboard

**What it is:** A compressed horizontal track showing when evidence appeared relative to price, with event glyphs overlaid. Not a standard chart — a "market narrative strip."

**Why not:**
- Our existing Recharts candlestick/bar charts on the Markets page serve the purpose
- The storyboard is a novel visualization but doesn't add information value over a timeline + chart combo
- Would require custom rendering

**Verdict:** The Market Pulse Timeline concept (enhanced activity feed) is more practical. Skip the storyboard.

---

## 8. Radar Sweep Loading Animation

**What it is:** When "Run Pulse Scan" is clicked, a cyan vertical line sweeps left-to-right across the graph, illuminating nodes as it touches them.

**Why not:**
- Cool but purely decorative
- Adds animation complexity for no information gain
- A simple sequential source-loading indicator achieves the same feedback

**Verdict:** Use a simpler loading state (sequential source LEDs lighting up) instead of the radar sweep.
