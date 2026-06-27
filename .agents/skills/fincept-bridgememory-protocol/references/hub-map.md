# Fincept Terminal — BridgeMemory Hub Map

Inventory of `.bridgememory/` as of 2026-06-26. Use this as the entry point when MCP tools are unavailable (see `fallback.md`).

## Top-level / hub navigation

- `[[Fincept Evidence Hub — ml-dataset-evidence-spine]]` — directory of all 50 files in `.omo/evidence/`
- `[[Session Context — ml-dataset-evidence-spine]]` — architecture narrative (longest evidence file)
- `[[In-Depth Review — ml-dataset-evidence-spine]]` — verdict + audit rollup + 12-item findings table
- `[[Outstanding Findings Table]]` — 12 open items (1 HIGH, 4 MED, 7 LOW), with severity/area/owner
- `[[Update Protocol for Evidence Hub]]` — how to maintain the evidence hub on new evidence or closed findings

## Specialty audits (6)

- `[[Audit — Architecture]]` — 0 architectural flaw, 4 concerns, 3 PASS. Circular-import verified 3 ways.
- `[[Audit — Code Style]]` — Ruff PASS, mypy PASS core / FAIL new services (6 errors)
- `[[Audit — Data Integrity]]` — 1 bug (`SettlementStore._find` returns first match)
- `[[Audit — Scope Fidelity]]` — all 11 "Must NOT have" guardrails held
- `[[Audit — Security]]` — 1 HIGH (TOCTOU), 2 MED, 2 LOW, 7 PASS
- `[[Audit — Test Quality]]` — 194 tests in scope, 2031 total, 0 failed

## Task reports (21)

Grouped by phase:

### Phase 1 — Core Primitives

- `[[Task 1 — ApprovedRoots module]]` — fail-closed filesystem gate
- `[[Task 2 — DatasetManifest + FeatureSnapshot schemas]]`
- `[[Task 3 — SettlementStore + SettlementRecord]]` — append-only JSONL ledger
- `[[Task 4 — schema/serializer work]]` — text-only evidence
- `[[Task 5 — FeatureSnapshotStore module]]`

### Phase 2 — CV Math + Dossier

- `[[Task 6 — cv.py]]` — `make_folds`, `derive_walk_forward_window`
- `[[Task 7 — dossier.py]]` — ECE, Brier, calibration
- `[[Task 8 — datasets facade]]` — re-export facade (bundled with task 6)
- `[[Task 9 — CV test suite]]`

### Phase 3 — Integration + Wiring

- `[[Task 10 — Settlements worker]]`
- `[[Task 11 — Market data bridge]]` — BarDataAdapter → async
- `[[Task 12 — /models/{name}/outcomes route]]`
- `[[Task 13 — approved-roots gate on /train and /backtest]]` — HIGH TOCTOU
- `[[Task 14 — shared exception handler]]`

### Phase 4 — Agent + Backtester Convergence

- `[[Task 15 — FeatureSnapshotStore wiring]]`
- `[[Task 16 — LogReg baseline scaffold]]`
- `[[Task 17 — backtester CV delegation]]`
- `[[Task 18 — quant_foundry CV delegation]]`
- `[[Task 19 — feature-health sidecar]]`
- `[[Task 20 — durable callback-metrics store]]`

### Phase 5 — Proof + Verification

- `[[Task 21 — paper_spine_replay --with-settlement]]` — settlement_hit_rate=1.0, brier=0.0

## Cross-references — other memories in the project

- `[[value_increase.md canonical plan]]` — 60+ task IDs across 15 groups
- `[[Experimental fork of fincept-terminal]]` — sibling repo `fincept-terminal-experimental/`
- `[[Experimental fork pre-work setup]]` — bootstrap protocol

## Stats

- Total memories: 30 (29 spine + 1 welcome)
- Total `[[wikilinks]]`: ~150 across the spine cluster
- Orphans: 0 within the spine cluster (verify with `list_orphans` on next MCP recovery)
- Test count covered: 2031 across 6 packages

## Related

- `../SKILL.md`
- `fallback.md`
- `maintenance-checklist.md`