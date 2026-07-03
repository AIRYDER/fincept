"""quant_foundry.lob_manifest — limit order book dataset manifest.

TASK-LOB.1: LOBDatasetManifest.

This module defines the manifest schema for **limit order book (LOB)**
datasets — collections of point-in-time order book snapshots / events used
to train high-frequency market microstructure models. Each record captures
the state of the book at an exchange event timestamp, along with the
receive time (when the local system observed the event), the book depth,
the bid/ask price levels, the adjustment policy, and the session it
belongs to.

Cross-cutting quant rigor enforced here (NEXT_STEPS_PLAN §1, §3):
- **No future leakage**: ``receive_time >= event_time`` (you cannot
  receive an event before the exchange generated it) and
  :func:`validate_no_future_leakage` fail-closes if a record's
  ``receive_time`` is after the decision time.
- **Deterministic record ids**: each :class:`LOBRecord` carries a
  deterministic ``record_id`` of the form
  ``venue_symbol_sequence_id`` so two runs over the same data produce
  identical ids.
- **Deterministic data hash**: :func:`compute_lob_data_hash` produces a
  stable SHA-256 over the canonical JSON of the record list (sorted by
  ``record_id``).
- **Strong dedupe**: no duplicate ``record_ids`` and no duplicate
  ``sequence_id`` within a session.
- **Ordering checks**: ``sequence_id`` strictly monotonic and
  ``event_time`` non-decreasing within each session.
- **Session splits**: at least one train and one validation session, with
  no overlapping sessions of the same type.
- **No crossed book**: the best ask must be strictly greater than the
  best bid (a crossed or locked book is rejected at construction time).

The module reuses the temporal parsing helper :func:`_parse_temporal` from
:mod:`quant_foundry.dataset_manifest` (T-3.1 / T-3.4).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_foundry.dataset_manifest import _parse_temporal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Allowed session types for :class:`LOBSession`.
_ALLOWED_SESSION_TYPES: frozenset[str] = frozenset(
    {"train", "validation", "test"}
)

#: Allowed label types for :class:`LabelSpec`.
_ALLOWED_LABEL_TYPES: frozenset[str] = frozenset(
    {"mid_price_return", "spread", "imbalance", "trade_direction"}
)

#: Allowed normalization strategies for :class:`LabelSpec`.
_ALLOWED_NORMALIZATIONS: frozenset[str] = frozenset(
    {"none", "z_score", "min_max"}
)

# 64-char lowercase hex (SHA-256) — same pattern as dataset_manifest.py.
_HEX256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


def _validate_hex256(value: str, field_name: str) -> str:
    """Require a 64-char hex SHA-256, return lowercase.

    Args:
        value: the hash string to validate.
        field_name: the field name for error messages.

    Returns:
        The lowercase hex string.

    Raises:
        ValueError: if ``value`` is not a 64-char hex string.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty 64-char hex string")
    if not _HEX256_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a 64-char hex SHA-256; got {value!r}"
        )
    return value.lower()


def _validate_iso_temporal(value: str, field_name: str) -> str:
    """Validate that ``value`` is a parseable ISO date/datetime string.

    Args:
        value: the string to validate.
        field_name: the field name for error messages.

    Returns:
        The validated string.

    Raises:
        ValueError: if ``value`` is not a parseable ISO temporal.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field_name} must be a non-empty ISO datetime string; "
            f"got {value!r}"
        )
    _parse_temporal(value)
    return value


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LOBVenue(str, Enum):
    """Trading venue for a LOB dataset.

    Members:
        NASDAQ: NASDAQ stock exchange.
        NYSE: New York Stock Exchange.
        CME: Chicago Mercantile Exchange.
        ICE: Intercontinental Exchange.
        EUREX: Eurex derivatives exchange.
        BATS: BATS Global Markets (now Cboe BATS).
        IEX: Investors Exchange.
        OTHER: any venue not listed above.
    """

    NASDAQ = "NASDAQ"
    NYSE = "NYSE"
    CME = "CME"
    ICE = "ICE"
    EUREX = "EUREX"
    BATS = "BATS"
    IEX = "IEX"
    OTHER = "OTHER"


class AdjustmentPolicy(str, Enum):
    """Corporate-action adjustment policy for the price/size data.

    Members:
        RAW: unadjusted raw prices/sizes.
        SPLIT_ADJUSTED: split-adjusted only.
        DIVIDEND_ADJUSTED: dividend-adjusted only.
        FULLY_ADJUSTED: both split- and dividend-adjusted.
        BACK_ADJUSTED: back-adjusted (panama) continuous contract.
    """

    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    DIVIDEND_ADJUSTED = "DIVIDEND_ADJUSTED"
    FULLY_ADJUSTED = "FULLY_ADJUSTED"
    BACK_ADJUSTED = "BACK_ADJUSTED"


class LabelHorizonUnit(str, Enum):
    """Unit for a label horizon in :class:`LabelSpec`.

    Members:
        EVENTS: horizon measured in order book events.
        MILLISECONDS: horizon measured in wall-clock milliseconds.
        SECONDS: horizon measured in wall-clock seconds.
    """

    EVENTS = "EVENTS"
    MILLISECONDS = "MILLISECONDS"
    SECONDS = "SECONDS"


# ---------------------------------------------------------------------------
# LOBSession
# ---------------------------------------------------------------------------


class LOBSession(BaseModel):
    """A single trading session in a LOB dataset.

    A :class:`LOBSession` describes one contiguous trading window (e.g. a
    single trading day for one venue/symbol). Sessions are partitioned
    into ``train``, ``validation``, and ``test`` types so that the
    manifest can enforce train/validation splits and detect overlapping
    sessions of the same type.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        session_id: the session identifier (e.g.
            ``"2024-01-15_NASDAQ_AAPL"``). Must be non-empty.
        session_start: ISO datetime — inclusive start of the session.
        session_end: ISO datetime — inclusive end of the session (must
            be > ``session_start``).
        session_type: one of ``"train"``, ``"validation"``, ``"test"``.
        n_events: the number of order book events in the session
            (must be >= 1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    session_start: str
    session_end: str
    session_type: str
    n_events: int

    @field_validator("session_id")
    @classmethod
    def _session_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("session_id must be a non-empty string")
        return v

    @field_validator("session_start", "session_end")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name or "temporal")

    @field_validator("session_type")
    @classmethod
    def _session_type_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_SESSION_TYPES:
            raise ValueError(
                f"session_type must be one of "
                f"{sorted(_ALLOWED_SESSION_TYPES)!r}; got {v!r}"
            )
        return v

    @field_validator("n_events")
    @classmethod
    def _n_events_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise ValueError(f"n_events must be an integer >= 1; got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_ordering(self) -> LOBSession:
        """Enforce session_end > session_start."""
        start = _parse_temporal(self.session_start)
        end = _parse_temporal(self.session_end)
        if not (end > start):
            raise ValueError(
                f"session_end must be > session_start "
                f"(session_start={self.session_start!r}, "
                f"session_end={self.session_end!r}) for session "
                f"{self.session_id!r}"
            )
        return self


# ---------------------------------------------------------------------------
# LOBRecord
# ---------------------------------------------------------------------------


class LOBRecord(BaseModel):
    """A single limit order book snapshot / event.

    A :class:`LOBRecord` captures the state of the order book at an
    exchange event timestamp (``event_time``), along with the receive
    time (``receive_time`` — when the local system observed the event),
    the book depth, and the bid/ask price levels.

    Leakage-safe invariants (fail-closed at construction):
    - ``receive_time >= event_time`` (you cannot receive an event before
      the exchange generated it).
    - ``len(bids) <= book_depth`` and ``len(asks) <= book_depth``.
    - All bid/ask prices > 0 and sizes >= 0.
    - Best ask > best bid (no crossed or locked book) when both sides
      are non-empty.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        record_id: deterministic id of the form
            ``venue_symbol_sequence_id``.
        venue: the :class:`LOBVenue` this record belongs to.
        symbol: the instrument symbol.
        sequence_id: monotonic event sequence number (>= 0).
        event_time: ISO datetime — exchange event timestamp.
        receive_time: ISO datetime — when the event was received
            (must be >= ``event_time``).
        book_depth: the number of price levels (>= 1).
        bids: list of ``(price, size)`` pairs (length <= ``book_depth``).
        asks: list of ``(price, size)`` pairs (length <= ``book_depth``).
        adjustment_policy: the :class:`AdjustmentPolicy` for this record.
        session_id: the session this record belongs to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str
    venue: LOBVenue
    symbol: str
    sequence_id: int
    event_time: str
    receive_time: str
    book_depth: int
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    adjustment_policy: AdjustmentPolicy
    session_id: str

    @field_validator("record_id")
    @classmethod
    def _record_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("record_id must be a non-empty string")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("symbol must be a non-empty string")
        return v

    @field_validator("sequence_id")
    @classmethod
    def _sequence_id_nonnegative(cls, v: int) -> int:
        if not isinstance(v, int) or v < 0:
            raise ValueError(
                f"sequence_id must be an integer >= 0; got {v!r}"
            )
        return v

    @field_validator("event_time", "receive_time")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name or "temporal")

    @field_validator("book_depth")
    @classmethod
    def _book_depth_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise ValueError(f"book_depth must be an integer >= 1; got {v!r}")
        return v

    @field_validator("session_id")
    @classmethod
    def _session_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("session_id must be a non-empty string")
        return v

    @field_validator("bids", "asks")
    @classmethod
    def _levels_valid(
        cls, v: list[tuple[float, float]], info: Any
    ) -> list[tuple[float, float]]:
        field_name = info.field_name or "levels"
        for level in v:
            if (
                not isinstance(level, (list, tuple))
                or len(level) != 2
            ):
                raise ValueError(
                    f"{field_name} entries must be (price, size) pairs; "
                    f"got {level!r}"
                )
            price, size = level
            if not isinstance(price, (int, float)) or isinstance(price, bool):
                raise ValueError(
                    f"{field_name} price must be a number; got {price!r}"
                )
            if not isinstance(size, (int, float)) or isinstance(size, bool):
                raise ValueError(
                    f"{field_name} size must be a number; got {size!r}"
                )
            if field_name == "bids":
                if price <= 0:
                    raise ValueError(
                        f"bid prices must be > 0; got {price!r}"
                    )
                if size < 0:
                    raise ValueError(
                        f"bid sizes must be >= 0; got {size!r}"
                    )
            else:
                if price <= 0:
                    raise ValueError(
                        f"ask prices must be > 0; got {price!r}"
                    )
                if size < 0:
                    raise ValueError(
                        f"ask sizes must be >= 0; got {size!r}"
                    )
        return v

    @model_validator(mode="after")
    def _check_invariants(self) -> LOBRecord:
        """Enforce receive_time >= event_time, level counts, and no
        crossed book."""
        # receive_time >= event_time
        event_epoch = _parse_temporal(self.event_time)
        receive_epoch = _parse_temporal(self.receive_time)
        if not (receive_epoch >= event_epoch):
            raise ValueError(
                f"receive_time must be >= event_time "
                f"(event_time={self.event_time!r}, "
                f"receive_time={self.receive_time!r}) for record "
                f"{self.record_id!r}"
            )
        # Level counts <= book_depth.
        if len(self.bids) > self.book_depth:
            raise ValueError(
                f"bids length ({len(self.bids)}) must be <= book_depth "
                f"({self.book_depth}) for record {self.record_id!r}"
            )
        if len(self.asks) > self.book_depth:
            raise ValueError(
                f"asks length ({len(self.asks)}) must be <= book_depth "
                f"({self.book_depth}) for record {self.record_id!r}"
            )
        # No crossed book: best_ask > best_bid (when both sides present).
        if self.bids and self.asks:
            best_bid = max(level[0] for level in self.bids)
            best_ask = min(level[0] for level in self.asks)
            if not (best_ask > best_bid):
                raise ValueError(
                    f"crossed/locked book: best_ask ({best_ask}) must be "
                    f"> best_bid ({best_bid}) for record "
                    f"{self.record_id!r}"
                )
        return self


# ---------------------------------------------------------------------------
# LabelSpec
# ---------------------------------------------------------------------------


class LabelSpec(BaseModel):
    """A label specification for a LOB dataset.

    A :class:`LabelSpec` describes how labels are computed from the order
    book: the prediction horizon, its unit, the label type, and the
    normalization strategy.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        horizon: the prediction horizon (>= 1).
        horizon_unit: the :class:`LabelHorizonUnit` for the horizon.
        label_type: one of ``"mid_price_return"``, ``"spread"``,
            ``"imbalance"``, ``"trade_direction"``.
        normalization: the normalization strategy — one of ``"none"``,
            ``"z_score"``, ``"min_max"``. Defaults to ``"none"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    horizon: int
    horizon_unit: LabelHorizonUnit
    label_type: str
    normalization: str = "none"

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise ValueError(f"horizon must be an integer >= 1; got {v!r}")
        return v

    @field_validator("label_type")
    @classmethod
    def _label_type_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_LABEL_TYPES:
            raise ValueError(
                f"label_type must be one of "
                f"{sorted(_ALLOWED_LABEL_TYPES)!r}; got {v!r}"
            )
        return v

    @field_validator("normalization")
    @classmethod
    def _normalization_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_NORMALIZATIONS:
            raise ValueError(
                f"normalization must be one of "
                f"{sorted(_ALLOWED_NORMALIZATIONS)!r}; got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# LOBDatasetManifest
# ---------------------------------------------------------------------------


class LOBDatasetManifest(BaseModel):
    """Manifest for a limit order book (LOB) dataset.

    This is the contract of record for a LOB dataset export. It fixes the
    venue, symbol, book depth, adjustment policy, the session split
    (train/validation/test), the label specifications, and the list of
    order book records.

    Leakage-safe invariants (fail-closed at construction):
    - No duplicate ``session_id`` values.
    - No duplicate ``record_id`` values.
    - Every record's ``session_id`` refers to a session in ``sessions``.
    - Every record's ``venue`` and ``symbol`` match the manifest.
    - Every record's ``book_depth`` matches the manifest.
    - Within each session, ``sequence_id`` values are strictly monotonic
      (no gaps or duplicates).
    - Within each session, ``event_time`` values are non-decreasing.
    - At least one ``train`` session and one ``validation`` session.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        dataset_id: the dataset identifier.
        venue: the :class:`LOBVenue` for this dataset.
        symbol: the instrument symbol.
        book_depth: the number of price levels (>= 1).
        adjustment_policy: the :class:`AdjustmentPolicy` for this dataset.
        sessions: list of :class:`LOBSession` (at least 1).
        label_specs: list of :class:`LabelSpec` (at least 1).
        records: list of :class:`LOBRecord` (at least 1).
        data_uri: path/URI to the LOB data file.
        data_hash: SHA-256 of the LOB data (64-char hex).
        created_at: ISO timestamp of manifest creation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    venue: LOBVenue
    symbol: str
    book_depth: int
    adjustment_policy: AdjustmentPolicy
    sessions: list[LOBSession]
    label_specs: list[LabelSpec]
    records: list[LOBRecord]
    data_uri: str
    data_hash: str
    created_at: str

    # --- field validators ------------------------------------------------

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("dataset_id must be a non-empty string")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("symbol must be a non-empty string")
        return v

    @field_validator("book_depth")
    @classmethod
    def _book_depth_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise ValueError(f"book_depth must be an integer >= 1; got {v!r}")
        return v

    @field_validator("sessions")
    @classmethod
    def _sessions_nonempty(cls, v: list[LOBSession]) -> list[LOBSession]:
        if not v:
            raise ValueError("sessions must contain at least 1 session")
        return v

    @field_validator("label_specs")
    @classmethod
    def _label_specs_nonempty(cls, v: list[LabelSpec]) -> list[LabelSpec]:
        if not v:
            raise ValueError("label_specs must contain at least 1 label spec")
        return v

    @field_validator("records")
    @classmethod
    def _records_nonempty(cls, v: list[LOBRecord]) -> list[LOBRecord]:
        if not v:
            raise ValueError("records must contain at least 1 record")
        return v

    @field_validator("created_at")
    @classmethod
    def _created_at_parseable(cls, v: str) -> str:
        return _validate_iso_temporal(v, "created_at")

    @field_validator("data_uri")
    @classmethod
    def _data_uri_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("data_uri must be a non-empty string")
        return v

    @field_validator("data_hash")
    @classmethod
    def _data_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "data_hash")

    # --- model validators ------------------------------------------------

    @model_validator(mode="after")
    def _no_duplicate_session_ids(self) -> LOBDatasetManifest:
        """Session ids must be unique."""
        ids = [s.session_id for s in self.sessions]
        if len(set(ids)) != len(ids):
            dupes = sorted({sid for sid in ids if ids.count(sid) > 1})
            raise ValueError(
                f"sessions must not contain duplicate session_ids: "
                f"{dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_record_ids(self) -> LOBDatasetManifest:
        """Record ids must be unique."""
        ids = [r.record_id for r in self.records]
        if len(set(ids)) != len(ids):
            dupes = sorted({rid for rid in ids if ids.count(rid) > 1})
            raise ValueError(
                f"records must not contain duplicate record_ids: {dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def _records_belong_to_sessions(self) -> LOBDatasetManifest:
        """Every record's session_id must refer to a session in
        ``sessions``."""
        session_ids = {s.session_id for s in self.sessions}
        bad = sorted({r.session_id for r in self.records
                      if r.session_id not in session_ids})
        if bad:
            raise ValueError(
                f"records reference unknown session_ids: {bad!r} "
                f"(known: {sorted(session_ids)!r})"
            )
        return self

    @model_validator(mode="after")
    def _records_match_venue_symbol(self) -> LOBDatasetManifest:
        """All records must have the same venue and symbol as the
        manifest."""
        bad_venue = sorted({r.record_id for r in self.records
                            if r.venue != self.venue})
        if bad_venue:
            raise ValueError(
                f"records with mismatched venue (expected "
                f"{self.venue.value!r}): {bad_venue!r}"
            )
        bad_symbol = sorted({r.record_id for r in self.records
                             if r.symbol != self.symbol})
        if bad_symbol:
            raise ValueError(
                f"records with mismatched symbol (expected "
                f"{self.symbol!r}): {bad_symbol!r}"
            )
        return self

    @model_validator(mode="after")
    def _records_match_book_depth(self) -> LOBDatasetManifest:
        """All records must have the same book_depth as the manifest."""
        bad = sorted({r.record_id for r in self.records
                      if r.book_depth != self.book_depth})
        if bad:
            raise ValueError(
                f"records with mismatched book_depth (expected "
                f"{self.book_depth}): {bad!r}"
            )
        return self

    @model_validator(mode="after")
    def _sequence_ordering_per_session(self) -> LOBDatasetManifest:
        """Within each session, sequence_ids must be strictly monotonic
        and event_times non-decreasing."""
        by_session: dict[str, list[LOBRecord]] = {}
        for r in self.records:
            by_session.setdefault(r.session_id, []).append(r)
        for sid, recs in by_session.items():
            # Sort by sequence_id to check monotonicity.
            ordered = sorted(recs, key=lambda r: r.sequence_id)
            seqs = [r.sequence_id for r in ordered]
            for i in range(1, len(seqs)):
                if seqs[i] <= seqs[i - 1]:
                    raise ValueError(
                        f"session {sid!r}: sequence_ids must be strictly "
                        f"monotonic — duplicate or out-of-order at "
                        f"sequence_id {seqs[i]}"
                    )
            # event_time non-decreasing in sequence_id order.
            times = [_parse_temporal(r.event_time) for r in ordered]
            for i in range(1, len(times)):
                if times[i] < times[i - 1]:
                    raise ValueError(
                        f"session {sid!r}: event_times must be "
                        f"non-decreasing — out-of-order at sequence_id "
                        f"{seqs[i]}"
                    )
        return self

    @model_validator(mode="after")
    def _has_train_and_validation(self) -> LOBDatasetManifest:
        """At least one train session and one validation session."""
        validate_session_split(self.sessions)
        return self


# ---------------------------------------------------------------------------
# validate_no_future_leakage
# ---------------------------------------------------------------------------


def validate_no_future_leakage(record: LOBRecord, decision_time: str) -> bool:
    """Check that a record has no future data leakage relative to a
    decision time.

    Returns True if the record's ``receive_time`` is <= ``decision_time``
    (the record was available on or before the decision time, so no
    future data bleeds into the decision).

    Args:
        record: the :class:`LOBRecord` to check.
        decision_time: ISO datetime — the decision time to check
            against.

    Returns:
        True if there is no future leakage.

    Raises:
        ValueError: if ``receive_time > decision_time`` (future leakage
            detected) — fail-closed.
    """
    receive_epoch = _parse_temporal(record.receive_time)
    decision_epoch = _parse_temporal(decision_time)
    if not (receive_epoch <= decision_epoch):
        raise ValueError(
            f"future leakage detected: receive_time "
            f"({record.receive_time!r}) must be <= decision_time "
            f"({decision_time!r}) for record {record.record_id!r}"
        )
    return True


# ---------------------------------------------------------------------------
# validate_session_split
# ---------------------------------------------------------------------------


def validate_session_split(sessions: list[LOBSession]) -> bool:
    """Validate that a list of sessions forms a proper train/validation
    split.

    Checks:
    - At least one ``train`` session and one ``validation`` session.
    - No overlapping sessions of the same type (two train sessions must
      not overlap in time; two validation sessions must not overlap;
      two test sessions must not overlap).

    Args:
        sessions: the list of :class:`LOBSession` to validate.

    Returns:
        True if the session split is valid.

    Raises:
        ValueError: if any check fails (fail-closed).
    """
    if not sessions:
        raise ValueError("sessions must be non-empty")

    types = {s.session_type for s in sessions}
    if "train" not in types:
        raise ValueError(
            "session split must contain at least one train session"
        )
    if "validation" not in types:
        raise ValueError(
            "session split must contain at least one validation session"
        )

    # Check no overlapping sessions of the same type.
    by_type: dict[str, list[LOBSession]] = {}
    for s in sessions:
        by_type.setdefault(s.session_type, []).append(s)
    for stype, group in by_type.items():
        intervals = sorted(
            ((_parse_temporal(s.session_start), _parse_temporal(s.session_end),
              s.session_id) for s in group),
            key=lambda t: t[0],
        )
        for i in range(1, len(intervals)):
            prev_start, prev_end, prev_id = intervals[i - 1]
            cur_start, cur_end, cur_id = intervals[i]
            if cur_start < prev_end:
                raise ValueError(
                    f"overlapping {stype} sessions: {prev_id!r} "
                    f"({prev_start}..{prev_end}) overlaps {cur_id!r} "
                    f"({cur_start}..{cur_end})"
                )
    return True


# ---------------------------------------------------------------------------
# validate_sequence_ordering
# ---------------------------------------------------------------------------


def validate_sequence_ordering(records: list[LOBRecord]) -> bool:
    """Validate that records are properly ordered within each session.

    Checks (per session, ordered by ``sequence_id``):
    - ``sequence_id`` values are strictly monotonic (no gaps or
      duplicates).
    - ``event_time`` values are non-decreasing.

    Args:
        records: the list of :class:`LOBRecord` to validate.

    Returns:
        True if the ordering is valid.

    Raises:
        ValueError: if any check fails (fail-closed).
    """
    if not records:
        raise ValueError("records must be non-empty for ordering validation")

    by_session: dict[str, list[LOBRecord]] = {}
    for r in records:
        by_session.setdefault(r.session_id, []).append(r)

    for sid, recs in by_session.items():
        ordered = sorted(recs, key=lambda r: r.sequence_id)
        seqs = [r.sequence_id for r in ordered]
        for i in range(1, len(seqs)):
            if seqs[i] <= seqs[i - 1]:
                raise ValueError(
                    f"session {sid!r}: sequence_ids must be strictly "
                    f"monotonic — duplicate or out-of-order at "
                    f"sequence_id {seqs[i]}"
                )
        times = [_parse_temporal(r.event_time) for r in ordered]
        for i in range(1, len(times)):
            if times[i] < times[i - 1]:
                raise ValueError(
                    f"session {sid!r}: event_times must be "
                    f"non-decreasing — out-of-order at sequence_id "
                    f"{seqs[i]}"
                )
    return True


# ---------------------------------------------------------------------------
# compute_lob_data_hash
# ---------------------------------------------------------------------------


def compute_lob_data_hash(records: list[LOBRecord]) -> str:
    """Compute a deterministic SHA-256 hash over a list of LOB records.

    The hash is computed over the canonical JSON of the records, sorted
    by ``record_id``. Each record is serialized via its Pydantic
    ``model_dump`` with ``mode="json"`` and ``exclude_none=True`` so that
    two runs over the same records produce identical hashes regardless of
    insertion order or unset optional fields.

    Args:
        records: the list of :class:`LOBRecord` to hash.

    Returns:
        A 64-character lowercase hex SHA-256 digest.

    Raises:
        ValueError: if ``records`` is empty.
    """
    if not records:
        raise ValueError("records must be non-empty to compute a data hash")
    serialized = [r.model_dump(mode="json", exclude_none=True) for r in records]
    serialized.sort(key=lambda d: d["record_id"])
    canonical = json.dumps(serialized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# LOBManifestBuilder
# ---------------------------------------------------------------------------


class LOBManifestBuilder:
    """Fluent builder for :class:`LOBDatasetManifest`.

    Provides a chainable API for constructing a LOB dataset manifest
    field-by-field, then calling :meth:`build` to validate and create the
    immutable manifest.

    Example::

        manifest = (
            LOBManifestBuilder(
                "lob_001", LOBVenue.NASDAQ, "AAPL", 10,
                AdjustmentPolicy.RAW,
            )
            .with_sessions([train_session, val_session])
            .with_label_specs([LabelSpec(horizon=10,
                horizon_unit=LabelHorizonUnit.EVENTS,
                label_type="mid_price_return")])
            .with_records([record_a, record_b])
            .with_data(
                uri="s3://bucket/lob_001.parquet",
                data_hash=compute_lob_data_hash([record_a, record_b]),
            )
            .build()
        )
    """

    def __init__(
        self,
        dataset_id: str,
        venue: LOBVenue,
        symbol: str,
        book_depth: int,
        adjustment_policy: AdjustmentPolicy,
    ) -> None:
        """Initialize the builder with the dataset identity.

        Args:
            dataset_id: the dataset identifier.
            venue: the :class:`LOBVenue` for this dataset.
            symbol: the instrument symbol.
            book_depth: the number of price levels (>= 1).
            adjustment_policy: the :class:`AdjustmentPolicy` for this
                dataset.
        """
        self._dataset_id: str = dataset_id
        self._venue: LOBVenue = venue
        self._symbol: str = symbol
        self._book_depth: int = book_depth
        self._adjustment_policy: AdjustmentPolicy = adjustment_policy
        self._sessions: list[LOBSession] = []
        self._label_specs: list[LabelSpec] = []
        self._records: list[LOBRecord] = []
        self._data_uri: str = ""
        self._data_hash: str = ""
        self._created_at: str = ""

    def with_sessions(
        self, sessions: list[LOBSession]
    ) -> LOBManifestBuilder:
        """Set the sessions (train/validation/test split).

        Args:
            sessions: list of :class:`LOBSession` (at least 1, with at
                least one train and one validation session).

        Returns:
            self (for chaining).
        """
        self._sessions = list(sessions)
        return self

    def with_label_specs(
        self, specs: list[LabelSpec]
    ) -> LOBManifestBuilder:
        """Set the label specifications.

        Args:
            specs: list of :class:`LabelSpec` (at least 1).

        Returns:
            self (for chaining).
        """
        self._label_specs = list(specs)
        return self

    def with_records(
        self, records: list[LOBRecord]
    ) -> LOBManifestBuilder:
        """Set the order book records.

        Args:
            records: list of :class:`LOBRecord` (at least 1).

        Returns:
            self (for chaining).
        """
        self._records = list(records)
        return self

    def with_data(
        self, uri: str, data_hash: str
    ) -> LOBManifestBuilder:
        """Set the data location and hash.

        Args:
            uri: path/URI to the LOB data file.
            data_hash: SHA-256 of the LOB data (64-char hex).

        Returns:
            self (for chaining).
        """
        self._data_uri = uri
        self._data_hash = data_hash
        return self

    def with_created_at(
        self, created_at: str
    ) -> LOBManifestBuilder:
        """Set the creation timestamp.

        Args:
            created_at: ISO timestamp of manifest creation.

        Returns:
            self (for chaining).
        """
        self._created_at = created_at
        return self

    def build(self) -> LOBDatasetManifest:
        """Build and validate the :class:`LOBDatasetManifest`.

        Returns:
            The validated, frozen manifest.

        Raises:
            ValueError: if any required field is missing or validation
                fails (fail-closed).
        """
        if not self._created_at:
            self._created_at = datetime.now(timezone.utc).isoformat()

        return LOBDatasetManifest(
            dataset_id=self._dataset_id,
            venue=self._venue,
            symbol=self._symbol,
            book_depth=self._book_depth,
            adjustment_policy=self._adjustment_policy,
            sessions=self._sessions,
            label_specs=self._label_specs,
            records=self._records,
            data_uri=self._data_uri,
            data_hash=self._data_hash,
            created_at=self._created_at,
        )
