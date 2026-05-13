# Data Shapes & Design Tokens

Concrete TypeScript interfaces, color tokens, and typography specs extracted from the agent designs that align with our existing `src/lib/types.ts` and Tailwind config.

---

## TypeScript Interfaces

These complement our existing schemas in `libs/fincept-core/.../schemas.py` and `src/lib/types.ts`.

### SignalNode

```typescript
// A node in the evidence graph — symbol, news, model, source, risk, strategy
export interface SignalNode {
  id: string;
  type: "symbol" | "news" | "model" | "provider" | "risk" | "strategy";
  label: string;
  symbol?: string;
  isPlaceholder: boolean; // true for dev mockups
  healthStatus: "verified" | "healthy" | "experimental" | "critical" | "inactive";
  confidenceScore?: number; // 0.0–1.0, drives visual ring
  hasCautionTape?: boolean; // true for all model artifacts
  freshness?: "fresh" | "recent" | "stale" | "unknown";
  coverage?: "complete" | "partial" | "missing" | "unknown";
  caveat?: string;
  payloadRef?: string; // points to JSON for L3 drawer
}
```

### SignalEdge

```typescript
export interface SignalEdge {
  id: string;
  sourceId: string;
  targetId: string;
  relation: "provides" | "references" | "consumes" | "warns" | "explains" | "exposes";
  confidence: "low" | "medium" | "high" | "unknown";
  status: "active" | "partial" | "broken" | "dimmed";
}
```

### CockpitFocusContext

```typescript
export interface CockpitFocusContext {
  focusSymbols: string[];
  feedProvider: string;
  temporalInterval: string;
  safetyMode: "READ_ONLY" | "PAPER_MODE" | "LIVE_ROUTING_LOCKED" | "NO_ORDER_PATH";
  orderPath: "DISABLED" | "LOCKED";
  lastScanAt: string | null;
}
```

### PulseEvent (Timeline)

```typescript
export interface PulseEvent {
  id: string;
  timestamp: string;
  scope: string;
  symbol?: string;
  type: "bar" | "news" | "model" | "risk" | "coverage" | "source" | "system";
  source: string;
  summary: string;
  confidence: "low" | "medium" | "high" | "unknown";
  severity: "info" | "warning" | "critical" | "inactive";
}
```

### SourceHealth

```typescript
export interface SourceHealth {
  name: string;
  status: "healthy" | "partial" | "unavailable" | "unknown";
  mode: string;
  freshness: "fresh" | "recent" | "stale" | "unknown";
  coverage: "complete" | "partial" | "missing" | "unknown";
  confidence: "low" | "medium" | "high" | "unknown";
  caveats: string[];
}
```

---

## Color Token System

The merged/fused version has the most complete token set. These map to our existing Tailwind config but add the semantic safety layer we're missing.

### Base Surfaces

| Token | Hex | Use |
|-------|-----|-----|
| `bg.void` | `#050505` | Deep background / graph canvas |
| `bg.chassis` | `#0D0E12` | Main panel background |
| `bg.panel.raised` | `#141922` | Cards, raised modules |
| `bg.inset` | `#090C11` | Inset neumorphic surfaces |
| `line.hairline` | `#1E1F26` | 1px panel separators |
| `line.edge` | `#2A3038` | Graph edges at rest |

### Foreground

| Token | Hex | Use |
|-------|-----|-----|
| `fg.primary` | `#E8EDF2` | Values, numbers, primary text |
| `fg.secondary` | `#A9B1BD` | Labels, descriptions |
| `fg.tertiary` | `#6F7785` | Metadata, timestamps |
| `fg.disabled` | `#343A44` | Inactive nodes, dimmed text |

### Semantic (Immutable Color-Lock)

| Token | Hex | Meaning | Use |
|-------|-----|---------|-----|
| `state.verified` | `#00E599` | Cyan-green | Read-only verified data, healthy feeds |
| `state.fresh` | `#54E39B` | Green | Fresh/healthy source |
| `state.experimental` | `#F6B858` | Amber | Partial data, experimental model, caveat |
| `state.critical` | `#FF4D5E` | Red | Risk-critical, order-capable warning |
| `state.ai` | `#A78BFA` | Purple | AI interpretation, model context |
| `state.inactive` | `#69717F` | Gray | Unavailable, disabled, stale |
| `accent.source` | `#5CC8FF` | Blue-cyan | Data provenance highlight |

### Key Rule

> No single screen may use more than 3 semantic colors at full saturation simultaneously. Additional states appear at 60% opacity until hovered.

### Our Current vs. Proposed

| Current | Proposed Token | Change |
|---------|---------------|--------|
| `hsl(142, 71%, 45%)` long green | `state.verified` `#00E599` | Shift to cyan-green for "verified" not "long" |
| `hsl(0, 84%, 60%)` short red | `state.critical` `#FF4D5E` | Red reserved for risk/order-capable only |
| `hsl(38, 92%, 55%)` warning amber | `state.experimental` `#F6B858` | Same concept, slightly different hex |
| (none) | `state.ai` `#A78BFA` | **New** — AI/purple semantic for model outputs |

---

## Typography

| Role | Font | Size | Weight | Notes |
|------|------|------|--------|-------|
| Primary data/values | JetBrains Mono | 13–15px | 500 | `font-variant-numeric: tabular-nums` |
| Section labels | Inter | 10–11px | 600 | Uppercase, `letter-spacing: +0.12em` |
| Body copy (Operator Rail) | Inter | 13px | 400 | `line-height: 1.5` |
| Panel headings | Inter | 11px | 700 | Uppercase, `letter-spacing: +0.18em` |
| Hero numerics (Inspector) | JetBrains Mono | 28px | 500 | Tabular, rare use |
| Timeline timestamps | JetBrains Mono | 11px | 500 | Monospaced alignment |

### Rules

- Only two font families across the entire cockpit (mono + sans)
- `tabular-nums` mandatory on all numeric values
- Never center-align body text; labels may center only inside LEDs
- We already use Inter + have monospace for data — adding JetBrains Mono as the mono font is the main change

---

## CSS Reference Specs

From the merged/fused version — concrete CSS for the hardware panel aesthetic:

```css
/* Inset hardware screen (Timeline bg, Safety status) */
.inset-screen {
  background: #060608;
  box-shadow: inset 2px 2px 5px rgba(0, 0, 0, 0.8),
              inset -1px -1px 2px rgba(255, 255, 255, 0.05);
  border-radius: 2px;
}

/* Raised Tactile Button (Pulse Scan, Suggested Checks) */
.tactile-btn {
  background: #16161A;
  box-shadow: 2px 2px 4px rgba(0, 0, 0, 0.7),
              -1px -1px 1px rgba(255, 255, 255, 0.08);
  border: 1px solid #2A2A35;
  transition: all 100ms cubic-bezier(0.1, 0.9, 0.2, 1);
}
.tactile-btn:active {
  box-shadow: inset 1px 1px 3px rgba(0, 0, 0, 0.6);
  transform: translateY(1px);
}

/* Caution Tape — diagonal amber/black stripes on model nodes */
.caution-tape {
  background: repeating-linear-gradient(
    -45deg,
    #FFB700,
    #FFB700 4px,
    #1A1200 4px,
    #1A1200 8px
  );
  height: 4px;
}
```

### Motion Budget

- Max animation duration: 1.4s
- No `ease-in-out` with bouncy springs
- All motion uses `cubic-bezier(0.1, 0.9, 0.2, 1)` — snappy, mechanical
- Duration range: 100ms–250ms for transitions
- Only continuous animation: active freshness pulse on news nodes
