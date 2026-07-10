"""Tests for quant_foundry.lob_manifest (T-LOB.1 LOBDatasetManifest).

Tests verify:
- LOBVenue, AdjustmentPolicy, LabelHorizonUnit enum construction and
  membership.
- LOBSession construction, defaults, and validation (ordering, session
  type, n_events).
- LOBRecord construction, leakage-safe invariants (receive_time >=
  event_time, level counts, no crossed book, positive prices).
- LabelSpec construction and validation (horizon, label type,
  normalization).
- LOBDatasetManifest construction, dedupe, ordering, session split,
  record/session consistency.
- validate_no_future_leakage (valid, future leakage).
- validate_session_split (valid, overlapping, missing train/validation).
- validate_sequence_ordering (valid, gap, duplicate, out-of-order
  event_time).
- compute_lob_data_hash (determinism, order-independence).
- LOBManifestBuilder (fluent, fail-closed).
- Fail-closed: crossed book, receive_time < event_time, duplicate
  sequence_ids, duplicate record_ids, missing session.
- Edge cases: single record, single session, minimal book depth.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from quant_foundry.lob_manifest import (
    AdjustmentPolicy,
    LabelHorizonUnit,
    LabelSpec,
    LOBDatasetManifest,
    LOBManifestBuilder,
    LOBRecord,
    LOBSession,
    LOBVenue,
    compute_lob_data_hash,
    validate_no_future_leakage,
    validate_sequence_ordering,
    validate_session_split,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str = "2024-01-15_NASDAQ_AAPL",
    session_start: str = "2024-01-15T09:30:00Z",
    session_end: str = "2024-01-15T16:00:00Z",
    session_type: str = "train",
    n_events: int = 1000,
) -> LOBSession:
    """Build a LOBSession with defaults."""
    return LOBSession(
        session_id=session_id,
        session_start=session_start,
        session_end=session_end,
        session_type=session_type,
        n_events=n_events,
    )


_RECORD_SENTINEL = object()


def _make_record(
    record_id: str = "NASDAQ_AAPL_0",
    venue: LOBVenue = LOBVenue.NASDAQ,
    symbol: str = "AAPL",
    sequence_id: int = 0,
    event_time: str = "2024-01-15T09:30:00.100Z",
    receive_time: Any = _RECORD_SENTINEL,
    book_depth: int = 10,
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW,
    session_id: str = "2024-01-15_NASDAQ_AAPL",
) -> LOBRecord:
    """Build a LOBRecord with defaults.

    If ``receive_time`` is not provided, it defaults to 5ms after
    ``event_time`` so the receive_time >= event_time invariant holds.
    """
    if bids is None:
        bids = [(150.00, 100), (149.99, 200)]
    if asks is None:
        asks = [(150.01, 100), (150.02, 200)]
    if receive_time is _RECORD_SENTINEL:
        # Default to 5ms after event_time.
        _et = event_time.replace("Z", "+00:00")
        _dt = datetime.fromisoformat(_et)
        receive_time = (_dt + timedelta(milliseconds=5)).isoformat()
    return LOBRecord(
        record_id=record_id,
        venue=venue,
        symbol=symbol,
        sequence_id=sequence_id,
        event_time=event_time,
        receive_time=receive_time,
        book_depth=book_depth,
        bids=bids,
        asks=asks,
        adjustment_policy=adjustment_policy,
        session_id=session_id,
    )


def _make_label_spec(
    horizon: int = 10,
    horizon_unit: LabelHorizonUnit = LabelHorizonUnit.EVENTS,
    label_type: str = "mid_price_return",
    normalization: str = "none",
) -> LabelSpec:
    """Build a LabelSpec with defaults."""
    return LabelSpec(
        horizon=horizon,
        horizon_unit=horizon_unit,
        label_type=label_type,
        normalization=normalization,
    )


def _make_train_session() -> LOBSession:
    return _make_session(
        session_id="2024-01-15_NASDAQ_AAPL_train",
        session_start="2024-01-15T09:30:00Z",
        session_end="2024-01-15T16:00:00Z",
        session_type="train",
    )


def _make_validation_session() -> LOBSession:
    return _make_session(
        session_id="2024-01-16_NASDAQ_AAPL_val",
        session_start="2024-01-16T09:30:00Z",
        session_end="2024-01-16T16:00:00Z",
        session_type="validation",
    )


def _make_manifest_kwargs(**overrides) -> dict:
    """Build kwargs for a valid LOBDatasetManifest."""
    train = _make_train_session()
    val = _make_validation_session()
    base = dict(
        dataset_id="lob_001",
        venue=LOBVenue.NASDAQ,
        symbol="AAPL",
        book_depth=10,
        adjustment_policy=AdjustmentPolicy.RAW,
        sessions=[train, val],
        label_specs=[_make_label_spec()],
        records=[
            _make_record(
                record_id="NASDAQ_AAPL_0",
                sequence_id=0,
                event_time="2024-01-15T09:30:00.100Z",
                receive_time="2024-01-15T09:30:00.105Z",
                session_id=train.session_id,
            ),
            _make_record(
                record_id="NASDAQ_AAPL_1",
                sequence_id=1,
                event_time="2024-01-15T09:30:00.200Z",
                receive_time="2024-01-15T09:30:00.205Z",
                session_id=train.session_id,
            ),
            _make_record(
                record_id="NASDAQ_AAPL_2",
                sequence_id=0,
                event_time="2024-01-16T09:30:00.100Z",
                receive_time="2024-01-16T09:30:00.105Z",
                session_id=val.session_id,
            ),
        ],
        data_uri="s3://bucket/lob_001.parquet",
        data_hash="a" * 64,
        created_at="2024-01-20T00:00:00Z",
    )
    base.update(overrides)
    return base


def _make_manifest(**overrides) -> LOBDatasetManifest:
    """Build a valid LOBDatasetManifest."""
    return LOBDatasetManifest(**_make_manifest_kwargs(**overrides))


# ---------------------------------------------------------------------------
# LOBVenue enum
# ---------------------------------------------------------------------------


class TestLOBVenue:
    """Tests for the LOBVenue enum."""

    def test_venue_values(self) -> None:
        assert LOBVenue.NASDAQ.value == "NASDAQ"
        assert LOBVenue.NYSE.value == "NYSE"
        assert LOBVenue.CME.value == "CME"
        assert LOBVenue.ICE.value == "ICE"
        assert LOBVenue.EUREX.value == "EUREX"
        assert LOBVenue.BATS.value == "BATS"
        assert LOBVenue.IEX.value == "IEX"
        assert LOBVenue.OTHER.value == "OTHER"

    def test_venue_from_string(self) -> None:
        assert LOBVenue("NASDAQ") is LOBVenue.NASDAQ
        assert LOBVenue("CME") is LOBVenue.CME

    def test_venue_is_str(self) -> None:
        assert isinstance(LOBVenue.NASDAQ, str)
        assert LOBVenue.NASDAQ == "NASDAQ"

    def test_venue_count(self) -> None:
        assert len(LOBVenue) == 8


# ---------------------------------------------------------------------------
# AdjustmentPolicy enum
# ---------------------------------------------------------------------------


class TestAdjustmentPolicy:
    """Tests for the AdjustmentPolicy enum."""

    def test_policy_values(self) -> None:
        assert AdjustmentPolicy.RAW.value == "RAW"
        assert AdjustmentPolicy.SPLIT_ADJUSTED.value == "SPLIT_ADJUSTED"
        assert AdjustmentPolicy.DIVIDEND_ADJUSTED.value == "DIVIDEND_ADJUSTED"
        assert AdjustmentPolicy.FULLY_ADJUSTED.value == "FULLY_ADJUSTED"
        assert AdjustmentPolicy.BACK_ADJUSTED.value == "BACK_ADJUSTED"

    def test_policy_from_string(self) -> None:
        assert AdjustmentPolicy("RAW") is AdjustmentPolicy.RAW
        assert AdjustmentPolicy("FULLY_ADJUSTED") is AdjustmentPolicy.FULLY_ADJUSTED

    def test_policy_is_str(self) -> None:
        assert isinstance(AdjustmentPolicy.RAW, str)

    def test_policy_count(self) -> None:
        assert len(AdjustmentPolicy) == 5


# ---------------------------------------------------------------------------
# LabelHorizonUnit enum
# ---------------------------------------------------------------------------


class TestLabelHorizonUnit:
    """Tests for the LabelHorizonUnit enum."""

    def test_unit_values(self) -> None:
        assert LabelHorizonUnit.EVENTS.value == "EVENTS"
        assert LabelHorizonUnit.MILLISECONDS.value == "MILLISECONDS"
        assert LabelHorizonUnit.SECONDS.value == "SECONDS"

    def test_unit_from_string(self) -> None:
        assert LabelHorizonUnit("EVENTS") is LabelHorizonUnit.EVENTS
        assert LabelHorizonUnit("SECONDS") is LabelHorizonUnit.SECONDS

    def test_unit_is_str(self) -> None:
        assert isinstance(LabelHorizonUnit.EVENTS, str)

    def test_unit_count(self) -> None:
        assert len(LabelHorizonUnit) == 3


# ---------------------------------------------------------------------------
# LOBSession
# ---------------------------------------------------------------------------


class TestLOBSession:
    """Tests for LOBSession construction and validation."""

    def test_valid_session(self) -> None:
        s = _make_session()
        assert s.session_id == "2024-01-15_NASDAQ_AAPL"
        assert s.session_type == "train"
        assert s.n_events == 1000

    def test_frozen(self) -> None:
        s = _make_session()
        with pytest.raises(ValidationError):
            s.session_type = "validation"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            LOBSession(
                session_id="s",
                session_start="2024-01-15T09:30:00Z",
                session_end="2024-01-15T16:00:00Z",
                session_type="train",
                n_events=100,
                extra_field="bad",
            )

    def test_empty_session_id(self) -> None:
        with pytest.raises(ValidationError, match="session_id"):
            _make_session(session_id="")

    def test_invalid_session_type(self) -> None:
        with pytest.raises(ValidationError, match="session_type"):
            _make_session(session_type="holdout")

    def test_n_events_zero(self) -> None:
        with pytest.raises(ValidationError, match="n_events"):
            _make_session(n_events=0)

    def test_n_events_negative(self) -> None:
        with pytest.raises(ValidationError, match="n_events"):
            _make_session(n_events=-1)

    def test_end_before_start(self) -> None:
        with pytest.raises(ValidationError, match="session_end must be > session_start"):
            _make_session(
                session_start="2024-01-15T16:00:00Z",
                session_end="2024-01-15T09:30:00Z",
            )

    def test_end_equal_start(self) -> None:
        with pytest.raises(ValidationError, match="session_end must be > session_start"):
            _make_session(
                session_start="2024-01-15T09:30:00Z",
                session_end="2024-01-15T09:30:00Z",
            )

    def test_validation_type(self) -> None:
        s = _make_session(session_type="validation")
        assert s.session_type == "validation"

    def test_test_type(self) -> None:
        s = _make_session(session_type="test")
        assert s.session_type == "test"


# ---------------------------------------------------------------------------
# LOBRecord
# ---------------------------------------------------------------------------


class TestLOBRecord:
    """Tests for LOBRecord construction and validation."""

    def test_valid_record(self) -> None:
        r = _make_record()
        assert r.record_id == "NASDAQ_AAPL_0"
        assert r.venue == LOBVenue.NASDAQ
        assert r.sequence_id == 0
        assert r.book_depth == 10

    def test_frozen(self) -> None:
        r = _make_record()
        with pytest.raises(ValidationError):
            r.sequence_id = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            LOBRecord(
                record_id="r",
                venue=LOBVenue.NASDAQ,
                symbol="AAPL",
                sequence_id=0,
                event_time="2024-01-15T09:30:00Z",
                receive_time="2024-01-15T09:30:00Z",
                book_depth=10,
                bids=[(150.0, 100)],
                asks=[(150.01, 100)],
                adjustment_policy=AdjustmentPolicy.RAW,
                session_id="s",
                extra_field="bad",
            )

    def test_empty_record_id(self) -> None:
        with pytest.raises(ValidationError, match="record_id"):
            _make_record(record_id="")

    def test_empty_symbol(self) -> None:
        with pytest.raises(ValidationError, match="symbol"):
            _make_record(symbol="")

    def test_negative_sequence_id(self) -> None:
        with pytest.raises(ValidationError, match="sequence_id"):
            _make_record(sequence_id=-1)

    def test_zero_sequence_id_ok(self) -> None:
        r = _make_record(sequence_id=0)
        assert r.sequence_id == 0

    def test_book_depth_zero(self) -> None:
        with pytest.raises(ValidationError, match="book_depth"):
            _make_record(book_depth=0)

    def test_receive_before_event(self) -> None:
        with pytest.raises(ValidationError, match="receive_time must be >= event_time"):
            _make_record(
                event_time="2024-01-15T09:30:00.200Z",
                receive_time="2024-01-15T09:30:00.100Z",
            )

    def test_receive_equal_event_ok(self) -> None:
        r = _make_record(
            event_time="2024-01-15T09:30:00.100Z",
            receive_time="2024-01-15T09:30:00.100Z",
        )
        assert r.receive_time == r.event_time

    def test_bids_exceed_book_depth(self) -> None:
        with pytest.raises(ValidationError, match="bids length"):
            _make_record(book_depth=1, bids=[(150.0, 100), (149.0, 200)])

    def test_asks_exceed_book_depth(self) -> None:
        with pytest.raises(ValidationError, match="asks length"):
            _make_record(
                book_depth=1,
                bids=[(150.0, 100)],
                asks=[(151.0, 100), (152.0, 200)],
            )

    def test_bid_price_zero(self) -> None:
        with pytest.raises(ValidationError, match="bid prices must be > 0"):
            _make_record(bids=[(0.0, 100)])

    def test_bid_price_negative(self) -> None:
        with pytest.raises(ValidationError, match="bid prices must be > 0"):
            _make_record(bids=[(-1.0, 100)])

    def test_ask_price_zero(self) -> None:
        with pytest.raises(ValidationError, match="ask prices must be > 0"):
            _make_record(asks=[(0.0, 100)])

    def test_bid_size_negative(self) -> None:
        with pytest.raises(ValidationError, match="bid sizes must be >= 0"):
            _make_record(bids=[(150.0, -1)])

    def test_ask_size_negative(self) -> None:
        with pytest.raises(ValidationError, match="ask sizes must be >= 0"):
            _make_record(asks=[(151.0, -1)])

    def test_bid_size_zero_ok(self) -> None:
        r = _make_record(bids=[(150.0, 0)])
        assert r.bids[0][1] == 0

    def test_crossed_book(self) -> None:
        with pytest.raises(ValidationError, match="crossed/locked book"):
            _make_record(
                bids=[(151.00, 100)],
                asks=[(150.00, 100)],
            )

    def test_locked_book(self) -> None:
        with pytest.raises(ValidationError, match="crossed/locked book"):
            _make_record(
                bids=[(150.00, 100)],
                asks=[(150.00, 100)],
            )

    def test_empty_bids_ok(self) -> None:
        r = _make_record(bids=[], asks=[(151.0, 100)])
        assert r.bids == []

    def test_empty_asks_ok(self) -> None:
        r = _make_record(bids=[(150.0, 100)], asks=[])
        assert r.asks == []

    def test_empty_both_sides_ok(self) -> None:
        r = _make_record(bids=[], asks=[])
        assert r.bids == [] and r.asks == []

    def test_minimal_book_depth_one(self) -> None:
        r = _make_record(
            book_depth=1,
            bids=[(150.0, 100)],
            asks=[(150.01, 100)],
        )
        assert r.book_depth == 1


# ---------------------------------------------------------------------------
# LabelSpec
# ---------------------------------------------------------------------------


class TestLabelSpec:
    """Tests for LabelSpec construction and validation."""

    def test_valid_label_spec(self) -> None:
        ls = _make_label_spec()
        assert ls.horizon == 10
        assert ls.horizon_unit == LabelHorizonUnit.EVENTS
        assert ls.label_type == "mid_price_return"
        assert ls.normalization == "none"

    def test_default_normalization(self) -> None:
        ls = LabelSpec(
            horizon=5,
            horizon_unit=LabelHorizonUnit.SECONDS,
            label_type="spread",
        )
        assert ls.normalization == "none"

    def test_frozen(self) -> None:
        ls = _make_label_spec()
        with pytest.raises(ValidationError):
            ls.horizon = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            LabelSpec(
                horizon=10,
                horizon_unit=LabelHorizonUnit.EVENTS,
                label_type="mid_price_return",
                extra_field="bad",
            )

    def test_horizon_zero(self) -> None:
        with pytest.raises(ValidationError, match="horizon"):
            _make_label_spec(horizon=0)

    def test_horizon_negative(self) -> None:
        with pytest.raises(ValidationError, match="horizon"):
            _make_label_spec(horizon=-1)

    def test_invalid_label_type(self) -> None:
        with pytest.raises(ValidationError, match="label_type"):
            _make_label_spec(label_type="unknown")

    def test_invalid_normalization(self) -> None:
        with pytest.raises(ValidationError, match="normalization"):
            _make_label_spec(normalization="unknown")

    def test_all_label_types(self) -> None:
        for lt in ["mid_price_return", "spread", "imbalance", "trade_direction"]:
            ls = _make_label_spec(label_type=lt)
            assert ls.label_type == lt

    def test_all_normalizations(self) -> None:
        for norm in ["none", "z_score", "min_max"]:
            ls = _make_label_spec(normalization=norm)
            assert ls.normalization == norm

    def test_all_horizon_units(self) -> None:
        for unit in LabelHorizonUnit:
            ls = _make_label_spec(horizon_unit=unit)
            assert ls.horizon_unit == unit


# ---------------------------------------------------------------------------
# LOBDatasetManifest
# ---------------------------------------------------------------------------


class TestLOBDatasetManifest:
    """Tests for LOBDatasetManifest construction and validation."""

    def test_valid_manifest(self) -> None:
        m = _make_manifest()
        assert m.dataset_id == "lob_001"
        assert m.venue == LOBVenue.NASDAQ
        assert len(m.sessions) == 2
        assert len(m.records) == 3

    def test_frozen(self) -> None:
        m = _make_manifest()
        with pytest.raises(ValidationError):
            m.dataset_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            LOBDatasetManifest(**_make_manifest_kwargs(), extra="bad")  # type: ignore[arg-type]

    def test_empty_dataset_id(self) -> None:
        with pytest.raises(ValidationError, match="dataset_id"):
            _make_manifest(dataset_id="")

    def test_empty_symbol(self) -> None:
        with pytest.raises(ValidationError, match="symbol"):
            _make_manifest(symbol="")

    def test_book_depth_zero(self) -> None:
        with pytest.raises(ValidationError, match="book_depth"):
            _make_manifest(book_depth=0)

    def test_empty_sessions(self) -> None:
        with pytest.raises(ValidationError, match="sessions"):
            _make_manifest(sessions=[])

    def test_empty_label_specs(self) -> None:
        with pytest.raises(ValidationError, match="label_specs"):
            _make_manifest(label_specs=[])

    def test_empty_records(self) -> None:
        with pytest.raises(ValidationError, match="records"):
            _make_manifest(records=[])

    def test_invalid_data_hash(self) -> None:
        with pytest.raises(ValidationError, match="data_hash"):
            _make_manifest(data_hash="short")

    def test_empty_data_uri(self) -> None:
        with pytest.raises(ValidationError, match="data_uri"):
            _make_manifest(data_uri="")

    def test_duplicate_session_ids(self) -> None:
        train = _make_train_session()
        val = _make_validation_session()
        dup = _make_session(
            session_id=train.session_id,
            session_type="test",
            session_start="2024-02-01T09:30:00Z",
            session_end="2024-02-01T16:00:00Z",
        )
        with pytest.raises(ValidationError, match="duplicate session_ids"):
            _make_manifest(sessions=[train, val, dup])

    def test_duplicate_record_ids(self) -> None:
        records = _make_manifest_kwargs()["records"]
        records.append(
            _make_record(
                record_id="NASDAQ_AAPL_0",
                sequence_id=99,
                session_id=_make_validation_session().session_id,
            )
        )
        with pytest.raises(ValidationError, match="duplicate record_ids"):
            _make_manifest(records=records)

    def test_record_unknown_session(self) -> None:
        r = _make_record(session_id="nonexistent_session")
        with pytest.raises(ValidationError, match="unknown session_ids"):
            _make_manifest(records=[r])

    def test_record_mismatched_venue(self) -> None:
        r = _make_record(
            venue=LOBVenue.NYSE,
            session_id=_make_train_session().session_id,
        )
        with pytest.raises(ValidationError, match="mismatched venue"):
            _make_manifest(records=[r])

    def test_record_mismatched_symbol(self) -> None:
        r = _make_record(
            symbol="MSFT",
            session_id=_make_train_session().session_id,
        )
        with pytest.raises(ValidationError, match="mismatched symbol"):
            _make_manifest(records=[r])

    def test_record_mismatched_book_depth(self) -> None:
        r = _make_record(
            book_depth=5,
            session_id=_make_train_session().session_id,
        )
        with pytest.raises(ValidationError, match="mismatched book_depth"):
            _make_manifest(records=[r])

    def test_duplicate_sequence_id_in_session(self) -> None:
        train = _make_train_session()
        records = [
            _make_record(record_id="NASDAQ_AAPL_0", sequence_id=0, session_id=train.session_id),
            _make_record(
                record_id="NASDAQ_AAPL_1",
                sequence_id=0,
                event_time="2024-01-15T09:30:00.200Z",
                receive_time="2024-01-15T09:30:00.205Z",
                session_id=train.session_id,
            ),
        ]
        with pytest.raises(ValidationError, match="strictly monotonic"):
            _make_manifest(records=records)

    def test_out_of_order_event_time(self) -> None:
        train = _make_train_session()
        records = [
            _make_record(
                record_id="NASDAQ_AAPL_0",
                sequence_id=0,
                event_time="2024-01-15T09:30:00.300Z",
                receive_time="2024-01-15T09:30:00.305Z",
                session_id=train.session_id,
            ),
            _make_record(
                record_id="NASDAQ_AAPL_1",
                sequence_id=1,
                event_time="2024-01-15T09:30:00.100Z",
                receive_time="2024-01-15T09:30:00.105Z",
                session_id=train.session_id,
            ),
        ]
        with pytest.raises(ValidationError, match="non-decreasing"):
            _make_manifest(records=records)

    def test_missing_train_session(self) -> None:
        val = _make_validation_session()
        test = _make_session(
            session_id="2024-01-17_NASDAQ_AAPL_test",
            session_type="test",
            session_start="2024-01-17T09:30:00Z",
            session_end="2024-01-17T16:00:00Z",
        )
        records = [
            _make_record(record_id="NASDAQ_AAPL_0", sequence_id=0, session_id=val.session_id),
        ]
        with pytest.raises(ValidationError, match="train"):
            _make_manifest(sessions=[val, test], records=records)

    def test_missing_validation_session(self) -> None:
        train = _make_train_session()
        test = _make_session(
            session_id="2024-01-17_NASDAQ_AAPL_test",
            session_type="test",
            session_start="2024-01-17T09:30:00Z",
            session_end="2024-01-17T16:00:00Z",
        )
        records = [
            _make_record(record_id="NASDAQ_AAPL_0", sequence_id=0, session_id=train.session_id),
        ]
        with pytest.raises(ValidationError, match="validation"):
            _make_manifest(sessions=[train, test], records=records)

    def test_single_record_single_session(self) -> None:
        train = _make_train_session()
        val = _make_validation_session()
        r = _make_record(session_id=train.session_id)
        m = _make_manifest(sessions=[train, val], records=[r])
        assert len(m.records) == 1

    def test_minimal_book_depth_one(self) -> None:
        train = _make_train_session()
        val = _make_validation_session()
        r = _make_record(
            book_depth=1,
            bids=[(150.0, 100)],
            asks=[(150.01, 100)],
            session_id=train.session_id,
        )
        m = _make_manifest(book_depth=1, sessions=[train, val], records=[r])
        assert m.book_depth == 1


# ---------------------------------------------------------------------------
# validate_no_future_leakage
# ---------------------------------------------------------------------------


class TestValidateNoFutureLeakage:
    """Tests for validate_no_future_leakage."""

    def test_valid_no_leakage(self) -> None:
        r = _make_record(
            receive_time="2024-01-15T09:30:00.100Z",
        )
        assert validate_no_future_leakage(r, "2024-01-15T09:30:01.000Z") is True

    def test_receive_equal_decision(self) -> None:
        r = _make_record(receive_time="2024-01-15T09:30:00.100Z")
        assert validate_no_future_leakage(r, "2024-01-15T09:30:00.100Z") is True

    def test_future_leakage_raises(self) -> None:
        r = _make_record(receive_time="2024-01-15T09:30:05.000Z")
        with pytest.raises(ValueError, match="future leakage"):
            validate_no_future_leakage(r, "2024-01-15T09:30:00.000Z")

    def test_invalid_decision_time(self) -> None:
        r = _make_record()
        with pytest.raises(ValueError):
            validate_no_future_leakage(r, "not-a-date")


# ---------------------------------------------------------------------------
# validate_session_split
# ---------------------------------------------------------------------------


class TestValidateSessionSplit:
    """Tests for validate_session_split."""

    def test_valid_split(self) -> None:
        sessions = [_make_train_session(), _make_validation_session()]
        assert validate_session_split(sessions) is True

    def test_empty_sessions(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_session_split([])

    def test_missing_train(self) -> None:
        with pytest.raises(ValueError, match="train"):
            validate_session_split([_make_validation_session()])

    def test_missing_validation(self) -> None:
        with pytest.raises(ValueError, match="validation"):
            validate_session_split([_make_train_session()])

    def test_overlapping_train_sessions(self) -> None:
        s1 = _make_session(
            session_id="s1",
            session_start="2024-01-15T09:30:00Z",
            session_end="2024-01-15T16:00:00Z",
            session_type="train",
        )
        s2 = _make_session(
            session_id="s2",
            session_start="2024-01-15T12:00:00Z",
            session_end="2024-01-15T18:00:00Z",
            session_type="train",
        )
        val = _make_validation_session()
        with pytest.raises(ValueError, match="overlapping train"):
            validate_session_split([s1, s2, val])

    def test_overlapping_validation_sessions(self) -> None:
        train = _make_train_session()
        v1 = _make_session(
            session_id="v1",
            session_start="2024-01-16T09:30:00Z",
            session_end="2024-01-16T16:00:00Z",
            session_type="validation",
        )
        v2 = _make_session(
            session_id="v2",
            session_start="2024-01-16T12:00:00Z",
            session_end="2024-01-16T18:00:00Z",
            session_type="validation",
        )
        with pytest.raises(ValueError, match="overlapping validation"):
            validate_session_split([train, v1, v2])

    def test_non_overlapping_different_types_ok(self) -> None:
        # train and validation can overlap in time (different types).
        train = _make_session(
            session_id="t1",
            session_start="2024-01-15T09:30:00Z",
            session_end="2024-01-15T16:00:00Z",
            session_type="train",
        )
        val = _make_session(
            session_id="v1",
            session_start="2024-01-15T09:30:00Z",
            session_end="2024-01-15T16:00:00Z",
            session_type="validation",
        )
        assert validate_session_split([train, val]) is True


# ---------------------------------------------------------------------------
# validate_sequence_ordering
# ---------------------------------------------------------------------------


class TestValidateSequenceOrdering:
    """Tests for validate_sequence_ordering."""

    def test_valid_ordering(self) -> None:
        records = [
            _make_record(record_id="r0", sequence_id=0, event_time="2024-01-15T09:30:00.100Z"),
            _make_record(record_id="r1", sequence_id=1, event_time="2024-01-15T09:30:00.200Z"),
        ]
        assert validate_sequence_ordering(records) is True

    def test_empty_records(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_sequence_ordering([])

    def test_duplicate_sequence_id(self) -> None:
        records = [
            _make_record(record_id="r0", sequence_id=0),
            _make_record(record_id="r1", sequence_id=0, event_time="2024-01-15T09:30:00.200Z"),
        ]
        with pytest.raises(ValueError, match="strictly monotonic"):
            validate_sequence_ordering(records)

    def test_gap_in_sequence_ok(self) -> None:
        # Gaps are allowed (strictly monotonic, not contiguous).
        records = [
            _make_record(record_id="r0", sequence_id=0),
            _make_record(record_id="r1", sequence_id=5, event_time="2024-01-15T09:30:00.200Z"),
        ]
        assert validate_sequence_ordering(records) is True

    def test_out_of_order_event_time(self) -> None:
        records = [
            _make_record(record_id="r0", sequence_id=0, event_time="2024-01-15T09:30:00.300Z"),
            _make_record(record_id="r1", sequence_id=1, event_time="2024-01-15T09:30:00.100Z"),
        ]
        with pytest.raises(ValueError, match="non-decreasing"):
            validate_sequence_ordering(records)

    def test_equal_event_time_ok(self) -> None:
        records = [
            _make_record(record_id="r0", sequence_id=0, event_time="2024-01-15T09:30:00.100Z"),
            _make_record(record_id="r1", sequence_id=1, event_time="2024-01-15T09:30:00.100Z"),
        ]
        assert validate_sequence_ordering(records) is True

    def test_multiple_sessions(self) -> None:
        records = [
            _make_record(record_id="r0", sequence_id=0, session_id="s1"),
            _make_record(
                record_id="r1",
                sequence_id=1,
                event_time="2024-01-15T09:30:00.200Z",
                session_id="s1",
            ),
            _make_record(record_id="r2", sequence_id=0, session_id="s2"),
            _make_record(
                record_id="r3",
                sequence_id=1,
                event_time="2024-01-16T09:30:00.200Z",
                session_id="s2",
            ),
        ]
        assert validate_sequence_ordering(records) is True


# ---------------------------------------------------------------------------
# compute_lob_data_hash
# ---------------------------------------------------------------------------


class TestComputeLOBDataHash:
    """Tests for compute_lob_data_hash."""

    def test_deterministic(self) -> None:
        r1 = _make_record(record_id="NASDAQ_AAPL_0")
        r2 = _make_record(record_id="NASDAQ_AAPL_1", sequence_id=1)
        h1 = compute_lob_data_hash([r1, r2])
        h2 = compute_lob_data_hash([r1, r2])
        assert h1 == h2

    def test_order_independent(self) -> None:
        r1 = _make_record(record_id="NASDAQ_AAPL_0")
        r2 = _make_record(record_id="NASDAQ_AAPL_1", sequence_id=1)
        h1 = compute_lob_data_hash([r1, r2])
        h2 = compute_lob_data_hash([r2, r1])
        assert h1 == h2

    def test_different_records_different_hash(self) -> None:
        r1 = _make_record(record_id="NASDAQ_AAPL_0")
        r2 = _make_record(record_id="NASDAQ_AAPL_1", sequence_id=1)
        h1 = compute_lob_data_hash([r1])
        h2 = compute_lob_data_hash([r2])
        assert h1 != h2

    def test_hash_is_64_hex(self) -> None:
        r = _make_record()
        h = compute_lob_data_hash([r])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_empty_records_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            compute_lob_data_hash([])

    def test_single_record(self) -> None:
        r = _make_record()
        h = compute_lob_data_hash([r])
        assert len(h) == 64


# ---------------------------------------------------------------------------
# LOBManifestBuilder
# ---------------------------------------------------------------------------


class TestLOBManifestBuilder:
    """Tests for the LOBManifestBuilder fluent API."""

    def test_fluent_build(self) -> None:
        train = _make_train_session()
        val = _make_validation_session()
        records = [
            _make_record(record_id="NASDAQ_AAPL_0", sequence_id=0, session_id=train.session_id),
            _make_record(
                record_id="NASDAQ_AAPL_1",
                sequence_id=1,
                event_time="2024-01-15T09:30:00.200Z",
                receive_time="2024-01-15T09:30:00.205Z",
                session_id=train.session_id,
            ),
            _make_record(
                record_id="NASDAQ_AAPL_2",
                sequence_id=0,
                event_time="2024-01-16T09:30:00.100Z",
                receive_time="2024-01-16T09:30:00.105Z",
                session_id=val.session_id,
            ),
        ]
        manifest = (
            LOBManifestBuilder(
                "lob_001",
                LOBVenue.NASDAQ,
                "AAPL",
                10,
                AdjustmentPolicy.RAW,
            )
            .with_sessions([train, val])
            .with_label_specs([_make_label_spec()])
            .with_records(records)
            .with_data("s3://bucket/lob_001.parquet", "a" * 64)
            .with_created_at("2024-01-20T00:00:00Z")
            .build()
        )
        assert manifest.dataset_id == "lob_001"
        assert len(manifest.records) == 3

    def test_build_defaults_created_at(self) -> None:
        train = _make_train_session()
        val = _make_validation_session()
        r = _make_record(session_id=train.session_id)
        manifest = (
            LOBManifestBuilder(
                "lob_002",
                LOBVenue.NASDAQ,
                "AAPL",
                10,
                AdjustmentPolicy.RAW,
            )
            .with_sessions([train, val])
            .with_label_specs([_make_label_spec()])
            .with_records([r])
            .with_data("s3://bucket/lob_002.parquet", "b" * 64)
            .build()
        )
        assert manifest.created_at != ""

    def test_build_fail_closed_missing_sessions(self) -> None:
        r = _make_record()
        with pytest.raises(ValidationError, match="sessions"):
            (
                LOBManifestBuilder(
                    "lob_003",
                    LOBVenue.NASDAQ,
                    "AAPL",
                    10,
                    AdjustmentPolicy.RAW,
                )
                .with_label_specs([_make_label_spec()])
                .with_records([r])
                .with_data("s3://bucket/lob_003.parquet", "c" * 64)
                .build()
            )

    def test_build_fail_closed_missing_train(self) -> None:
        val = _make_validation_session()
        r = _make_record(session_id=val.session_id)
        with pytest.raises(ValidationError, match="train"):
            (
                LOBManifestBuilder(
                    "lob_004",
                    LOBVenue.NASDAQ,
                    "AAPL",
                    10,
                    AdjustmentPolicy.RAW,
                )
                .with_sessions([val])
                .with_label_specs([_make_label_spec()])
                .with_records([r])
                .with_data("s3://bucket/lob_004.parquet", "d" * 64)
                .build()
            )

    def test_build_fail_closed_crossed_book(self) -> None:
        train = _make_train_session()
        _make_validation_session()
        # A crossed book record cannot even be constructed (fail-closed
        # at the record level).
        with pytest.raises(ValidationError, match="crossed/locked book"):
            _make_record(
                bids=[(151.0, 100)],
                asks=[(150.0, 100)],
                session_id=train.session_id,
            )

    def test_build_with_data_hash_from_compute(self) -> None:
        train = _make_train_session()
        val = _make_validation_session()
        records = [
            _make_record(record_id="NASDAQ_AAPL_0", sequence_id=0, session_id=train.session_id),
            _make_record(
                record_id="NASDAQ_AAPL_1",
                sequence_id=1,
                event_time="2024-01-15T09:30:00.200Z",
                receive_time="2024-01-15T09:30:00.205Z",
                session_id=train.session_id,
            ),
        ]
        h = compute_lob_data_hash(records)
        manifest = (
            LOBManifestBuilder(
                "lob_006",
                LOBVenue.NASDAQ,
                "AAPL",
                10,
                AdjustmentPolicy.RAW,
            )
            .with_sessions([train, val])
            .with_label_specs([_make_label_spec()])
            .with_records(records)
            .with_data("s3://bucket/lob_006.parquet", h)
            .with_created_at("2024-01-20T00:00:00Z")
            .build()
        )
        assert manifest.data_hash == h
