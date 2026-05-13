# Safety-System Design

The strongest idea across all 4 agents. Every single one made the safety-state system their #1 non-negotiable principle.

---

## Core Concept

Safety is not a settings checkbox — it is a **visual architectural state** rendered in multiple places simultaneously, derived from a single source of truth.

The operator should never have to wonder: "Can this interface route trades?"

---

## Safety States

| State | Banner Color | LED | Text | When |
|-------|-------------|-----|------|------|
| READ_ONLY | Cyan `#00E599` | ◉ cyan | `READ ONLY · DATA-ONLY CONTEXT` | Default. No order submission possible. |
| PAPER_MODE | Green `#54E39B` | ◉ green | `PAPER MODE · SIMULATED ORDER PATH` | Paper trading enabled. Orders go to paper OMS only. |
| LIVE_ROUTING_LOCKED | Amber `#F6B858` | ◉ amber striped | `LIVE LOCKED · KEY REQUIRED TO UNLOCK` | Live broker connected but orders require explicit unlock. |
| NO_ORDER_PATH | Cyan `#00E599` | ◉ cyan | `NO ORDER PATH · OUTPUT DISABLED` | No broker connection at all. |
| ORDER_CAPABLE (reserved) | Red `#FF4D5E` | ◉ red pulsing | `CAUTION · ORDER PATH ENABLED` | Live orders possible. Highest visual priority. |

### Priority Rules

- If multiple states coexist (e.g., READ_ONLY + NO_ORDER_PATH), show both LEDs; banner reflects the **higher-severity** state
- Severity order: Red (order-capable) > Amber (experimental/locked) > Purple (AI) > Cyan/Green (verified) > Gray (inactive)
- Red is reserved exclusively for order-capable or risk-critical states — **never for decoration or hover**

---

## Where Safety Is Rendered

1. **Focus Bar LEDs** — Two round indicators in the top bar (READ ONLY, NO ORDER PATH)
2. **Safety Banner Stripe** — 4px horizontal stripe beneath focus bar, color = active safety state
3. **Operator Rail Footer** — Risk caveats section always lists current safety disclaimers

All three read from the same `safetyMode` store slice. No component may independently decide its safety state.

---

## Hard Rules (from all agents)

1. **Zero execution lexicon** — The words "Buy", "Sell", "Trade", "Execute", "Take Profit" do not exist in the DOM under any safety mode in this cockpit
2. **No order path permanence** — The safety bar permanently renders `NO ORDER PATH` until explicitly changed by an operator action with confirmation
3. **AI disabling limitations** — AI interpretations are strictly mapped from structured `DetectedCondition[]` arrays. The Operator Rail is permanently barred from returning LLM free-text to prevent hallucinated financial advice
4. **Watermarked briefings** — Any AI-generated text forces a permanent 9px footer: `AI GENERATED INTERPRETATION // DATA CONTEXT ONLY // NO ORDER ACTION IMPLIED`
5. **Model disclaimer** — Any prediction node displays an amber "caution tape" visual motif, permanently classifying it as a "Dev Artifact"

---

## Accepted vs. Banned Language

### Accepted

- "Evidence suggests..."
- "Source coverage indicates..."
- "Model artifact shows..."
- "Confidence is limited by..."
- "Suggested next check..."

### Banned

- Buy, Sell, Hold
- Go long, Go short
- Enter, Exit
- Target price
- Guaranteed signal
- Any directional trading verb

---

## Implementation in Our System

Our current system already has:

- Kill switch (`POST/DELETE /kill-switch`) on the Risk page
- Paper OMS as the only order path
- Risk page with exposure bars and alerts

What we'd add:

1. A `useSafetyStore()` Zustand slice with `mode`, `orderPath`, `lastConfirmedAt`
2. A `<SafetyBanner>` component rendered inside `<AppShell>` (topbar area)
3. LED indicators in the topbar next to the existing kill-switch button
4. The kill-switch state feeds into the safety store as the source of truth
5. Paper mode is the default — no code change needed, just visual surfacing

This is the single highest-ROI item from the entire PDF.
