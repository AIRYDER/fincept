# Evidence Stack Pattern

Progressive disclosure for data provenance — L1→L4 depth system.

---

## The Pattern

Every evidence item (market data, news, model output, risk, source) has four depth levels:

| Level | Name | Content | Default State |
|-------|------|---------|---------------|
| L1 | Summary | Headline, health, confidence | Visible |
| L2 | Evidence | Extracted data points, source links | Expand on click |
| L3 | Raw Payload | Formatted JSON response | Collapsed |
| L4 | Debug Trace | Millisecond timing, API endpoints hit | Collapsed, auto-close after 30s |

---

## Evidence Stack Sections

Five collapsible sections, each with L1 visible by default:

### Market Data

```text
MARKET DATA                     MED CONF
AAPL
  Bars: 41 in sampled window
  Latest close: 188.40 USD
  Feed: Alpaca IEX
  Coverage: partial/recent
NVDA
  Bars: 0 in sampled sub-window
  Latest close: unavailable in sample
  Feed: Alpaca IEX
  Coverage: partial
  Caveat: expand window before interpreting
```

### News

```text
NEWS                            MED CONF
AAPL
  Headlines: 3
  Freshest: 09:44
  Topics: product, analyst commentary
NVDA
  Headlines: 5
  Freshest: 09:58
  Topics: AI chips, data center, earnings
```

### Models

```text
MODELS                          LOW/MED CONF
GBM
  Status: generated artifact
  Confidence: medium-low
  Caveat: experimental model output
News Alpha
  Status: partial
  Confidence: low/medium
  Warning: dev artifact; verify inputs
```

### Risk

```text
RISK                            VERIFIED SAFETY
Mode: READ ONLY
Order path: Disabled
Exposure: unavailable
Warnings:
  - Source coverage may be partial
  - Model output is experimental
  - No order action suggested
```

### Sources

```text
SOURCES                         MIXED CONF
Alpaca    ● Healthy    IEX free feed    recent    partial    MED
OpenBB    ◌ Available  provider-dep.    unknown   unknown   UNK
Models    ◐ Partial    dev artifact    —         —         LOW/MED
Cache     ● Ready      current session —         —         OK
```

---

## Depth Control

Each section has a depth selector: `[L1] [L2] [L3] [L4]`

- L1 is always visible
- L2 expands evidence rows on click
- L3 reveals raw JSON payload — syntax-highlighted, zebra-striped, 1-click copy
- L4 reveals timing/debug trace — visually subdued, auto-collapses after 30s of inactivity

---

## Source Confidence Badge Format

Every data value carries source, freshness, and confidence metadata:

```text
[● ALPACA-IEX | FRESH | PARTIAL | MED]
```

The dot is a colored LED:
- Green = healthy
- Amber = partial/warning
- Red = critical
- Gray = unavailable

---

## Implementation in Our System

We already have the data for most of this:

- **Market Data L1/L2:** From `/data/coverage` and the existing bar data endpoints
- **News L1/L2:** From `/news` endpoint (already returns headlines with timestamps)
- **Models L1/L2:** From `/models/predictions` (already returns confidence, direction, horizon)
- **Risk L1:** From kill-switch state and exposure endpoints
- **Sources L1:** From `/health/openbb` and datasource registry

What we'd add:

- L3: Raw JSON viewer (already in TanStack Query cache, just needs a viewer component)
- L4: Add `X-Response-Time` header to API responses for timing data
- Source confidence badges: Extend API responses to include `freshness`, `coverage`, `confidence` fields
- Depth selector tabs on existing evidence cards

This is a UX enhancement to existing pages, not a new page.
