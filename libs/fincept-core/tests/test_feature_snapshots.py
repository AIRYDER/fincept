"""Tests for fincept_core.datasets.feature_snapshot.

Covers the acceptance criteria from the ml-dataset-evidence-spine plan
(todo 4): append + read round-trip; read respects ``since_ns``; read
respects ``limit``; malformed JSONL line skipped (matches
``prediction_log.py:282-286``); append with bad agent_id raises
ValueError; ``append_if_missing`` de-duplicates by prediction_id.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from fincept_core.datasets.feature_snapshot import FeatureSnapshotStore
from fincept_core.datasets.schemas import FeatureRow, FeatureSnapshot

# A valid 64-char lowercase hex SHA-256 used as feature_schema_hash.
_SCHEMASH = "a" * 64


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _row(symbol: str = "AAPL", ts: int = 1_000_000_000) -> FeatureRow:
    return FeatureRow(symbol=symbol, ts=ts, features={"f1": 1.0, "f2": 2.0})


def _snapshot(
    *,
    decision_time_ns: int = 1_000_000_000,
    rows: list[FeatureRow] | None = None,
    feature_schema_hash: str = _SCHEMASH,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        decision_time_ns=decision_time_ns,
        rows=rows if rows is not None else [_row(ts=decision_time_ns)],
        feature_schema_hash=feature_schema_hash,
    )


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


def test_append_read_round_trip(tmp_path: pathlib.Path) -> None:
    """append then read_for_symbol returns the same snapshot."""
    store = FeatureSnapshotStore(root=tmp_path)
    snap = _snapshot(decision_time_ns=1_000_000_000)
    store.append(snap, agent_id="agent-a")

    out = store.read_for_symbol("AAPL", agent_id="agent-a")
    assert len(out) == 1
    assert out[0] == snap


def test_read_respects_since_ns(tmp_path: pathlib.Path) -> None:
    """snapshots with decision_time_ns < since_ns are filtered out."""
    store = FeatureSnapshotStore(root=tmp_path)
    old = _snapshot(decision_time_ns=1_000_000_000)
    new = _snapshot(decision_time_ns=2_000_000_000)
    store.append(old, agent_id="agent-a")
    store.append(new, agent_id="agent-a")

    out = store.read_for_symbol("AAPL", agent_id="agent-a", since_ns=1_500_000_000)
    assert len(out) == 1
    assert out[0].decision_time_ns == 2_000_000_000


def test_read_respects_limit(tmp_path: pathlib.Path) -> None:
    """only the most-recent ``limit`` snapshots are returned."""
    store = FeatureSnapshotStore(root=tmp_path)
    for i in range(5):
        store.append(
            _snapshot(decision_time_ns=1_000_000_000 + i),
            agent_id="agent-a",
        )

    out = store.read_for_symbol("AAPL", agent_id="agent-a", limit=2)
    assert len(out) == 2
    # Newest-first.
    assert out[0].decision_time_ns == 1_000_000_004
    assert out[1].decision_time_ns == 1_000_000_003


def test_read_filters_by_symbol(tmp_path: pathlib.Path) -> None:
    """snapshots whose rows don't carry the symbol are skipped."""
    store = FeatureSnapshotStore(root=tmp_path)
    store.append(
        _snapshot(rows=[_row(symbol="AAPL", ts=1_000_000_000)]),
        agent_id="agent-a",
    )
    store.append(
        _snapshot(rows=[_row(symbol="MSFT", ts=1_000_000_000)]),
        agent_id="agent-a",
    )

    aapl = store.read_for_symbol("AAPL", agent_id="agent-a")
    msft = store.read_for_symbol("MSFT", agent_id="agent-a")
    assert len(aapl) == 1
    assert len(msft) == 1
    assert aapl[0].rows[0].symbol == "AAPL"
    assert msft[0].rows[0].symbol == "MSFT"


def test_read_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    """an agent with no file yet returns an empty list."""
    store = FeatureSnapshotStore(root=tmp_path)
    assert store.read_for_symbol("AAPL", agent_id="agent-a") == []


# --------------------------------------------------------------------------- #
# append_if_missing                                                           #
# --------------------------------------------------------------------------- #


def test_append_if_missing_dedup(tmp_path: pathlib.Path) -> None:
    """the same prediction_id is only recorded once."""
    store = FeatureSnapshotStore(root=tmp_path)
    snap = _snapshot(decision_time_ns=1_000_000_000)

    first = store.append_if_missing("pred-0001", snap, agent_id="agent-a")
    second = store.append_if_missing("pred-0001", snap, agent_id="agent-a")

    assert first is True
    assert second is False
    out = store.read_for_symbol("AAPL", agent_id="agent-a")
    assert len(out) == 1


def test_append_if_missing_distinct_ids(tmp_path: pathlib.Path) -> None:
    """distinct prediction_ids are both recorded."""
    store = FeatureSnapshotStore(root=tmp_path)
    snap = _snapshot(decision_time_ns=1_000_000_000)

    assert store.append_if_missing("pred-0001", snap, agent_id="agent-a") is True
    assert store.append_if_missing("pred-0002", snap, agent_id="agent-a") is True
    out = store.read_for_symbol("AAPL", agent_id="agent-a")
    assert len(out) == 2


def test_append_if_missing_empty_prediction_id_raises(
    tmp_path: pathlib.Path,
) -> None:
    """an empty prediction_id is rejected."""
    store = FeatureSnapshotStore(root=tmp_path)
    with pytest.raises(ValueError):
        store.append_if_missing("", _snapshot(), agent_id="agent-a")


# --------------------------------------------------------------------------- #
# Failure path                                                                #
# --------------------------------------------------------------------------- #


def test_malformed_jsonl_line_skipped(tmp_path: pathlib.Path) -> None:
    """a corrupt line in the middle must not take the read down."""
    store = FeatureSnapshotStore(root=tmp_path)
    good = _snapshot(decision_time_ns=1_000_000_000)
    store.append(good, agent_id="agent-a")

    path = store._path("agent-a")
    # Inject a malformed line between two good ones.
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    store.append(_snapshot(decision_time_ns=2_000_000_000), agent_id="agent-a")

    out = store.read_for_symbol("AAPL", agent_id="agent-a")
    # Both good snapshots survive; the corrupt line is skipped.
    assert len(out) == 2
    assert {s.decision_time_ns for s in out} == {1_000_000_000, 2_000_000_000}


def test_malformed_snapshot_payload_skipped(tmp_path: pathlib.Path) -> None:
    """a line whose snapshot payload fails schema validation is skipped."""
    store = FeatureSnapshotStore(root=tmp_path)
    store.append(_snapshot(decision_time_ns=1_000_000_000), agent_id="agent-a")

    path = store._path("agent-a")
    # Well-formed JSON, but the snapshot payload is missing required
    # fields -- FeatureSnapshot.model_validate raises ValueError.
    bad_line = json.dumps({"prediction_id": "x", "snapshot": {"rows": []}})
    with path.open("a", encoding="utf-8") as f:
        f.write(bad_line + "\n")

    out = store.read_for_symbol("AAPL", agent_id="agent-a")
    assert len(out) == 1


def test_append_bad_agent_id_raises(tmp_path: pathlib.Path) -> None:
    """agent_id with a forbidden character is rejected."""
    store = FeatureSnapshotStore(root=tmp_path)
    with pytest.raises(ValueError):
        store.append(_snapshot(), agent_id="bad/agent")
    with pytest.raises(ValueError):
        store.append(_snapshot(), agent_id="")
    with pytest.raises(ValueError):
        store.append(_snapshot(), agent_id=".hidden")


def test_read_for_symbol_bad_symbol_raises(tmp_path: pathlib.Path) -> None:
    """an empty symbol is rejected on the read path."""
    store = FeatureSnapshotStore(root=tmp_path)
    with pytest.raises(ValueError):
        store.read_for_symbol("", agent_id="agent-a")
    with pytest.raises(ValueError):
        store.read_for_symbol("AAPL", agent_id="agent-a", limit=0)


# --------------------------------------------------------------------------- #
# read_by_prediction_id                                                       #
# --------------------------------------------------------------------------- #


def test_read_by_prediction_id_returns_matching_snapshot(
    tmp_path: pathlib.Path,
) -> None:
    """read_by_prediction_id returns the snapshot for the given id."""
    store = FeatureSnapshotStore(root=tmp_path)
    snap_a = _snapshot(decision_time_ns=1_000_000_000)
    snap_b = _snapshot(decision_time_ns=2_000_000_000)

    store.append_if_missing("pred-0001", snap_a, agent_id="agent-a")
    store.append_if_missing("pred-0002", snap_b, agent_id="agent-a")

    out = store.read_by_prediction_id("pred-0001", agent_id="agent-a")
    assert out is not None
    assert out == snap_a

    out = store.read_by_prediction_id("pred-0002", agent_id="agent-a")
    assert out is not None
    assert out == snap_b


def test_read_by_prediction_id_returns_none_when_missing(
    tmp_path: pathlib.Path,
) -> None:
    """read_by_prediction_id returns None when no match is found."""
    store = FeatureSnapshotStore(root=tmp_path)
    store.append_if_missing("pred-0001", _snapshot(), agent_id="agent-a")

    assert store.read_by_prediction_id("pred-9999", agent_id="agent-a") is None


def test_read_by_prediction_id_returns_none_when_file_missing(
    tmp_path: pathlib.Path,
) -> None:
    """read_by_prediction_id returns None when the agent file doesn't exist."""
    store = FeatureSnapshotStore(root=tmp_path)
    assert store.read_by_prediction_id("pred-0001", agent_id="agent-a") is None


def test_read_by_prediction_id_empty_prediction_id_returns_none(
    tmp_path: pathlib.Path,
) -> None:
    """an empty prediction_id short-circuits to None."""
    store = FeatureSnapshotStore(root=tmp_path)
    assert store.read_by_prediction_id("", agent_id="agent-a") is None


def test_read_by_prediction_id_skips_malformed_lines(
    tmp_path: pathlib.Path,
) -> None:
    """a malformed line is skipped and does not prevent a later match."""
    store = FeatureSnapshotStore(root=tmp_path)
    good = _snapshot(decision_time_ns=1_000_000_000)
    store.append_if_missing("pred-0001", good, agent_id="agent-a")

    path = store._path("agent-a")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    snap_b = _snapshot(decision_time_ns=2_000_000_000)
    store.append_if_missing("pred-0002", snap_b, agent_id="agent-a")

    out = store.read_by_prediction_id("pred-0002", agent_id="agent-a")
    assert out is not None
    assert out == snap_b
