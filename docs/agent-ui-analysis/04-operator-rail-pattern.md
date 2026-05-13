# Operator Rail Pattern

The structured AI copilot pattern — the second strongest idea across all agents.

---

## Core Principle

The Operator Rail is **not a chat component**. It is five fixed sections populated from structured evidence. This architectural constraint prevents hallucinated financial advice.

---

## The Five Sections

### 1. Current Focus

Echoes the cockpit state: focused symbols, active feed, interval, safety mode.

```
CURRENT FOCUS
AAPL, NVDA
Alpaca IEX · 1Min
◉ READ ONLY · NO ORDER PATH
```

### 2. Detected

Bulleted observations with glyphs. Populated from structured `DetectedCondition[]` arrays — NOT from LLM free-text.

```
DETECTED
● 3 fresh headlines (AAPL)
▥ 41 recent bars (AAPL)
△ Missing coverage on NVDA sample window
◇ GBM model artifact generated
◌ OpenBB status provider-dependent
```

Glyph vocabulary:

- `●` Fresh data
- `▥` Bar/market data available
- `△` Warning / coverage gap
- `◇` Model output
- `◌` Unknown / unavailable
- `⚠` Risk caveat

### 3. Why It Matters

2-3 sentence interpretation in purple-tinted text. Badged `AI INTERPRETATION`. Watermarked with a permanent 9px footer: `AI GENERATED INTERPRETATION // DATA CONTEXT ONLY // NO ORDER ACTION IMPLIED`.

```
WHY IT MATTERS
AAPL shows contiguous 1-minute bars across the sampled window
and three fresh Alpaca headlines. NVDA coverage is sparse: the
sampled window contains gaps. Model confidence throttled to LOW
due to incomplete data matrix.

AI GENERATED INTERPRETATION // DATA CONTEXT ONLY // NO ORDER ACTION IMPLIED
```

**Key constraint:** This section is populated from structured data analysis, not from an unconstrained LLM prompt. The "interpretation" is template-driven from detected conditions.

### 4. Suggested Next Checks

Numbered list (1-5) of clickable actions. Each action routes the UI to perform that check. **No trading actions appear.**

```
SUGGESTED NEXT CHECKS
[ 1. Expand bar window ]
[ 2. Fetch OpenBB quote ]
[ 3. Compare news sentiment ]
[ 4. Inspect raw payload ]
[ 5. Check exposure if available ]
```

Clicking an action:
- Updates evidence (e.g., changes interval, fetches new data)
- Opens the relevant inspector
- Appends to the timeline
- Never routes an order

### 5. Risk Caveats

Fixed list of disclaimers. Always visible. Not generated — hardcoded.

```
RISK CAVEATS
- Data-only context
- Model output is experimental
- No order action suggested
- Source coverage may be partial
- This cockpit does not provide financial advice
```

---

## Confidence Meter

A horizontal bar or LED meter computed from source coverage completeness:

```
CONFIDENCE   ▰▰▰▱▱  MEDIUM
Based on source freshness, coverage completeness, and model caveats.
```

---

## Why This Is Better Than a Chat Box

| Chat Box | Operator Rail |
|----------|--------------|
| LLM generates free-form text | Fixed sections populated from structured data |
| Can say "buy AAPL" | Architecturally prevented from directional language |
| No provenance on claims | Every claim links back to graph evidence nodes |
| Unpredictable output | Deterministic, testable output structure |
| Single input field | Five purpose-built sections |
| No safety constraints | Safety constraints are structural, not prompt-based |

---

## Implementation in Our System

We already have the data sources for most sections:

- **Current Focus:** From existing dashboard state (selected symbols, active feed)
- **Detected:** From `/data/coverage` (gaps), `/news` (fresh headlines), `/models/predictions` (artifacts)
- **Why It Matters:** Template-driven from detected conditions (no LLM needed for v1). Could use the existing `/news/impact` endpoint which already does sentiment analysis
- **Suggested Checks:** Mapped from detected conditions to existing UI actions (interval change → Markets page, OpenBB fetch → Research page, payload inspect → API response viewer)
- **Risk Caveats:** Hardcoded strings matching our paper-trading disclaimers

The `<OperatorRail>` component could be added as a right-side panel on the Overview or Markets page, using existing Zustand stores and API data.
