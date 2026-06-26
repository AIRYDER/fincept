# Release Hygiene & Working-Tree Inventory

> Source: `AAAAAAAAA_BIG_PLAN.md` TASK-0001.
> Purpose: keep a clear map of what is changed in the working tree so
> implementation commits never accidentally stage unrelated work or
> local tool state.

## Staging discipline

- Never use broad `git add -A` / `git add .` while the tree is dirty.
- Stage files explicitly per task: `git add <path1> <path2> ...`.
- Run `git status --short` before every commit and confirm only the
  intended files are staged.
- Run `git diff --check` before committing to catch whitespace errors.

## Working-tree categories

When reviewing `git status --short`, group entries into:

1. **Product code** — service/library/dashboard source changes.
2. **Tests** — anything under `*/tests/` or `*.test.*`.
3. **Docs** — `docs/`, `featuresmenu.md`, `*/docs/`.
4. **Generated reports** — receipts under `reports/` (mostly gitignored).
5. **Local tool state** — must be gitignored, never staged.
6. **Unknown / needs human review** — surface before staging.

## Confirmed local-only tool state (gitignored)

These directories are written by local agent/tooling sessions and must
never be committed. Ignore rules live in `.gitignore` under
"Local agent/tool working state":

- `.devin/dialectic-repo/` — dialectic skill thinking logs.
- `.devin/thinking-logs/` — thinking-logger skill output.
- `.opencode/` — opencode tooling runtime (node_modules, tmp).
- `.playwright-cli/` — playwright CLI session logs.
- `.worktrees/` — local git worktrees.

`.devin/` itself is intentionally **partially tracked**: shared config
such as `.devin/workflows/` and `.devin/skills/` is committed, while the
runtime-state subdirectories above are ignored.

## Pre-existing dirty files (operator in-progress work)

As of TASK-0001, the tree contains in-progress operator work across the
dashboard, core libs, API, ingestor, jobs, oms, and docs. These are not
part of the Phase 0 safety tasks. Do not broad-stage them. Each later
task stages only its own files and leaves unrelated dirty files alone.
