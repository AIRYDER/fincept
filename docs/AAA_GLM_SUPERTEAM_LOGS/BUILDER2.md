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

---

### TASK-0306: Add Quant Foundry API Route in Local Mock Mode — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22
**Order:** 21
**Depends on:** TASK-0305 (✅ DONE by me — mock dispatcher + callback processor)

**Why this task:**
- Direct downstream consumer of my TASK-0305 mock loop. I have the deepest
  context on the gateway/dispatcher/processor API.
- Unblocked by TASK-0305. No other builder had claimed it.
- File-disjoint from all active builders:
  - Builder 3 (TASK-0402 DONE, TASK-0403 DONE, TASK-0404 in flight) owns
    `shadow_ledger.py`, `dossier_registry.py`, `tournament.py`.
  - Builder 4 (TASK-0405 DONE) owns `feature_lake.py` etc.
  - Builder 5 (TASK-0203 in flight) owns `services/api/src/api/routes/modules.py`,
    dashboard system page. Shared file: `main.py` — I add a separate
    import + include_router block, no overlap with their modules router.
  - Builder 1 (TASK-0401 DONE, TASK-0104 DONE) owns settlement + CI.

**Files owned:**
- `services/quant_foundry/src/quant_foundry/gateway.py` (created)
- `services/api/src/api/routes/quant_foundry.py` (created)
- `services/api/tests/test_quant_foundry.py` (created)
- `services/api/src/api/main.py` (additive: 1 import + 1 include_router + comment)
- `services/api/pyproject.toml` (additive: quant-foundry workspace dep)

**What shipped:**
- `gateway.py` — `QuantFoundryGateway` facade wiring outbox + inbox +
  MockDispatcher + CallbackProcessor + ShadowLedgerStub + DossierStub.
  Config from env (`QUANT_FOUNDRY_ENABLED`, `QUANT_FOUNDRY_MODE`,
  `QUANT_FOUNDRY_SHADOW_ONLY`, `QUANT_FOUNDRY_CALLBACK_SECRET`,
  `QUANT_FOUNDRY_BASE_DIR`) — no edit to shared `fincept_core/config.py`.
  `create_job` runs the full local_mock loop synchronously (enqueue ->
  dispatch -> process). `receive_callback` verifies HMAC signature FIRST
  (fail-closed, no inbox record on bad sig), then records in inbox (catches
  diff-hash security rejection gracefully), then processes. `health` /
  `heartbeats` / `list_jobs` / `get_job` for operator read endpoints.
- `routes/quant_foundry.py` — 6 endpoints: POST/GET /jobs, GET /jobs/{id},
  POST /callbacks/runpod, GET /health, GET /heartbeats. Operator endpoints
  require bearer JWT (`require_user`). Callback endpoint uses HMAC headers
  (X-QF-Job-Id, X-QF-Timestamp, X-QF-Signature) — NOT bearer. Missing
  headers -> 401; bad sig -> 401; unknown job -> 404; payload hash mismatch
  -> 400. No bus / sig.predict writes.
- `test_quant_foundry.py` — 13 tests: disabled safe state, disabled job
  creation, auth required, create+complete in local_mock, get job detail,
  unknown job 404, list jobs, bad signature rejected, missing HMAC headers
  rejected, unknown job callback rejected, duplicate callback idempotent,
  health enabled, heartbeats enabled.

**Verification:**
- `uv run pytest services/api/tests/test_quant_foundry.py -q` → 13 passed.
- `uv run pytest services/api/tests/test_quant_foundry.py services/quant_foundry/tests/test_outbox.py services/quant_foundry/tests/test_inbox.py services/quant_foundry/tests/test_mock_flow.py services/quant_foundry/tests/test_schemas.py services/quant_foundry/tests/test_signatures.py services/quant_foundry/tests/test_ids.py -q` → 64 passed.
- `uv run pytest services/api/tests/test_health.py services/api/tests/test_auth.py -q` → 9 passed (no regressions from main.py edit).
- `uv run ruff check` → All checks passed.
- `uv run mypy gateway.py routes/quant_foundry.py` → Success: no issues found in 2 source files.

**Design notes:**
- The gateway is stashed on `app.state.quant_foundry_gateway` by the app
  lifespan or a test fixture. When absent, operator endpoints return 503
  (disabled) and health returns a safe disabled state. The route is always
  registered (so 404 doesn't hide the surface).
- `receive_callback` verifies the HMAC signature BEFORE touching the inbox.
  This ensures a bad signature never creates a durable record (fail-closed,
  no side effect). The inbox's diff-hash guard is a second layer of defense
  — if a validly-signed callback arrives with a different payload than a
  previous one, the gateway catches the ValueError and returns a clean 400
  (no crash).
- In `local_mock` mode, `create_job` runs the full loop synchronously
  (enqueue -> dispatch -> process). This proves the contract end-to-end in
  a single HTTP call. The future RunPod path would enqueue only and rely on
  a dispatcher tick + the callback endpoint.
- The `ShadowLedgerStub` is still a stub (not Builder 3's real
  `ShadowLedger`). When the promotion gate wiring lands, the gateway can
  swap to the real ledger by injecting it in place of the stub.

**File-disjoint confirmation:**
- Builder 3's in-flight `test_tournament.py` (TASK-0404) imports
  `quant_foundry.tournament` which doesn't exist yet — I excluded it from
  my test runs (it's their TDD red state, not my regression).
- `main.py` shared with Builder 5 — my edit is a separate import + router
  block, no overlap with their modules router.
- `schemas.py`, `ids.py`, `signatures.py`, `outbox.py`, `inbox.py`,
  `mock_dispatcher.py`, `callbacks.py` consumed read-only (not modified).
- `fincept_core/config.py` NOT modified (config read from env directly).

**Next:** TASK-0306 unblocks the Phase 3 gateway surface. The Quant Foundry
loop is now provable end-to-end over HTTP in local_mock mode. Next
candidates: TASK-0307 (RunPod dispatcher — when RunPod is available) or
Phase 4 evidence-loop tasks.

---

### TASK-0501: Build RunPod Training Container MVP — ADOPTED + COMPLETED 2026-06-22

**Status:** COMPLETED 2026-06-22
**Order:** 27
**Depends on:** TASK-0403 (✅ DONE — dossier registry), TASK-0405 (✅ DONE — feature lake)

**Why this task:**
- Unblocked (both deps DONE). Unclaimed by any other builder.
- In my quant_foundry domain — I own the contract pieces (schemas, signatures,
  outbox, inbox, mock dispatcher) that the RunPod handler must use.
- File-disjoint from ALL active builders:
  - Builder 3 (TASK-0404 IN PROGRESS) owns `tournament.py` / `test_tournament.py`.
  - Builder 4 (TASK-0402 IN PROGRESS) owns `shadow_ledger.py` (second adoption).
  - Builder 5 (TASK-0203 IN PROGRESS) owns `modules.py` / dashboard.
  - Builder 1 (TASK-0104 IN PROGRESS) owns `.github/workflows/`.
- All files are new: `runpod_training.py`, `test_runpod_training.py`,
  `runpod/quant-foundry-training/*`.

**Plan (TDD):**
1. Write failing tests in `test_runpod_training.py` covering:
   - Local handler accepts RunPodTrainingRequest, produces ArtifactManifest +
     ModelDossier + training receipt + signed callback envelope.
   - Artifact manifest is hash-verifiable (same inputs -> same artifact_id).
   - No broker credentials / Redis / stream access (hard invariant + test).
   - Training failure returns a safe terminal status (not a crash).
   - Time/budget limit enforcement (timeout -> terminal failure).
   - Handler uses the same schemas/signatures as the mock dispatcher.
2. Implement `runpod_training.py`:
   - `RunPodTrainingHandler` — accepts a RunPodTrainingRequest, reads a
     dataset manifest ref, trains a tiny baseline (deterministic from seed),
     writes ArtifactManifest + ModelDossier, builds a RunPodCallbackEnvelope,
     signs it, returns the callback payload + signature.
   - `LocalTrainer` — CPU-only deterministic trainer (sklearn-free; uses
     simple statistics or a stub model). No GPU dependency.
   - Time/budget enforcement via a deadline check.
3. Create `runpod/quant-foundry-training/`:
   - `handler.py` — RunPod entrypoint that calls RunPodTrainingHandler.
   - `Dockerfile` — minimal Python container (no broker creds, no Redis).
   - `README.md` — build + run instructions.
4. Run `uv run pytest services/quant_foundry/tests -q -k runpod_training` green;
   ruff + mypy clean.
5. Atomic commit.

**What shipped:**
- `runpod_training.py` — `RunPodTrainingHandler` + `LocalTrainer` +
  `TrainingFailure` + `TrainingResult`. The handler accepts a
  RunPodTrainingRequest, trains a tiny deterministic baseline (CPU-only,
  no sklearn/GPU), writes ArtifactManifest + ModelDossier, builds a
  RunPodCallbackEnvelope, signs it with the same `sign_callback` as the
  mock dispatcher, and returns the callback payload + signature.
  Deterministic: same inputs -> same artifact_id/sha256. Shadow-only
  authority. Time/budget enforced (deadline_seconds, `>=` check so 0s
  fails immediately). TrainingFailure = safe terminal status (error_code
  + error_summary), not a raw crash.
- `runpod/quant-foundry-training/handler.py` — RunPod serverless
  entrypoint. Parses `event["input"]` into RunPodTrainingRequest, invokes
  the handler, returns the signed callback (or error dict on failure).
  Reads `QUANT_FOUNDRY_CALLBACK_SECRET` + `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS`
  from env. No broker/Redis/stream access.
- `runpod/quant-foundry-training/Dockerfile` — minimal python:3.12-slim
  container. No broker creds, no Redis. Only the callback secret is
  injected at runtime.
- `runpod/quant-foundry-training/README.md` — build + run instructions,
  security boundary, contract, env vars, reproducibility pins.
- `test_runpod_training.py` — 8 tests: imports, no broker credentials,
  happy path (signed callback + dossier + artifact), hash verifiability
  (same inputs -> same artifact_id), different seed -> different artifact,
  training failure -> safe terminal, time limit enforced, same contract
  as mock dispatcher.

**Verification:**
- `uv run pytest services/quant_foundry/tests/test_runpod_training.py -q` → 8 passed.
- `uv run pytest services/quant_foundry/tests/test_runpod_training.py services/quant_foundry/tests/test_outbox.py services/quant_foundry/tests/test_inbox.py services/quant_foundry/tests/test_mock_flow.py services/quant_foundry/tests/test_schemas.py services/quant_foundry/tests/test_signatures.py services/quant_foundry/tests/test_ids.py services/api/tests/test_quant_foundry.py -q` → 72 passed.
- `uv run ruff check` → All checks passed.
- `uv run mypy runpod_training.py handler.py` → Success: no issues found in 2 source files.

**Design notes:**
- The handler is a pure function over its inputs — no I/O, no subprocess,
  no network. The RunPod entrypoint (handler.py) is the only thing that
  reads env vars; the handler itself takes the secret as a constructor
  arg. This makes the handler unit-testable without env manipulation.
- The `LocalTrainer` produces a deterministic stub model whose artifact
  hash is `sha256(canonical_json(request_inputs))`. This proves the
  contract end-to-end without ML deps. The future real trainer would
  swap in here without changing the handler/callback contract.
- The deadline check uses `>=` (not `>`) so a 0-second deadline fails
  immediately — this is the test case for the timeout path.
- The handler has NO `redis`, `broker`, `bus`, `producer`, `stream`,
  `sig_predict_writer`, `order_writer`, or trading attributes. This is
  enforced by a test that iterates over a known denylist.

**File-disjoint confirmation:**
- All files are new — no overlap with any active builder.
- `schemas.py`, `signatures.py`, `ids.py` consumed read-only.
- No edit to `main.py`, `config.py`, or any shared file.

**Next:** TASK-0501 unblocks TASK-0502 (RunPod Job Dispatch Client) and
TASK-0503 (Artifact Import From Object Storage). The RunPod training
contract is now provable end-to-end locally; the next step is wiring the
dispatcher to actually send jobs to RunPod (TASK-0502) and pull artifacts
back (TASK-0503).
