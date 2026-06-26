"""Callback metrics store tests (TASK-14).

These tests lock down the durable ``CallbackMetricsStore`` that backs
``shadow_health()["callback_rejection_rate"]``. The store writes
append-only JSONL at ``data/quant_foundry/callback_metrics.jsonl`` and
computes a rolling ``rejected / (accepted + rejected)`` over a
configurable nanosecond window.

Coverage:
  * windowing (old events excluded by ``window_ns``),
  * divide-by-zero returns ``0.0`` (NOT an exception),
  * persistence across instances (re-instantiating the store reads the
    same JSONL file),
  * malformed lines are skipped without crashing the read,
  * the happy QA scenario (3 accepted + 1 rejected -> 0.25),
  * integration with ``shadow_health`` (numeric rate after a recorded
    callback sequence; ``None`` only when no events recorded).
"""

from __future__ import annotations

import json
import time

from quant_foundry.callback_metrics import CallbackMetricsStore
from quant_foundry.gateway import QuantFoundryGateway


def _store(tmp_path) -> CallbackMetricsStore:
    return CallbackMetricsStore(metrics_dir=tmp_path / "callback_metrics")


def test_happy_path_three_accepted_one_rejected(tmp_path) -> None:
    """3 accepted + 1 rejected -> rejection_rate() == 0.25."""
    store = _store(tmp_path)
    now = time.time_ns()
    for _ in range(3):
        store.record("accepted", ts_ns=now)
    store.record("rejected", reason_code="bad_signature", ts_ns=now)

    rate = store.rejection_rate()
    assert rate == 0.25


def test_empty_store_returns_zero_not_exception(tmp_path) -> None:
    """Empty store -> rejection_rate() returns 0.0 (no exception)."""
    store = _store(tmp_path)
    assert store.rejection_rate() == 0.0
    assert store.has_any_events() is False


def test_divide_by_zero_returns_zero(tmp_path) -> None:
    """Only ``received`` events (no accepted/rejected) -> 0.0, not exception."""
    store = _store(tmp_path)
    now = time.time_ns()
    # ``received`` events are excluded from the denominator, so this is a
    # divide-by-zero case that must return 0.0 rather than raise.
    store.record("received", ts_ns=now)
    store.record("received", ts_ns=now)

    assert store.rejection_rate() == 0.0
    assert store.has_any_events() is True


def test_windowing_excludes_old_events(tmp_path) -> None:
    """Events outside ``window_ns`` are excluded from the rate."""
    store = _store(tmp_path)
    now = time.time_ns()
    # 1000 events far in the past (1 hour ago) — outside a 1s window.
    old_ts = now - (3600 * 1_000_000_000)
    for _ in range(500):
        store.record("accepted", ts_ns=old_ts)
    for _ in range(500):
        store.record("rejected", ts_ns=old_ts)

    # 1s window -> all old events excluded -> 0.0 (no in-window events).
    assert store.rejection_rate(window_ns=1_000_000_000) == 0.0

    # Large window -> all events included -> 500 / 1000 == 0.5.
    assert store.rejection_rate(window_ns=24 * 3600 * 1_000_000_000) == 0.5


def test_persistence_across_instances(tmp_path) -> None:
    """A new store instance pointing at the same dir reads prior events."""
    store_a = _store(tmp_path)
    now = time.time_ns()
    store_a.record("accepted", ts_ns=now)
    store_a.record("accepted", ts_ns=now)
    store_a.record("rejected", reason_code="bad_signature", ts_ns=now)

    # New instance, same directory — must see the prior writes.
    store_b = CallbackMetricsStore(metrics_dir=tmp_path / "callback_metrics")
    assert store_b.has_any_events() is True
    assert store_b.rejection_rate() == (1.0 / 3.0)


def test_malformed_lines_skipped(tmp_path) -> None:
    """Corrupt JSONL lines are skipped without crashing the read."""
    store = _store(tmp_path)
    now = time.time_ns()
    store.record("accepted", ts_ns=now)

    # Append a few malformed lines directly to the file.
    path = store._path()
    with path.open("a", encoding="utf-8") as f:
        f.write("not-json\n")
        f.write(json.dumps({"event": "accepted"}) + "\n")  # missing ts_ns
        f.write(json.dumps({"ts_ns": "not-an-int", "event": "accepted"}) + "\n")
        f.write(json.dumps({"ts_ns": now, "event": "bogus_event"}) + "\n")

    # The one valid accepted event still counts; malformed lines skipped.
    assert store.rejection_rate() == 0.0  # 0 rejected / 1 accepted
    assert store.has_any_events() is True


def test_invalid_event_rejected_on_write(tmp_path) -> None:
    """``record`` rejects unknown event labels (fail-loud, not silent drop)."""
    store = _store(tmp_path)
    try:
        store.record("bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("record() must raise ValueError for unknown event")


def test_no_secret_or_payload_in_record(tmp_path) -> None:
    """The JSONL line must NOT contain a secret or raw payload."""
    store = _store(tmp_path)
    store.record("rejected", reason_code="bad_signature")
    path = store._path()
    line = path.read_text(encoding="utf-8").strip()
    obj = json.loads(line)
    assert set(obj.keys()) == {"ts_ns", "event", "reason_code"}
    assert "secret" not in obj
    assert "payload" not in obj


def _build_gateway(tmp_path) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="metrics-secret",
        base_dir=tmp_path / "qf",
        runpod_clients={},
    )


def test_shadow_health_none_when_no_events(tmp_path) -> None:
    """``shadow_health`` returns ``None`` when no callbacks recorded yet."""
    gateway = _build_gateway(tmp_path)
    health = gateway.shadow_health()
    assert health["callback_rejection_rate"] is None


def test_shadow_health_numeric_after_recorded_sequence(tmp_path) -> None:
    """After 3 accepted + 1 rejected, ``shadow_health`` surfaces 0.25."""
    gateway = _build_gateway(tmp_path)
    store = gateway.callback_metrics_store()
    now = time.time_ns()
    for _ in range(3):
        store.record("accepted", ts_ns=now)
    store.record("rejected", reason_code="bad_signature", ts_ns=now)

    health = gateway.shadow_health()
    assert health["callback_rejection_rate"] == 0.25
