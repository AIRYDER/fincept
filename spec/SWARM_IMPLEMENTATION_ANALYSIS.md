# Swarm Implementation Analysis — `fincept-terminal/spec/` and the Broader Codebase

> **Author:** Devin (analysis pass)
> **Date:** 2026-06-26
> **Scope:** How the `spec/` directory and every subdirectory was laid out and implemented via swarms.
> **Branch analyzed:** `codex/portfolio-optimizer-core`

---

## Table of Contents

1. [Executive summary](#1-executive-summary)
2. [The two-layer swarm design](#2-the-two-layer-swarm-design)
3. [Layer 1 — the spec-driven single-agent paste loop](#3-layer-1--the-spec-driven-single-agent-paste-loop)
4. [Layer 2 — the real multi-agent swarm (Quant Foundry)](#4-layer-2--the-real-multi-agent-swarm-quant-foundry)
5. [How the layout maps to the swarm](#5-how-the-layout-maps-to-the-swarm)
6. [The swarm coordination protocol](#6-the-swarm-coordination-protocol)
7. [Agent-to-agent messaging and conflict resolution](#7-agent-to-agent-messaging-and-conflict-resolution)
8. [What the swarm actually built vs. the spec](#8-what-the-swarm-actually-built-vs-the-spec)
9. [Hard safety invariants enforced by the swarm](#9-hard-safety-invariants-enforced-by-the-swarm)
10. [Phase map and builder roster](#10-phase-map-and-builder-roster)
11. [Verification and gating discipline](#11-verification-and-gating-discipline)
12. [Observations and anti-patterns avoided](#12-observations-and-anti-patterns-avoided)
13. [Source index](#13-source-index)

---

## 1. Executive summary

The `fincept-terminal` repository was built using **two stacked swarm patterns**:

- **Layer 1 (the blueprint):** The `spec/` directory is a *contract-first orchestration protocol for a human driving a single coding agent at a time*. It is not itself a multi-agent swarm spec — it is the discipline that keeps one agent on rails across ~68 atomic tasks. Contracts are immutable, layout is authoritative, tasks are atomic, and verification is a separate prompt from implementation.

- **Layer 2 (the execution):** When the work scaled beyond the original spec into the **Quant Foundry** (the candidate-model lifecycle in `services/quant_foundry/`, which is *not* in the original `LAYOUT.md`), the same discipline was lifted into a *real multi-agent swarm*: six GLM-5.2 builder agents working file-disjoint tracks in parallel, coordinating via a swarm board and agent-to-agent yield messages, each running the same TDD + atomic-commit + log-update discipline.

The contracts and layout map are the shared substrate that makes both layers work. Agents never need to share code — they only conform to the contract.

---

## 2. The two-layer swarm design

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — SPEC-DRIVEN PASTE LOOP (single agent, human-driven)      │
│                                                                     │
│  spec/CONTRACTS.md  ──┐                                             │
│  spec/LAYOUT.md    ──┤── operator pastes ──▶ 1 coding agent         │
│  spec/ARCHITECTURE.md─┤    per-task block     runs PLAN→IMPL→       │
│  spec/BUILD_ORDER.md──┤                      VERIFY→REPORT loop     │
│  spec/PROMPTS.md    ──┤                                             │
│  spec/prompts/*     ──┘                                             │
│  spec/tasks/TASK-*.md                                               │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  │  work scales beyond spec
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 2 — MULTI-AGENT SWARM (Quant Foundry, 6 GLM-5.2 builders)    │
│                                                                     │
│  docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER{1..6}.md  ── per-agent logs    │
│  docs/AAA_GLM_SUPERTEAM_LOGS/AGENT_TO_AGENT_MESSAGING/  ── yields   │
│  docs/NEXT_STEPS_PLAN.md  ── task scope + ownership                 │
│  AAA_MULTI_AGENT_6_25_plan.md  ── Track A/B/C parallel plan         │
│  SWARM_BOARD.md  ── file-disjoint zone claims                       │
│                                                                     │
│  Track A (Settlement)   ┐                                           │
│  Track B (Tournament)   ├─ parallel, file-disjoint, TDD per agent   │
│  Track C (Paper Bridge) ┘   (C waits for A+B)                       │
└─────────────────────────────────────────────────────────────────────┘
```

Both layers share the same invariants: contracts immutable, layout authoritative, TDD, atomic commits, hard safety gates enforced by negative tests.

---

## 3. Layer 1 — the spec-driven single-agent paste loop

The `spec/` directory is a self-contained protocol. Its components, all in `spec/`:

### 3.1 The immutable contract — `spec/CONTRACTS.md`

The single source of truth for every event, enum, schema, and interface. Pinned field names, types, defaults. Version 1.0.0; any change requires a version bump and migration note. Tasks say "copy verbatim from §N" — they never redefine types. This is what makes parallel work possible: agents share the contract, not the code.

### 3.2 The authoritative layout — `spec/LAYOUT.md`

The complete file-tree map of the entire repo. The rule is explicit and enforced:

> *"If a file's purpose isn't described here, it should not exist in the repo. PR that adds a file must update this doc first."*

Creating a path outside this map is a **stop-condition** in the session opener. This is the swarm's work-partition contract — every box in the architecture diagram is a file-disjoint zone a builder can own.

The layout declares:
- **5 shared Python libs** (`libs/fincept-core`, `-bus`, `-db`, `-tools`, `-sdk`) — no network I/O.
- **11 deployable Python services** (`services/ingestor`, `features`, `agents`, `orchestrator`, `risk`, `oms`, `portfolio`, `api`, `backtester`, `jobs`, plus `agents` containing 8 sub-agents).
- **1 Next.js dashboard** (`apps/dashboard`).
- **~180–220 Python files, ~40–60 TS files at MVP.**

### 3.3 The one-page architecture — `spec/ARCHITECTURE.md`

The data-flow diagram plus a **hard module-boundary table**:

| Layer | Owns | Never does |
|---|---|---|
| ingestor | Raw venue data → normalized events | Compute features, make decisions |
| features | Deterministic transforms, store reads/writes | Call external APIs, make trading decisions |
| agents | Predictions, signals, LLM calls, tool use | Direct order submission, mutate portfolio state |
| orchestrator | Combine agent outputs, allocate capital | Train models, ingest data |
| risk | Approve/reject/reshape decisions; kill switch | Generate ideas or execute |
| oms | Order state, fill simulation / venue routing | Compute features or train models |
| portfolio | Positions, P&L, attribution | Trade |
| api | Expose read models over HTTP/WS | Mutate trading state outside explicit control endpoints |
| ui | Render read models, issue control commands | Business logic |

> *"If you find yourself crossing a boundary, you're doing it wrong. Fix the layering before you fix the code."*

Three Redis Streams (`md.*` market data, `sig.*` signals, `ord.*` orders) are the **inter-service bus** — services communicate only via typed events, never by direct calls. This is what makes file-disjoint parallel work possible.

### 3.4 The phased DAG — `spec/BUILD_ORDER.md`

The sequenced task graph with **checkpoints as hard gates**:

| Phase | Theme | Tasks | Status |
|---|---|---|---|
| F | Foundation (monorepo, libs, CI) | 001–006 | `[x]` complete |
| D | Data Spine (ingestor, features, PIT) | 010–017 | `[x]` complete |
| B | Backtesting (engine, costs, broker) | 020–024 | mostly `[x]` |
| A | Agents v1 (GBM, regime, pairs) | 030–033 | `[x]` partial |
| O | Orchestrator + Risk + OMS | 040–045 | `[x]` mostly |
| U | UI + API | 050–057 | `[x]` complete |
| X | Cutting Edge (LLM, foundation models, RL) | 060–066 | partial |
| H | Hardening (chaos, mTLS, HSM, archival) | 070–076 | mostly `[ ]` |
| X+ | Profitability Layer | 080–089 | `[ ]` |
| Y | Differentiation | 090–096 | `[ ]` |
| Z | Research Frontier | 100–104 | `[ ]` |

You do not advance a phase until its checkpoint passes. Example checkpoints:
- **F:** `make dev` works, `pytest libs/` green, CI green on a PR.
- **D:** 24-hour soak on 5 crypto pairs, zero dropped messages, feature store <10ms p99.
- **O:** end-to-end paper trade auditable from `ord.*` streams.
- **X:** 4-week shadow ensemble Sharpe ≥ baseline +0.5, p<0.05.

### 3.5 The paste-ready prompts — `spec/PROMPTS.md` + `spec/prompts/`

The actual operator workflow:

1. **Paste `SESSION_OPENER.md` once per session** — establishes 12 coding norms, 6 stop conditions, the PLAN→IMPLEMENT→VERIFY→REPORT edit loop, and the report format. The agent must acknowledge by restating the norms before any task.
2. **Paste a phase kickoff once per phase** — phase-specific landmines (e.g., Phase D's "use Decimal not float," Phase O's "singleton via leader election").
3. **Paste one per-task block at a time** — the agent runs the edit loop and replies in the report format.
4. **Paste the phase-exit verification** when all tasks are `[x]` — the agent walks the checklist; you record "Checkpoint X: passed YYYY-MM-DD."

Key prompt-design principles (from `spec/prompts/README.md`):
1. **Persona first** — each kickoff sets the agent's role and constraints.
2. **Inputs declared** — every prompt lists the exact docs to load (`CONTRACTS.md` always one).
3. **Stop conditions** — every prompt tells the agent when to halt and ask.
4. **Verification is a separate prompt** — don't conflate "implement" with "verify."
5. **Phase-specific landmines surfaced explicitly** to prevent class-of-bug regressions.

### 3.6 The atomic task specs — `spec/tasks/`

Each task is bounded: **≤400 lines of contract + tests, ≤6 files, explicit "out of scope," tests gate completion.** Authored tasks (TASK-001…061) follow a template:

```
# TASK-XXX · <title>
Phase: <F|D|B|A|O|U|X|H> · Depends on: TASK-YYY · Blocks: TASK-ZZZ
## Goal           — one sentence
## Files to create — exact paths from LAYOUT.md
## Contracts      — "Copy verbatim from §N" or inline signatures
## Implementation notes — 5–10 bullets, libraries + pitfalls
## Tests (MUST pass) — minimal pytest stubs
## Out of scope   — anything belonging in a sibling task
## Done when      — files exist, pytest green, mypy/ruff clean
```

The `spec/tasks/README.md` index tracks authored vs. to-author tasks. To-author tasks are generated on demand using the template in `PROMPTS.md`.

### 3.7 The 12 coding norms (from SESSION_OPENER.md)

These apply to every task, every phase:

1. Contracts are immutable.
2. Layout is authoritative.
3. Small atomic edits.
4. Test-first or test-with.
5. Reuse > create.
6. No comments unless asked.
7. No emojis unless asked.
8. No cosmetic reformatting.
9. Never delete or weaken tests without permission.
10. Precision for money (`Decimal`, never `float`).
11. Determinism (bit-identical given input + seed + code-version).
12. Structured logging only (structlog JSON, never `print()`).

### 3.8 The 6 stop conditions

The agent halts and asks when:
- A contract appears wrong or insufficient.
- The task needs a path not in `LAYOUT.md`.
- A test fails for a reason unrelated to the current task.
- The same tool/command fails twice with the same mode.
- A third-party dependency not already in `pyproject.toml`/`package.json` is needed.
- The task spec contradicts `CONTRACTS.md` (contracts win).

---

## 4. Layer 2 — the real multi-agent swarm (Quant Foundry)

When the work moved beyond the original spec into the **Quant Foundry** — the candidate-model lifecycle (settlement → tournament → promotion → paper bridge) in `services/quant_foundry/` — the execution shifted to a real multi-agent swarm. This directory is *not* in the original `LAYOUT.md`; it was added by the swarm as a major extension.

### 4.1 The swarm composition

**Six GLM-5.2 builder agents** worked between 2026-06-22 and 2026-06-23, plus a handoff author. Evidence lives in `docs/AAA_GLM_SUPERTEAM_LOGS/`:

| Agent | Track | Work log | Key commits |
|---|---|---|---|
| Builder 1 (GLM-5.2) | Evidence-loop foundations: settlement ledger, metrics, outcomes | `BUILDER1_GLM.md` (37 KB) | `855f01b` |
| Builder 2 (GLM-5.2) | Durability layer + gateway: outbox, inbox, mock dispatcher, callbacks | `BUILDER2.md` (38 KB) | `48c0c27` |
| Builder 3 (GLM-5.2) | Dossier, tournament, RunPod containers, Phases 5-7 + 10 | `BUILDER3.md` (90 KB — largest) | `de56c38`, `fd3f115`, `df326d4`, `e95c51f`, `a88e8c2`, etc. |
| Builder 4 (GLM-5.2) | Feature lake + shadow ledger (Phase 4) | `BUILDER4.md` (15 KB) | `7f704bd` |
| Builder 5 (GLM-5.2) | Operator experience (on-demand module control) | `BUILDER5.md` (7 KB) | (in BUILDER5.md) |
| Builder 6 (GLM-5.2) | Remaining tasks: causal graph, dashboard pages, readiness review | `BUILDER6.md` (14 KB) | `808e7ab`, `8f3a589`, `7027db4`, `4233e64` |

Builder 3 was the heaviest contributor — it drove Phases 5, 6, 7, and most of 10 (RunPod training, shadow inference, tournament, promotion, paper bridge, MoE router, conformal gate, drift sentinel).

### 4.2 The parallel track plan

The `AAA_MULTI_AGENT_6_25_plan.md` document defines three parallel tracks with an explicit dependency graph:

```
Track A (Settlement)          Track B (Tournament/Promotion)     Track C (Paper Bridge)
─────────────────────         ──────────────────────────────     ─────────────────────────
A1: Market data adapter       B1: Tournament sweep               (waits for A + B)
A2: Settlement sweep          B2: Gateway tournament wiring
A3: Gateway settlement wiring B3: Promotion POST endpoints
A4: Settlement integration    B4: Dashboard promotion wiring
        │                            │
        └──────────┬─────────────────┘
                   ▼
            C1: Paper bridge proof
            C2: End-to-end demonstration
```

**Parallelism rules:**
- A1 and B1 start in parallel immediately (no dependency on each other).
- B1 can start in parallel with A1 because tournament sweep code is written against the settlement ledger *interface*; B1's tests use fixture settlement records.
- A3 and B2 run in parallel — they edit different methods in `gateway.py`.
- B3 and B4 are sequential (dashboard wiring depends on POST endpoints).
- C1 starts after A4 and B4 complete (needs a promoted model with real settlement evidence).

### 4.3 The Quant Foundry module inventory

The swarm produced 38 source modules in `services/quant_foundry/src/quant_foundry/`, organized by concern:

**Evidence loop (Phases 3-4):**
- `gateway.py` (19,450 bytes) — the facade the API talks to; owns outbox, inbox, dispatcher, callback processor, dossier registry, leaderboard, promotion queue, shadow ledger, budget guard.
- `outbox.py` / `inbox.py` — durable JSONL job outbox + callback inbox; idempotent on `(job_id, payload_hash)`; reject same job_id + different hash as security event.
- `signatures.py` / `ids.py` — HMAC-SHA256 callback signing + deterministic hashing.
- `schemas.py` — cross-boundary Pydantic models (`RunPodTrainingRequest`, `ShadowPrediction`, `ModelDossier`, `Authority`); all `extra="forbid"`, frozen.
- `settlement.py` / `outcomes.py` / `metrics.py` — settlement ledger with look-ahead guard, idempotent reruns, versioned cost model, Brier score, calibration buckets, abnormal return.
- `shadow_ledger.py` / `shadow_settlement.py` / `shadow_inference.py` — durable shadow prediction storage; `authority=shadow-only` is structural.
- `dossier.py` / `registry.py` / `artifacts.py` — model dossier with full reproducibility pins (feature_schema_hash, code_git_sha, container_image_digest, random_seed, hardware class).

**Scoring (Phases 4, 7):**
- `tournament.py` (24,610 bytes) — `ScoringInput` schema, `TournamentResult`, stationary/block bootstrap p-value, deflated Sharpe ratio.
- `leaderboard.py` / `leaderboard_expanded.py` — ranking with horizon/regime/cluster slices, decay indicators, explanations.
- `significance.py` / `pbo.py` — bootstrap significance + probability of backtest overfitting.
- `sentinel.py` (30,568 bytes — largest module) — leakage/overfit sentinel for the promotion gate.
- `promotion.py` — `PromotionReviewQueue`, `PromotionGate.evaluate()` (4 fail-closed checks).
- `retirement.py` — retirement / edge-decay flags.

**Routing + gating (Phase 10):**
- `moe_router.py` — mixture-of-experts model router.
- `conformal_gate.py` — conformal prediction risk gate.
- `drift_sentinel.py` — adversarial drift sentinel.
- `causal_graph.py` — **research only**, no live data, no `sig.predict`, no order fields.

**Bridge + infra:**
- `paper_bridge.py` — refuses unless `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`; creates rollback pointer before publish; circuit breaker trips after 5 failures.
- `budget.py` — `BudgetGuard` fail-closed; per-job budget + monthly ceiling + kill switch.
- `callbacks.py` / `mock_dispatcher.py` / `runpod_client.py` / `runpod_training.py` — dispatch + callback processing.
- `dataset_manifest.py` / `feature_lake.py` / `feature_availability.py` / `feature_snapshot_export.py` — PIT-proof feature lake with embargo ≥ max label horizon, as-of universe (no survivorship bias).

### 4.4 The RunPod boundary

The system has a hard trust boundary between the Fincept side and the RunPod GPU side:

```
FINCEPT (non-RunPod)                RUNPOD (containers)
────────────────────                ──────────────────
gateway                             quant-foundry-training/handler.py
  ├─ JobOutbox (JSONL)              quant-foundry-inference/handler.py
  ├─ CallbackInbox                    │
  ├─ MockDispatcher                   │  Returns HMAC-signed
  ├─ CallbackProcessor  ◀─────────────┘  RunPodCallbackEnvelope
  ├─ DossierRegistry
  ├─ ShadowLedger (real, durable)
  └─ BudgetGuard

  Only crossing: POST /quant-foundry/callbacks/runpod (HMAC-signed)
  RunPod invariant: NO broker creds, NO Redis, NO sig.predict, NO order fields.
  Only QUANT_FOUNDRY_CALLBACK_SECRET (HMAC) is consumed.
```

---

## 5. How the layout maps to the swarm

The `spec/LAYOUT.md` tree is the **swarm's work-partition contract**. Every box in the architecture diagram is a service; every service is a file-disjoint zone a builder can own:

```
libs/     → fincept-core, -bus, -db, -tools, -sdk   (shared primitives, no network I/O)
services/ → ingestor, features, agents, orchestrator, risk, oms,
            portfolio, api, backtester, jobs         (+ quant_foundry, strategy_host,
                                                      settlements added by the swarm)
apps/     → dashboard (Next.js)
```

The three Redis Streams (`md.*`, `sig.*`, `ord.*`) are the **inter-agent/inter-service bus**. Services communicate only via typed events defined in `CONTRACTS.md`, never by direct calls. This is what makes file-disjoint parallel work possible: agents don't need to see each other's code, only the contract.

The swarm extended the layout with three services not in the original `LAYOUT.md`:
- `services/quant_foundry/` — the candidate-model lifecycle (38 modules).
- `services/strategy_host/` — strategy hosting.
- `services/settlements/` — settlement service.

These additions followed the same contract-first discipline but were planned in `docs/NEXT_STEPS_PLAN.md` rather than `spec/LAYOUT.md`.

---

## 6. The swarm coordination protocol

The swarm coordination rules that emerged (codified in `HANDOFF_REMAINDER_GLM52.md`):

### 6.1 File-disjoint zones

Each builder claims files on a `SWARM_BOARD.md`. Before editing, you confirm no other in-flight builder owns the file. This is the parallelism mechanism — agents don't share files, they share contracts. Example from Builder 2's log:

> *"Files owned (file-disjoint from active tasks): `outbox.py`, `inbox.py`. TASK-0401 (Builder 1, in flight) owns `settlement.py`, `outcomes.py`, `metrics.py` — no overlap. `schemas.py` is intentionally NOT touched by me."*

### 6.2 TDD per agent

Failing test first, then implement, then `uv run pytest` + `ruff` + `mypy` clean. Builder 2's log shows the pattern: *"TDD starting state: Failing tests already committed in `d7dcaf4`. Both fail at import. Plan: implement `outbox.py`, implement `inbox.py`, run pytest green, ruff/mypy clean, atomic commit."*

### 6.3 Atomic commits per task

One commit per task, message style:
```
feat(<area>): TASK-XXXX <summary> (TDD, file-disjoint)
Co-Authoried-By: ...
```

### 6.4 Tracking docs updated in the same session

Completion is logged in **both** the plan doc (`docs/NEXT_STEPS_PLAN.md` — add a `> **Owner:** ... COMPLETED 2026-MM-DD (commit ...)` blockquote under the task header) **and** the builder's personal `BUILDER*.md` log with: what shipped, verification output, design notes for downstream, file-disjoint confirmation.

### 6.5 Never stage unrelated changes

`docs/NEXT_STEPS_PLAN.md` may carry another builder's uncommitted edits — use `git add -p` to stage only your hunks.

### 6.6 The handoff prompt

When a builder session ends, it writes a self-contained handoff prompt for the next builder (`HANDOFF_REMAINDER_GLM52.md`). This codifies the discipline so the next agent doesn't need to re-derive it. The handoff includes: role, repo + environment, non-negotiable builder discipline, the remaining work in recommended order, and definition of done per task.

---

## 7. Agent-to-agent messaging and conflict resolution

The swarm has an explicit agent-to-agent messaging channel in `docs/AAA_GLM_SUPERTEAM_LOGS/AGENT_TO_AGENT_MESSAGING/`. Two patterns appear:

### 7.1 Yield on collision

When two agents claim the same task, the one whose design is less spec-aligned **yields and documents why**. Example (`BUILDER3_TASK-0404_yield.md`):

> *"Builder 3, I detected a collision: we both adopted TASK-0404. I had marked ownership on `SWARM_BOARD.md` and written my own `test_tournament.py` + `significance.py` + `tournament.py` + `leaderboard.py`. By the time I went to run the tests, your `test_tournament.py` had overwritten mine on disk. **Resolution — I yield TASK-0404 to you.** Your design is more aligned with the spec: your local `ScoringInput` schema is exactly what the spec asks for; `TournamentStatus.STALE` as a separate state is cleaner; `PromotionRecommendation` enum is the explicit promotion-packet signal; `score_components` as a list of named components is more auditable than my dict approach."*

The yielding agent then:
1. Deleted its own uncommitted scratch (never committed, safe to remove).
2. Confirmed the other agent's file on disk is intact (did NOT overwrite it).
3. Documented the design rationale so the winning design is understood.

### 7.2 Surface pre-existing failures to the file owner

When a builder's new CI check surfaces a pre-existing failure in a file owned by another builder, the discoverer **does not fix it** — they message the owner. Example (`Answered_BUILDER5_startup_safety_matrix_failure.md`):

> *"Builder 5, while implementing TASK-0104 (CI hardening), I added a new required CI job `startup-safety-matrix`. This surfaces a pre-existing failure in `services/api/src/api/main.py` — which you own (TASK-0203). The fix is a one-line addition: `assert_safe_for_runtime()`. This is a safety-critical guard (audit R4/P3): without it, a non-dev deployment with the default dev JWT secret would silently start up and accept forged tokens."*

This preserves file-ownership discipline even for one-line fixes.

---

## 8. What the swarm actually built vs. the spec

Comparing `BUILD_ORDER.md` status markers against the real tree:

| Phase | Spec status | Real tree | Notes |
|---|---|---|---|
| F (Foundation) | `[x]` | `libs/fincept-core`, `-bus`, `-db`, `-tools`, `-sdk` all exist | Spec-driven paste loop drove this |
| D (Data Spine) | `[x]` | `services/ingestor`, `features` exist | Binance, Coinbase, Kraken adapters shipped |
| B (Backtesting) | mostly `[x]` | `services/backtester` exists | TASK-023 (walk-forward) still `[ ]` |
| A (Agents v1) | `[x]` partial | `services/agents/gbm_predictor`, `regime`, `pairs` exist | Pairs agent incomplete |
| O (Orchestrator + Risk + OMS) | `[x]` mostly | `services/orchestrator`, `risk`, `oms`, `portfolio` exist | Kelly + VaR still `[ ]` |
| U (UI + API) | `[x]` | `services/api`, `apps/dashboard` exist | Full Next.js dashboard with command palette |
| X (Cutting Edge) | partial | `services/agents/llm_sentiment`, `ts_foundation` exist | Memory, event_miner, RL execution, research `[ ]` |
| H (Hardening) | mostly `[ ]` | only TASK-075a (Alpaca paper-broker) `[x]` | Chaos, mTLS, HSM, archival remain |
| X+ / Y / Z | `[ ]` | not started | Profitability / differentiation / frontier layers |

**Beyond the spec — the swarm's major addition:**

`services/quant_foundry/` (38 modules) — the candidate-model lifecycle. This is where the multi-agent swarm pattern was used most heavily, with 6 builders working in parallel tracks. It is **not** in the original `LAYOUT.md`; it was planned in `docs/NEXT_STEPS_PLAN.md` and executed via the Track A/B/C parallel plan.

The readiness verdict (`docs/LIMITED_LIVE_READINESS_REVIEW.md`, commit `7027db4`) is **NOT READY** with 8 specific blockers (B1-B8). No real RunPod GPU has run. No model has been promoted. Live trading is disabled by default.

---

## 9. Hard safety invariants enforced by the swarm

These are enforced by existing code and negative tests, and must not be broken by any agent (from `AAA_MULTI_AGENT_6_25_plan.md`):

1. No RunPod worker gets broker credentials.
2. No RunPod worker writes to `ord.orders`, `ord.decisions`, `ord.fills`, `ord.positions`, or `sig.predict`.
3. All callbacks are HMAC-signed and verified. Fail-closed on bad signature.
4. `ModelDossier` always carries `authority=SHADOW_ONLY`. Promotion to live is human-gated.
5. Paper bridge is disabled by default. `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true` required.
6. Paper bridge refuses non-paper runtime. `runtime_mode` must be `"paper"`.
7. Paper bridge refuses models without evidence packet. Dossier + tournament result + sentinel receipt required.
8. Paper bridge refuses models not `paper-approved`. Promotion gate must have approved.
9. Circuit breaker trips after 5 failures. Blocks further publishes until reset.
10. Rollback pointer created before publishing. Operator can always roll back.
11. OMS and risk services remain authoritative. Paper bridge only produces `PaperPrediction` — no order fields.
12. Settlement uses only post-decision prices. Look-ahead guard enforced in `metrics.py`.
13. Settlement is idempotent. Same `(prediction_id, cost_model_version)` returns existing record.
14. Tournament is advisory. No automatic promotion. Human approval required.
15. Promotion gate fails closed. Missing evidence → REJECTED, not APPROVED.
16. MVP promotion limit: `shadow_approved` max. `paper_approved` requires explicit level unlock.
17. Secrets never in source, logs, receipts, or dashboard responses.
18. No new dependencies without package manager command (`uv add` / `pnpm add`).

Plus the spec-level invariants from the session opener:
- All timestamps are `int` nanoseconds (`ts_ns`).
- All cross-system IDs are ULIDs (sortable, 26-char base32).
- Stream names live in `fincept_bus.streams` constants — never hardcoded.
- Every price/size/fee/balance/P&L is `decimal.Decimal` (Python) or `string` (TS) — never `float`.
- Backtests are bit-identical given (input, seed, code-version).
- Structured logging only (structlog JSON + correlation IDs from contextvars).

---

## 10. Phase map and builder roster

### 10.1 Quant Foundry phase map (from `docs/NEXT_STEPS_PLAN.md`)

| Phase | Theme | Tasks | Builder | Key commits |
|---|---|---|---|---|
| 3 | Durability layer | 0301–0306 (ids, signatures, schemas, outbox, inbox, mock dispatcher, gateway) | 1, 2, 3 | `48c0c27` |
| 4 | Evidence loop foundations | 0401 (settlement), 0402 (shadow ledger), 0403 (dossier), 0404 (tournament), 0405 (feature lake), 0406 (leakage sentinel) | 1, 3, 4 | `855f01b`, `de56c38`, `fd3f115`, `7f704bd`, `d864b94` |
| 5 | RunPod training | 0501–0504 (container MVP, dispatch client, artifact import, first baseline) | 3 | `b3fc4e1`, `ae893a6`, `caeb468` |
| 6 | RunPod shadow inference | 0601–0603 (container MVP, feature snapshot export, shadow settlement) | 3 | `df326d4`, `1a91a82`, `0aa4aef` |
| 7 | Tournament & promotion | 0701–0704 (expanded leaderboard, promotion queue, retirement, paper bridge) | 3 | `0831e2c`, `60f9e61`, `ffe9ce7`, `e95c51f` |
| 8 | Operator experience | 0801 (QF overview dashboard) | 3 | (in BUILDER3.md) |
| 9 | Deployment & cost | 0901 (BudgetGuard), 0902–0903 (design only) | 3 | `6256cdf` |
| 10 | Research + gating | 1001 (MoE router), 1002 (causal graph), 1003 (conformal gate), 1004 (drift sentinel) | 3, 6 | `a88e8c2`, `808e7ab`, `e272b6e`, `22700a7` |
| post-10 | Remaining | 0604 (shadow health), 0802 (dashboard pages), 1101 (readiness review) | 6 | `4233e64`, `8f3a589`, `7027db4` |

### 10.2 Session ID conventions (from `.omo/boulder.json`)

- Builders 1–5 are NOT in the boulder — they completed before the orchestrator (Atlas) picked up the remaining-tasks plan. Their evidence lives only in their `.md` work logs and `git log --all` commits.
- Builder 6 sessions are tracked: `opencode:ses_10d...` for orchestrator Atlas, plus deep/writing category sessions per task.
- Final-wave F1–F4 oracle sessions (plan compliance, code quality, real manual QA, scope fidelity) reviewed the completed work.

---

## 11. Verification and gating discipline

### 11.1 Per-task verification (Layer 1)

From `PROMPTS.md` — run before considering ANY task done:
```bash
uv run ruff check libs services
uv run mypy libs services
uv run pytest libs services
```
If all three are green AND the task's "Done when" checklist is complete, mark `[x]` in `BUILD_ORDER.md`. Otherwise, the task is not done — even if the model claims it is.

Windows shortcut: `scripts/task-check.ps1 -PackagePath <package> -PytestPath <task-test-path>` wraps pytest + ruff + mypy.

### 11.2 Per-agent verification (Layer 2)

Each builder runs, per task:
```powershell
$env:UV_CACHE_DIR = (Get-Location).Path + '\.uv-cache'
uv run --package quant-foundry pytest services/quant_foundry/tests/test_<module>.py -q
uv run ruff check <files>
uv run mypy <files>
```

Builder 2's verification output (typical):
- `uv run pytest ... test_outbox.py test_inbox.py -q` → 11 passed.
- `uv run pytest services/quant_foundry/tests -q` → 69 passed (no regressions).
- `uv run ruff check outbox.py inbox.py` → All checks passed.
- `uv run mypy outbox.py inbox.py` → Success: no issues found.

### 11.3 Phase-exit checkpoints

Each phase has a hard gate. The phase-exit verification prompt makes the agent walk the full checklist before the phase is declared complete. Example (Phase F):
1. `make dev` brings up the full local stack.
2. `uv run pytest libs/` runs ≥4 library test suites, exits 0.
3. `uv run mypy --strict libs/*/src` exits 0.
4. `uv run ruff check .` exits 0.
5. `pre-commit run --all-files` exits 0.
6. CI workflow on a real PR exits green.
7. All TASK-*.md docs exist.
8. Mark TASK-001..006 as `[x]` in `BUILD_ORDER.md`.

If all green: declare Phase F COMPLETE, add "Checkpoint F: passed YYYY-MM-DD". If any red: do NOT advance.

### 11.4 Final-wave oracle review (Layer 2)

After the Quant Foundry remaining-tasks plan completed (14/14 tasks, 6h 28m 25s), four oracle sessions reviewed the work:
- **F1 oracle** — plan compliance.
- **F2 oracle** — code quality.
- **F3 oracle** — real manual QA.
- **F4 oracle** — scope fidelity.

---

## 12. Observations and anti-patterns avoided

### 12.1 What the swarm design prevents

The session opener lists explicit anti-patterns the agents must avoid:
- Random refactors of files near your edit.
- Adding new top-level directories without a layout update.
- Adding helper scripts named `scratch.py` / `debug.sh` / `temp.*`.
- Catching exceptions broadly (`except Exception`) without re-raising.
- Creating `.env` / `.pem` files or anything containing secrets.
- Hard-coding API keys, exchange URLs, or config values.
- Inventing a new event type, contract, or schema.
- Adding `if TYPE_CHECKING` guards as a workaround for circular imports — fix the cycle instead.
- Skipping mypy errors with `# type: ignore` without an issue link.
- Using `from X import *`.
- Creating "temporary" tables or columns (schema changes go through alembic migrations).

### 12.2 What makes the swarm work

1. **Contracts as the shared substrate.** Agents never need to share code — they conform to the contract. This is what enables file-disjoint parallelism.
2. **Layout as the partition contract.** Every file has one owner; creating a path outside the map is a stop-condition.
3. **TDD as the verification gate.** Tests are part of the deliverable, not optional polish. Failing test first, then implement.
4. **Atomic commits with trailers.** One commit per task, `Co-Authored-By:` trailers, `(TDD, file-disjoint)` tags make the history auditable.
5. **Yield-on-collision conflict resolution.** When two agents collide, the less-spec-aligned design yields and documents why — no merge conflicts, no silent overwrites.
6. **Hard safety invariants enforced by negative tests.** The invariants list (§9) is enforced by tests that must keep passing. Agents cannot weaken them.
7. **Handoff prompts codify the discipline.** When a session ends, the next builder gets a self-contained prompt that restates the discipline — no re-derivation needed.
8. **Phase checkpoints as hard gates.** You don't advance until the checkpoint passes. This prevents drift from compounding across phases.
9. **Verification is a separate prompt from implementation.** Agents skip verification when it's bundled with implementation.
10. **Phase-specific landmines surfaced explicitly.** "Use Decimal not float" (Phase D), "singleton via leader election" (Phase O) are repeated in the prompts to prevent class-of-bug regressions.

### 12.3 Where the swarm diverged from the spec

The Quant Foundry (`services/quant_foundry/`) is a major addition not in `spec/LAYOUT.md`. It was planned in `docs/NEXT_STEPS_PLAN.md` and executed via the Track A/B/C parallel plan. This is the swarm scaling beyond the original blueprint — but it preserved the same discipline (contracts, TDD, atomic commits, file-disjoint zones, hard invariants).

The `spec/LAYOUT.md` rule says "if a file's purpose isn't described here, it should not exist." The Quant Foundry violates this letter but preserves the spirit — the `NEXT_STEPS_PLAN.md` served as the equivalent authoritative layout for the swarm's extension work.

---

## 13. Source index

### 13.1 Spec layer (`spec/`)

| File | Purpose |
|---|---|
| `spec/CONTRACTS.md` | Immutable type/interface source of truth (423 lines) |
| `spec/LAYOUT.md` | Authoritative file-tree map (294 lines) |
| `spec/ARCHITECTURE.md` | One-page data-flow + module-boundary table (101 lines) |
| `spec/BUILD_ORDER.md` | Phased DAG with checkpoint gates (219 lines) |
| `spec/EDGE_ROADMAP.md` | Strategic thesis for Phase X+/Y/Z (148 lines) |
| `spec/PROMPTS.md` | Generic recipe for driving a coding model (132 lines) |
| `spec/prompts/SESSION_OPENER.md` | Universal pre-flight: 12 norms, 6 stop conditions, edit loop (194 lines) |
| `spec/prompts/PASTE_READY.md` | Single-file index of every paste block (2,959 lines) |
| `spec/prompts/README.md` | Index of per-phase prompt files (80 lines) |
| `spec/prompts/phase-*.md` | Per-phase kickoff + per-task + exit verification prompts |
| `spec/tasks/TASK-*.md` | Atomic task specs (TASK-001…061 authored) |
| `spec/tasks/README.md` | Task index + new-task template instructions (75 lines) |

### 13.2 Swarm layer (`docs/` + root)

| File | Purpose |
|---|---|
| `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER{1..6}.md` | Per-builder work logs (6 files) |
| `docs/AAA_GLM_SUPERTEAM_LOGS/AGENT_TO_AGENT_MESSAGING/` | Yield messages + failure notifications |
| `docs/AAA_GLM_SUPERTEAM_LOGS/HANDOFF_REMAINDER_GLM52.md` | Self-contained handoff prompt for next builder |
| `docs/AGENT_SYSTEM_REPORT.md` | Deep system report: roster, phase map, module inventory (655 lines) |
| `docs/NEXT_STEPS_PLAN.md` | Source of truth for Quant Foundry task scope + ownership |
| `docs/LIMITED_LIVE_READINESS_REVIEW.md` | Formal NOT-READY verdict + 8 blockers |
| `AAA_MULTI_AGENT_6_25_plan.md` | Track A/B/C parallel multi-agent plan (532 lines) |
| `.omo/boulder.json` | Orchestrator session state (Builder 6 + oracle sessions) |
| `.omo/plans/quant-foundry-remaining-tasks.md` | The plan that drove Builder 6 |
| `.devin/workflows/phase-kickoff.md` | Guided workflow for surfacing the right paste block |

### 13.3 Implementation (`libs/` + `services/` + `apps/`)

| Directory | Contents |
|---|---|
| `libs/fincept-core` | schemas, events, config, clock, ids, errors, logging, tracing, leadership |
| `libs/fincept-bus` | Redis Streams producer, consumer, stream constants |
| `libs/fincept-db` | async SQLAlchemy, ORM, alembic, ticks/bars/audit, PIT joins |
| `libs/fincept-tools` | MCP-style tool protocol, registry, data/analytics/exec tools |
| `libs/fincept-sdk` | Public Python SDK (data, strategy, universe) |
| `services/ingestor` | Binance, Coinbase, Kraken WS adapters + normalizer + writer + quality |
| `services/features` | Online/offline transforms, store, PIT joins |
| `services/agents` | gbm_predictor, regime, pairs, llm_sentiment, ts_foundation, event_miner, execution_rl, research |
| `services/orchestrator` | router, consensus, regime, allocator, decisions |
| `services/risk` | gate, limits, kelly, var, concentration, kill_switch |
| `services/oms` | paper OMS, state machine, audit, venue adapters (Alpaca) |
| `services/portfolio` | positions, pnl, attribution |
| `services/api` | FastAPI HTTP + WebSocket, auth, routes |
| `services/backtester` | engine, broker, costs, datasource, report, walk_forward |
| `services/jobs` | nightly_retrain, daily_eod_load, weekly_report, compaction |
| `services/quant_foundry` | 38 modules: settlement, tournament, promotion, paper_bridge, RunPod, MoE, conformal, drift, causal |
| `services/strategy_host` | Strategy hosting |
| `services/settlements` | Settlement service |
| `apps/dashboard` | Next.js 16 UI with chart, table, risk-panel, command-palette |

---

*End of report. Generated 2026-06-26 from analysis of `fincept-terminal/spec/` and the broader codebase.*
