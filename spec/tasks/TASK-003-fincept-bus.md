# TASK-003 · fincept-bus library

Phase: F · Depends on: TASK-002 · Blocks: services that communicate over Redis Streams.

## Goal

Implement Redis Streams pub/sub primitives used by services for event-bus communication, without business logic.

## Contracts used

- `spec/CONTRACTS.md §1` core enums used by event payload schemas.
- `spec/CONTRACTS.md §2` market data event union types carried by `fincept_core.events.Event`.
- `spec/CONTRACTS.md §6` stream names copied verbatim into `fincept_bus.streams`.

## Files

- `libs/fincept-bus/pyproject.toml`
- `libs/fincept-bus/src/fincept_bus/__init__.py`
- `libs/fincept-bus/src/fincept_bus/streams.py`
- `libs/fincept-bus/src/fincept_bus/types.py`
- `libs/fincept-bus/src/fincept_bus/producer.py`
- `libs/fincept-bus/src/fincept_bus/consumer.py`
- `libs/fincept-bus/tests/test_producer.py`
- `libs/fincept-bus/tests/test_consumer.py`

## Requirements

- Use `redis.asyncio.Redis` only.
- `Producer.publish(stream: str, event: Event) -> str` serializes with `fincept_core.events.serialize` and returns the Redis stream ID.
- `Producer.publish` applies `RETENTION.get(stream)` as `MAXLEN` with `approximate=True`.
- `Consumer.consume(streams, group, consumer_name, handler)` creates consumer groups with `XGROUP CREATE ... MKSTREAM`.
- `Consumer.consume` reads with `XREADGROUP` and acks only after the handler returns successfully.
- If the handler raises, the entry remains pending.
- `Consumer.claim_pending` uses `XPENDING` + `XCLAIM` to recover stale pending messages after a consumer crash.
- Consumer handlers must be idempotent because pending claim can deliver an entry more than once.
- Handler latency must remain below `block_ms`; exceeding it raises `TimeoutError` before acking.
- Do not catch `asyncio.CancelledError`; cancellation must propagate for shutdown.

## Tests

- Publisher returns Redis stream IDs.
- Publisher emits serialized `Event` fields and retention constants match §6.
- Successful consumer handling acks after handler return.
- Handler failure leaves one pending entry.
- Stale pending entries are claimed, handled, and acked.
- 1000 published events are consumed with no losses and no pending entries.
- Round-trip p99 latency is under 5ms against single-node fake Redis.
- Slow handlers violate the backpressure contract and leave the entry pending.
- Internal type aliases remain string-compatible.

## Verification

```bash
cd libs/fincept-bus
uv sync
uv run pytest -v
uv run mypy --strict src
```
