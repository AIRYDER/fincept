# Builder 2 (GLM) — Work Log

**Agent:** Builder 2 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry durability layer (outbox + inbox)

---

## Task Adoption Log

### TASK-0304: Implement Durable Local Job Outbox and Callback Inbox — ADOPTED 2026-06-22

**Status:** IN PROGRESS
**Order:** 19
**Depends on:** TASK-0303 (✅ DONE — ids + signatures)
**TDD starting state:** Failing tests already committed in `d7dcaf4`
  - `services/quant_foundry/tests/test_outbox.py`
  - `services/quant_foundry/tests/test_inbox.py`
  Both fail at import (`ModuleNotFoundError: quant_foundry.outbox / .inbox`).

**Files owned (file-disjoint from active tasks):**
- `services/quant_foundry/src/quant_foundry/outbox.py` (to create)
- `services/quant_foundry/src/quant_foundry/inbox.py` (to create)
- `reports/quant-foundry/.gitkeep` (to create)
- (tests already committed by prior TDD step — read-only for me)

**File-disjoint check:**
- TASK-0401 (Builder 1, in flight) owns `settlement.py`, `outcomes.py`, `metrics.py`, `test_settlement.py` — no overlap.
- TASK-0204 (Builder 1 orig) owns `apps/dashboard/src/lib/api.ts` — no overlap.
- `schemas.py` is intentionally NOT touched by me (Builder 1's track uses it for outcome records; I keep outbox/inbox record schemas local to their own modules to keep ownership clean).
- `ids.py` / `signatures.py` are DONE (TASK-0303) — I consume `hash_payload` only, no edits.

**Plan (TDD — make red tests green):**
1. Implement `outbox.py`:
   - `JobStatus` StrEnum: QUEUED, DISPATCHING, DISPATCHED, RUNNING, CALLBACK_RECEIVED, VALIDATING, COMPLETED, FAILED.
   - `OutboxRecord` Pydantic frozen model with all spec fields + `history` list + ns timestamps.
   - `JobOutbox` class: JSONL durability under `base_dir/outbox.jsonl`, append-on-write, reload-on-init.
   - `enqueue`, `update_status`, `get`, `list` (with optional status filter).
   - Security: reject same `job_id` with different `request_payload_hash` (ValueError mentioning "payload hash mismatch"/"security").
   - Idempotent re-enqueue with identical idempotency_key + payload hash returns existing record.
2. Implement `inbox.py`:
   - `CallbackStatus` StrEnum: RECEIVED, DUPLICATE, PROCESSED, REJECTED, FAILED.
   - `InboxRecord` Pydantic frozen model with spec fields + ns timestamps + history.
   - `CallbackInbox` class: JSONL durability under `base_dir/inbox.jsonl`.
   - `receive`, `get_by_job_id`, `mark_processed`.
   - Idempotent duplicate (same job_id + same payload_hash) → status DUPLICATE, no error.
   - Security: same job_id + DIFFERENT payload_hash → ValueError (security event).
3. Run `uv run pytest services/quant_foundry/tests/test_outbox.py services/quant_foundry/tests/test_inbox.py -q` green.
4. `ruff check` + `mypy` clean on touched files.
5. Create `reports/quant-foundry/.gitkeep` (spec-listed artifact dir).
6. Atomic commit.

---

## Completion Log

(pending)
