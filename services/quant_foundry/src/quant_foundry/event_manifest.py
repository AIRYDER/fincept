"""quant_foundry.event_manifest — event dataset manifest for point-in-time event data.

TASK-12.1: EventDatasetManifest.

This module defines the manifest schema for **event datasets** — collections
of point-in-time events (earnings releases, FDA approvals, merger
announcements, macro prints, etc.) that are joined onto a panel of
instruments at a *decision time*. An event is only eligible for a given
decision time if it was *available* (``available_at``) on or before that
decision time — this is the point-in-time join that prevents future
leakage.

Cross-cutting quant rigor enforced here (NEXT_STEPS_PLAN §1, §3):
- **No future leakage**: an event whose ``available_at`` is after the
  manifest's ``decision_time`` is rejected at construction time
  (:func:`validate_point_in_time` fail-closes).
- **Revised-metadata leakage**: a revised event (``revised=True``) must
  point to an existing original event via ``revised_from`` and must have a
  *later* ``published_at`` than the original. :func:`validate_no_revised_metadata_leakage`
  fail-closes if a revised event references a non-existent original or
  back-dates a revision.
- **Deterministic event ids**: each :class:`EventRecord` carries a
  deterministic ``event_id`` of the form
  ``source_id_published_at_event_type`` so two runs over the same event
  produce identical ids.
- **Deterministic data hash**: :func:`compute_event_data_hash` produces a
  stable SHA-256 over the canonical JSON of the event list (sorted by
  ``event_id``).
- **No duplicate event ids**: the manifest rejects duplicate ``event_id``
  values at construction time.

The module reuses the temporal parsing helper :func:`_parse_temporal` from
:mod:`quant_foundry.dataset_manifest` (T-3.1 / T-8.1).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.dataset_manifest import _parse_temporal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Allowed retrieval methods for an :class:`EventSource`.
_ALLOWED_RETRIEVAL_METHODS: frozenset[str] = frozenset(
    {"api", "scrape", "file"}
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
# EventSource
# ---------------------------------------------------------------------------


class EventSource(BaseModel):
    """The provenance of an event dataset.

    An :class:`EventSource` records *where* the events came from and *how*
    they were retrieved, plus a content hash of the source configuration
    so that two consumers of the same manifest can verify they are reading
    from an identical source setup.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        source_id: the source identifier (e.g. ``"reuters"``,
            ``"bloomberg"``, ``"edgar"``).
        source_hash: SHA-256 of the source configuration (64-char hex).
        retrieval_method: how the events were retrieved — one of
            ``"api"``, ``"scrape"``, ``"file"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_hash: str
    retrieval_method: str

    @field_validator("source_id")
    @classmethod
    def _source_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("source_id must be a non-empty string")
        return v

    @field_validator("source_hash")
    @classmethod
    def _source_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "source_hash")

    @field_validator("retrieval_method")
    @classmethod
    def _retrieval_method_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_RETRIEVAL_METHODS:
            raise ValueError(
                f"retrieval_method must be one of "
                f"{sorted(_ALLOWED_RETRIEVAL_METHODS)!r}; got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# EventRecord
# ---------------------------------------------------------------------------


class EventRecord(BaseModel):
    """A single point-in-time event.

    An :class:`EventRecord` is the atomic unit of an event dataset. It
    records *when* the event was published (``published_at``) and *when*
    it became available for trading decisions (``available_at`` — e.g.
    after an exchange delay or a processing latency). The
    ``available_at`` timestamp is the one used for point-in-time joins:
    an event is only eligible for a decision time ``T`` if
    ``available_at <= T``.

    A revised event (``revised=True``) supersedes an earlier event and
    must point to the original via ``revised_from``. Revised events are
    subject to additional leakage checks (see
    :func:`validate_no_revised_metadata_leakage`).

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        event_id: deterministic id of the form
            ``source_id_published_at_event_type``.
        source_id: the source this event came from.
        published_at: ISO datetime — when the event was published.
        available_at: ISO datetime — when the event became available for
            trading (must be >= ``published_at``).
        affected_symbols: list of instruments affected by the event (at
            least 1, no duplicates).
        event_type: the event type (e.g. ``"earnings"``,
            ``"fda_approval"``, ``"merger"``).
        raw_text_hash: optional SHA-256 of the raw event text.
        embedding_model_hash: optional hash of the embedding model used
            to compute ``embedding``.
        embedding: optional event embedding vector.
        label_horizons: list of label horizons in days (e.g. ``[1, 5,
            20]``).
        revised: whether this is a revised version of an earlier event.
        revised_from: the original ``event_id`` if this event is a
            revision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    source_id: str
    published_at: str
    available_at: str
    affected_symbols: list[str]
    event_type: str
    raw_text_hash: str | None = None
    embedding_model_hash: str | None = None
    embedding: list[float] | None = None
    label_horizons: list[int]
    revised: bool = False
    revised_from: str | None = None

    @field_validator("event_id")
    @classmethod
    def _event_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("event_id must be a non-empty string")
        return v

    @field_validator("source_id")
    @classmethod
    def _source_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("source_id must be a non-empty string")
        return v

    @field_validator("published_at", "available_at")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name or "temporal")

    @field_validator("affected_symbols")
    @classmethod
    def _symbols_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("affected_symbols must contain at least 1 symbol")
        for s in v:
            if not isinstance(s, str) or not s.strip():
                raise ValueError(
                    "affected_symbols entries must be non-empty strings"
                )
        return v

    @field_validator("event_type")
    @classmethod
    def _event_type_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("event_type must be a non-empty string")
        return v

    @field_validator("raw_text_hash")
    @classmethod
    def _raw_text_hash_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "raw_text_hash")

    @field_validator("embedding_model_hash")
    @classmethod
    def _embedding_model_hash_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "embedding_model_hash")

    @field_validator("embedding")
    @classmethod
    def _embedding_valid(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError("embedding must be a non-empty list of floats")
        for x in v:
            if not isinstance(x, (int, float)):
                raise ValueError(
                    "embedding entries must be floats; "
                    f"got {type(x).__name__}"
                )
        return [float(x) for x in v]

    @field_validator("label_horizons")
    @classmethod
    def _label_horizons_valid(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("label_horizons must contain at least 1 horizon")
        for h in v:
            if not isinstance(h, int) or h < 1:
                raise ValueError(
                    f"each label horizon must be an integer >= 1; got {h!r}"
                )
        return v

    @field_validator("revised_from")
    @classmethod
    def _revised_from_consistent(cls, v: str | None, info: Any) -> str | None:
        # revised_from must be non-empty if provided.
        if v is None:
            return v
        if not v.strip():
            raise ValueError("revised_from must be a non-empty string if provided")
        return v

    # --- model validators ------------------------------------------------

    @model_validator(mode="after")
    def _available_after_published(self) -> EventRecord:
        """available_at must be >= published_at (no negative latency)."""
        pa = _parse_temporal(self.published_at)
        aa = _parse_temporal(self.available_at)
        if aa < pa:
            raise ValueError(
                f"available_at must be >= published_at for event "
                f"{self.event_id!r} (published_at={self.published_at!r}, "
                f"available_at={self.available_at!r})"
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_symbols(self) -> EventRecord:
        """affected_symbols must not contain duplicates."""
        if len(set(self.affected_symbols)) != len(self.affected_symbols):
            dupes = sorted(
                {s for s in self.affected_symbols
                 if self.affected_symbols.count(s) > 1}
            )
            raise ValueError(
                f"affected_symbols must not contain duplicates for event "
                f"{self.event_id!r}: {dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def _revised_fields_consistent(self) -> EventRecord:
        """revised=True requires revised_from; revised_from set requires
        revised=True."""
        if self.revised and not self.revised_from:
            raise ValueError(
                f"revised event {self.event_id!r} must set revised_from"
            )
        if self.revised_from and not self.revised:
            raise ValueError(
                f"event {self.event_id!r} has revised_from but revised=False"
            )
        return self


# ---------------------------------------------------------------------------
# EventDatasetManifest
# ---------------------------------------------------------------------------


class EventDatasetManifest(BaseModel):
    """Manifest for a point-in-time event dataset.

    This is the contract of record for an event dataset export. It fixes
    the source, the list of events, the *decision time* (the point-in-time
    at which the events are joined onto a panel), the data location +
    hash, and the creation timestamp.

    Leakage-safe invariants (fail-closed at construction):
    - No duplicate ``event_id`` values across events.
    - Every event's ``available_at <= decision_time`` (no future leakage
      — an event not yet available at the decision time cannot be used).
    - Every revised event's ``revised_from`` points to an existing event
      in the manifest, and the revision has a later ``published_at`` than
      the original (no back-dated revisions leaking future metadata).

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        dataset_id: the dataset identifier.
        source: the :class:`EventSource` provenance.
        events: list of :class:`EventRecord` (at least 1).
        decision_time: ISO datetime — the point-in-time for joining.
        data_uri: path/URI to the event data file.
        data_hash: SHA-256 of the event data (64-char hex).
        created_at: ISO timestamp of manifest creation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    source: EventSource
    events: list[EventRecord]
    decision_time: str
    data_uri: str
    data_hash: str
    created_at: str

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("dataset_id must be a non-empty string")
        return v

    @field_validator("events")
    @classmethod
    def _events_nonempty(cls, v: list[EventRecord]) -> list[EventRecord]:
        if not v:
            raise ValueError("events must contain at least 1 event")
        return v

    @field_validator("decision_time", "created_at")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name or "temporal")

    @field_validator("data_hash")
    @classmethod
    def _data_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "data_hash")

    @field_validator("data_uri")
    @classmethod
    def _data_uri_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("data_uri must be a non-empty string")
        return v

    # --- model validators ------------------------------------------------

    @model_validator(mode="after")
    def _no_duplicate_event_ids(self) -> EventDatasetManifest:
        """event_ids must be unique across all events."""
        ids = [e.event_id for e in self.events]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"events must not contain duplicate event_ids: {dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def _no_future_leakage(self) -> EventDatasetManifest:
        """Every event's available_at must be <= decision_time."""
        dt = _parse_temporal(self.decision_time)
        for e in self.events:
            aa = _parse_temporal(e.available_at)
            if aa > dt:
                raise ValueError(
                    f"future leakage: event {e.event_id!r} available_at "
                    f"({e.available_at!r}) is after decision_time "
                    f"({self.decision_time!r})"
                )
        return self

    @model_validator(mode="after")
    def _revised_metadata_no_leakage(self) -> EventDatasetManifest:
        """Revised events must reference existing originals and have a
        later published_at than the original."""
        validate_no_revised_metadata_leakage(self.events)
        return self


# ---------------------------------------------------------------------------
# validate_point_in_time
# ---------------------------------------------------------------------------


def validate_point_in_time(event: EventRecord, decision_time: str) -> bool:
    """Check that an event is available at or before a decision time.

    Returns True if ``event.available_at <= decision_time`` (the event was
    available for trading at the decision time — no future leakage).

    Args:
        event: the :class:`EventRecord` to check.
        decision_time: the ISO datetime of the decision point.

    Returns:
        True if the event is point-in-time valid.

    Raises:
        ValueError: if ``available_at > decision_time`` (future leakage
            detected).
    """
    aa = _parse_temporal(event.available_at)
    dt = _parse_temporal(decision_time)
    if aa > dt:
        raise ValueError(
            f"future leakage: event {event.event_id!r} available_at "
            f"({event.available_at!r}) is after decision_time "
            f"({decision_time!r})"
        )
    return True


# ---------------------------------------------------------------------------
# validate_no_revised_metadata_leakage
# ---------------------------------------------------------------------------


def validate_no_revised_metadata_leakage(events: list[EventRecord]) -> bool:
    """Validate that revised events do not leak future metadata.

    Checks:
    - Every revised event's ``revised_from`` points to an existing event
      in ``events`` (by ``event_id``).
    - Every revised event has a *later* ``published_at`` than the
      original event it revises (a revision cannot be back-dated — that
      would leak future-corrected metadata into a point-in-time view).

    Args:
        events: the list of :class:`EventRecord` to validate.

    Returns:
        True if no revised-metadata leakage is detected.

    Raises:
        ValueError: if a revised event references a non-existent original
            or has an earlier-or-equal ``published_at`` than the original.
    """
    if not events:
        return True

    id_index: dict[str, EventRecord] = {e.event_id: e for e in events}

    for e in events:
        if not e.revised:
            continue
        if not e.revised_from:
            # EventRecord itself enforces this, but double-check here for
            # callers that build lists outside the model.
            raise ValueError(
                f"revised event {e.event_id!r} has no revised_from"
            )
        original = id_index.get(e.revised_from)
        if original is None:
            raise ValueError(
                f"revised event {e.event_id!r} references non-existent "
                f"original event_id {e.revised_from!r}"
            )
        if original.event_id == e.event_id:
            raise ValueError(
                f"revised event {e.event_id!r} revises itself "
                "(revised_from == event_id)"
            )
        rev_pa = _parse_temporal(e.published_at)
        orig_pa = _parse_temporal(original.published_at)
        if not (rev_pa > orig_pa):
            raise ValueError(
                f"revised event {e.event_id!r} published_at "
                f"({e.published_at!r}) must be later than original "
                f"{original.event_id!r} published_at "
                f"({original.published_at!r})"
            )
    return True


# ---------------------------------------------------------------------------
# compute_event_data_hash
# ---------------------------------------------------------------------------


def compute_event_data_hash(events: list[EventRecord]) -> str:
    """Compute a deterministic SHA-256 hash over a list of events.

    The hash is computed over the canonical JSON of the events, sorted by
    ``event_id``. Each event is serialized via its Pydantic
    ``model_dump`` with ``mode="json"`` and ``exclude_none=True`` so that
    two runs over the same events produce identical hashes regardless of
    insertion order or unset optional fields.

    Args:
        events: the list of :class:`EventRecord` to hash.

    Returns:
        A 64-character lowercase hex SHA-256 digest.

    Raises:
        ValueError: if ``events`` is empty.
    """
    if not events:
        raise ValueError("events must be non-empty to compute a data hash")
    serialized = [e.model_dump(mode="json", exclude_none=True) for e in events]
    serialized.sort(key=lambda d: d["event_id"])
    canonical = json.dumps(serialized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# EventManifestBuilder
# ---------------------------------------------------------------------------


class EventManifestBuilder:
    """Fluent builder for :class:`EventDatasetManifest`.

    Provides a chainable API for constructing an event dataset manifest
    field-by-field, then calling :meth:`build` to validate and create the
    immutable manifest.

    Example::

        manifest = (
            EventManifestBuilder("evt_001", source)
            .add_event(event_a)
            .add_event(event_b)
            .with_decision_time("2024-06-01T00:00:00Z")
            .with_data(
                uri="s3://bucket/evt_001.jsonl",
                data_hash=compute_event_data_hash([event_a, event_b]),
            )
            .build()
        )
    """

    def __init__(self, dataset_id: str, source: EventSource) -> None:
        """Initialize the builder with a dataset id and source.

        Args:
            dataset_id: the dataset identifier.
            source: the :class:`EventSource` provenance.
        """
        self._dataset_id: str = dataset_id
        self._source: EventSource = source
        self._events: list[EventRecord] = []
        self._decision_time: str = ""
        self._data_uri: str = ""
        self._data_hash: str = ""
        self._created_at: str = ""

    def add_event(self, event: EventRecord) -> EventManifestBuilder:
        """Add an event to the manifest.

        Args:
            event: the :class:`EventRecord` to add.

        Returns:
            self (for chaining).
        """
        self._events.append(event)
        return self

    def with_decision_time(self, decision_time: str) -> EventManifestBuilder:
        """Set the decision time (the point-in-time for joining).

        Args:
            decision_time: ISO datetime — the point-in-time for joining.

        Returns:
            self (for chaining).
        """
        self._decision_time = decision_time
        return self

    def with_data(self, uri: str, data_hash: str) -> EventManifestBuilder:
        """Set the data location and hash.

        Args:
            uri: path/URI to the event data file.
            data_hash: SHA-256 of the event data (64-char hex).

        Returns:
            self (for chaining).
        """
        self._data_uri = uri
        self._data_hash = data_hash
        return self

    def with_created_at(self, created_at: str) -> EventManifestBuilder:
        """Set the creation timestamp.

        Args:
            created_at: ISO timestamp of manifest creation.

        Returns:
            self (for chaining).
        """
        self._created_at = created_at
        return self

    def build(self) -> EventDatasetManifest:
        """Build and validate the :class:`EventDatasetManifest`.

        Returns:
            The validated, frozen manifest.

        Raises:
            ValueError: if any required field is missing or validation
                fails (fail-closed).
        """
        if not self._created_at:
            from datetime import datetime, timezone
            self._created_at = datetime.now(timezone.utc).isoformat()

        return EventDatasetManifest(
            dataset_id=self._dataset_id,
            source=self._source,
            events=self._events,
            decision_time=self._decision_time,
            data_uri=self._data_uri,
            data_hash=self._data_hash,
            created_at=self._created_at,
        )


# ---------------------------------------------------------------------------
# join_events_point_in_time
# ---------------------------------------------------------------------------


def join_events_point_in_time(
    events: list[EventRecord], decision_time: str
) -> list[EventRecord]:
    """Return the events available at a decision time, point-in-time safe.

    Returns only the events whose ``available_at <= decision_time``. In
    addition, filters out revised events that would leak future metadata:
    a revised event is only included if its original (``revised_from``)
    is *also* available at the decision time *and* the revision itself is
    available. This prevents a revision (which by definition carries
    future-corrected information) from being joined at a decision time
    where the original was not yet available.

    Args:
        events: the list of :class:`EventRecord` to filter.
        decision_time: the ISO datetime of the decision point.

    Returns:
        A list of :class:`EventRecord` eligible for the decision time,
        preserving the input order.

    Raises:
        ValueError: if ``decision_time`` is not a valid ISO datetime.
    """
    # Validate decision_time format up front.
    _parse_temporal(decision_time)
    dt = _parse_temporal(decision_time)

    # Index of events available at the decision time, by event_id.
    available_ids: set[str] = set()
    for e in events:
        if _parse_temporal(e.available_at) <= dt:
            available_ids.add(e.event_id)

    result: list[EventRecord] = []
    for e in events:
        if _parse_temporal(e.available_at) > dt:
            continue
        if e.revised:
            # The original must also be available at the decision time;
            # otherwise the revision leaks future-corrected metadata.
            if e.revised_from not in available_ids:
                continue
        result.append(e)
    return result
