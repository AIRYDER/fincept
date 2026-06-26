# Core Logic Review — Last Two Days (2026-06-22 → 2026-06-23)

**Branch:** `codex/portfolio-optimizer-core`
**Scope:** Feature commits touching core logic since `9ecb121` (first commit in the window).
**Method:** Read-only review of `services/quant_foundry/`, `libs/fincept-db/`, `libs/fincept-core/`, `services/api/routes/`, `services/oms/alpaca/`, and `apps/dashboard/src/lib/`. No code was modified.
**Diff footprint:** ~133 files, +38,762 / -69 lines. The bulk is the new `services/quant_foundry` package (a shadow-only ML platform) plus provider-evidence redaction, an on-demand module control panel, and dashboard client hardening.

> Verification note: the two findings marked **VERIFIED** below were independently confirmed against the source. One subagent-flagged item (`moe_router.py` null `decay_indicator`) was checked and found to be **already guarded** (`entry.decay_indicator is not None and (...)` at `moe_router.py:230-231`); it is listed under "Dismissed findings" for traceability.

---

## 1. Executive Summary

The two-day change set introduces a large, internally consistent "Quant Foundry" shadow-ML platform and hardens several cross-cutting concerns (provider-evidence redaction, fetch timeouts, module control). The safety posture is generally strong: budget guards fail closed, callback HMAC verification is constant-time with skew checks, shadow-only authority is enforced structurally, and redaction is conservative (fail-closed on unknown field names).

However, there are a handful of issues that should be resolved before any non-dev deployment:

- **Two confirmed runtime bugs** (`MARK_TTL_SEC` AttributeError; timestamp unit mismatch between two redaction-adjacent modules).
- **One stub shipped as a real client** (`HttpRunPodClient.dispatch` raises `NotImplementedError`).
- **One silent budget bypass** (prospective cost defaults to 0 when an attribute is missing).
- **One atomicity gap** (budget reservation is not atomic with job enqueue).
- **One CORS hardcoding** with no production enforcement.
- **One TOCTOU race** on module start/stop.

None of these are security-exploitable in the current default-disabled configuration, but several become live the moment `QUANT_FOUNDRY_ENABLED=true` or the module panel is exposed beyond localhost.

---

## 2. Findings by Severity

### CRITICAL

#### C1. `MARK_TTL_SEC` is not a `Settings` field — `write_mark` will raise `AttributeError` **[VERIFIED]**

`services/oms/src/oms/alpaca/marks.py:50`:

```python
ttl = get_settings().MARK_TTL_SEC or MARK_TTL_SEC
```

`MARK_TTL_SEC` is **not** declared in `libs/fincept-core/src/fincept_core/config.py` `Settings` (confirmed by reading the full class — fields end at `MAX_DAILY_LOSS_USD`). Pydantic `BaseSettings` with `extra="ignore"` only ignores *unknown env inputs*; it does **not** synthesize undefined attributes. Accessing `settings.MARK_TTL_SEC` raises `AttributeError` before the `or` fallback is evaluated.

- **Impact:** `write_mark()` crashes on every call. Mark writes to Redis silently fail (the surrounding `try/except Exception: pass` at lines 58-62 only wraps the *evidence* write, not the TTL line — the `AttributeError` propagates from line 50).
- **Assumption violated:** "Settings has a `MARK_TTL_SEC` field." It does not.
- **Fix:** Either add `MARK_TTL_SEC: int = Field(default=300)` to `Settings`, or use `getattr(get_settings(), "MARK_TTL_SEC", None) or MARK_TTL_SEC`.

#### C2. Timestamp unit mismatch: `provider_receipts.py` expects seconds, `provider_data.py` stores nanoseconds **[VERIFIED]**

- `libs/fincept-db/src/fincept_db/provider_receipts.py:107`: `ts_event: int  # unix seconds`
- `libs/fincept-db/src/fincept_db/provider_data.py:297`: `event_ns = ts_event if ts_event is not None else time.time_ns()` (nanoseconds)

`build_evidence_receipt()` computes `age_sec = ts_recv - ts_event` (line 172). If a caller passes a `ts_event` taken from a `ProviderDataRecord` (nanoseconds, e.g. `1714000000000000000`) alongside a `ts_received` in seconds (`1714000000`), the age is ~`-1.7e9` seconds → classified "degraded" / negative → every receipt is misclassified.

- **Impact:** Freshness receipts are wrong by ~9 orders of magnitude whenever the two modules are wired together naively. The freshness badges shown in the dashboard news panel would be meaningless.
- **Assumption violated:** "Both modules agree on timestamp units." They do not, and there is no unit annotation or runtime guard.
- **Edge case:** Negative age from clock skew is already handled (treated as degraded), which masks this bug — a huge negative age looks like "skewed" rather than "wrong units."
- **Fix:** Pick one unit (nanoseconds is the house standard elsewhere) and document/enforce it on both sides, or add a magnitude sanity check in `build_evidence_receipt` (e.g. reject `ts_event > 1e15` as "looks like nanoseconds, expected seconds").

#### C3. `HttpRunPodClient.dispatch` is a stub that raises `NotImplementedError` **[VERIFIED]**

`services/quant_foundry/src/quant_foundry/runpod_client.py:195-205`: the real HTTP client's `dispatch` raises `NotImplementedError`. Only `MockRunPodClient` is implemented.

- **Impact:** If `QUANT_FOUNDRY_MODE=runpod` is set without the mock, every dispatch crashes. The gateway does not pre-check that the configured client is non-stub.
- **Assumption violated:** "The HTTP client will be implemented before live mode is enabled." There is no guard preventing live mode from selecting the stub.
- **Fix:** Either implement `dispatch`, or have the dispatcher/gateway refuse to start in `runpod` mode when the client is the unimplemented HTTP variant.

### HIGH

#### H1. Prospective cost defaults to 0, silently bypassing the budget guard

`services/quant_foundry/src/quant_foundry/runpod_client.py:273`:

```python
prospective_cost = getattr(self.client, "cost_per_dispatch_cents", 0)
```

If the real `HttpRunPodClient` does not expose `cost_per_dispatch_cents` (and currently it does not — only `MockRunPodClient` does at line 153), prospective cost is `0`, so `budget_guard.check_and_reserve(0)` always passes.

- **Impact:** Unbounded GPU spend the moment the HTTP client is wired in without the attribute. The budget guard's fail-closed semantics are defeated by a missing attribute, not by an explicit override.
- **Risky pattern:** `getattr(..., 0)` as a "safe" default for a *security-relevant* value. Defaults for cost should fail closed (raise), not open (0).
- **Fix:** Require the attribute: `prospective_cost = self.client.cost_per_dispatch_cents` (let `AttributeError` surface) or validate at dispatcher construction.

#### H2. Budget reservation is not atomic with job enqueue

`services/quant_foundry/src/quant_foundry/gateway.py:159-187`: `budget_guard.check_and_reserve()` appends to the spend ledger **before** the job is enqueued to the outbox. If enqueue fails after reservation, the ledger entry persists with no corresponding job.

- **Impact:** Budget can be exhausted by failed enqueues; no rollback path exists. Under repeated enqueue failures this is a budget-DoS.
- **Assumption violated:** "Reserve-then-enqueue is atomic." It is not — the two writes are to different files with no transaction spanning them.
- **Edge case:** Process crash between the two writes leaves a "ghost" spend.
- **Fix:** Either enqueue-then-reserve (and release on enqueue failure), or implement a compensating `release_reservation()` on the budget guard and call it from the enqueue error path.

#### H3. CORS origins hardcoded; production override is a comment, not enforced

`services/api/src/api/main.py:91-96`: origins are `http://localhost:3000`, `127.0.0.1:3000`, `:5173`, etc. A comment says production should override via env, but nothing reads an env var and nothing fails if production runs with these defaults.

- **Impact:** If the API is exposed publicly with the default config, browser CSRF surface is wider than intended (credentials + permissive localhost origins).
- **Fix:** Read `CORS_ORIGINS` from env; fail at startup in non-dev `ENV` if unset.

#### H4. Dual redaction systems with divergent coverage

- `libs/fincept-db/src/fincept_db/provider_data.py:434-483` uses a local `_redact_sensitive()` (regex set A).
- `libs/fincept-db/src/fincept_db/provider_receipts.py:182` uses `redact_dict()` from `evidence_redaction.py` (regex set B).

The two pattern sets are **not identical**. Set B has explicit query-param and bearer-header patterns; set A does not. A secret redacted in a receipt may survive in the underlying evidence record, or vice versa.

- **Impact:** Inconsistent protection; a secret's fate depends on which code path writes it. This is exactly the kind of inconsistency that produces a later leak.
- **Assumption violated:** "There is one redaction module." There are two.
- **Fix:** Consolidate on `evidence_redaction.redact_dict()` everywhere, or document a deliberate split and add a parity test.

#### H5. TOCTOU race on module start/stop

`services/api/src/api/routes/modules.py:443-446`: `start_module` checks freshness, then spawns a process, then writes state. Two concurrent requests can both observe "not running" and both spawn.

- **Impact:** Duplicate processes, doubled cost, inconsistent state. The local-only guard (`_assert_local`) limits the blast radius but does not eliminate it (a single operator double-clicking, or a retrying client, suffices).
- **Fix:** Use a Redis compare-and-set on the module state key before spawning; treat the spawn as the critical section.

### MEDIUM

#### M1. `shadow_ledger._reload()` crashes on a malformed JSONL line

`services/quant_foundry/src/quant_foundry/shadow_ledger.py:182-193`: `_reload()` calls `model_validate_json` per line with no per-line try/except. A single corrupted/partial trailing line (common after a crash mid-write) prevents the process from starting.

- **Assumption:** "The ledger file is always well-formed." Append-only JSONL is not crash-safe without fsync + atomic rename, and even then a torn final write is possible.
- **Note:** `outbox.py` and `inbox.py` *do* skip malformed lines defensively (e.g. `outbox.py:162`); `shadow_ledger.py` is inconsistent with its siblings.
- **Fix:** Wrap per-line validation in try/except, log the skip, and continue. Optionally truncate the torn final line.

#### M2. `shadow_settlement.store_batch` swallows all exceptions

`services/quant_foundry/src/quant_foundry/shadow_settlement.py:199`: bare `except Exception` around schema validation. In Python < 3.11 this also catches `KeyboardInterrupt`/`SystemExit`; even on 3.11+ it masks unexpected errors (e.g. a `MemoryError` during validation) as "bad schema."

- **Fix:** Catch `(ValidationError, ValueError, TypeError)` explicitly; let everything else propagate.

#### M3. `budget.record_spend` docstring contradicts the code

`services/quant_foundry/src/quant_foundry/budget.py:210` comment says "Use a negative amount to adjust," but lines 212-215 reject negatives. Either the adjustment path is missing or the comment is stale.

- **Impact:** An operator following the comment to issue a refund/adjustment will get a silent rejection (or, if the guard is later loosened, an unintended spend reversal).
- **Fix:** Reconcile — either implement negative adjustments with an audit reason, or remove the comment.

#### M4. Empty callback secret is allowed by default

`services/quant_foundry/src/quant_foundry/gateway.py:103`: `callback_secret` defaults to `""`. HMAC with an empty key is computable and verifiable, so if the env var is unset, any caller who knows the (empty) secret can forge callbacks.

- **Impact:** Callback forgery when the secret is unset. Currently mitigated by `QUANT_FOUNDRY_ENABLED=false` default, but the moment it's enabled without setting the secret, the callback endpoint is unauthenticated.
- **Fix:** Refuse to construct the gateway (or refuse `/callbacks/runpod`) when `callback_secret` is empty in non-dev env.

#### M5. `stop_module` is not idempotent

`services/api/src/api/routes/modules.py:500-537`: stopping an already-stopped module runs the stop script and raises on failure rather than returning "already stopped."

- **Impact:** Operator-facing errors on retry; receipts record spurious stop attempts.
- **Fix:** Check state first; return 200 with an `already_stopped` status.

#### M6. Settlement `_find()` is O(n) over all predictions

`services/quant_foundry/src/quant_foundry/settlement.py:283-289`: locating a prediction scans every model file. No index.

- **Impact:** Settlement latency grows linearly with total prediction count. Fine at MVP scale; a problem at millions of predictions.
- **Fix:** Maintain a `prediction_id → (file, offset)` index, or move settlement lookups to a database.

#### M7. In-memory circuit breaker in `paper_bridge`

`services/quant_foundry/src/quant_foundry/paper_bridge.py:166-188`: the breaker resets on process restart, so a burst of failures can slip through immediately after a restart.

- **Mitigation:** Bridge is disabled by default (`allow_paper_bridge=False`) and requires "paper" runtime mode + an approved dossier, so the blast radius is small today.
- **Fix:** Persist breaker state (count + tripped flag) to disk or Redis.

#### M8. Broad `except Exception: pass` in OMS evidence writes (silent failures)

`services/oms/src/oms/alpaca/marks.py:58-62` and `services/oms/src/oms/alpaca/news_sync.py:300-308, 354-372`: evidence recording and event publishing failures are silently swallowed (no log).

- **Intent:** Best-effort by design ("never let evidence recording break news ingest") — this is a reasonable choice for the ingest path.
- **Risk:** A bug in redaction or `write_provider_data` is invisible. Combined with C1, the mark-evidence path is currently *crashing silently*.
- **Fix:** Keep the swallow, but add a structured warning log (counter + last error) so operators can see evidence recording is failing.

### LOW

- **L1.** `gateway.py:331,336` — settlement lag and rejection rate are hardcoded `None` ("not yet durable"), so the health endpoint always reports null for these fields, masking missing instrumentation.
- **L2.** `gateway.py:464` — `_aggregate_feature_availability` uses `getattr(..., None)` and silently skips records lacking the attribute, masking schema drift.
- **L3.** `signatures.py:25` — `MAX_TS_SKEW_SECONDS=300` is hardcoded; inflexible but acceptable for a 5-minute window. Wall-clock dependence means >5min clock drift between signer and verifier breaks verification (or, if verifier lags, accepts stale signatures).
- **L4.** `modules.py:205-210` — output redaction stops at first whitespace; multi-word secrets could leak into receipts. Low likelihood but worth a regex-based boundary.
- **L5.** `modules.py:652 vs 424` — actor is `"system"` in sweep but `user.sub` (or `"unknown"`) elsewhere; audit trail inconsistency.
- **L6.** `quant_foundry.py:249-303` — callback error handling has no `else` for unexpected gateway error codes; an unknown code is silently passed through to the client.
- **L7.** `runpod_training.py:261` — `_git_sha_or_default()` returns hardcoded `"local-git-sha"`; if the container build doesn't inject the real SHA, all artifacts share a fake SHA, masking reproducibility gaps.
- **L8.** `drift_sentinel.py:228-242` — severity thresholds conflate drift *count* and *magnitude* (1 high-drift indicator → SHADOW_ONLY; 3 medium → RETRAIN). Brittle but currently advisory only.
- **L9.** `settlement.py:49-55` — model-ID validation rejects Windows-forbidden filesystem chars; on Linux this is overly strict and could reject valid IDs.
- **L10.** `evidence_redaction.py` `_LONG_TOKEN_PATTERN` (32+ alphanumeric) is aggressive and may false-positive on long UUIDs/hashes; acceptable given fail-closed intent, but monitor.

---

## 3. Cross-Cutting Observations

### Assumptions that recur across modules

1. **Single-process, no concurrent writers** — assumed by every JSONL ledger (`outbox`, `inbox`, `shadow_ledger`, `settlement`, `budget`). True today (one API process); false the moment the API is scaled horizontally. There is no file lock or advisory-lock scheme.
2. **Wall-clock time is trustworthy** — `signatures.py`, `budget.py` (month bucketing), `runpod_training.py` (deadline), `provider_receipts.py` (age) all use `time.time()`/`time.time_ns()`. None use monotonic time or NTP-drift-aware bounds. A clock jump can mis-bucket spend months or reject/accept signatures.
3. **Append-only JSONL is durable enough** — most ledgers `fsync` after append (`outbox.py:159`, `inbox.py:124`, `shadow_ledger.py:203`), but `budget.py:266-267` does **not** fsync, relying on OS buffering. A crash mid-append can lose a spend entry (under-counting spend → budget bypass).
4. **Caller computes batch hashes deterministically** — `shadow_ledger`, `shadow_settlement`, `outbox`, `inbox` all trust a caller-supplied hash and use a hash mismatch as a tamper/security signal. If the caller's JSON serialization differs from the verifier's expectation (e.g. Pydantic `model_dump` key ordering changes across versions), legitimate batches are flagged as tampered.

### Edge cases handled well

- Look-ahead guard in `settlement.py:180-197` (distinguishes `pending_time` from `pending_data`).
- Order-like field rejection in `shadow_ledger.py:215-221` (structural shadow-only enforcement).
- Constant-time HMAC compare (`signatures.py:73`) and explicit skew check (`signatures.py:67-69`).
- Fail-closed redaction on unknown sensitive field names (`evidence_redaction.py:228-237`).
- AbortController + typed error classification in `apps/dashboard/src/lib/api.ts` (timeouts actually cancel; errors are distinguishable).
- Decimal-as-string transport in `types.ts` (preserves precision).

### Edge cases handled poorly or not at all

- Torn final JSONL line on restart (M1).
- Negative age from unit mismatch masquerading as clock skew (C2).
- Missing `cost_per_dispatch_cents` attribute silently zeroing cost (H1).
- Concurrent module start double-spawn (H5).
- Process restart resetting the paper-bridge breaker (M7).

### Risky patterns summary

| Pattern | Where | Severity |
|---|---|---|
| `getattr(client, "cost", 0)` for a security value | `runpod_client.py:273` | High |
| Stub `dispatch` raising `NotImplementedError` reachable in live mode | `runpod_client.py:202` | Critical |
| Accessing undefined `Settings` attribute | `marks.py:50` | Critical |
| Mixed timestamp units across modules | `provider_receipts.py` vs `provider_data.py` | Critical |
| Reserve-before-enqueue without rollback | `gateway.py:159-187` | High |
| Two divergent redaction implementations | `provider_data.py` vs `evidence_redaction.py` | High |
| Hardcoded CORS, no prod enforcement | `main.py:91-96` | High |
| TOCTOU on module state | `modules.py:443-446` | High |
| Bare `except Exception` around validation | `shadow_settlement.py:199` | Medium |
| No fsync on budget ledger append | `budget.py:266-267` | Medium |
| `except Exception: pass` with no log | `marks.py:61`, `news_sync.py:307,371` | Medium |
| Empty HMAC secret allowed by default | `gateway.py:103` | Medium |

---

## 4. Dismissed / Corrected Findings

- **`moe_router.py` null `decay_indicator` crash** — flagged by a reviewer as an `AttributeError` risk. **Verified safe:** `moe_router.py:230-231` reads `entry.decay_indicator is not None and (entry.decay_indicator.is_stale or entry.decay_indicator.is_decayed)`. The null check short-circuits correctly. No action needed.
- **"`_ALLOWED_TRANSITIONS` is defined but never used" in `outbox.py`** — noted as incomplete implementation. This is accurate: the dict exists at `outbox.py:60-69` but no transition enforcement references it. Listed as L-level (status transitions are permissive). Not a correctness bug today, but the dead code implies an intended guard that was never wired.

---

## 5. Recommended Action Order

1. **C1** — add `MARK_TTL_SEC` to `Settings` (or `getattr` fallback). One-line fix; currently breaking mark evidence writes.
2. **C2** — unify timestamp units between `provider_receipts` and `provider_data`; add a magnitude sanity check.
3. **H1 + C3** — make prospective cost fail-closed and guard against the stub `HttpRunPodClient` before any live enablement.
4. **H2** — add a budget release/compensating-write on enqueue failure.
5. **H3** — env-driven CORS with non-dev enforcement.
6. **H4** — consolidate redaction and add a parity test.
7. **H5** — Redis CAS on module start.
8. **M1, M2, M4** — ledger reload resilience, narrow exception scope, require non-empty callback secret.
9. **M8** — add logging to the silent `except Exception: pass` blocks so C1-class bugs surface.

---

*Generated 2026-06-23 from a read-only review. No source files were modified.*
