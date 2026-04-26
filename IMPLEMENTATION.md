# Implementation Structure — How to Build Fincept Terminal With a Simple Coding Model

## Purpose

This directory (`spec/`) is the **executable blueprint**. Every file in the final system has a corresponding atomic task spec here. A weak LLM, a junior developer, or an AI coding agent can be given one task at a time and produce correct, integrated code — because every contract, interface, and acceptance test is pinned down in advance.

## The contract-first principle

Normal dev: "Here's a feature, figure out the code." Result: a strong model is required.

Contract-first: "Here is the exact file path. Here are the imports you may use. Here is the function signature you must match. Here are pytest assertions that must pass. Here is what is out of scope." Result: **a simple model produces correct code** because all ambiguity has been removed upstream.

This works because:

1. `spec/CONTRACTS.md` defines every data type and interface before any implementation.
2. `spec/LAYOUT.md` assigns every responsibility to exactly one file.
3. `spec/BUILD_ORDER.md` sequences tasks so each depends only on completed ones.
4. `spec/tasks/TASK-*.md` contains fully self-contained prompts — no external context required.

## The cutting-edge bet

The system is designed to extract alpha from four edges that cheap retail platforms cannot easily replicate:

| Edge | Mechanism | Module |
|---|---|---|
| **Alternative data** | News + filings + social → LLM sentiment/event extraction | `agents/llm_sentiment`, `agents/event_miner` |
| **Foundation models for forecasting** | TimesFM / Lag-Llama / Moirai for zero-shot multi-horizon | `agents/ts_foundation` |
| **Multi-agent orchestration with tool use** | Agents call typed tools (data, risk, exec) via MCP-style protocol | `orchestrator/` |
| **RL for execution & position sizing** | PPO over order-slice scheduling + Kelly-optimal sizing | `agents/execution_rl`, `risk/kelly` |

Profit does not come from any one model. It comes from (a) uncorrelated strategy diversification, (b) adaptive allocation across regimes, (c) ruthless cost control at execution, and (d) fast iteration cadence from a clean codebase.

## How to use this spec

### As a solo developer with an AI assistant

1. Read `spec/ARCHITECTURE.md` once (10 min).
2. Open `spec/BUILD_ORDER.md`. Pick the next unchecked task.
3. Open its `spec/tasks/TASK-XXX.md`. Paste the whole file into your AI assistant with: "Implement this task exactly. Do not deviate from the contract."
4. Run the pytest in the task spec. If green, check the box in `BUILD_ORDER.md` and move on.
5. If red, fix and re-run. Do not modify the contract.

### As a small team

Same as above, but assign one task per engineer. Because contracts are fixed, tasks are independent and parallelizable within a phase.

### As a coding agent (autonomous)

Feed one task spec as the sole prompt. Tools the agent needs: `read_file`, `write_file`, `run_command` (for pytest). No other context should be needed — if the spec requires more, the spec is wrong and must be fixed.

## Index

| Doc | Purpose |
|---|---|
| `spec/ARCHITECTURE.md` | One-page mental model |
| `spec/LAYOUT.md` | Every file in the repo, one line each |
| `spec/CONTRACTS.md` | All schemas, events, interfaces — as copy-pasteable Python |
| `spec/BUILD_ORDER.md` | Sequenced task list with dependencies + checkpoints |
| `spec/PROMPTS.md` | How to invoke a coding model with a task spec |
| `spec/tasks/TASK-*.md` | Atomic implementation units |

## Non-negotiables (violation breaks the structure)

1. **Never change a contract in `CONTRACTS.md` without a version bump.** Downstream tasks depend on them exactly.
2. **Never skip the test stubs in a task.** They are the acceptance gate.
3. **Never introduce a file that isn't in `LAYOUT.md`.** If you need one, add it to `LAYOUT.md` first in a PR that updates only that doc.
4. **Never merge a task with a forward dependency.** If task 12 needs task 15 done first, reorder `BUILD_ORDER.md` instead.
5. **Never trade real capital before all Phase 5 tasks pass.** The paper → shadow → limited → full rollout in `docs/ROADMAP.md §Gate 5` is the only way.
