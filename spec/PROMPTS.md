# Prompts — How to Drive a Coding Model With This Spec

## Two layers

This document covers the **generic recipe** for driving a coding model. For phase-specific prompts (Foundation, Data Spine, Backtesting, Agents, Orchestrator+Risk+OMS, UI+API, Cutting Edge, Hardening), see `spec/prompts/`:

- `spec/prompts/SESSION_OPENER.md` — universal pre-flight; **paste once per session** before any phase prompt.
- `spec/prompts/PASTE_READY.md` — single-file index of every paste-ready block (kickoffs, per-task, exit verifications) across ALL phases.
- `spec/prompts/README.md` — index of all per-phase prompt files + canonical workflow.
- `spec/prompts/phase-F-foundation.md` — TASK-001..006
- `spec/prompts/phase-D-data-spine.md` — TASK-010..017
- `spec/prompts/phase-B-backtesting.md` — TASK-020..024
- `spec/prompts/phase-A-agents.md` — TASK-030..033
- `spec/prompts/phase-O-orchestrator-risk-oms.md` — TASK-040..045
- `spec/prompts/phase-U-ui-api.md` — TASK-050..057
- `spec/prompts/phase-X-cutting-edge.md` — TASK-060..066
- `spec/prompts/phase-H-hardening.md` — TASK-070..076
- `spec/prompts/phase-Xplus.md` — TASK-080..089 (Profitability Layer; see `spec/EDGE_ROADMAP.md`)
- _(planned)_ `spec/prompts/phase-Y-differentiation.md` — TASK-090..096
- _(planned)_ `spec/prompts/phase-Z-frontier.md` — TASK-100..104

Each phase file has: a kickoff prompt to brief the agent on phase-specific landmines, per-task copy-paste prompts, and a phase-exit verification prompt that gates progression.

## The execution recipe (generic)

For each task in `spec/BUILD_ORDER.md`, run this single prompt against your coding model:

```text
You are implementing a task from a contract-first codebase. Your job is to produce code that compiles, type-checks, lints, and passes the embedded tests — nothing more, nothing less.

## Constants you MUST honor

1. The contracts in <paste contents of spec/CONTRACTS.md> are immutable. Do not change field names, types, or defaults. If you believe one is wrong, STOP and report it instead of editing.
2. The repo layout in <paste contents of spec/LAYOUT.md> is authoritative. Do not create files outside the paths listed in the task.
3. Architecture rules in <paste contents of spec/ARCHITECTURE.md> are non-negotiable. Do not cross module boundaries.

## Your task

<paste contents of spec/tasks/TASK-XXX.md>

## Work order

1. Create exactly the files listed under "Files to create".
2. Match every signature in "Contracts" verbatim.
3. Implement the bodies. You may add private helper functions. Do not add public functions not listed in the contract.
4. Write the tests as given. Do not weaken assertions.
5. Run: `uv run pytest <task-test-path>`. If red, fix and re-run.
6. Run: `uv run ruff check <package>` and `uv run mypy <package>`. Fix any violations.
7. Stop. Do not move on to other tasks. Do not modify files outside this task.

Windows shortcut: `powershell -ExecutionPolicy Bypass -File .\scripts\task-check.ps1 -PackagePath <package> -PytestPath <task-test-path>` wraps steps 5-6 without changing the verification bar. Add `-Sync` if the task also needs a fresh workspace sync first.

## What to output

A single message containing the contents of every file you created or changed, with absolute paths. No prose explanation unless I ask.
```

## When the model gets stuck

If a task references something that doesn't exist yet:

1. Check `spec/BUILD_ORDER.md` — is the dependency completed?
2. If not, **stop**. Do not stub it. Instead, complete the dependency first.
3. If yes, the import path is wrong — check `spec/LAYOUT.md`.

If the model proposes changing a contract:

> "STOP. The contract in `CONTRACTS.md §X` is immutable. Either implement to match it, or open a PR against `CONTRACTS.md` first explaining the breaking change. Do not silently deviate."

## Template for a NEW task spec

When a layout entry has no task spec yet, generate one with this template:

```markdown
# TASK-XXX · <title>

**Phase:** <F|D|B|A|O|U|X|H> · **Depends on:** TASK-YYY · **Blocks:** TASK-ZZZ

## Goal
One sentence — what does this file/service do, and what observable effect does it have.

## Files to create
\`\`\`
<exact paths from spec/LAYOUT.md>
\`\`\`

## Contracts (MUST match exactly)
- Either reference a section of `spec/CONTRACTS.md` ("Copy verbatim from §N"), or
- Define new function signatures inline with EXACT types.

## Implementation notes
- Bullet list of HOW. 5–10 items max.
- Mention specific libraries and why.
- Call out any non-obvious correctness pitfall.

## Tests (MUST pass)
\`\`\`python
# minimal pytest stubs that exercise the contract
\`\`\`

## Out of scope
- Anything that belongs in a sibling task.

## Done when
- [ ] Files exist
- [ ] `pytest <path>` green
- [ ] `mypy`, `ruff` clean
- [ ] Manual integration step (if applicable)
```

## How big should a task be?

If a single task spec exceeds **400 lines** of contract + tests, split it. Two healthy splits:

- "Define interfaces + tests (no impl)" + "Implement to match interfaces."
- "Module A" + "Module B that depends on A."

If a task touches more than 6 files, split it. The smaller, the easier for a weaker model.

## Cost-saving tip

For very weak models (e.g., 7B local LLMs), pre-process each task into chunks of one file at a time. Feed `CONTRACTS.md`, the task header, and one file's spec. Iterate per file. Slower but tractable.

## Verification gate (run before considering ANY task done)

```bash
uv run ruff check libs services
uv run mypy libs services
uv run pytest libs services
```

If all three are green AND the task's "Done when" checklist is complete, mark `[x]` in `spec/BUILD_ORDER.md`. Otherwise, the task is not done — even if the model claims it is.
