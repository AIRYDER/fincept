"""
Tests for ``fincept_core.prediction_log``.

The store is small and the surface area is the four public entry points
(``append``, ``read``, ``stats``, plus row dataclasses).  These tests
focus on the round-trip property (write a row, read it back), filter
correctness (model_name / since_ns), aggregate accuracy, and the
defensive behaviours that protect operators from corrupt data on disk.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from fincept_core.prediction_log import (
    PredictionLog,
    PredictionRow,
    PredictionStats,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path: pathlib.Path) -> PredictionLog:
    return PredictionLog(predictions_dir=tmp_path / "predictions")


# --------------------------------------------------------------------------- #
# Validation                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "agent/v1",
        "agent\\v1",
        "..",
        ".hidden",
        "agent:v1",
        "agent*",
        'agent"v1',
    ],
)
def test_append_rejects_bad_agent_id(store: PredictionLog, bad: str) -> None:
    with pytest.raises(ValueError):
        store.append(
            agent_id=bad,
            model_name="m",
            ts_event=0,
            horizon_ns=0,
            symbol="BTC-USD",
            direction=0.0,
            confidence=0.0,
        )


def test_append_rejects_empty_model_name(store: PredictionLog) -> None:
    with pytest.raises(ValueError, match="model_name"):
        store.append(
            agent_id="gbm.v1",
            model_name="",
            ts_event=0,
            horizon_ns=0,
            symbol="BTC-USD",
            direction=0.0,
            confidence=0.0,
        )


def test_append_rejects_empty_symbol(store: PredictionLog) -> None:
    with pytest.raises(ValueError, match="symbol"):
        store.append(
            agent_id="gbm.v1",
            model_name="m",
            ts_event=0,
            horizon_ns=0,
            symbol="",
            direction=0.0,
            confidence=0.0,
        )


def test_read_rejects_zero_limit(store: PredictionLog) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.read(agent_id="gbm.v1", limit=0)


# --------------------------------------------------------------------------- #
# Round-trip                                                                 #
# --------------------------------------------------------------------------- #


def test_append_returns_populated_row(store: PredictionLog) -> None:
    row = store.append(
        agent_id="gbm.v1",
        model_name="model_a",
        ts_event=1_000_000,
        horizon_ns=900_000_000_000,
        symbol="BTC-USD",
        direction=0.4,
        confidence=0.4,
    )
    assert row.id  # uuid generated
    assert row.agent_id == "gbm.v1"
    assert row.model_name == "model_a"
    assert row.ts_event == 1_000_000
    assert row.horizon_ns == 900_000_000_000
    assert row.symbol == "BTC-USD"
    assert row.direction == pytest.approx(0.4)
    assert row.confidence == pytest.approx(0.4)
    # ts_recorded is a wall-clock ns; sanity check it's in this decade.
    assert row.ts_recorded > 1_700_000_000_000_000_000  # 2023-11-15


def test_read_returns_appended_row(store: PredictionLog) -> None:
    written = store.append(
        agent_id="gbm.v1",
        model_name="model_a",
        ts_event=1,
        horizon_ns=1,
        symbol="ETH-USD",
        direction=-0.3,
        confidence=0.3,
    )
    rows = store.read(agent_id="gbm.v1")
    assert len(rows) == 1
    assert rows[0] == written


def test_read_returns_empty_when_no_file(store: PredictionLog) -> None:
    assert store.read(agent_id="gbm.v1") == []


def test_read_returns_newest_first(store: PredictionLog) -> None:
    a = store.append(
        agent_id="gbm.v1",
        model_name="m",
        ts_event=1,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.1,
        confidence=0.1,
    )
    b = store.append(
        agent_id="gbm.v1",
        model_name="m",
        ts_event=2,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.2,
        confidence=0.2,
    )
    rows = store.read(agent_id="gbm.v1")
    # Newest first.  ``b`` was written second, so its ts_recorded is
    # >= ``a``'s -- on Windows time_ns can return identical values
    # for back-to-back calls, so allow equality.
    assert rows[0].id == b.id or rows[0].ts_recorded == rows[1].ts_recorded
    assert rows[-1].id == a.id or rows[0].ts_recorded == rows[1].ts_recorded


def test_read_truncates_to_limit(store: PredictionLog) -> None:
    for i in range(5):
        store.append(
            agent_id="gbm.v1",
            model_name="m",
            ts_event=i,
            horizon_ns=1,
            symbol="BTC-USD",
            direction=0.0,
            confidence=0.0,
        )
    assert len(store.read(agent_id="gbm.v1", limit=3)) == 3


# --------------------------------------------------------------------------- #
# Filters                                                                    #
# --------------------------------------------------------------------------- #


def test_read_filters_by_model_name(store: PredictionLog) -> None:
    store.append(
        agent_id="gbm.v1",
        model_name="model_a",
        ts_event=1,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.1,
        confidence=0.1,
    )
    store.append(
        agent_id="gbm.v1",
        model_name="model_b",
        ts_event=2,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.2,
        confidence=0.2,
    )
    a_rows = store.read(agent_id="gbm.v1", model_name="model_a")
    b_rows = store.read(agent_id="gbm.v1", model_name="model_b")
    assert len(a_rows) == 1 and a_rows[0].model_name == "model_a"
    assert len(b_rows) == 1 and b_rows[0].model_name == "model_b"


def test_read_filters_by_since_ns(store: PredictionLog) -> None:
    a = store.append(
        agent_id="gbm.v1",
        model_name="m",
        ts_event=1,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.0,
        confidence=0.0,
    )
    b = store.append(
        agent_id="gbm.v1",
        model_name="m",
        ts_event=2,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.0,
        confidence=0.0,
    )
    # Cut just above ``a``'s recorded timestamp.  ``b`` should remain.
    rows = store.read(agent_id="gbm.v1", since_ns=a.ts_recorded + 1)
    # ``b.ts_recorded`` >= a.ts_recorded + 1 may not always hold on
    # Windows due to time_ns granularity.  If both are equal to the
    # same ns, the filter excludes both -- that's correct semantics.
    if b.ts_recorded == a.ts_recorded:
        assert rows == []
    else:
        assert {r.id for r in rows} == {b.id}


# --------------------------------------------------------------------------- #
# Tolerance for malformed lines                                              #
# --------------------------------------------------------------------------- #


def test_read_skips_malformed_lines(
    store: PredictionLog, tmp_path: pathlib.Path
) -> None:
    """A corrupt line in the middle of the JSONL must not break the read."""
    good = store.append(
        agent_id="gbm.v1",
        model_name="m",
        ts_event=1,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.5,
        confidence=0.5,
    )
    # Inject garbage into the file directly.
    path = store.predictions_dir / "gbm.v1.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("not valid json\n")
        f.write('{"missing":"required_keys"}\n')

    rows = store.read(agent_id="gbm.v1")
    assert len(rows) == 1
    assert rows[0].id == good.id


def test_read_skips_blank_lines(store: PredictionLog) -> None:
    good = store.append(
        agent_id="gbm.v1",
        model_name="m",
        ts_event=1,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.0,
        confidence=0.0,
    )
    path = store.predictions_dir / "gbm.v1.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("\n\n\n")
    rows = store.read(agent_id="gbm.v1")
    assert len(rows) == 1
    assert rows[0].id == good.id


# --------------------------------------------------------------------------- #
# Aggregate stats                                                            #
# --------------------------------------------------------------------------- #


def test_stats_is_zero_when_empty(store: PredictionLog) -> None:
    s = store.stats(agent_id="gbm.v1")
    assert s == PredictionStats(
        count=0, mean_confidence=0.0, long_count=0, short_count=0, flat_count=0
    )


def test_stats_counts_directions(store: PredictionLog) -> None:
    for direction, conf in [(0.5, 0.5), (-0.4, 0.4), (0.0, 0.0), (0.2, 0.2)]:
        store.append(
            agent_id="gbm.v1",
            model_name="m",
            ts_event=1,
            horizon_ns=1,
            symbol="BTC-USD",
            direction=direction,
            confidence=conf,
        )
    s = store.stats(agent_id="gbm.v1")
    assert s.count == 4
    assert s.long_count == 2  # 0.5, 0.2
    assert s.short_count == 1  # -0.4
    assert s.flat_count == 1  # 0.0
    assert s.mean_confidence == pytest.approx((0.5 + 0.4 + 0.0 + 0.2) / 4)


def test_stats_respects_model_filter(store: PredictionLog) -> None:
    store.append(
        agent_id="gbm.v1",
        model_name="model_a",
        ts_event=1,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=0.5,
        confidence=0.5,
    )
    store.append(
        agent_id="gbm.v1",
        model_name="model_b",
        ts_event=2,
        horizon_ns=1,
        symbol="BTC-USD",
        direction=-0.5,
        confidence=0.5,
    )
    s_a = store.stats(agent_id="gbm.v1", model_name="model_a")
    s_b = store.stats(agent_id="gbm.v1", model_name="model_b")
    assert s_a.count == 1 and s_a.long_count == 1 and s_a.short_count == 0
    assert s_b.count == 1 and s_b.long_count == 0 and s_b.short_count == 1


# --------------------------------------------------------------------------- #
# Row dataclass                                                              #
# --------------------------------------------------------------------------- #


def test_row_round_trips_through_json() -> None:
    row = PredictionRow(
        id="abcd",
        agent_id="gbm.v1",
        model_name="m",
        ts_recorded=10,
        ts_event=20,
        horizon_ns=30,
        symbol="BTC-USD",
        direction=0.5,
        confidence=0.5,
    )
    line = row.to_json()
    decoded = PredictionRow.from_json(line)
    assert decoded == row
    # Sanity-check the format is compact JSONL (no embedded newlines).
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["id"] == "abcd"
