# Per-Phase Agent Prompts

Drop-in prompts for each implementation phase. Two ways to consume them:

- **`PASTE_READY.md`** — single-file index with EVERY paste block (kickoffs, per-task, exit verifications) for ALL phases. Use this when you want one searchable doc and clear `▼ PASTE START` / `▲ PASTE END` markers around each block.
- **Per-phase files** (`phase-*.md`) — narrative-style, one file per phase. Use this when you want context, design notes, and per-phase landmines woven into the prompts.

Both are kept in sync; per-task prompt content is identical (PASTE_READY is more compact; the per-phase files have a bit more prose). Pick whichever fits your reading style.

## The session opener (paste once per session)

Before ANY phase prompt, paste **`SESSION_OPENER.md`** once at the top of a fresh chat. It establishes the coding-agent norms (12 invariants, edit loop, stop conditions, anti-patterns, output format) that every per-phase / per-task prompt depends on. Without it, you'll see retries, drift, and inconsistent reports.

## How to use (canonical workflow)

```text
operator → coding agent

  [once per session]
  paste spec/prompts/SESSION_OPENER.md
  wait for the agent to acknowledge by listing the 12 norms + 6 stop conditions

  [once per phase]
  paste the phase kickoff (from PASTE_READY.md or phase-X.md)
  wait for the agent to list the phase-specific rules

  [per task, in BUILD_ORDER.md sequence]
  1. paste the per-task prompt (from PASTE_READY.md or phase-X.md)
  2. agent runs the EDIT LOOP from the session opener
  3. agent replies in the REPORT format from the session opener
  4. if verification green, mark [x] in spec/BUILD_ORDER.md

  [once per phase]
  paste the phase exit verification when all tasks are [x]
  agent walks the checklist; you record "Checkpoint X: passed YYYY-MM-DD"
```

The session opener does NOT need to be re-pasted unless you start a fresh chat or the agent appears to have drifted. If drift occurs mid-session, repaste only the **CODING NORMS** + **EDIT LOOP** sections.

## Index

| File | Phase | Tasks covered | Checkpoint |
|---|---|---|---|
| `SESSION_OPENER.md` | (universal) | — | establishes coding-agent norms; paste once per session |
| `PASTE_READY.md` | ALL | TASK-001..089 | single-file index of every paste block |
| `phase-F-foundation.md` | Foundation | TASK-001..006 | F: `make dev` works, CI green |
| `phase-D-data-spine.md` | Data Spine | TASK-010..017 | D: 24-hr soak, <100ms ingestion latency |
| `phase-B-backtesting.md` | Backtesting | TASK-020..024 | B: reference strategy reproduces known Sharpe |
| `phase-A-agents.md` | Agents v1 | TASK-030..033 | A1: GBM ≥52% directional acc., regime labels validated |
| `phase-O-orchestrator-risk-oms.md` | Orchestrator + Risk + OMS | TASK-040..045 | O: end-to-end paper trade auditable |
| `phase-U-ui-api.md` | UI + API | TASK-050..057 | U: operator can sign in, see live P&L, kill switch in <3s |
| `phase-X-cutting-edge.md` | Cutting Edge | TASK-060..066 | X: 4-week shadow ensemble Sharpe ≥ baseline +0.5 |
| `phase-H-hardening.md` | Hardening | TASK-070..076 | H: SOC-2 internal audit + DR drill |
| `phase-Xplus.md` | Profitability Layer | TASK-080..089 | X+: 8-week shadow Sharpe ≥ baseline +0.7, DD ≤ benchmark |
| `phase-Y-differentiation.md` | Differentiation | TASK-090..096 | Y: 12-week multi-regime outperformance, capacity stress |
| `phase-Z-frontier.md` | Research Frontier | TASK-100..104 | Z: per-module whitepaper + reproducible OOS |

> Phases X+, Y, Z extend the alpha layer beyond the original Phase X checkpoint. Their strategic thesis lives in `spec/EDGE_ROADMAP.md`. Phase X+ may run in parallel with Phase H once Phase X has passed.

## Prompt design principles (so agents perform consistently)

1. **Persona first.** Each kickoff opens by setting the agent's role and constraints.
2. **Inputs declared.** Every prompt lists the exact docs to load (`spec/CONTRACTS.md` is always one of them).
3. **Stop conditions.** Every prompt tells the agent when to halt and ask, vs press on.
4. **Verification is a separate prompt.** Don't conflate "implement" with "verify" — agents skip verification when bundled.
5. **Phase-specific landmines surfaced explicitly.** E.g., Phase D's "use Decimal not float" or Phase O's "singleton via leader election" are repeated in the prompts to prevent class-of-bug regressions.

## When the agent isn't following the prompt

If the model produces code that violates a contract or skips a test:

> "STOP. Re-read `spec/CONTRACTS.md §<N>` and the 'Done when' checklist of `TASK-XXX`. List every divergence between your output and the contract. Then redo the task without changing the contract."

If the model hallucinates a dependency or import:

> "STOP. List every import in your output. For each, identify which `spec/LAYOUT.md` entry or `spec/CONTRACTS.md` section provides it. Any import without a source must be removed or sourced from PyPI with the version added to the relevant `pyproject.toml`."

If the model proposes architectural changes:

> "Out of scope. Refactors and new modules require a `docs/DECISIONS.md` ADR and a `spec/LAYOUT.md` update first. Implement to match the existing contract; flag the concern in your final message instead of acting on it."
