# Phase F · Foundation — Agent Prompts

**Tasks:** TASK-001 (done), TASK-002, TASK-003, TASK-004, TASK-005, TASK-006
**Checkpoint:** `make dev` spins up the stack; `make ci` is green; CI workflow runs green on a PR.

---

## Phase kickoff (paste once before starting Phase F work)

```text
You are a senior Python platform engineer joining a contract-first codebase.

Project: Fincept Terminal — an AI-agentic stock & crypto trading platform.

Your work in this phase: build the foundation libraries that every other service depends on. Your code will be imported by 10+ services. Bugs here corrupt every downstream layer, so correctness matters more than speed.

NON-NEGOTIABLE CONTEXT TO LOAD BEFORE WRITING ANY CODE:
1. spec/ARCHITECTURE.md — module boundaries you must respect.
2. spec/CONTRACTS.md — every schema, event, and interface you implement must match it byte-for-byte.
3. spec/LAYOUT.md — every file you create must already be listed there.
4. The pyproject.toml at the repo root — uv workspace, your package is a member.
5. The relevant TASK-XXX.md from spec/tasks/ — the unit you are implementing.

PRINCIPLES THIS PHASE:
- Pydantic v2 only. Do not import from pydantic.v1.
- Decimal for all prices, sizes, and money. Never float.
- Timestamps are integer nanoseconds since UNIX epoch (UTC). Use `fincept_core.clock`.
- structlog for logging. Never use `print` or stdlib logging directly outside of bootstrap.
- All public APIs typed strictly; mypy --strict must pass.
- Tests live under <package>/tests/. Tests do NOT have `tests/__init__.py` (mypy duplicate-module rule).
- For anything async, use asyncio + Redis async client. No sync redis-py calls.

WHEN YOU GET STUCK:
- Missing dependency? Check spec/BUILD_ORDER.md — is the prereq task complete? If no, STOP and report.
- Contract feels wrong? STOP. Do not deviate. Reply: "ADR needed for <contract section>" and wait.
- Test fails? Read the failure carefully. The test is the contract. Fix the implementation, not the test.

When you finish a task: run `uv run ruff check libs services && uv run mypy libs services && uv run pytest`. All three must be green. Then say "TASK-XXX done" and stop.

Acknowledge by listing the 5 docs above and your understanding of the principles. Then wait for the first task.
```

---

## TASK-002 prompt — `fincept-core` library

```text
Implement TASK-002 from spec/tasks/TASK-002-fincept-core.md.

This is THE foundational library. Everything imports from it.

Specific landmines for this task:
- The schemas in spec/CONTRACTS.md §1–§5 are copy-verbatim. Field order, defaults, frozen-ness, Decimal vs float, all matter.
- pydantic ConfigDict(frozen=True) means assigning to fields after construction must raise. Test that.
- `get_settings()` is a cached singleton. Calling Settings() directly outside that function is forbidden — add a comment.
- `Leader` (leadership.py) uses Lua scripts for atomic CAS on Redis. Do not replace with multi-step GET+SET; that creates a race window where two leaders coexist.
- `idempotency_key` must be deterministic: same args → same hash forever. Use blake2b, not Python's hash().
- `now_ns()` returns time.time_ns() for wall clock; do not use time.monotonic_ns() (that's for relative timing only).
- `configure_logging` and `configure_tracing` are called once per process at startup. They are idempotent.

Append the full contents of spec/tasks/TASK-002-fincept-core.md and implement exactly. When done, run:

  uv run pytest libs/fincept-core
  uv run mypy libs/fincept-core
  uv run ruff check libs/fincept-core

Report each exit code. If any non-zero, fix and re-run before declaring done.
```

---

## TASK-003 prompt — `fincept-bus`

```text
Implement TASK-003 from spec/tasks/TASK-003-fincept-bus.md.

Prereqs you can rely on: TASK-002 is complete. Import StreamEnvelope, serialize, deserialize from fincept_core.events.

Specific landmines:
- Producer.publish must use XADD with maxlen ~ (approximate trimming). Exact trimming kills throughput.
- Consumer.read uses xreadgroup with ">" (only new messages, not pending). To handle pending after restart, separate API later — out of scope here.
- ensure_group catches BUSYGROUP error specifically (group already exists). Do not swallow other errors.
- Poisoned messages (decode failure) must be ack'd to prevent infinite redelivery. Log them at error level.
- Tests require a running Redis on localhost:6379 (db 15 is the test DB). docker compose up -d redis must already be running.

Append spec/tasks/TASK-003-fincept-bus.md and implement. Run:

  uv run pytest libs/fincept-bus

Tests will fail if Redis is not running — that's expected feedback, not a code bug.
```

---

## TASK-004 prompt — `fincept-db`

```text
Implement TASK-004. This task does not yet have a fully-authored spec; use the template in spec/PROMPTS.md and the LAYOUT.md entries for libs/fincept-db/ to derive the file list.

Required deliverables:
1. libs/fincept-db/src/fincept_db/engine.py — async SQLAlchemy 2.0 engine + sessionmaker + get_db() FastAPI dependency.
2. libs/fincept-db/src/fincept_db/models.py — SQLAlchemy 2.0 declarative models for: trades, book_deltas, bars_1m, bars_1h, bars_1d, orders, fills, positions, audit_log, strategies_registry.
3. libs/fincept-db/src/fincept_db/migrations/ — Alembic environment + initial migration creating Timescale hypertables for time-series tables.
4. libs/fincept-db/src/fincept_db/ticks.py — batch_insert_trades, batch_insert_book_deltas using COPY (asyncpg's copy_records_to_table).
5. libs/fincept-db/src/fincept_db/bars.py — read_bars (async iterator) + read_bars_list (list).
6. libs/fincept-db/src/fincept_db/audit.py — append_audit(kind: str, payload: dict) -> None.
7. tests/ exercising migrations + a roundtrip insert/read.

Specific landmines:
- Hypertables: SELECT create_hypertable('trades', 'ts_event', chunk_time_interval => INTERVAL '1 day') AFTER table create.
- ts_event is BIGINT (nanoseconds), not TIMESTAMPTZ. Convert at read time only when needed for human display.
- Use asyncpg via SQLAlchemy 2.0 (postgresql+asyncpg://...). DO NOT use psycopg.
- Decimal precision: NUMERIC(38, 18) for prices, NUMERIC(38, 8) for sizes. Never DOUBLE PRECISION.
- Indexes: (symbol, ts_event DESC) on trades, book_deltas. (strategy_id, ts_event DESC) on orders.

Add the relevant entry to spec/tasks/ as TASK-004-fincept-db.md following spec/PROMPTS.md template before implementing. Update spec/tasks/README.md and spec/BUILD_ORDER.md when done.
```

---

## TASK-005 prompt — `fincept-tools`

```text
Implement TASK-005. Authoring + implementation in one pass.

Goal: an MCP-style typed tool protocol that LLM agents call. Each tool has Pydantic ToolInput / ToolOutput, async __call__, and JSON-schema metadata for OpenAI/Anthropic function-calling.

Files (per spec/LAYOUT.md):
- libs/fincept-tools/src/fincept_tools/protocol.py — Tool Protocol, ToolInput, ToolOutput, ToolRegistry.
- libs/fincept-tools/src/fincept_tools/registry.py — global registry instance + register decorator.
- libs/fincept-tools/src/fincept_tools/data.py — tools: data.get_bars, data.get_position, data.get_quote (read-only, hit fincept-db).
- libs/fincept-tools/src/fincept_tools/analytics.py — tools: analytics.compute_vwap, analytics.compute_realized_vol.
- libs/fincept-tools/src/fincept_tools/exec.py — tools: exec.submit_paper_order, exec.cancel_paper_order. PAPER ONLY in this task; live execution is gated by TRADING_MODE=live which will fail loudly here.

Specific landmines:
- Each tool MUST emit a JSON schema compatible with OpenAI function-calling AND Anthropic tools. Test that ToolRegistry.list() returns both shapes.
- exec tools must reject when settings.trading_mode == "live" (raise FinceptError). Until TASK-075 wires the live adapter, paper-only is enforced here.
- Tool names use dot notation: "data.get_bars". Hyphens forbidden (breaks LLM function-call parsing).
- Output schemas always include `ok: bool` and optional `error: str`. LLMs handle errors better when they're typed.

Author TASK-005-fincept-tools.md in spec/tasks/ from the spec/PROMPTS.md template, then implement. Run gates and update BUILD_ORDER.md.
```

---

## TASK-006 prompt — CI refinement

```text
TASK-006 is mostly already done by TASK-001. Your job: verify CI is complete and add what's missing.

Verify .github/workflows/ci.yml has all of:
- Matrix Python 3.12 only (do not expand; we pin one version).
- Postgres + Redis service containers with env DATABASE_URL + REDIS_URL exported.
- Steps: ruff check, ruff format --check, mypy, pytest with --cov.
- A separate js job: pnpm install, pnpm -r lint, pnpm -r build.
- A security job using gitleaks-action.
- concurrency block to cancel stale PR runs.
- Coverage upload artifact.

If any gap, edit .github/workflows/ci.yml to fix.

Then verify locally that `make ci` (which runs lint + typecheck + test) exits 0.

Update spec/BUILD_ORDER.md task 006 to [x] only if CI runs green on a real PR (not just locally).
```

---

## Phase F exit verification (paste after TASK-006 reports done)

```text
Run the Phase F checkpoint validation:

1. From a fresh clone, in a clean directory:
   - `make dev` — must exit 0; postgres, redis, minio containers healthy.
   - `make ci` — must exit 0; ruff + mypy + pytest all clean.
   - `make build` — must produce 15 wheels in dist/.

2. Push a branch to origin and open a draft PR. The CI workflow must complete green within 10 minutes.

3. Verify spec/BUILD_ORDER.md tasks 001–006 are all [x].

4. Open `psql` via `make db-shell`. Confirm the migrations from TASK-004 applied:
   \dt
   SELECT * FROM timescaledb_information.hypertables;

5. Open `make redis-cli`. Confirm with `XINFO STREAM md.trades` (will show empty stream — that's fine, structure is what matters).

6. Confirm libs/fincept-tools registry exposes ≥4 tools:
   uv run python -c "from fincept_tools.registry import registry; import json; print(json.dumps([t['name'] for t in registry.list()], indent=2))"

If all six pass, declare Phase F COMPLETE. Update spec/BUILD_ORDER.md with a "Checkpoint F: passed YYYY-MM-DD" note. Proceed to spec/prompts/phase-D-data-spine.md.

If any fail, do NOT advance to Phase D. File the failure as an issue and fix in this phase first.
```
