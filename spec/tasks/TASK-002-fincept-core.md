# TASK-002 · `fincept-core` library

**Phase:** F · **Depends on:** TASK-001 · **Blocks:** everything else

## Goal

Implement the canonical schemas, event parsing, config, logging, tracing, clock, IDs, errors, and leader election used by every other package.

## Files

```
libs/fincept-core/
├── pyproject.toml
├── src/fincept_core/
│   ├── __init__.py
│   ├── schemas.py
│   ├── events.py
│   ├── config.py
│   ├── logging.py
│   ├── tracing.py
│   ├── clock.py
│   ├── ids.py
│   ├── leadership.py
│   └── errors.py
└── tests/
    ├── test_schemas.py
    ├── test_events.py
    ├── test_config.py
    ├── test_clock.py
    ├── test_ids.py
    ├── test_logging.py
    ├── test_imports.py
    └── test_leadership.py
```

## Implementation notes

- `schemas.py` matches `spec/CONTRACTS.md` sections 1 through 5 exactly.
- `events.py` provides an `Event` envelope, `make_event(type, payload, ...)`, and `parse_event(raw_dict)` that dispatches on the `type` discriminator.
- `config.py` uses `pydantic-settings` with `FINCEPT_` prefix and `Settings()` as the singleton entrypoint.
- `clock.py` exposes `Clock`, `MonotonicClock`, `FrozenClock(now_ns)`, plus `now_ns`, `ns_to_iso`, and `iso_to_ns` helpers.
- `ids.py` exposes `new_id()` and `idempotency_key(...)`.
- `logging.py` configures structlog JSON output with ISO timestamps and `correlation_id` from contextvars.
- `tracing.py` configures OTLP HTTP tracing with a localhost default endpoint.
- `leadership.py` provides Redis-based leader election.

## Verification

- `uv run pytest libs/fincept-core` is green.
- `uv run mypy --strict libs/fincept-core/src` is green.
- `uv run ruff check libs/fincept-core/src libs/fincept-core/tests` is green.
- `from fincept_core import schemas, events, config, clock, ids, errors, logging, tracing` succeeds.
- Every event type in `spec/CONTRACTS.md §2` round-trips through `make_event` and `parse_event`.

## Done when

- [x] All files exist
- [x] Tests pass
- [x] Typecheck passes
- [x] Lint passes
