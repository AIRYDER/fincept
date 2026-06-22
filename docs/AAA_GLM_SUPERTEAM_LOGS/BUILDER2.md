# Builder 2 (GLM) — Work Log

**Agent:** Builder 2 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Quant Foundry durability layer (outbox + inbox)

---

## Task Adoption Log

### TASK-0304: Implement Durable Local Job Outbox and Callback Inbox — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22 (commit `48c0c27`)
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

### TASK-0304 — COMPLETED 2026-06-22 (commit `48c0c27`)

**What shipped:**
- `services/quant_foundry/src/quant_foundry/outbox.py` — `JobOutbox`, `JobStatus` (8 states), `OutboxRecord` (frozen Pydantic, extra='forbid'). Append-only JSONL at `<base_dir>/outbox.jsonl` with fsync. `enqueue` idempotent on `(job_id, payload_hash)`; rejects same `job_id` + different hash as security event (ValueError). `update_status` appends history entries (status, ts_ns, optional runpod_*/error/note). `get` / `list(status=)` / `receipt`. Restart-safe via JSONL replay (last line per job_id wins).
- `services/quant_foundry/src/quant_foundry/inbox.py` — `CallbackInbox`, `CallbackStatus` (5 states), `InboxRecord` (frozen, extra='forbid'). Append-only JSONL at `<base_dir>/inbox.jsonl`. `receive` idempotent on `(job_id, payload_hash)` → DUPLICATE status, no duplicate effects; rejects same `job_id` + different hash as security event. `get_by_job_id` / `get` / `mark_processed` (sets `processed_at_ns`) / `list(status=)`. Restart-safe.
- `reports/quant-foundry/.gitkeep` — artifact dir reserved per spec.

**Verification:**
- `uv run pytest services/quant_foundry/tests/test_outbox.py services/quant_foundry/tests/test_inbox.py -q` → 11 passed.
- `uv run pytest services/quant_foundry/tests -q` → 69 passed (no regressions; TASK-0301/0302/0303/0401 all still green).
- `uv run ruff check outbox.py inbox.py` → All checks passed.
- `uv run mypy outbox.py inbox.py` → Success: no issues found in 2 source files.

**Design notes for downstream tasks (TASK-0305 mock dispatcher):**
- `JobOutbox.enqueue` accepts `request_payload` as `bytes | str | dict | list | None` (dict/list canonicalized via `json.dumps(sort_keys=True).encode()`). The mock dispatcher should pass the serialized `RunPodTrainingRequest` / `RunPodInferenceRequest` payload here.
- `JobOutbox.update_status` is permissive on transitions (dispatcher drives); terminal states `COMPLETED` / `FAILED` are sticky.
- `CallbackInbox.receive` records `signature_valid` but does NOT enforce it — the signature layer (`signatures.verify_callback`) is the gate; the inbox just audits the verdict. TASK-0305 should verify before calling `receive`.
- Payload ref (`request_payload_ref` / `payload_ref`) fields exist but are left `None` in MVP (hash is the dedup key). If TASK-0305+ needs payload retrieval, write payload bytes to `<base_dir>/payloads/<job_id>.<hash>` and set the ref — extension point only, not required by current tests.
- JSONL is append-only and unbounded; a compaction/rotation step is a future hardening task (not in TASK-0304 scope).

**File-disjoint confirmation (post-commit):**
- Builder 1 shipped TASK-0401 in parallel (`855f01b`: `settlement.py`, `outcomes.py`, `metrics.py`, `test_settlement.py`) — zero overlap with my files.
- `schemas.py`, `ids.py`, `signatures.py` untouched by me (consumed `hash_payload` only).

**Next:** TASK-0304 unblocks TASK-0305 (mock dispatcher + callback processor). Available for adoption if no other builder has claimed it.

---

### TASK-0305: Add Mock Dispatcher and Mock Callback Processor — ADOPTED 2026-06-22

**Status:** COMPLETED 2026-06-22
**Order:** 20
**Depends on:** TASK-0304 (✅ DONE by me — outbox + inbox)

**Why this task:**
- It is the direct downstream consumer of my TASK-0304 outbox/inbox. I have
  the deepest context on the durability layer's API and invariants.
- Unblocked now that TASK-0304 is complete. No other builder has claimed it.
- File-disjoint from ALL active builders:
  - Builder 3 (TASK-0402) owns `shadow_ledger.py` / `test_shadow_ledger.py`.
    Per spec ("shadow-only ledger stub"), I use a local stub, NOT Builder 3's
    real ledger — zero overlap, and the stub is explicitly spec-sanctioned.
  - Builder 4 (TASK-0405) owns `feature_lake.py` / `dataset_manifest.py` /
    `feature_availability.py` / `test_feature_lake.py`.
  - Builder 5 (TASK-0203) owns `services/api/src/api/routes/modules.py`,
    dashboard system page, `scripts/modules/`.
  - Builder 1 (TASK-0401, DONE) owns `settlement.py` / `outcomes.py` /
    `metrics.py`.

**Files owned (file-disjoint):**
- `services/quant_foundry/src/quant_foundry/mock_dispatcher.py` (to create)
- `services/quant_foundry/src/quant_foundry/callbacks.py` (to create)
- `services/quant_foundry/tests/test_mock_flow.py` (to create)

**Files consumed read-only (NOT modified):**
- `outbox.py` / `inbox.py` (mine, TASK-0304) — consume API only.
- `schemas.py` (TASK-0302) — `RunPodTrainingRequest`, `RunPodInferenceRequest`,
  `RunPodCallbackEnvelope`, `ModelDossier`, `ArtifactManifest`,
  `ShadowPrediction`, `Authority`, `JobType`.
- `signatures.py` (TASK-0303) — `sign_callback`, `verify_callback`.
- `ids.py` (TASK-0303) — `hash_payload`, `make_idempotency_key`.

**Plan (TDD):**
1. Write failing tests in `test_mock_flow.py` covering:
   - Mock training job completes through the real contract
     (enqueue -> dispatch -> process -> outbox COMPLETED, inbox PROCESSED,
     dossier stored in stub).
   - Mock inference job stores shadow predictions in the shadow-only stub
     ledger; authority is shadow-only; no `sig.predict` write path exists.
   - Bad signature callback fails closed (inbox signature_valid=False ->
     processor rejects, outbox FAILED, no domain effect).
   - Invalid schema callback rejected (payload doesn't validate against
     RunPodCallbackEnvelope -> REJECTED).
   - Duplicate callback is idempotent (process same job twice -> no
     duplicate dossier/predictions).
   - Terminal job failure (dispatcher simulates failure -> outbox FAILED,
     error recorded).
   - Negative: no bus producer / no `sig.predict` writer attribute on
     dispatcher or processor (hard invariant).
2. Implement `mock_dispatcher.py`:
   - `MockDispatcher(outbox, inbox, callback_secret, base_dir)`.
   - `dispatch(job_id, request_payload)`:
     - verify request payload hash matches outbox record (tamper check).
     - parse request via `RunPodTrainingRequest` / `RunPodInferenceRequest`.
     - transition outbox DISPATCHING -> DISPATCHED -> RUNNING.
     - deterministic mock work (artifact_id / dossier metrics derived from
       payload hash; shadow predictions derived from request symbols).
     - build `RunPodCallbackEnvelope` result, serialize, write to
       `<base_dir>/payloads/<job_id>.json`, sign via `sign_callback`.
     - `inbox.receive(..., signature_valid=True, payload=bytes,
       payload_ref=path)`.
     - transition outbox CALLBACK_RECEIVED.
     - return receipt.
   - `dispatch_failure(job_id, error_code, error_summary)` for terminal
     failure simulation.
3. Implement `callbacks.py`:
   - `ShadowLedgerStub` — in-memory + optional JSONL append; `store(predictions)`,
     `list()`. NO bus producer, NO `sig.predict` writer (hard invariant + test).
   - `DossierStub` — in-memory store for training results.
   - `CallbackProcessor(outbox, inbox, callback_secret, shadow_ledger,
     dossier_store)`:
     - `process(job_id)`:
       - load inbox record; if already PROCESSED -> idempotent skip.
       - if `signature_valid` is False -> mark REJECTED, transition outbox
         FAILED, return (fail closed, no domain effect).
       - read payload bytes from `payload_ref`, verify hash matches
         `inbox.payload_hash` (tamper check).
       - validate against `RunPodCallbackEnvelope` (Pydantic, extra='forbid').
       - dispatch on `result_type`: training_complete -> store dossier;
         inference_batch -> store shadow predictions in stub (assert
         authority shadow-only).
       - `inbox.mark_processed(PROCESSED)`, transition outbox
         VALIDATING -> COMPLETED.
       - return receipt.
4. Run `uv run pytest services/quant_foundry/tests/test_mock_flow.py -q` green;
   `ruff check` + `mypy` clean on touched files.
5. Atomic commit.

---

## Completion Log (continued)

### TASK-0305 — COMPLETED 2026-06-22

**What shipped:**
- `services/quant_foundry/src/quant_foundry/mock_dispatcher.py` — `MockDispatcher`
  (outbox, inbox, callback_secret, base_dir). `dispatch(job_id, request_payload)`:
  tamper-checks the request payload hash against the outbox record, parses via
  `RunPodTrainingRequest` / `RunPodInferenceRequest`, drives outbox transitions
  (DISPATCHING -> DISPATCHED -> RUNNING -> CALLBACK_RECEIVED), simulates
  deterministic work (artifact_id / dossier metrics / shadow predictions derived
  from the payload hash), builds a `RunPodCallbackEnvelope`, durably writes the
  payload to `<base_dir>/payloads/<safe_name>.json` (job_id sanitized for
  Windows-safe filenames), signs via `sign_callback`, and records the callback
  in the inbox with `signature_valid=True` + `payload_ref`. `dispatch_failure`
  transitions the outbox to FAILED with error metadata and writes NO callback.
- `services/quant_foundry/src/quant_foundry/callbacks.py` — `CallbackProcessor`
  (outbox, inbox, callback_secret, shadow_ledger, dossier_store). `process(job_id)`:
  idempotent skip if already PROCESSED; fail-closed on bad signature (REJECTED +
  outbox FAILED, no domain effect); tamper-check on payload bytes vs inbox hash;
  schema-validates against `RunPodCallbackEnvelope` (rejects invalid schema);
  cross-job replay guard (envelope.job_id must match); applies domain effect by
  result_type (training_complete -> DossierStub; inference_batch -> ShadowLedgerStub
  with shadow-only authority assertion); marks PROCESSED + outbox VALIDATING ->
  COMPLETED. `ShadowLedgerStub` + `DossierStub` are in-process stubs with NO bus
  producer / NO `sig.predict` writer (hard invariant + negative test).
- `services/quant_foundry/tests/test_mock_flow.py` — 9 tests covering: module
  imports, no-trading-stream-writer negative invariant, training happy path,
  inference -> shadow stub happy path, bad signature fail-closed, invalid schema
  rejected, duplicate callback idempotent, terminal job failure, tampered request
  payload rejected.

**Verification:**
- `uv run pytest services/quant_foundry/tests/test_mock_flow.py -q` → 9 passed.
- `uv run pytest services/quant_foundry/tests -q` → 96 passed (no regressions;
  TASK-0301/0302/0303/0304/0401 all still green).
- `uv run ruff check mock_dispatcher.py callbacks.py test_mock_flow.py` → All checks passed.
- `uv run mypy mock_dispatcher.py callbacks.py` → Success: no issues found in 2 source files.

**Design notes for downstream tasks (TASK-0306 API route):**
- The mock loop is: `outbox.enqueue` -> `MockDispatcher.dispatch` -> `CallbackProcessor.process`.
  TASK-0306's FastAPI router should wrap these three calls behind
  `POST /quant-foundry/jobs` (enqueue), a dispatcher tick (dispatch), and
  `POST /quant-foundry/callbacks/runpod` (inbox.receive + processor).
- `MockDispatcher.dispatch` requires the operator to pass the same `request_payload`
  that was enqueued (it tamper-checks the hash). In the API route, the enqueue
  handler should stash the payload so the dispatcher tick can replay it, OR
  TASK-0306 should extend the outbox to store the payload ref (extension point
  noted in TASK-0304 completion log). For `local_mock` mode, stashing in-memory
  is acceptable.
- `CallbackProcessor.process` is idempotent and fail-closed — safe to call
  repeatedly from a retry loop.
- The `ShadowLedgerStub` is intentionally NOT `ShadowLedger` (TASK-0402, Builder 3).
  When TASK-0402 lands, the processor can swap to the real ledger by injecting it
  in place of the stub (same `store(predictions)` / `list()` surface). The stub's
  shadow-only authority assertion mirrors the real ledger's invariant.
- Cross-job replay guard: `CallbackProcessor` rejects a callback whose
  `envelope.job_id` != inbox `job_id`. This is defense-in-depth on top of the
  signature's job_id binding (TASK-0303).

**File-disjoint confirmation:**
- Builder 3 (TASK-0402, in flight) owns `shadow_ledger.py` / `test_shadow_ledger.py`
  — I used a stub, zero overlap. (Confirmed via BUILDER3_TASK-0402_yield.md:
  Builder 1 yielded TASK-0402 to Builder 3; Builder 3's design includes the
  no-sig.predict invariant my stub also enforces.)
- Builder 4 (TASK-0405, in flight) owns `feature_lake.py` / `dataset_manifest.py`
  / `feature_availability.py` / `test_feature_lake.py` — no overlap.
- Builder 5 (TASK-0203, in flight) owns `services/api/src/api/routes/modules.py`
  / dashboard system page / `scripts/modules/` — no overlap.
- `schemas.py`, `ids.py`, `signatures.py`, `outbox.py`, `inbox.py` consumed
  read-only (not modified).

**Next:** TASK-0305 unblocks TASK-0306 (Quant Foundry API route in local mock
mode). Available for adoption if no other builder has claimed it.
