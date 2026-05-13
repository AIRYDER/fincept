# Fincept UI Aesthetic Audit

Date: 2026-05-09

## Scope

This audit covers the new and recently expanded dashboard surfaces as a live mock-and-runtime review, not a design-theory pass. I tested the rendered dashboard at `http://127.0.0.1:3000`, checked the API at `http://127.0.0.1:8010`, and reviewed the shell plus these routes:

- `/`
- `/positions`
- `/orders`
- `/reconciliation`
- `/strategies`
- `/portfolio-builder`
- `/news`
- `/research`
- `/news-impact-lab`
- `/predictions`
- `/markets`
- `/models`
- `/risk`
- `/signal-cockpit-demo`

I also used the route smoke receipt at `reports/route-smoke/route-smoke-20260506-211742.json`.

## Environment

- Dashboard: Next.js dev server on `127.0.0.1:3000`
- API: FastAPI on `127.0.0.1:8010`
- Browser path used: Codex Browser Use in-app browser
- Viewports checked: default desktop viewport, `390x844` mobile viewport

## Functional Test Summary

- API smoke passed `8/9` probes. The only failure was `/data/coverage`, which timed out after about `5s`.
- Browser route smoke passed for all listed routes in the sense that each route rendered meaningful content and no framework error overlay appeared.
- Targeted interaction checks completed on:
  - `signal-cockpit-demo`: interval control and raw payload panel
  - `news-impact-lab`: horizon selection plus score action path
  - `reconciliation`: recheck action path
- Strategies route logs a React ref warning originating from `apps/dashboard/src/components/strategies/row-actions.tsx`. This does not blank the page, but it is a real console-quality issue.

## Highest-Value Aesthetic Strengths

The shell has a strong identity. The black hardware-panel base, mono display type, amber action accents, cyan section labels, and green live-status grammar read as a deliberate operating console instead of a generic SaaS dashboard.

The best screens use spatial density well. `signal-cockpit-demo` is the strongest example: it creates real contrast against the current product because the evidence is arranged as an instrument panel, not a stack of cards. It feels specific, directional, and distinct enough to be worth demoing as a serious alternative.

`portfolio-builder` and `news-impact-lab` also show good discipline in the left-control/right-result split. They feel like tools, not landing pages.

## Highest-Value Aesthetic Liabilities

The top shell is over-packed. Between the status ribbon, nav rail, command field, UTC clock, environment badges, and kill/logout actions, the header competes with the page body instead of framing it. On desktop it is dense; on mobile it becomes cramped and visibly clipped.

The system mixes live-looking telemetry with mock or seeded values without enough disclosure. On the Overview screen, the metrics look production-real, but nearby status badges can still show `API OFFLINE` or `WS CLOSED` during otherwise healthy route checks. That undermines trust even when the layout looks polished.

Several routes have strong shell styling but weak first-state balance. `portfolio-builder` opens with a well-structured control stack on the left and a very empty right pane. `news-impact-lab` has the same issue before scoring. The framing is attractive, but the initial visual weight is too left-heavy.

Mobile readiness is not there yet for the shell. At `390x844`, the header wraps badly, nav labels clip, the command area collides with the top rail, and the overall first fold feels cramped.

## Route Notes

### Overview

Strong visual first impression. The KPI cards, sparkline, and feature control grid feel coherent and high-contrast. The main risk is credibility drift: the surface looks live and authoritative even when top-level connectivity badges are inconsistent.

### Positions

Loads cleanly and keeps the shell language consistent. Good density, but the page is visually quieter than the shell around it.

### Orders

Loads cleanly with visible action controls and state filters. Empty-state presentation is acceptable, though it inherits the shell density without adding much hierarchy of its own.

### Reconciliation

The tool framing is good and the action buttons are clear. This is one of the better utilitarian screens because it reads as a task surface instead of a decorative dashboard.

### Strategies

Visually consistent, but the React ref warning needs cleanup. A screen this operational should be console-clean.

### Portfolio Builder

One of the strongest concepts after the signal cockpit. The segmented time horizon and risk controls feel deliberate. The main gap is the large empty output pane before generation; it needs a slightly richer idle state or a smaller visual footprint.

### News

Clean render and refresh action present. The page does not visually separate itself enough from the rest of the shell yet.

### Research

Loads, but during spot checks the shell sometimes reported `API OFFLINE` / `WS CLOSED` even though other routes reported healthy API status. That makes the visual state feel less reliable than the rest of the dashboard.

### News Impact Lab

Good workbench layout. The manual event tester is understandable and appropriately serious. Pre-score, the right pane feels too empty, but the screen has strong potential once populated.

### Predictions

Loads with clear threshold filters. The route reads as an extension of the shell rather than a distinctive analysis surface.

### Markets

Useful control set (`Data autopilot`, `Seed from positions`, `Run demo`) and good fit for the operating-console direction. Empty-state handling is currently more functional than elegant.

### Models

Action model is clear (`Train new model`, `Shadow`, `Promote`). This route has better operational clarity than emotional polish, which is acceptable for its domain.

### Risk

The route lands well visually. The scenario/control naming is strong and the screen feels appropriately high-stakes without becoming noisy.

### Signal Cockpit Demo

This is the best contrast surface in the set. It is materially different from the current connected UI, still feels native to the Fincept visual language, and is the clearest candidate for a future direction demo. The evidence graph, rail layout, and service hatch are the most distinctive UI ideas in the repo right now.

## Contrast Against The Current Surface

If the goal is to test a materially different UI direction without touching the connected product, `signal-cockpit-demo` succeeds. It preserves the Fincept tone but changes the mental model from dashboard-plus-tables to instrument-panel-plus-evidence-graph.

The connected screens still rely more heavily on repeated panels and shell framing. The fused mock is more spatial, more assertive, and better at signaling hierarchy from the first viewport.

## Recommended Next Polish Slice

1. Fix the mobile shell first: header compression, nav overflow, and command-field behavior.
2. Clean the strategies ref warning so the operational routes are console-clean.
3. Normalize status truthfulness so API/WS badges cannot contradict healthy route behavior.
4. Add stronger idle states to right-hand result panes in `portfolio-builder` and `news-impact-lab`.
5. If this direction continues, use `signal-cockpit-demo` as the reference surface for hierarchy, evidence layout, and contrast testing.

## Verification Limits

This was a live local pass, but not a full cross-browser matrix.

I did not verify every service-backed path end to end because the local stack was intentionally lean and `/data/coverage` timed out in API smoke.

The Browser Use runtime was enough for route inspection, screenshots, and targeted interactions, but it was less stable for long multi-route automation loops than a dedicated external Playwright run would be.
