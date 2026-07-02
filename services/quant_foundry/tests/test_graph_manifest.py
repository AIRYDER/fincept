"""Tests for quant_foundry.graph_manifest (T-12.3 GraphDatasetManifest).

Tests verify:
- NodeFeatureSchema construction, defaults, and validation.
- GraphEdge construction, self-loop rejection, availability ordering.
- GraphNode construction and validation.
- GraphDatasetManifest construction, PIT-safe invariants, duplicate
  detection, node id mapping, feature/schema consistency, future edge
  rejection.
- validate_no_future_edge (valid, invalid, edge cases).
- validate_node_id_mapping (valid, missing nodes).
- compute_graph_data_hash determinism.
- GraphManifestBuilder fluent API.
- filter_edges_point_in_time.
- Fail-closed: future edge, missing node mapping, self-loops, duplicate
  node_ids.
- Edge cases: single node, single edge, single feature.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from quant_foundry.graph_manifest import (
    GraphDatasetManifest,
    GraphEdge,
    GraphManifestBuilder,
    GraphNode,
    NodeFeatureSchema,
    compute_graph_data_hash,
    filter_edges_point_in_time,
    validate_no_future_edge,
    validate_node_id_mapping,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_feature_schema(
    feature_name: str = "market_cap",
    dtype: str = "float32",
    description: str = "market capitalization",
) -> NodeFeatureSchema:
    """Build a NodeFeatureSchema with defaults."""
    return NodeFeatureSchema(
        feature_name=feature_name,
        dtype=dtype,
        description=description,
    )


def _make_edge_kwargs(**overrides) -> dict:
    """Build kwargs for a valid GraphEdge."""
    base = dict(
        edge_id="AAPL_MSFT_sector_2024-01-01T00:00:00Z",
        src_node="AAPL",
        dst_node="MSFT",
        edge_type="sector",
        edge_weight=0.8,
        edge_observed_at="2024-01-01T00:00:00Z",
        edge_available_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return base


def _make_edge(**overrides) -> GraphEdge:
    """Build a valid GraphEdge."""
    return GraphEdge(**_make_edge_kwargs(**overrides))


def _make_node_kwargs(**overrides) -> dict:
    """Build kwargs for a valid GraphNode."""
    base = dict(
        node_id="AAPL",
        node_type="symbol",
        features={"market_cap": 100.0},
        observed_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return base


def _make_node(**overrides) -> GraphNode:
    """Build a valid GraphNode."""
    return GraphNode(**_make_node_kwargs(**overrides))


def _make_manifest_kwargs(**overrides) -> dict:
    """Build kwargs for a valid GraphDatasetManifest."""
    base = dict(
        dataset_id="graph_001",
        node_ids=["AAPL", "MSFT"],
        edges=[_make_edge()],
        nodes=[_make_node(), _make_node(node_id="MSFT")],
        node_feature_schema=[_make_feature_schema()],
        graph_snapshot_time="2024-06-01T00:00:00Z",
        label_horizon=5,
        data_uri="s3://bucket/graph_001.json",
        data_hash="a" * 64,
        created_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return base


def _make_manifest(**overrides) -> GraphDatasetManifest:
    """Build a valid GraphDatasetManifest."""
    return GraphDatasetManifest(**_make_manifest_kwargs(**overrides))


# ---------------------------------------------------------------------------
# NodeFeatureSchema
# ---------------------------------------------------------------------------


class TestNodeFeatureSchema:
    """Tests for NodeFeatureSchema."""

    def test_construction_defaults(self) -> None:
        schema = NodeFeatureSchema(feature_name="market_cap", dtype="float32")
        assert schema.feature_name == "market_cap"
        assert schema.dtype == "float32"
        assert schema.description == ""

    def test_construction_with_description(self) -> None:
        schema = _make_feature_schema(description="cap")
        assert schema.description == "cap"

    @pytest.mark.parametrize("dtype", ["float32", "float64", "int32"])
    def test_allowed_dtypes(self, dtype: str) -> None:
        schema = NodeFeatureSchema(feature_name="f", dtype=dtype)
        assert schema.dtype == dtype

    def test_empty_feature_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NodeFeatureSchema(feature_name="", dtype="float32")

    def test_whitespace_feature_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NodeFeatureSchema(feature_name="   ", dtype="float32")

    def test_invalid_dtype_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NodeFeatureSchema(feature_name="f", dtype="float16")

    def test_frozen(self) -> None:
        schema = _make_feature_schema()
        with pytest.raises(ValidationError):
            schema.feature_name = "other"  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            NodeFeatureSchema(
                feature_name="f", dtype="float32", extra="bad"  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# GraphEdge
# ---------------------------------------------------------------------------


class TestGraphEdge:
    """Tests for GraphEdge."""

    def test_construction_defaults(self) -> None:
        edge = GraphEdge(
            edge_id="AAPL_MSFT_sector_2024-01-01T00:00:00Z",
            src_node="AAPL",
            dst_node="MSFT",
            edge_type="sector",
            edge_observed_at="2024-01-01T00:00:00Z",
            edge_available_at="2024-01-01T00:00:00Z",
        )
        assert edge.edge_weight == 1.0

    @pytest.mark.parametrize(
        "edge_type",
        ["sector", "industry", "correlation", "supply_chain"],
    )
    def test_allowed_edge_types(self, edge_type: str) -> None:
        edge = _make_edge(edge_type=edge_type)
        assert edge.edge_type == edge_type

    def test_self_loop_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_edge(src_node="AAPL", dst_node="AAPL")

    def test_available_before_observed_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_edge(
                edge_observed_at="2024-06-01T00:00:00Z",
                edge_available_at="2024-01-01T00:00:00Z",
            )

    def test_available_equal_observed_ok(self) -> None:
        edge = _make_edge(
            edge_observed_at="2024-01-01T00:00:00Z",
            edge_available_at="2024-01-01T00:00:00Z",
        )
        assert edge.edge_available_at == edge.edge_observed_at

    def test_invalid_edge_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_edge(edge_type="unknown")

    def test_empty_edge_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_edge(edge_id="")

    def test_empty_src_node_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_edge(src_node="")

    def test_empty_dst_node_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_edge(dst_node="")

    def test_invalid_observed_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_edge(edge_observed_at="not-a-date")

    def test_frozen(self) -> None:
        edge = _make_edge()
        with pytest.raises(ValidationError):
            edge.src_node = "GOOG"  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GraphEdge(
                **{**_make_edge_kwargs(), "extra": "bad"}  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# GraphNode
# ---------------------------------------------------------------------------


class TestGraphNode:
    """Tests for GraphNode."""

    def test_construction(self) -> None:
        node = _make_node()
        assert node.node_id == "AAPL"
        assert node.node_type == "symbol"
        assert node.features == {"market_cap": 100.0}

    def test_empty_node_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_node(node_id="")

    def test_whitespace_node_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_node(node_id="  ")

    def test_empty_node_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_node(node_type="")

    def test_invalid_observed_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_node(observed_at="bad")

    def test_empty_features_ok(self) -> None:
        node = _make_node(features={})
        assert node.features == {}

    def test_frozen(self) -> None:
        node = _make_node()
        with pytest.raises(ValidationError):
            node.node_id = "MSFT"  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(
                **{**_make_node_kwargs(), "extra": "bad"}  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# GraphDatasetManifest
# ---------------------------------------------------------------------------


class TestGraphDatasetManifest:
    """Tests for GraphDatasetManifest."""

    def test_construction(self) -> None:
        manifest = _make_manifest()
        assert manifest.dataset_id == "graph_001"
        assert manifest.label_horizon == 5
        assert len(manifest.node_ids) == 2
        assert len(manifest.edges) == 1
        assert len(manifest.nodes) == 2
        assert len(manifest.node_feature_schema) == 1

    def test_empty_node_ids_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(node_ids=[])

    def test_empty_edges_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(edges=[])

    def test_empty_nodes_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(nodes=[])

    def test_empty_feature_schema_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(node_feature_schema=[])

    def test_label_horizon_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(label_horizon=0)

    def test_label_horizon_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(label_horizon=-1)

    def test_duplicate_node_ids_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(
                node_ids=["AAPL", "AAPL"],
                nodes=[_make_node(), _make_node()],
            )

    def test_edge_src_not_in_node_ids_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(
                edges=[_make_edge(src_node="GOOG")],
            )

    def test_edge_dst_not_in_node_ids_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(
                edges=[_make_edge(dst_node="GOOG")],
            )

    def test_node_feature_not_in_schema_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(
                nodes=[_make_node(features={"unknown_feature": 1.0})],
            )

    def test_future_edge_rejected(self) -> None:
        # Edge available after snapshot time.
        with pytest.raises(ValidationError):
            _make_manifest(
                edges=[
                    _make_edge(
                        edge_available_at="2024-12-01T00:00:00Z",
                    )
                ],
                graph_snapshot_time="2024-06-01T00:00:00Z",
            )

    def test_edge_available_equal_snapshot_ok(self) -> None:
        manifest = _make_manifest(
            edges=[
                _make_edge(
                    edge_available_at="2024-06-01T00:00:00Z",
                )
            ],
            graph_snapshot_time="2024-06-01T00:00:00Z",
        )
        assert manifest.graph_snapshot_time == "2024-06-01T00:00:00Z"

    def test_invalid_data_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(data_hash="short")

    def test_invalid_snapshot_time_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(graph_snapshot_time="bad")

    def test_empty_data_uri_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(data_uri="")

    def test_duplicate_feature_names_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_manifest(
                node_feature_schema=[
                    _make_feature_schema("cap"),
                    _make_feature_schema("cap"),
                ],
            )

    def test_frozen(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(ValidationError):
            manifest.dataset_id = "other"  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GraphDatasetManifest(
                **{**_make_manifest_kwargs(), "extra": "bad"}  # type: ignore[arg-type]
            )

    def test_single_node_single_edge_single_feature(self) -> None:
        """Edge case: minimal graph with one node, one edge, one feature.

        Note: a single node cannot have an edge (self-loops forbidden), so
        we use two nodes but one edge and one feature.
        """
        manifest = _make_manifest(
            node_ids=["AAPL", "MSFT"],
            nodes=[_make_node(), _make_node(node_id="MSFT")],
            edges=[_make_edge()],
            node_feature_schema=[_make_feature_schema()],
        )
        assert len(manifest.nodes) == 2
        assert len(manifest.edges) == 1
        assert len(manifest.node_feature_schema) == 1


# ---------------------------------------------------------------------------
# validate_no_future_edge
# ---------------------------------------------------------------------------


class TestValidateNoFutureEdge:
    """Tests for validate_no_future_edge."""

    def test_valid_edge_available_before_snapshot(self) -> None:
        edge = _make_edge(edge_available_at="2024-01-01T00:00:00Z")
        assert validate_no_future_edge(edge, "2024-06-01T00:00:00Z") is True

    def test_valid_edge_available_equal_snapshot(self) -> None:
        edge = _make_edge(edge_available_at="2024-06-01T00:00:00Z")
        assert validate_no_future_edge(edge, "2024-06-01T00:00:00Z") is True

    def test_future_edge_rejected(self) -> None:
        edge = _make_edge(edge_available_at="2024-12-01T00:00:00Z")
        with pytest.raises(ValueError, match="future edge"):
            validate_no_future_edge(edge, "2024-06-01T00:00:00Z")

    def test_invalid_snapshot_time_raises(self) -> None:
        edge = _make_edge()
        with pytest.raises(ValueError):
            validate_no_future_edge(edge, "not-a-date")


# ---------------------------------------------------------------------------
# validate_node_id_mapping
# ---------------------------------------------------------------------------


class TestValidateNodeIdMapping:
    """Tests for validate_node_id_mapping."""

    def test_valid_mapping(self) -> None:
        edges = [_make_edge(src_node="AAPL", dst_node="MSFT")]
        assert validate_node_id_mapping(edges, ["AAPL", "MSFT"]) is True

    def test_missing_src_node_rejected(self) -> None:
        edges = [_make_edge(src_node="GOOG", dst_node="MSFT")]
        with pytest.raises(ValueError, match="src_node"):
            validate_node_id_mapping(edges, ["AAPL", "MSFT"])

    def test_missing_dst_node_rejected(self) -> None:
        edges = [_make_edge(src_node="AAPL", dst_node="GOOG")]
        with pytest.raises(ValueError, match="dst_node"):
            validate_node_id_mapping(edges, ["AAPL", "MSFT"])

    def test_empty_edges_ok(self) -> None:
        assert validate_node_id_mapping([], ["AAPL"]) is True


# ---------------------------------------------------------------------------
# compute_graph_data_hash
# ---------------------------------------------------------------------------


class TestComputeGraphDataHash:
    """Tests for compute_graph_data_hash."""

    def test_determinism_same_order(self) -> None:
        edges = [_make_edge(), _make_edge(src_node="MSFT", dst_node="AAPL", edge_id="MSFT_AAPL_sector_2024-01-01T00:00:00Z")]
        nodes = [_make_node(), _make_node(node_id="MSFT")]
        h1 = compute_graph_data_hash(edges, nodes)
        h2 = compute_graph_data_hash(edges, nodes)
        assert h1 == h2
        assert len(h1) == 64

    def test_order_independent(self) -> None:
        e1 = _make_edge()
        e2 = _make_edge(
            src_node="MSFT",
            dst_node="AAPL",
            edge_id="MSFT_AAPL_sector_2024-01-01T00:00:00Z",
        )
        n1 = _make_node()
        n2 = _make_node(node_id="MSFT")
        h_a = compute_graph_data_hash([e1, e2], [n1, n2])
        h_b = compute_graph_data_hash([e2, e1], [n2, n1])
        assert h_a == h_b

    def test_different_edges_different_hash(self) -> None:
        e1 = _make_edge(edge_weight=0.5)
        e2 = _make_edge(edge_weight=0.9)
        nodes = [_make_node(), _make_node(node_id="MSFT")]
        assert compute_graph_data_hash([e1], nodes) != compute_graph_data_hash([e2], nodes)

    def test_different_nodes_different_hash(self) -> None:
        edge = _make_edge()
        n1 = _make_node(features={"market_cap": 100.0})
        n2 = _make_node(features={"market_cap": 200.0})
        assert compute_graph_data_hash([edge], [n1]) != compute_graph_data_hash([edge], [n2])

    def test_returns_64_char_hex(self) -> None:
        h = compute_graph_data_hash([_make_edge()], [_make_node()])
        assert len(h) == 64
        int(h, 16)  # parses as hex


# ---------------------------------------------------------------------------
# GraphManifestBuilder
# ---------------------------------------------------------------------------


class TestGraphManifestBuilder:
    """Tests for GraphManifestBuilder."""

    def test_fluent_build(self) -> None:
        nodes = [_make_node(), _make_node(node_id="MSFT")]
        edges = [_make_edge()]
        schema = [_make_feature_schema()]
        manifest = (
            GraphManifestBuilder("graph_001")
            .with_nodes(nodes)
            .with_edges(edges)
            .with_feature_schema(schema)
            .with_snapshot_time("2024-06-01T00:00:00Z")
            .with_label_horizon(5)
            .with_data(uri="s3://bucket/graph.json", data_hash="a" * 64)
            .with_created_at("2024-01-01T00:00:00Z")
            .build()
        )
        assert manifest.dataset_id == "graph_001"
        assert manifest.node_ids == ["AAPL", "MSFT"]
        assert len(manifest.edges) == 1

    def test_build_defaults_created_at(self) -> None:
        manifest = (
            GraphManifestBuilder("graph_002")
            .with_nodes([_make_node(), _make_node(node_id="MSFT")])
            .with_edges([_make_edge()])
            .with_feature_schema([_make_feature_schema()])
            .with_snapshot_time("2024-06-01T00:00:00Z")
            .with_label_horizon(1)
            .with_data(uri="s3://b/g.json", data_hash="b" * 64)
            .build()
        )
        assert manifest.created_at != ""

    def test_build_fail_closed_missing_nodes(self) -> None:
        with pytest.raises(ValidationError):
            (
                GraphManifestBuilder("graph_003")
                .with_nodes([])
                .with_edges([_make_edge()])
                .with_feature_schema([_make_feature_schema()])
                .with_snapshot_time("2024-06-01T00:00:00Z")
                .with_label_horizon(1)
                .with_data(uri="s3://b/g.json", data_hash="b" * 64)
                .build()
            )

    def test_build_fail_closed_future_edge(self) -> None:
        with pytest.raises(ValidationError):
            (
                GraphManifestBuilder("graph_004")
                .with_nodes([_make_node(), _make_node(node_id="MSFT")])
                .with_edges([
                    _make_edge(edge_available_at="2024-12-01T00:00:00Z")
                ])
                .with_feature_schema([_make_feature_schema()])
                .with_snapshot_time("2024-06-01T00:00:00Z")
                .with_label_horizon(1)
                .with_data(uri="s3://b/g.json", data_hash="b" * 64)
                .build()
            )

    def test_with_nodes_derives_node_ids(self) -> None:
        builder = GraphManifestBuilder("g")
        builder.with_nodes([_make_node(), _make_node(node_id="MSFT")])
        assert builder._node_ids == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# filter_edges_point_in_time
# ---------------------------------------------------------------------------


class TestFilterEdgesPointInTime:
    """Tests for filter_edges_point_in_time."""

    def test_filters_future_edges(self) -> None:
        e1 = _make_edge(edge_available_at="2024-01-01T00:00:00Z")
        e2 = _make_edge(
            src_node="MSFT",
            dst_node="AAPL",
            edge_id="MSFT_AAPL_sector_2024-01-01T00:00:00Z",
            edge_available_at="2024-12-01T00:00:00Z",
        )
        result = filter_edges_point_in_time(
            [e1, e2], "2024-06-01T00:00:00Z"
        )
        assert result == [e1]

    def test_keeps_all_when_snapshot_late(self) -> None:
        e1 = _make_edge(edge_available_at="2024-01-01T00:00:00Z")
        e2 = _make_edge(
            src_node="MSFT",
            dst_node="AAPL",
            edge_id="MSFT_AAPL_sector_2024-01-01T00:00:00Z",
            edge_available_at="2024-05-01T00:00:00Z",
        )
        result = filter_edges_point_in_time(
            [e1, e2], "2024-06-01T00:00:00Z"
        )
        assert result == [e1, e2]

    def test_empty_when_all_future(self) -> None:
        e1 = _make_edge(edge_available_at="2024-12-01T00:00:00Z")
        result = filter_edges_point_in_time([e1], "2024-01-01T00:00:00Z")
        assert result == []

    def test_preserves_order(self) -> None:
        e1 = _make_edge(edge_available_at="2024-01-01T00:00:00Z")
        e2 = _make_edge(
            src_node="MSFT",
            dst_node="AAPL",
            edge_id="MSFT_AAPL_sector_2024-01-01T00:00:00Z",
            edge_available_at="2024-02-01T00:00:00Z",
        )
        result = filter_edges_point_in_time(
            [e1, e2], "2024-06-01T00:00:00Z"
        )
        assert result == [e1, e2]

    def test_equal_available_kept(self) -> None:
        e = _make_edge(edge_available_at="2024-06-01T00:00:00Z")
        result = filter_edges_point_in_time([e], "2024-06-01T00:00:00Z")
        assert result == [e]
