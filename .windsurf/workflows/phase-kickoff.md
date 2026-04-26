---
description: Locate, surface, and paste the appropriate phase-kickoff or per-task block from spec/prompts/PASTE_READY.md based on the next pending task in spec/BUILD_ORDER.md
---

# /phase-kickoff

A guided workflow for sending the right paste block to the coding agent at the right time.

## Step 1 — Determine current state

Read `spec/BUILD_ORDER.md` and identify:

1. The most-recent phase whose checkpoint is `passed` (or "F" if none yet).
2. The next pending task — first `[ ]` (not started) row in dependency order. Call this `NEXT_TASK_ID` (e.g. `TASK-004`).
3. Whether `NEXT_TASK_ID` is the **first** task of a new phase (i.e. the previously-passed checkpoint is one phase behind).

```
// turbo
echo "BUILD_ORDER.md status read."
```

## Step 2 — Decide which block(s) to surface

Branch on the result of Step 1:

- **If a fresh chat session** → surface in this order:
  1. `spec/prompts/SESSION_OPENER.md` (entire file).
  2. The phase kickoff for `NEXT_TASK_ID`'s phase, from `spec/prompts/PASTE_READY.md`, between the matching `▼ PASTE START` and `▲ PASTE END` markers.
  3. The per-task block for `NEXT_TASK_ID` from `spec/prompts/PASTE_READY.md`.

- **If continuing an existing session AND `NEXT_TASK_ID` is the first task of a new phase** → surface:
  1. The phase kickoff for the new phase.
  2. The per-task block for `NEXT_TASK_ID`.

- **If continuing AND `NEXT_TASK_ID` is mid-phase** → surface ONLY:
  1. The per-task block for `NEXT_TASK_ID`.

- **If `NEXT_TASK_ID` is the LAST task of its phase AND it is now `[x]`** → surface the phase exit verification block instead.

## Step 3 — Locate the target block

Search `spec/prompts/PASTE_READY.md` for the heading matching the chosen block:

- Phase kickoffs: `## Phase X — Kickoff` (where X is the phase letter).
- Per-task blocks: `## TASK-NNN — <title>` (use exact NNN match).
- Exit verifications: `## Phase X — Exit verification`.

Each block is delimited by:

```
### ▼ PASTE START
```text
... block content ...
```
### ▲ PASTE END
```

Extract everything between `▼ PASTE START` and `▲ PASTE END` (exclusive of those markers themselves).

## Step 4 — Surface the block to the operator

Print the extracted block(s) to the chat in a fenced code block (preserve exact whitespace; do NOT modify). Prefix with a one-line note saying which task / phase / block this is, e.g.:

```text
[Phase F kickoff — paste once at the start of Phase F]
```

```text
[Per-task block for TASK-004 — paste after the phase kickoff]
```

If the operator requested a specific block by name (e.g. `/phase-kickoff TASK-031`), skip Step 1 and go straight to Step 3 with that ID.

## Step 5 — Confirm next steps

After surfacing the block, briefly remind the operator:

- After the agent acknowledges the kickoff, paste the per-task block.
- After the agent's REPORT comes back green, mark `[x]` in `spec/BUILD_ORDER.md` and re-run `/phase-kickoff` for the next task.
- If the agent reports a STOP CONDITION, address it before continuing.

## Notes

- This workflow does NOT execute the prompts itself; it only assembles + surfaces the right paste blocks for the operator. The coding agent is a separate session.
- The session opener does not need to be re-pasted unless drift occurs or a new chat is started.
- For Phase Z (research frontier), an additional precondition applies: surface a reminder that a whitepaper must exist before any code is requested.
