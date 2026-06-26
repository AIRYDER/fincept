"""Tests for fincept_core.datasets.settlement.

Covers the acceptance criteria from the ml-dataset-evidence-spine plan
(todo 3): round-trip, idempotency (duplicate rejected), look-ahead
(rejected), status state transitions, missing file -> empty list,
malformed JSONL line skipped.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from fincept_core.datasets.settlement import (
    DEFAULT_COST_MODEL,
    DEFAULT_COST_MODEL_VERSION,
    SettlementError,
    SettlementRecord,
    SettlementStore,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_record(
    *,
    prediction_id: str = "pred-0001",
    agent_id: str = "agent-a",
    model_name: str = "model-x",
    symbol: str = "AAPL",
    ts_event: int = 1_000_000_000,
    horizon_ns: int = 60_000_000_000,
    cost_model_version: str = DEFAULT_COST_MODEL_VERSION,
    status: str = "pending_time",
    settled_at_ns: int | None = None,
    realized_return_gross: float | None = None,
    realized_return_net: float | None = None,
    brier_component: float | None = None,
    decision_window_start_ns: int | None = None,
    decision_window_end_ns: int | None = None,
    failure_reason: str | None = None,
    cost_breakdown_spread_bps_override: float | None = None,
) -> SettlementRecord:
    dws = ts_event if decision_window_start_ns is None else decision_window_start_ns
    dwe = ts_event + horizon_ns if decision_window_end_ns is None else decision_window_end_ns
    return SettlementRecord(
        prediction_id=prediction_id,
        agent_id=agent_id,
        model_name=model_name,
        symbol=symbol,
        ts_event=ts_event,
        horizon_ns=horizon_ns,
        decision_window_start_ns=dws,
        decision_window_end_ns=dwe,
        cost_model_version=cost_model_version,
        realized_return_gross=realized_return_gross,
        realized_return_net=realized_return_net,
        cost_breakdown_fee_bps=DEFAULT_COST_MODEL["fee_bps"],
        cost_breakdown_spread_bps=(
            DEFAULT_COST_MODEL["spread_bps"]
            if cost_breakdown_spread_bps_override is None
            else cost_breakdown_spread_bps_override
        ),
        cost_breakdown_slippage_bps=DEFAULT_COST_MODEL["slippage_bps"],
        brier_component=brier_component,
        status=status,
        settled_at_ns=settled_at_ns,
        failure_reason=failure_reason,
    )


# A fixed "now" well past every test record's decision window end so the
# look-ahead guard passes by default.  Tests that exercise the guard
# pass their own now_ns.
NOW_NS = 10_000_000_000_000


# --------------------------------------------------------------------------- #
# 1. Round-trip                                                               #
# --------------------------------------------------------------------------- #


def test_append_and_read_round_trip(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(
        status="settled",
        settled_at_ns=NOW_NS - 1,
        realized_return_gross=0.012,
        realized_return_net=0.004,
        brier_component=0.18,
    )
    store.append(rec, now_ns=NOW_NS)

    got = store.read("pred-0001")
    assert got is not None
    assert got.prediction_id == "pred-0001"
    assert got.agent_id == "agent-a"
    assert got.status == "settled"
    assert got.realized_return_gross == pytest.approx(0.012)
    assert got.realized_return_net == pytest.approx(0.004)
    assert got.brier_component == pytest.approx(0.18)
    assert got.settled_at_ns == NOW_NS - 1


def test_read_for_agent_returns_record(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(status="settled", settled_at_ns=NOW_NS - 1)
    store.append(rec, now_ns=NOW_NS)

    rows = store.read_for_agent("agent-a")
    assert len(rows) == 1
    assert rows[0].prediction_id == "pred-0001"


def test_cost_model_version_defaults_to_v1_default(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(status="settled", settled_at_ns=NOW_NS - 1)
    store.append(rec, now_ns=NOW_NS)

    got = store.read("pred-0001")
    assert got is not None
    assert got.cost_model_version == DEFAULT_COST_MODEL_VERSION == "v1.default"
    assert got.cost_breakdown_fee_bps == 5.0
    assert got.cost_breakdown_spread_bps == 3.0
    assert got.cost_breakdown_slippage_bps == 0.0


# --------------------------------------------------------------------------- #
# 2. Idempotency                                                              #
# --------------------------------------------------------------------------- #


def test_duplicate_prediction_id_and_cost_model_rejected(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(status="settled", settled_at_ns=NOW_NS - 1)
    store.append(rec, now_ns=NOW_NS)

    with pytest.raises(SettlementError) as exc:
        store.append(rec, now_ns=NOW_NS)
    assert exc.value.code == "duplicate"


def test_same_prediction_id_different_cost_model_allowed(tmp_path: pathlib.Path) -> None:
    """A new cost-model version appends a new row (history preserved)."""
    store = SettlementStore(root=tmp_path)
    rec1 = _make_record(
        prediction_id="pred-dup",
        status="settled",
        settled_at_ns=NOW_NS - 1,
        cost_model_version="v1.default",
    )
    store.append(rec1, now_ns=NOW_NS)

    rec2 = _make_record(
        prediction_id="pred-dup",
        status="settled",
        settled_at_ns=NOW_NS - 1,
        cost_model_version="v2.tight",
    )
    store.append(rec2, now_ns=NOW_NS)

    rows = store.read_for_agent("agent-a")
    assert len(rows) == 2
    versions = {r.cost_model_version for r in rows}
    assert versions == {"v1.default", "v2.tight"}


# --------------------------------------------------------------------------- #
# 3. Look-ahead guard                                                         #
# --------------------------------------------------------------------------- #


def test_look_ahead_rejected(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    # decision_window_end_ns is ts_event + horizon_ns = 61_000_000_000.
    rec = _make_record(status="settled", settled_at_ns=60_000_000_000)
    with pytest.raises(SettlementError) as exc:
        store.append(rec, now_ns=60_000_000_000)  # now < window_end
    assert exc.value.code == "look_ahead"


def test_look_ahead_boundary_succeeds(tmp_path: pathlib.Path) -> None:
    """decision_window_end_ns == now_ns is allowed (horizon just elapsed)."""
    store = SettlementStore(root=tmp_path)
    window_end = 1_000_000_000 + 60_000_000_000  # 61_000_000_000
    rec = _make_record(
        status="settled",
        settled_at_ns=window_end,
        decision_window_end_ns=window_end,
    )
    store.append(rec, now_ns=window_end)  # boundary
    got = store.read("pred-0001")
    assert got is not None
    assert got.status == "settled"


# --------------------------------------------------------------------------- #
# 4. Invalid prediction_id                                                    #
# --------------------------------------------------------------------------- #


def test_empty_prediction_id_rejected(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(prediction_id="", status="settled", settled_at_ns=NOW_NS - 1)
    with pytest.raises(SettlementError) as exc:
        store.append(rec, now_ns=NOW_NS)
    assert exc.value.code == "invalid_prediction_id"


# --------------------------------------------------------------------------- #
# 5. Missing settled_at_ns                                                    #
# --------------------------------------------------------------------------- #


def test_settled_without_settled_at_rejected(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(status="settled", settled_at_ns=None)
    with pytest.raises(SettlementError) as exc:
        store.append(rec, now_ns=NOW_NS)
    assert exc.value.code == "missing_settled_at"


# --------------------------------------------------------------------------- #
# 6. Status state transitions                                                 #
# --------------------------------------------------------------------------- #


def test_pending_time_status_round_trip(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(status="pending_time", settled_at_ns=None)
    store.append(rec, now_ns=NOW_NS)
    got = store.read("pred-0001")
    assert got is not None
    assert got.status == "pending_time"
    assert got.settled_at_ns is None
    assert got.realized_return_gross is None


def test_pending_data_status_round_trip(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(status="pending_data", settled_at_ns=None)
    store.append(rec, now_ns=NOW_NS)
    got = store.read("pred-0001")
    assert got is not None
    assert got.status == "pending_data"


def test_failed_status_with_reason_round_trip(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(
        status="failed",
        settled_at_ns=NOW_NS - 1,
        failure_reason="price feed gap > 5m",
    )
    store.append(rec, now_ns=NOW_NS)
    got = store.read("pred-0001")
    assert got is not None
    assert got.status == "failed"
    assert got.failure_reason == "price feed gap > 5m"


def test_settled_then_pending_data_for_different_prediction(tmp_path: pathlib.Path) -> None:
    """Mixed statuses coexist in one agent file."""
    store = SettlementStore(root=tmp_path)
    settled = _make_record(
        prediction_id="pred-s",
        status="settled",
        settled_at_ns=NOW_NS - 1,
        realized_return_gross=0.01,
        realized_return_net=0.002,
    )
    pending = _make_record(
        prediction_id="pred-p",
        status="pending_data",
        settled_at_ns=None,
    )
    store.append(settled, now_ns=NOW_NS)
    store.append(pending, now_ns=NOW_NS)

    rows = store.read_for_agent("agent-a")
    assert len(rows) == 2
    by_id = {r.prediction_id: r for r in rows}
    assert by_id["pred-s"].status == "settled"
    assert by_id["pred-p"].status == "pending_data"


# --------------------------------------------------------------------------- #
# 7. Missing file -> empty list                                               #
# --------------------------------------------------------------------------- #


def test_read_for_agent_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    assert store.read_for_agent("never-seen") == []


def test_read_missing_prediction_id_returns_none(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    assert store.read("nope") is None


# --------------------------------------------------------------------------- #
# 8. Malformed JSONL line skipped                                             #
# --------------------------------------------------------------------------- #


def test_malformed_jsonl_line_skipped(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(prediction_id="pred-good", status="settled", settled_at_ns=NOW_NS - 1)
    store.append(rec, now_ns=NOW_NS)

    # Manually append a garbage line and a second good line.
    path = tmp_path / "agent-a.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
        f.write("plain text not json\n")

    rec2 = _make_record(prediction_id="pred-good2", status="settled", settled_at_ns=NOW_NS - 1)
    store.append(rec2, now_ns=NOW_NS)

    rows = store.read_for_agent("agent-a")
    # Two good records survive; the two garbage lines are skipped.
    assert len(rows) == 2
    ids = {r.prediction_id for r in rows}
    assert ids == {"pred-good", "pred-good2"}


def test_malformed_line_does_not_break_read_by_prediction_id(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(prediction_id="pred-target", status="settled", settled_at_ns=NOW_NS - 1)
    store.append(rec, now_ns=NOW_NS)

    path = tmp_path / "agent-a.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("garbage line\n")

    got = store.read("pred-target")
    assert got is not None
    assert got.prediction_id == "pred-target"


# --------------------------------------------------------------------------- #
# 9. Frozen + extra='forbid'                                                  #
# --------------------------------------------------------------------------- #


def test_record_is_frozen() -> None:
    rec = _make_record()
    with pytest.raises(ValidationError):
        rec.status = "settled"  # type: ignore[misc]


def test_record_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        SettlementRecord(
            prediction_id="p",
            agent_id="a",
            model_name="m",
            symbol="S",
            ts_event=1,
            horizon_ns=2,
            decision_window_start_ns=1,
            decision_window_end_ns=3,
            cost_breakdown_fee_bps=5.0,
            cost_breakdown_spread_bps=3.0,
            unexpected_extra_field="boom",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# 10. Bad agent_id                                                            #
# --------------------------------------------------------------------------- #


def test_bad_agent_id_rejected(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(agent_id="../escape", status="settled", settled_at_ns=NOW_NS - 1)
    with pytest.raises(ValueError):
        store.append(rec, now_ns=NOW_NS)


# --------------------------------------------------------------------------- #
# 11. JSONL persistence shape                                                 #
# --------------------------------------------------------------------------- #


def test_jsonl_line_is_valid_json(tmp_path: pathlib.Path) -> None:
    store = SettlementStore(root=tmp_path)
    rec = _make_record(status="settled", settled_at_ns=NOW_NS - 1)
    store.append(rec, now_ns=NOW_NS)

    path = tmp_path / "agent-a.jsonl"
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["prediction_id"] == "pred-0001"
    assert payload["settlement_schema_version"] == 1
    assert payload["cost_model_version"] == "v1.default"


# --------------------------------------------------------------------------- #
# 12. Status state machine (todo 12)                                          #
# --------------------------------------------------------------------------- #
#
# Focused state-machine tests added by todo 12 of the
# ml-dataset-evidence-spine plan.  These exercise the terminal/non-terminal
# status rules and the two new Pydantic validators
# (``cost_breakdown_spread_bps <= 100`` and
# ``decision_window_start_ns <= decision_window_end_ns``) without pulling
# in any state-machine framework or the real settlement store in
# ``services/quant_foundry``.


def test_pending_time_to_settled_allowed_when_window_elapsed(tmp_path: pathlib.Path) -> None:
    """(a) pending_time -> settled succeeds when now_ns >= decision_window_end_ns."""
    store = SettlementStore(root=tmp_path)
    pending = _make_record(
        prediction_id="pred-sm-a",
        status="pending_time",
        settled_at_ns=None,
    )
    store.append(pending, now_ns=NOW_NS)

    settled = _make_record(
        prediction_id="pred-sm-a",
        status="settled",
        settled_at_ns=NOW_NS - 1,
        realized_return_gross=0.02,
        realized_return_net=0.01,
    )
    store.append(settled, now_ns=NOW_NS)

    rows = store.read_for_agent("agent-a")
    assert len(rows) == 2
    assert rows[0].status == "pending_time"
    assert rows[1].status == "settled"


def test_pending_time_to_settled_rejected_when_window_in_future(
    tmp_path: pathlib.Path,
) -> None:
    """(a) pending_time -> settled raises look_ahead when now < window_end."""
    store = SettlementStore(root=tmp_path)
    pending = _make_record(
        prediction_id="pred-sm-a2",
        status="pending_time",
        settled_at_ns=None,
    )
    store.append(pending, now_ns=NOW_NS)

    window_end = 1_000_000_000 + 60_000_000_000  # 61_000_000_000
    settled = _make_record(
        prediction_id="pred-sm-a2",
        status="settled",
        settled_at_ns=window_end,
        decision_window_end_ns=window_end,
    )
    with pytest.raises(SettlementError) as exc:
        store.append(settled, now_ns=window_end - 1)  # now < window_end
    assert exc.value.code == "look_ahead"


def test_pending_time_to_failed_allowed_with_reason(tmp_path: pathlib.Path) -> None:
    """(b) pending_time -> failed is allowed with a failure_reason."""
    store = SettlementStore(root=tmp_path)
    pending = _make_record(
        prediction_id="pred-sm-b",
        status="pending_time",
        settled_at_ns=None,
    )
    store.append(pending, now_ns=NOW_NS)

    failed = _make_record(
        prediction_id="pred-sm-b",
        status="failed",
        settled_at_ns=NOW_NS - 1,
        failure_reason="price feed gap > 5m",
    )
    store.append(failed, now_ns=NOW_NS)

    rows = store.read_for_agent("agent-a")
    assert len(rows) == 2
    assert rows[1].status == "failed"
    assert rows[1].failure_reason == "price feed gap > 5m"


def test_settled_is_terminal_rewrite_raises_duplicate(tmp_path: pathlib.Path) -> None:
    """(c) settled is terminal -- re-writing same key raises code=duplicate."""
    store = SettlementStore(root=tmp_path)
    rec = _make_record(
        prediction_id="pred-sm-c",
        status="settled",
        settled_at_ns=NOW_NS - 1,
    )
    store.append(rec, now_ns=NOW_NS)

    with pytest.raises(SettlementError) as exc:
        store.append(rec, now_ns=NOW_NS)
    assert exc.value.code == "duplicate"


def test_failed_is_terminal_same_cost_model_raises_duplicate(
    tmp_path: pathlib.Path,
) -> None:
    """(d) failed is terminal -- re-writing same cost_model_version raises duplicate."""
    store = SettlementStore(root=tmp_path)
    failed = _make_record(
        prediction_id="pred-sm-d",
        status="failed",
        settled_at_ns=NOW_NS - 1,
        failure_reason="price feed gap > 5m",
    )
    store.append(failed, now_ns=NOW_NS)

    with pytest.raises(SettlementError) as exc:
        store.append(failed, now_ns=NOW_NS)
    assert exc.value.code == "duplicate"


def test_failed_rewrite_different_cost_model_allowed(tmp_path: pathlib.Path) -> None:
    """(d) failed re-writing with a different cost_model_version succeeds."""
    store = SettlementStore(root=tmp_path)
    failed_v1 = _make_record(
        prediction_id="pred-sm-d2",
        status="failed",
        settled_at_ns=NOW_NS - 1,
        failure_reason="price feed gap > 5m",
        cost_model_version="v1.default",
    )
    store.append(failed_v1, now_ns=NOW_NS)

    failed_v2 = _make_record(
        prediction_id="pred-sm-d2",
        status="failed",
        settled_at_ns=NOW_NS - 1,
        failure_reason="price feed gap > 5m",
        cost_model_version="v2.tight",
    )
    store.append(failed_v2, now_ns=NOW_NS)

    rows = store.read_for_agent("agent-a")
    assert len(rows) == 2
    versions = {r.cost_model_version for r in rows}
    assert versions == {"v1.default", "v2.tight"}


def test_cost_breakdown_spread_bps_over_100_rejected() -> None:
    """(e) cost_breakdown_spread_bps > 100 is rejected by a field_validator."""
    with pytest.raises(ValidationError):
        SettlementRecord(
            prediction_id="pred-sm-e",
            agent_id="agent-a",
            model_name="model-x",
            symbol="AAPL",
            ts_event=1_000_000_000,
            horizon_ns=60_000_000_000,
            decision_window_start_ns=1_000_000_000,
            decision_window_end_ns=61_000_000_000,
            cost_breakdown_fee_bps=5.0,
            cost_breakdown_spread_bps=101.0,  # over the sanity bound
        )


def test_cost_breakdown_spread_bps_at_100_allowed() -> None:
    """(e) the boundary spread_bps == 100 is allowed."""
    rec = _make_record(
        prediction_id="pred-sm-e2",
        cost_breakdown_spread_bps_override=100.0,
    )
    assert rec.cost_breakdown_spread_bps == 100.0


def test_decision_window_start_after_end_rejected() -> None:
    """(f) decision_window_start_ns > decision_window_end_ns is rejected."""
    with pytest.raises(ValidationError):
        SettlementRecord(
            prediction_id="pred-sm-f",
            agent_id="agent-a",
            model_name="model-x",
            symbol="AAPL",
            ts_event=1_000_000_000,
            horizon_ns=60_000_000_000,
            decision_window_start_ns=70_000_000_000,  # after end
            decision_window_end_ns=61_000_000_000,
            cost_breakdown_fee_bps=5.0,
            cost_breakdown_spread_bps=3.0,
        )


def test_decision_window_start_equals_end_allowed() -> None:
    """(f) the boundary start == end is allowed (zero-length window)."""
    rec = _make_record(
        prediction_id="pred-sm-f2",
        decision_window_start_ns=61_000_000_000,
        decision_window_end_ns=61_000_000_000,
    )
    assert rec.decision_window_start_ns == rec.decision_window_end_ns
