"""Tests for quant_foundry.event_manifest (T-12.1 EventDatasetManifest).

Tests verify:
- EventSource construction, defaults, and validation.
- EventRecord construction, leakage-safe invariants, duplicate detection,
  revised-field consistency.
- EventDatasetManifest construction, no-future-leakage, duplicate
  event_ids, revised-metadata leakage.
- validate_point_in_time (valid, invalid, edge cases).
- validate_no_revised_metadata_leakage (valid, invalid revised_from,
  earlier published_at).
- compute_event_data_hash determinism.
- EventManifestBuilder fluent API.
- join_events_point_in_time.
- Fail-closed: future leakage, duplicate event_ids, revised metadata
  leakage.
- Edge cases: single event, single symbol, multiple horizons.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from quant_foundry.event_manifest import (
    EventDatasetManifest,
    EventManifestBuilder,
    EventRecord,
    EventSource,
    compute_event_data_hash,
    join_events_point_in_time,
    validate_no_revised_metadata_leakage,
    validate_point_in_time,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_SHA = "a" * 64
_SHA_B = "b" * 64


def _make_source(**overrides) -> EventSource:
    """Build an EventSource with defaults."""
    base = dict(
        source_id="reuters",
        source_hash=_SHA,
        retrieval_method="api",
    )
    base.update(overrides)
    return EventSource(**base)


def _make_event_kwargs(**overrides) -> dict:
    """Build kwargs for a valid EventRecord."""
    base = dict(
        event_id="reuters_2024-01-01T00:00:00Z_earnings",
        source_id="reuters",
        published_at="2024-01-01T00:00:00Z",
        available_at="2024-01-01T00:05:00Z",
        affected_symbols=["AAPL"],
        event_type="earnings",
        label_horizons=[1, 5, 20],
    )
    base.update(overrides)
    return base


def _make_event(**overrides) -> EventRecord:
    """Build a valid EventRecord."""
    return EventRecord(**_make_event_kwargs(**overrides))


def _make_manifest_kwargs(**overrides) -> dict:
    """Build kwargs for a valid EventDatasetManifest."""
    base = dict(
        dataset_id="evt_001",
        source=_make_source(),
        events=[_make_event()],
        decision_time="2024-06-01T00:00:00Z",
        data_uri="s3://bucket/evt_001.jsonl",
        data_hash=_SHA,
        created_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return base


def _make_manifest(**overrides) -> EventDatasetManifest:
    """Build a valid EventDatasetManifest."""
    return EventDatasetManifest(**_make_manifest_kwargs(**overrides))


# ---------------------------------------------------------------------------
# EventSource
# ---------------------------------------------------------------------------


class TestEventSource:
    def test_construct_defaults(self):
        s = _make_source()
        assert s.source_id == "reuters"
        assert s.source_hash == _SHA
        assert s.retrieval_method == "api"

    def test_all_retrieval_methods(self):
        for m in ("api", "scrape", "file"):
            s = _make_source(retrieval_method=m)
            assert s.retrieval_method == m

    def test_frozen(self):
        s = _make_source()
        with pytest.raises(ValidationError):
            s.source_id = "bloomberg"  # type: ignore[misc]

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            _make_source(extra_field="x")

    def test_empty_source_id_rejected(self):
        with pytest.raises(ValidationError):
            _make_source(source_id="")

    def test_whitespace_source_id_rejected(self):
        with pytest.raises(ValidationError):
            _make_source(source_id="   ")

    def test_invalid_source_hash_length_rejected(self):
        with pytest.raises(ValidationError):
            _make_source(source_hash="a" * 63)

    def test_invalid_source_hash_nonhex_rejected(self):
        with pytest.raises(ValidationError):
            _make_source(source_hash="z" * 64)

    def test_source_hash_lowercased(self):
        s = _make_source(source_hash="A" * 64)
        assert s.source_hash == "a" * 64

    def test_invalid_retrieval_method_rejected(self):
        with pytest.raises(ValidationError):
            _make_source(retrieval_method="ftp")


# ---------------------------------------------------------------------------
# EventRecord
# ---------------------------------------------------------------------------


class TestEventRecord:
    def test_construct_defaults(self):
        e = _make_event()
        assert e.event_id == "reuters_2024-01-01T00:00:00Z_earnings"
        assert e.source_id == "reuters"
        assert e.published_at == "2024-01-01T00:00:00Z"
        assert e.available_at == "2024-01-01T00:05:00Z"
        assert e.affected_symbols == ["AAPL"]
        assert e.event_type == "earnings"
        assert e.raw_text_hash is None
        assert e.embedding_model_hash is None
        assert e.embedding is None
        assert e.label_horizons == [1, 5, 20]
        assert e.revised is False
        assert e.revised_from is None

    def test_frozen(self):
        e = _make_event()
        with pytest.raises(ValidationError):
            e.event_id = "x"  # type: ignore[misc]

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            EventRecord(**_make_event_kwargs(extra_field="x"))

    def test_empty_event_id_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(event_id="")

    def test_empty_source_id_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(source_id="")

    def test_empty_event_type_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(event_type="")

    def test_empty_affected_symbols_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(affected_symbols=[])

    def test_whitespace_symbol_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(affected_symbols=["AAPL", "   "])

    def test_duplicate_symbols_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(affected_symbols=["AAPL", "AAPL"])

    def test_available_before_published_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(
                published_at="2024-01-01T00:10:00Z",
                available_at="2024-01-01T00:05:00Z",
            )

    def test_available_equal_published_ok(self):
        e = _make_event(
            published_at="2024-01-01T00:05:00Z",
            available_at="2024-01-01T00:05:00Z",
        )
        assert e.available_at == e.published_at

    def test_empty_label_horizons_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(label_horizons=[])

    def test_zero_horizon_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(label_horizons=[0])

    def test_negative_horizon_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(label_horizons=[-1])

    def test_invalid_published_at_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(published_at="not-a-date")

    def test_invalid_available_at_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(available_at="not-a-date")

    def test_raw_text_hash_valid(self):
        e = _make_event(raw_text_hash=_SHA_B)
        assert e.raw_text_hash == _SHA_B

    def test_raw_text_hash_invalid_length(self):
        with pytest.raises(ValidationError):
            _make_event(raw_text_hash="a" * 10)

    def test_embedding_model_hash_valid(self):
        e = _make_event(embedding_model_hash=_SHA_B)
        assert e.embedding_model_hash == _SHA_B

    def test_embedding_valid(self):
        e = _make_event(embedding=[0.1, 0.2, 0.3])
        assert e.embedding == [0.1, 0.2, 0.3]

    def test_embedding_empty_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(embedding=[])

    def test_embedding_non_float_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(embedding=[0.1, "x", 0.3])  # type: ignore[list-item]

    def test_revised_without_revised_from_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(revised=True, revised_from=None)

    def test_revised_from_without_revised_rejected(self):
        with pytest.raises(ValidationError):
            _make_event(revised=False, revised_from="orig_001")

    def test_revised_with_revised_from_ok(self):
        e = _make_event(revised=True, revised_from="orig_001")
        assert e.revised is True
        assert e.revised_from == "orig_001"

    def test_single_symbol_ok(self):
        e = _make_event(affected_symbols=["MSFT"])
        assert e.affected_symbols == ["MSFT"]

    def test_multiple_horizons_ok(self):
        e = _make_event(label_horizons=[1, 5, 20, 60])
        assert e.label_horizons == [1, 5, 20, 60]


# ---------------------------------------------------------------------------
# EventDatasetManifest
# ---------------------------------------------------------------------------


class TestEventDatasetManifest:
    def test_construct_defaults(self):
        m = _make_manifest()
        assert m.dataset_id == "evt_001"
        assert m.source.source_id == "reuters"
        assert len(m.events) == 1
        assert m.decision_time == "2024-06-01T00:00:00Z"
        assert m.data_uri == "s3://bucket/evt_001.jsonl"
        assert m.data_hash == _SHA
        assert m.schema_version == 1

    def test_frozen(self):
        m = _make_manifest()
        with pytest.raises(ValidationError):
            m.dataset_id = "x"  # type: ignore[misc]

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            EventDatasetManifest(**_make_manifest_kwargs(extra="x"))

    def test_empty_dataset_id_rejected(self):
        with pytest.raises(ValidationError):
            _make_manifest(dataset_id="")

    def test_empty_events_rejected(self):
        with pytest.raises(ValidationError):
            _make_manifest(events=[])

    def test_empty_data_uri_rejected(self):
        with pytest.raises(ValidationError):
            _make_manifest(data_uri="")

    def test_invalid_data_hash_rejected(self):
        with pytest.raises(ValidationError):
            _make_manifest(data_hash="a" * 10)

    def test_duplicate_event_ids_rejected(self):
        e1 = _make_event()
        e2 = _make_event(event_id="reuters_2024-01-02T00:00:00Z_earnings")
        e2_dup = _make_event(event_id="reuters_2024-01-02T00:00:00Z_earnings")
        with pytest.raises(ValidationError):
            _make_manifest(events=[e1, e2, e2_dup])

    def test_future_leakage_rejected(self):
        e = _make_event(available_at="2024-07-01T00:00:00Z")
        with pytest.raises(ValidationError):
            _make_manifest(events=[e], decision_time="2024-06-01T00:00:00Z")

    def test_available_equal_decision_time_ok(self):
        e = _make_event(available_at="2024-06-01T00:00:00Z")
        m = _make_manifest(events=[e], decision_time="2024-06-01T00:00:00Z")
        assert m.events[0].available_at == m.decision_time

    def test_invalid_decision_time_rejected(self):
        with pytest.raises(ValidationError):
            _make_manifest(decision_time="not-a-date")

    def test_invalid_created_at_rejected(self):
        with pytest.raises(ValidationError):
            _make_manifest(created_at="not-a-date")

    def test_revised_from_nonexistent_rejected(self):
        orig = _make_event(event_id="reuters_2024-01-01T00:00:00Z_earnings")
        rev = _make_event(
            event_id="reuters_2024-01-01T00:00:00Z_earnings_rev",
            published_at="2024-01-02T00:00:00Z",
            available_at="2024-01-02T00:05:00Z",
            revised=True,
            revised_from="does_not_exist",
        )
        with pytest.raises(ValidationError):
            _make_manifest(events=[orig, rev])

    def test_revised_earlier_published_at_rejected(self):
        orig = _make_event(
            event_id="reuters_2024-01-05T00:00:00Z_earnings",
            published_at="2024-01-05T00:00:00Z",
            available_at="2024-01-05T00:05:00Z",
        )
        rev = _make_event(
            event_id="reuters_2024-01-05T00:00:00Z_earnings_rev",
            published_at="2024-01-04T00:00:00Z",
            available_at="2024-01-04T00:05:00Z",
            revised=True,
            revised_from="reuters_2024-01-05T00:00:00Z_earnings",
        )
        with pytest.raises(ValidationError):
            _make_manifest(events=[orig, rev])

    def test_revised_valid_ok(self):
        orig = _make_event(
            event_id="reuters_2024-01-01T00:00:00Z_earnings",
            published_at="2024-01-01T00:00:00Z",
            available_at="2024-01-01T00:05:00Z",
        )
        rev = _make_event(
            event_id="reuters_2024-01-01T00:00:00Z_earnings_rev",
            published_at="2024-01-02T00:00:00Z",
            available_at="2024-01-02T00:05:00Z",
            revised=True,
            revised_from="reuters_2024-01-01T00:00:00Z_earnings",
        )
        m = _make_manifest(events=[orig, rev])
        assert len(m.events) == 2


# ---------------------------------------------------------------------------
# validate_point_in_time
# ---------------------------------------------------------------------------


class TestValidatePointInTime:
    def test_valid_event(self):
        e = _make_event(available_at="2024-01-01T00:05:00Z")
        assert validate_point_in_time(e, "2024-06-01T00:00:00Z") is True

    def test_equal_available_and_decision(self):
        e = _make_event(available_at="2024-06-01T00:00:00Z")
        assert validate_point_in_time(e, "2024-06-01T00:00:00Z") is True

    def test_future_leakage_raises(self):
        e = _make_event(available_at="2024-07-01T00:00:00Z")
        with pytest.raises(ValueError, match="future leakage"):
            validate_point_in_time(e, "2024-06-01T00:00:00Z")

    def test_invalid_decision_time_raises(self):
        e = _make_event()
        with pytest.raises(ValueError):
            validate_point_in_time(e, "not-a-date")


# ---------------------------------------------------------------------------
# validate_no_revised_metadata_leakage
# ---------------------------------------------------------------------------


class TestValidateNoRevisedMetadataLeakage:
    def test_no_revised_events_ok(self):
        events = [_make_event(), _make_event(event_id="b")]
        assert validate_no_revised_metadata_leakage(events) is True

    def test_empty_list_ok(self):
        assert validate_no_revised_metadata_leakage([]) is True

    def test_valid_revision_ok(self):
        orig = _make_event(
            event_id="orig_001",
            published_at="2024-01-01T00:00:00Z",
            available_at="2024-01-01T00:05:00Z",
        )
        rev = _make_event(
            event_id="rev_001",
            published_at="2024-01-02T00:00:00Z",
            available_at="2024-01-02T00:05:00Z",
            revised=True,
            revised_from="orig_001",
        )
        assert validate_no_revised_metadata_leakage([orig, rev]) is True

    def test_invalid_revised_from_raises(self):
        orig = _make_event(event_id="orig_001")
        rev = _make_event(
            event_id="rev_001",
            published_at="2024-01-02T00:00:00Z",
            available_at="2024-01-02T00:05:00Z",
            revised=True,
            revised_from="ghost",
        )
        with pytest.raises(ValueError, match="non-existent"):
            validate_no_revised_metadata_leakage([orig, rev])

    def test_earlier_published_at_raises(self):
        orig = _make_event(
            event_id="orig_001",
            published_at="2024-01-05T00:00:00Z",
            available_at="2024-01-05T00:05:00Z",
        )
        rev = _make_event(
            event_id="rev_001",
            published_at="2024-01-04T00:00:00Z",
            available_at="2024-01-04T00:05:00Z",
            revised=True,
            revised_from="orig_001",
        )
        with pytest.raises(ValueError, match="must be later"):
            validate_no_revised_metadata_leakage([orig, rev])

    def test_equal_published_at_raises(self):
        orig = _make_event(
            event_id="orig_001",
            published_at="2024-01-05T00:00:00Z",
            available_at="2024-01-05T00:05:00Z",
        )
        rev = _make_event(
            event_id="rev_001",
            published_at="2024-01-05T00:00:00Z",
            available_at="2024-01-05T00:06:00Z",
            revised=True,
            revised_from="orig_001",
        )
        with pytest.raises(ValueError, match="must be later"):
            validate_no_revised_metadata_leakage([orig, rev])

    def test_self_revision_raises(self):
        rev = _make_event(
            event_id="rev_001",
            published_at="2024-01-02T00:00:00Z",
            available_at="2024-01-02T00:05:00Z",
            revised=True,
            revised_from="rev_001",
        )
        with pytest.raises(ValueError, match="revises itself"):
            validate_no_revised_metadata_leakage([rev])


# ---------------------------------------------------------------------------
# compute_event_data_hash
# ---------------------------------------------------------------------------


class TestComputeEventDataHash:
    def test_deterministic_same_order(self):
        e1 = _make_event()
        e2 = _make_event(event_id="reuters_2024-01-02T00:00:00Z_earnings")
        h1 = compute_event_data_hash([e1, e2])
        h2 = compute_event_data_hash([e1, e2])
        assert h1 == h2
        assert len(h1) == 64

    def test_order_independent(self):
        e1 = _make_event()
        e2 = _make_event(event_id="reuters_2024-01-02T00:00:00Z_earnings")
        h1 = compute_event_data_hash([e1, e2])
        h2 = compute_event_data_hash([e2, e1])
        assert h1 == h2

    def test_different_events_different_hash(self):
        e1 = _make_event()
        e2 = _make_event(event_id="reuters_2024-01-02T00:00:00Z_earnings")
        h1 = compute_event_data_hash([e1])
        h2 = compute_event_data_hash([e2])
        assert h1 != h2

    def test_single_event_ok(self):
        e = _make_event()
        h = compute_event_data_hash([e])
        assert len(h) == 64

    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            compute_event_data_hash([])

    def test_field_change_changes_hash(self):
        e1 = _make_event()
        e1b = _make_event(affected_symbols=["MSFT"])
        assert compute_event_data_hash([e1]) != compute_event_data_hash([e1b])

    def test_is_hex(self):
        e = _make_event()
        h = compute_event_data_hash([e])
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# EventManifestBuilder
# ---------------------------------------------------------------------------


class TestEventManifestBuilder:
    def test_build_minimal(self):
        e = _make_event()
        m = (
            EventManifestBuilder("evt_001", _make_source())
            .add_event(e)
            .with_decision_time("2024-06-01T00:00:00Z")
            .with_data("s3://bucket/evt.jsonl", _SHA)
            .with_created_at("2024-01-01T00:00:00Z")
            .build()
        )
        assert m.dataset_id == "evt_001"
        assert len(m.events) == 1

    def test_build_default_created_at(self):
        e = _make_event()
        m = (
            EventManifestBuilder("evt_001", _make_source())
            .add_event(e)
            .with_decision_time("2024-06-01T00:00:00Z")
            .with_data("s3://bucket/evt.jsonl", _SHA)
            .build()
        )
        assert m.created_at != ""

    def test_add_event_chaining(self):
        b = EventManifestBuilder("evt_001", _make_source())
        assert b.add_event(_make_event()) is b

    def test_with_decision_time_chaining(self):
        b = EventManifestBuilder("evt_001", _make_source())
        assert b.with_decision_time("2024-06-01T00:00:00Z") is b

    def test_with_data_chaining(self):
        b = EventManifestBuilder("evt_001", _make_source())
        assert b.with_data("uri", _SHA) is b

    def test_build_multiple_events(self):
        e1 = _make_event()
        e2 = _make_event(event_id="reuters_2024-01-02T00:00:00Z_earnings")
        m = (
            EventManifestBuilder("evt_001", _make_source())
            .add_event(e1)
            .add_event(e2)
            .with_decision_time("2024-06-01T00:00:00Z")
            .with_data("s3://bucket/evt.jsonl", _SHA)
            .with_created_at("2024-01-01T00:00:00Z")
            .build()
        )
        assert len(m.events) == 2

    def test_build_future_leakage_raises(self):
        e = _make_event(available_at="2024-07-01T00:00:00Z")
        with pytest.raises(ValidationError):
            (
                EventManifestBuilder("evt_001", _make_source())
                .add_event(e)
                .with_decision_time("2024-06-01T00:00:00Z")
                .with_data("s3://bucket/evt.jsonl", _SHA)
                .with_created_at("2024-01-01T00:00:00Z")
                .build()
            )


# ---------------------------------------------------------------------------
# join_events_point_in_time
# ---------------------------------------------------------------------------


class TestJoinEventsPointInTime:
    def test_filters_future_events(self):
        e1 = _make_event(
            event_id="e1",
            available_at="2024-01-01T00:05:00Z",
        )
        e2 = _make_event(
            event_id="e2",
            available_at="2024-07-01T00:00:00Z",
        )
        result = join_events_point_in_time([e1, e2], "2024-06-01T00:00:00Z")
        assert [e.event_id for e in result] == ["e1"]

    def test_keeps_available_events(self):
        e1 = _make_event(event_id="e1", available_at="2024-01-01T00:05:00Z")
        e2 = _make_event(event_id="e2", available_at="2024-05-01T00:00:00Z")
        result = join_events_point_in_time([e1, e2], "2024-06-01T00:00:00Z")
        assert {e.event_id for e in result} == {"e1", "e2"}

    def test_equal_available_kept(self):
        e = _make_event(event_id="e1", available_at="2024-06-01T00:00:00Z")
        result = join_events_point_in_time([e], "2024-06-01T00:00:00Z")
        assert len(result) == 1

    def test_empty_input(self):
        assert join_events_point_in_time([], "2024-06-01T00:00:00Z") == []

    def test_preserves_order(self):
        e1 = _make_event(event_id="e1", available_at="2024-01-01T00:05:00Z")
        e2 = _make_event(event_id="e2", available_at="2024-02-01T00:05:00Z")
        e3 = _make_event(event_id="e3", available_at="2024-03-01T00:05:00Z")
        result = join_events_point_in_time([e1, e2, e3], "2024-06-01T00:00:00Z")
        assert [e.event_id for e in result] == ["e1", "e2", "e3"]

    def test_revised_without_original_available_filtered(self):
        orig = _make_event(
            event_id="orig_001",
            published_at="2024-01-01T00:00:00Z",
            available_at="2024-07-01T00:00:00Z",
        )
        rev = _make_event(
            event_id="rev_001",
            published_at="2024-01-02T00:00:00Z",
            available_at="2024-01-02T00:05:00Z",
            revised=True,
            revised_from="orig_001",
        )
        # orig not available at decision time -> revision filtered out
        result = join_events_point_in_time([orig, rev], "2024-06-01T00:00:00Z")
        assert [e.event_id for e in result] == []

    def test_revised_with_original_available_kept(self):
        orig = _make_event(
            event_id="orig_001",
            published_at="2024-01-01T00:00:00Z",
            available_at="2024-01-01T00:05:00Z",
        )
        rev = _make_event(
            event_id="rev_001",
            published_at="2024-01-02T00:00:00Z",
            available_at="2024-01-02T00:05:00Z",
            revised=True,
            revised_from="orig_001",
        )
        result = join_events_point_in_time([orig, rev], "2024-06-01T00:00:00Z")
        assert {e.event_id for e in result} == {"orig_001", "rev_001"}

    def test_invalid_decision_time_raises(self):
        with pytest.raises(ValueError):
            join_events_point_in_time([_make_event()], "not-a-date")
