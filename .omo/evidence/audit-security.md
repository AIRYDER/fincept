# Security Review Audit — ml-dataset-evidence-spine

## Summary

The ml-dataset-evidence-spine changes (18 commits, `7dc5fc1..HEAD`) introduce an
approved-roots filesystem gate, a settlement side-store with look-ahead/idempotency
guards, a durable callback-metrics store, removal of the legacy unsigned-callback
compat shim, and a feature-health sidecar. The overall security posture is strong:
fail-closed defaults are used consistently, error messages do not leak the
approved-roots list, HMAC verification is constant-time with skew/replay protection,
and the callback-metrics store carries no secrets or raw payloads.

The most significant finding is a **TOCTOU (time-of-check-time-of-use) gap** in the
API routes: `ApprovedRoots.resolve()` validates and returns a `ResolvedPath`
containing the symlink-resolved absolute path, but both `backtest.py` and
`models.py` **discard the resolved path** and re-parse the raw user-supplied string
downstream. This means a symlink swapped between the check and the file read can
escape the approved root. Using the already-resolved `ResolvedPath.path` downstream
would close this window for existing files. The remaining findings are
defense-in-depth gaps and informational notes.

## Findings

### [HIGH] TOCTOU: API routes discard the resolved path and re-use the raw user input

- **Location:** `services/api/src/api/routes/backtest.py:128-130`, `services/api/src/api/routes/models.py:519-531`
- **Description:** Both routes call `approved_roots.resolve(body.bars_path)` /
  `_get_approved_roots().resolve(body.input_path)` to validate the path, but the
  returned `ResolvedPath` object (which carries the symlink-resolved absolute path)
  is **discarded**. The downstream code then re-parses the raw user-supplied string:

  ```python
  # backtest.py:128-130
  approved_roots.resolve(body.bars_path)          # result discarded
  bars_path = pathlib.Path(body.bars_path)        # raw path re-used!
  if not bars_path.exists(): ...

  # models.py:519-531
  _get_approved_roots().resolve(body.input_path)  # result discarded
  req = TrainingRequest(..., input_path=body.input_path, ...)  # raw path re-used
  ```

  The `ApprovedRoots.resolve()` method resolves symlinks and checks that the
  resolved path is inside an approved root at check time. But between the check and
  the actual file read (in `run_backtest` or the training subprocess), an attacker
  with filesystem write access to the approved root can replace a regular file with
  a symlink pointing outside the root (e.g., `data/bars.parquet -> /etc/passwd`).
  The downstream `pathlib.Path(body.bars_path)` would then follow the new symlink
  and read the out-of-root file.

- **Impact:** An attacker who can write to the approved-root directory between the
  validation and the file read can bypass the approved-roots gate and cause the
  backtest/training orchestrator to read arbitrary files outside the approved roots.
  The window is small (same request, milliseconds) but exploitable on systems where
  an attacker has concurrent filesystem access (e.g., a shared multi-tenant
  environment, or a compromised worker process).

- **Recommendation:** Use the resolved path from `ResolvedPath.path` downstream
  instead of re-parsing the raw input. For existing files, `resolve()` already
  follows symlinks to the real location, so using the resolved path eliminates the
  symlink-swap attack. For non-existent files (write paths), consider opening with
  `O_NOFOLLOW` at the point of creation. Minimal fix:

  ```python
  # backtest.py
  resolved = approved_roots.resolve(body.bars_path)
  bars_path = resolved.path  # use the validated, symlink-resolved path
  ```

### [MEDIUM] Settlement store idempotency check is not atomic (TOCTOU race)

- **Location:** `libs/fincept-core/src/fincept_core/datasets/settlement.py:283-301`
- **Description:** `SettlementStore.append()` first calls `_find()` to scan the
  agent's JSONL file for an existing `(prediction_id, cost_model_version)` record,
  then appends a new line if none is found. These two steps are not atomic — two
  concurrent calls for the same idempotency key can both pass the `_find()` check
  (both see no existing record) and both append, resulting in duplicate settled
  rows that the duplicate guard was supposed to prevent.

- **Impact:** Under concurrent settlement workers (or a worker retry racing with
  the original), duplicate settled rows for the same
  `(prediction_id, cost_model_version)` can be written, violating the "no silent
  duplicate" invariant. At MVP volumes with a single worker this is unlikely, but
  it becomes a real risk if the worker is ever parallelized or if retries overlap.

- **Recommendation:** Use file locking (`fcntl.flock` on POSIX, `msvcrt.locking`
  on Windows) around the check-then-append sequence, or use an atomic
  compare-and-append primitive. Alternatively, accept the race and add a
  post-append reconciliation step that detects and flags duplicates.

### [MEDIUM] Approved-roots env-var override can widen the attack surface

- **Location:** `libs/fincept-core/src/fincept_core/datasets/approved_roots.py:211-226`
- **Description:** `default_approved_roots()` reads `FINCEPT_APPROVED_DATA_ROOTS`
  from the environment. An operator (or an attacker who can inject env vars) can
  set this to any path, including `/` or `/etc`, effectively disabling the
  approved-roots gate. The design explicitly treats an empty value as "use the
  default" (fail-open on empty) to avoid bricking production, which is the correct
  operational choice, but there is no validation that the configured roots are
  "reasonable" (e.g., not `/` or a world-writable directory).

- **Impact:** If an attacker can control the `FINCEPT_APPROVED_DATA_ROOTS` env var
  (e.g., via a misconfigured CI secret, a container env injection, or a `.env`
  file), they can widen the approved roots to allow any path. However, env-var
  control generally implies process-level access, which is already game over for
  most threat models.

- **Recommendation:** Document that `FINCEPT_APPROVED_DATA_ROOTS` is a
  security-sensitive configuration that must be set at deploy time and not
  user-controllable. Consider adding a startup warning if any configured root is
  `/` or a known world-writable path. No code change required for MVP.

### [LOW] X-Approved-Roots-Code header leaks the specific rejection reason

- **Location:** `services/api/src/api/approved_roots.py:56-60`
- **Description:** The exception handler sets the `X-Approved-Roots-Code` response
  header to the specific `ApprovedRootsError.code` value (`outside_root`,
  `traversal`, `symlink_escape`, `no_roots`). While the approved-roots list itself
  is not leaked (good), the specific rejection code tells a probing caller which
  validation rule was triggered, which could help them understand and attempt to
  bypass the validation logic.

- **Impact:** Minor information leak. An attacker learns whether their path was
  rejected for being outside the root, containing `..`, or traversing a symlink.
  This does not directly enable a bypass but aids reconnaissance.

- **Recommendation:** Acceptable for operational observability. If the threat model
  requires hiding the rejection reason from untrusted callers, consider gating the
  header behind an internal/admin auth check or removing it entirely and relying
  on server-side logs for the specific code.

### [LOW] paper_spine_replay.py non-dry-run mode writes to production paths

- **Location:** `scripts/paper_spine_replay.py:368-389, 502-511`
- **Description:** When `FINCEPT_REPLAY_DRY_RUN` is not set, the script writes
  fixture predictions to `data/predictions/` and settlements to `data/settlements/`
  under the repo root, and deletes the fixture agent's existing settlement ledger
  file (`fixture_settlements.unlink()`) for idempotency. The `write_receipt`
  function always writes to `reports/paper-spine/` regardless of dry-run mode.

- **Impact:** If the script is run accidentally without `FINCEPT_REPLAY_DRY_RUN=1`
  in a production or CI environment, it will overwrite production settlement data
  for the fixture agent (`fixture_momentum_agent.v1`) and write fixture predictions
  to the real predictions directory. The `unlink()` call is scoped to the fixture
  agent only, so it won't delete other agents' ledgers, but it could mask real
  settlement state.

- **Recommendation:** The dry-run mode is correctly implemented and safe for CI.
  Consider making dry-run the default (opt-in to real writes via
  `--persist` flag instead of opt-out via env var) to prevent accidental
  production writes. The `write_receipt` write to `reports/` is benign (report
  artifact, not production data).

### [PASS] Approved-roots gate core logic (symlink, traversal, encoding)

- **Location:** `libs/fincept-core/src/fincept_core/datasets/approved_roots.py`
- **Description:** The core gate is sound:
  - `..` components are rejected in the raw path before any resolution (line 128).
  - `resolve(strict=False)` follows symlinks and makes the path absolute.
  - `_find_root()` checks the resolved path against canonicalized roots using
    `relative_to()` (no string-prefix bypass possible).
  - `_reject_symlinks()` walks every component from leaf to root using
    `is_symlink()` (lstat-based), rejecting any symlink even if it resolves inside
    the root (TOCTOU defense at check time).
  - `_canonical_root()` rejects `..` in configured roots.
  - Error messages never include the roots list.
  - Empty roots list raises `ApprovedRootsError("no_roots")` at construction
    (fail-closed).
  - Windows path separators (`\`) in the candidate are handled by `pathlib.Path`
    which normalizes them on Windows; on POSIX, `\` is a valid filename character
    but not a path separator, so no traversal risk.

### [PASS] _compat_sign_callback removal is complete

- **Location:** `services/quant_foundry/src/quant_foundry/gateway.py`
- **Description:** The `_compat_sign_callback` function is fully removed. The only
  remaining reference is in a test file comment (`test_gateway_callbacks.py:6`).
  When `_extract_callback_fields()` returns `None` (unsigned/missing callback
  fields), the gateway now fail-closes: records a `rejected` metric event, marks
  the job as `FAILED` with `error_code="missing_runpod_callback_fields"`, and
  continues to the next job. There is no code path that signs on behalf of an
  unsigned handler. The `sign_callback` import is retained with `# noqa: F401`
  solely so tests can monkey-patch it to assert the poller never calls it — it is
  not invoked at runtime.

### [PASS] Callback metrics store — no secret/payload leakage, safe JSONL append

- **Location:** `services/quant_foundry/src/quant_foundry/callback_metrics.py`
- **Description:** The store records only `{"ts_ns": int, "event": str,
  "reason_code": str|None}`. No secrets, no raw payloads, no job IDs, no
  signatures. The `event` field is validated against `_VALID_EVENTS` and
  `reason_code` is validated as `str|None`. JSONL append uses `json.dumps()` which
  properly escapes all characters, preventing JSON injection (a malicious
  `reason_code` with embedded newlines would be escaped as `\n`, not a real
  newline). All `reason_code` values are hardcoded strings in the gateway, not
  user-supplied.

### [PASS] Settlement look-ahead guard and idempotency logic

- **Location:** `libs/fincept-core/src/fincept_core/datasets/settlement.py:224-302`
- **Description:** The look-ahead guard (`decision_window_end_ns > now_ns` raises
  `SettlementError("look_ahead")`) is correct and cannot be bypassed by the record
  itself — the `now_ns` parameter defaults to `time.time_ns()` and is controlled by
  the caller (the worker). The worker passes its own `now_ns` from the tick
  parameter, which is the wall clock at tick time. A caller who controls `now_ns`
  could bypass the guard, but that requires controlling the worker's tick
  parameter (internal service config, not user input). Idempotency is enforced:
  settled/failed rows for `(prediction_id, cost_model_version)` raise `duplicate`;
  pending rows can be superseded (by design, for retry). Settled rows cannot be
  overwritten — the store is append-only and the duplicate guard prevents
  re-settlement under the same cost model version. `_validate_agent_id()` blocks
  path traversal via `agent_id` (forbidden chars include `/`, `\`, `..`, `.`).

### [PASS] Feature health sidecar — best-effort write cannot crash inference

- **Location:** `services/agents/src/agents/gbm_predictor/main.py:589-610`
- **Description:** The feature-health write in `_publish_loop` is wrapped in
  `try: ... except Exception as exc: # noqa: BLE001` with a logged warning. Any
  failure (disk full, permission error, validation error) is swallowed and the
  publish loop continues. The `FeatureHealthLog.append()` method validates
  `agent_id` via `_validate_agent_id()` (same forbidden-char set as the prediction
  log) and validates `prediction_id`/`symbol` as non-empty strings. The write
  itself is a single `f.write(line + "\n")` call on an append-mode file handle.

### [PASS] HMAC callback verification — constant-time, skew-protected, fail-closed

- **Location:** `services/quant_foundry/src/quant_foundry/signatures.py:47-73`,
  `services/quant_foundry/src/quant_foundry/gateway.py:1455-1547`
- **Description:** `verify_callback()` uses `hmac.compare_digest()` for
  constant-time comparison, enforces a 5-minute skew window
  (`MAX_TS_SKEW_SECONDS = 300`), binds the signature to `job_id` (preventing
  cross-job replay), and returns `False` (not an exception) on any input
  validation failure. The gateway verifies the signature **before** recording in
  the inbox (fail-closed: a bad signature creates no durable trace). The
  `receive_callback` method records `received` before verification and
  `accepted`/`rejected` after, with all metric writes wrapped in
  `contextlib.suppress(OSError)` so a disk error cannot mask the security verdict.

### [PASS] FeatureSnapshot look-ahead guard on schema

- **Location:** `libs/fincept-core/src/fincept_core/datasets/schemas.py:224-231`
- **Description:** `FeatureSnapshot._no_lookahead` validator rejects any
  `FeatureRow` with `ts > decision_time_ns`. This is enforced at the Pydantic
  schema level, so any construction (from JSON or in code) is validated. The
  `FeatureSnapshotStore` does not re-check (by design — the schema is the
  enforcement point), which is correctly documented.

## Verdict

The implementation demonstrates a strong security posture with consistent
fail-closed defaults, no secret leakage in error paths or metrics, and a
well-reasoned approved-roots gate. The single HIGH finding (TOCTOU from discarding
the resolved path) is a real but narrow vulnerability that requires concurrent
filesystem access to exploit and has a straightforward fix. The MEDIUM findings
(idempotency race, env-var widening) are defense-in-depth gaps acceptable for MVP
but should be tracked for hardening. No CRITICAL vulnerabilities were found. The
callback security redesign (removing `_compat_sign_callback`) is complete and
correct — there is no remaining path for unsigned callbacks to be accepted.
