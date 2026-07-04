"""quant_foundry.graph_manifest — graph dataset manifest for PIT-safe graph snapshots.

TASK-12.3: GraphDatasetManifest.

This module defines the manifest schema for **graph** datasets — collections
of nodes (e.g. instrument symbols, sectors, industries) and edges (e.g.
``"sector"``, ``"industry"``, ``"correlation"``, ``"supply_chain"``) used to
train graph models (GNN / GAT / GraphSAGE).

Cross-cutting quant rigor enforced here (NEXT_STEPS_PLAN §1, §3):
- **No future edges**: every edge's ``edge_available_at`` must be <= the
  graph snapshot time. :func:`validate_no_future_edge` fail-closes if an
  edge was not yet available at the snapshot time (no future leakage into
  the graph snapshot).
- **Node id mapping**: every edge's ``src_node`` / ``dst_node`` must refer
  to a node declared in the manifest's ``node_ids``.
  :func:`validate_node_id_mapping` fail-closes on a dangling reference.
- **No self-loops**: an edge's ``src_node`` must differ from its
  ``dst_node``.
- **Deterministic edge ids**: each :class:`GraphEdge` has a deterministic
  ``edge_id`` of the form ``src_dst_type_observed`` so two runs over the
  same data produce identical ids.
- **Deterministic data hash**: :func:`compute_graph_data_hash` produces a
  stable SHA-256 over canonical JSON of edges and nodes (sorted by id).
- **Feature/schema consistency**: every node feature must be declared in
  the manifest's :class:`NodeFeatureSchema`.

The module reuses the temporal parsing helper :func:`_parse_temporal` from
:mod:`quant_foundry.dataset_manifest`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_foundry.dataset_manifest import _parse_temporal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Allowed dtype strings for :class:`NodeFeatureSchema`.
_ALLOWED_DTYPES: frozenset[str] = frozenset({"float32", "float64", "int32"})

#: Allowed edge types for :class:`GraphEdge`.
_ALLOWED_EDGE_TYPES: frozenset[str] = frozenset(
    {"sector", "industry", "correlation", "supply_chain"}
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
        raise ValueError(f"{field_name} must be a 64-char hex SHA-256; got {value!r}")
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
        raise ValueError(f"{field_name} must be a non-empty ISO datetime string; got {value!r}")
    _parse_temporal(value)
    return value


# ---------------------------------------------------------------------------
# NodeFeatureSchema
# ---------------------------------------------------------------------------


class NodeFeatureSchema(BaseModel):
    """Schema declaration for a single node feature.

    A node feature is one column of the per-node feature vector (e.g.
    ``"market_cap"``, ``"momentum_20d"``). The schema declaration fixes its
    dtype and human-readable description so two consumers of the same
    manifest produce identical tensors.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        feature_name: the feature name (e.g. ``"market_cap"``). Must be a
            non-empty string.
        dtype: the numpy dtype of the feature data. One of ``"float32"``,
            ``"float64"``, ``"int32"``.
        description: a human-readable description. Defaults to ``""``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_name: str
    dtype: str
    description: str = ""

    @field_validator("feature_name")
    @classmethod
    def _feature_name_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("NodeFeatureSchema.feature_name must be a non-empty string")
        return v

    @field_validator("dtype")
    @classmethod
    def _dtype_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_DTYPES:
            raise ValueError(
                f"NodeFeatureSchema.dtype must be one of {sorted(_ALLOWED_DTYPES)!r}; got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# GraphEdge
# ---------------------------------------------------------------------------


class GraphEdge(BaseModel):
    """A single directed edge in a graph dataset.

    An edge connects a ``src_node`` to a ``dst_node`` with a typed
    relationship (``edge_type``) and a weight. The ``edge_id`` is
    deterministic (``src_dst_type_observed``) so two runs over the same
    data produce identical ids.

    Point-in-time safe invariants (fail-closed at construction):
    - ``src_node != dst_node`` (no self-loops).
    - ``edge_available_at >= edge_observed_at`` (an edge cannot be
      available before it was observed).

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        edge_id: deterministic id of the form
            ``"{src_node}_{dst_node}_{edge_type}_{edge_observed_at}"``.
        src_node: the source node id.
        dst_node: the destination node id (must differ from src_node).
        edge_type: the relationship type. One of ``"sector"``,
            ``"industry"``, ``"correlation"``, ``"supply_chain"``.
        edge_weight: the edge weight. Defaults to ``1.0``.
        edge_observed_at: ISO datetime — when the edge was observed.
        edge_available_at: ISO datetime — when the edge became available
            (must be >= edge_observed_at).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str
    src_node: str
    dst_node: str
    edge_type: str
    edge_weight: float = 1.0
    edge_observed_at: str
    edge_available_at: str

    @field_validator("edge_id", "src_node", "dst_node")
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return v

    @field_validator("edge_type")
    @classmethod
    def _edge_type_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_EDGE_TYPES:
            raise ValueError(
                f"GraphEdge.edge_type must be one of {sorted(_ALLOWED_EDGE_TYPES)!r}; got {v!r}"
            )
        return v

    @field_validator("edge_observed_at", "edge_available_at")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name)

    @model_validator(mode="after")
    def _no_self_loop(self) -> GraphEdge:
        """src_node must differ from dst_node (no self-loops)."""
        if self.src_node == self.dst_node:
            raise ValueError(
                f"GraphEdge.src_node must differ from dst_node "
                f"(no self-loops); got src={self.src_node!r}, "
                f"dst={self.dst_node!r}"
            )
        return self

    @model_validator(mode="after")
    def _available_after_observed(self) -> GraphEdge:
        """edge_available_at must be >= edge_observed_at."""
        obs = _parse_temporal(self.edge_observed_at)
        avail = _parse_temporal(self.edge_available_at)
        if not (avail >= obs):
            raise ValueError(
                f"edge_available_at must be >= edge_observed_at "
                f"(edge_observed_at={self.edge_observed_at!r}, "
                f"edge_available_at={self.edge_available_at!r})"
            )
        return self


# ---------------------------------------------------------------------------
# GraphNode
# ---------------------------------------------------------------------------


class GraphNode(BaseModel):
    """A single node in a graph dataset.

    A node represents an entity (e.g. symbol ``"AAPL"``, sector
    ``"Technology"``) with a feature vector. The ``features`` dict maps
    feature names (which must be declared in the manifest's
    :class:`NodeFeatureSchema`) to float values.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        node_id: the node id (e.g. symbol ``"AAPL"``). Must be a non-empty
            string.
        node_type: the node type (e.g. ``"symbol"``, ``"sector"``,
            ``"industry"``).
        features: a dict mapping feature name -> float value.
        observed_at: ISO datetime — when the node was observed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    node_type: str
    features: dict[str, float]
    observed_at: str

    @field_validator("node_id")
    @classmethod
    def _node_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("GraphNode.node_id must be a non-empty string")
        return v

    @field_validator("node_type")
    @classmethod
    def _node_type_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("GraphNode.node_type must be a non-empty string")
        return v

    @field_validator("observed_at")
    @classmethod
    def _temporal_parseable(cls, v: str) -> str:
        return _validate_iso_temporal(v, "observed_at")


# ---------------------------------------------------------------------------
# GraphDatasetManifest
# ---------------------------------------------------------------------------


class GraphDatasetManifest(BaseModel):
    """Manifest for a graph dataset (PIT-safe graph snapshot).

    This is the contract of record for a graph dataset export. It fixes
    the universe (node ids), the node feature schema, the edges, the
    snapshot time, the label horizon, and the data location + hash.

    Point-in-time safe invariants (fail-closed at construction):
    - No duplicate ``node_ids``.
    - Every edge's ``src_node`` / ``dst_node`` is in ``node_ids``.
    - Every node feature is declared in ``node_feature_schema``.
    - Every edge's ``edge_available_at`` <= ``graph_snapshot_time`` (no
      future edges in the snapshot).
    - ``label_horizon >= 1``.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        dataset_id: the dataset identifier.
        node_ids: list of all node ids in the graph (at least 1, no
            duplicates).
        edges: list of :class:`GraphEdge` (at least 1).
        nodes: list of :class:`GraphNode` (at least 1).
        node_feature_schema: list of :class:`NodeFeatureSchema` (at least
            1, no duplicate feature names).
        graph_snapshot_time: ISO datetime — the point-in-time of the
            snapshot.
        label_horizon: the forecast horizon in days (>= 1).
        data_uri: path/URI to the graph data file.
        data_hash: SHA-256 of the graph data (64-char hex).
        created_at: ISO timestamp of manifest creation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    node_ids: list[str]
    edges: list[GraphEdge]
    nodes: list[GraphNode]
    node_feature_schema: list[NodeFeatureSchema]
    graph_snapshot_time: str
    label_horizon: int
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

    @field_validator("node_ids")
    @classmethod
    def _node_ids_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("node_ids must contain at least 1 node id")
        for nid in v:
            if not isinstance(nid, str) or not nid.strip():
                raise ValueError("node_ids entries must be non-empty strings")
        return v

    @field_validator("edges")
    @classmethod
    def _edges_nonempty(cls, v: list[GraphEdge]) -> list[GraphEdge]:
        if not v:
            raise ValueError("edges must contain at least 1 edge")
        return v

    @field_validator("nodes")
    @classmethod
    def _nodes_nonempty(cls, v: list[GraphNode]) -> list[GraphNode]:
        if not v:
            raise ValueError("nodes must contain at least 1 node")
        return v

    @field_validator("node_feature_schema")
    @classmethod
    def _schema_nonempty(cls, v: list[NodeFeatureSchema]) -> list[NodeFeatureSchema]:
        if not v:
            raise ValueError("node_feature_schema must contain at least 1 feature")
        return v

    @field_validator("label_horizon")
    @classmethod
    def _label_horizon_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise ValueError(f"label_horizon must be an integer >= 1; got {v!r}")
        return v

    @field_validator("graph_snapshot_time", "created_at")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name)

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
    def _no_duplicate_node_ids(self) -> GraphDatasetManifest:
        """node_ids must be unique (no duplicate nodes)."""
        if len(set(self.node_ids)) != len(self.node_ids):
            dupes = sorted({nid for nid in self.node_ids if self.node_ids.count(nid) > 1})
            raise ValueError(f"node_ids must not contain duplicates: {dupes!r}")
        return self

    @model_validator(mode="after")
    def _no_duplicate_feature_names(self) -> GraphDatasetManifest:
        """Feature names in the schema must be unique."""
        names = [s.feature_name for s in self.node_feature_schema]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(
                f"node_feature_schema must not contain duplicate feature names: {dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def _edge_nodes_in_node_ids(self) -> GraphDatasetManifest:
        """Every edge's src/dst must be in node_ids."""
        validate_node_id_mapping(self.edges, self.node_ids)
        return self

    @model_validator(mode="after")
    def _node_features_in_schema(self) -> GraphDatasetManifest:
        """Every node feature must be declared in node_feature_schema."""
        allowed = {s.feature_name for s in self.node_feature_schema}
        for node in self.nodes:
            for fname in node.features:
                if fname not in allowed:
                    raise ValueError(
                        f"node {node.node_id!r} has feature {fname!r} "
                        f"which is not declared in node_feature_schema "
                        f"(allowed: {sorted(allowed)!r})"
                    )
        return self

    @model_validator(mode="after")
    def _no_future_edges(self) -> GraphDatasetManifest:
        """Every edge's edge_available_at must be <= graph_snapshot_time."""
        _parse_temporal(self.graph_snapshot_time)
        for edge in self.edges:
            validate_no_future_edge(edge, self.graph_snapshot_time)
        return self


# ---------------------------------------------------------------------------
# validate_no_future_edge
# ---------------------------------------------------------------------------


def validate_no_future_edge(edge: GraphEdge, snapshot_time: str) -> bool:
    """Check that an edge was available at or before the snapshot time.

    Returns True if ``edge.edge_available_at <= snapshot_time`` (the edge
    was available at the snapshot time, so no future leakage).

    Args:
        edge: the :class:`GraphEdge` to check.
        snapshot_time: the graph snapshot ISO datetime.

    Returns:
        True if the edge is not a future edge.

    Raises:
        ValueError: if ``edge_available_at > snapshot_time`` (future edge
            detected).
    """
    avail_epoch = _parse_temporal(edge.edge_available_at)
    snap_epoch = _parse_temporal(snapshot_time)
    if avail_epoch > snap_epoch:
        raise ValueError(
            f"future edge detected: edge_available_at "
            f"({edge.edge_available_at!r}) must be <= snapshot_time "
            f"({snapshot_time!r}) for edge {edge.edge_id!r}"
        )
    return True


# ---------------------------------------------------------------------------
# validate_node_id_mapping
# ---------------------------------------------------------------------------


def validate_node_id_mapping(edges: list[GraphEdge], node_ids: list[str]) -> bool:
    """Check that all edge src/dst nodes are declared in ``node_ids``.

    Args:
        edges: the list of :class:`GraphEdge` to check.
        node_ids: the list of declared node ids.

    Returns:
        True if all edge endpoints are in ``node_ids``.

    Raises:
        ValueError: if any edge's ``src_node`` or ``dst_node`` is not in
            ``node_ids`` (dangling reference — fail-closed).
    """
    node_set = set(node_ids)
    for edge in edges:
        if edge.src_node not in node_set:
            raise ValueError(f"edge {edge.edge_id!r} src_node {edge.src_node!r} is not in node_ids")
        if edge.dst_node not in node_set:
            raise ValueError(f"edge {edge.edge_id!r} dst_node {edge.dst_node!r} is not in node_ids")
    return True


# ---------------------------------------------------------------------------
# compute_graph_data_hash
# ---------------------------------------------------------------------------


def compute_graph_data_hash(edges: list[GraphEdge], nodes: list[GraphNode]) -> str:
    """Compute a deterministic SHA-256 hash of graph data.

    The hash is computed over canonical JSON of the edges and nodes,
    sorted by id (``edge_id`` for edges, ``node_id`` for nodes). Two
    graphs with the same edges and nodes (in any order) produce the same
    hash; any change to an edge, node, feature, or weight alters the hash.

    Args:
        edges: the list of :class:`GraphEdge`.
        nodes: the list of :class:`GraphNode`.

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    edges_sorted = sorted((e.model_dump() for e in edges), key=lambda d: d["edge_id"])
    nodes_sorted = sorted((n.model_dump() for n in nodes), key=lambda d: d["node_id"])
    # Sort node features by key for determinism.
    for n in nodes_sorted:
        n["features"] = {k: n["features"][k] for k in sorted(n["features"])}
    payload = {
        "edges": edges_sorted,
        "nodes": nodes_sorted,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# GraphManifestBuilder
# ---------------------------------------------------------------------------


class GraphManifestBuilder:
    """Fluent builder for :class:`GraphDatasetManifest`.

    Provides a chainable API for constructing a graph dataset manifest
    field-by-field, then calling :meth:`build` to validate and create the
    immutable manifest.

    Example::

        manifest = (
            GraphManifestBuilder("graph_001")
            .with_nodes([GraphNode(node_id="AAPL", ...)])
            .with_edges([GraphEdge(edge_id="AAPL_MSFT_sector_...", ...)])
            .with_feature_schema([NodeFeatureSchema(feature_name="cap", ...)])
            .with_snapshot_time("2024-06-01T00:00:00Z")
            .with_label_horizon(5)
            .with_data(
                uri="s3://bucket/graph_001.json",
                data_hash=compute_graph_data_hash(edges, nodes),
            )
            .build()
        )
    """

    def __init__(self, dataset_id: str) -> None:
        """Initialize the builder with a dataset id.

        Args:
            dataset_id: the dataset identifier.
        """
        self._dataset_id: str = dataset_id
        self._node_ids: list[str] = []
        self._edges: list[GraphEdge] = []
        self._nodes: list[GraphNode] = []
        self._node_feature_schema: list[NodeFeatureSchema] = []
        self._graph_snapshot_time: str = ""
        self._label_horizon: int = 0
        self._data_uri: str = ""
        self._data_hash: str = ""
        self._created_at: str = ""

    def with_nodes(self, nodes: list[GraphNode]) -> Self:
        """Set the nodes (and derive node_ids from them).

        Args:
            nodes: list of :class:`GraphNode` (at least 1).

        Returns:
            self (for chaining).
        """
        self._nodes = list(nodes)
        self._node_ids = [n.node_id for n in nodes]
        return self

    def with_edges(self, edges: list[GraphEdge]) -> Self:
        """Set the edges.

        Args:
            edges: list of :class:`GraphEdge` (at least 1).

        Returns:
            self (for chaining).
        """
        self._edges = list(edges)
        return self

    def with_feature_schema(self, schema: list[NodeFeatureSchema]) -> Self:
        """Set the node feature schema.

        Args:
            schema: list of :class:`NodeFeatureSchema` (at least 1).

        Returns:
            self (for chaining).
        """
        self._node_feature_schema = list(schema)
        return self

    def with_snapshot_time(self, time: str) -> Self:
        """Set the graph snapshot time.

        Args:
            time: ISO datetime — the point-in-time of the snapshot.

        Returns:
            self (for chaining).
        """
        self._graph_snapshot_time = time
        return self

    def with_label_horizon(self, horizon: int) -> Self:
        """Set the label forecast horizon.

        Args:
            horizon: the forecast horizon in days (>= 1).

        Returns:
            self (for chaining).
        """
        self._label_horizon = horizon
        return self

    def with_data(self, uri: str, data_hash: str) -> Self:
        """Set the data location and hash.

        Args:
            uri: path/URI to the graph data file.
            data_hash: SHA-256 of the graph data (64-char hex).

        Returns:
            self (for chaining).
        """
        self._data_uri = uri
        self._data_hash = data_hash
        return self

    def with_created_at(self, created_at: str) -> Self:
        """Set the creation timestamp.

        Args:
            created_at: ISO timestamp of manifest creation.

        Returns:
            self (for chaining).
        """
        self._created_at = created_at
        return self

    def build(self) -> GraphDatasetManifest:
        """Build and validate the :class:`GraphDatasetManifest`.

        Returns:
            The validated, frozen manifest.

        Raises:
            ValueError: if any required field is missing or validation
                fails (fail-closed).
        """
        if not self._created_at:
            # Default to now if not set.
            from datetime import datetime

            self._created_at = datetime.now(UTC).isoformat()

        return GraphDatasetManifest(
            dataset_id=self._dataset_id,
            node_ids=self._node_ids,
            edges=self._edges,
            nodes=self._nodes,
            node_feature_schema=self._node_feature_schema,
            graph_snapshot_time=self._graph_snapshot_time,
            label_horizon=self._label_horizon,
            data_uri=self._data_uri,
            data_hash=self._data_hash,
            created_at=self._created_at,
        )


# ---------------------------------------------------------------------------
# filter_edges_point_in_time
# ---------------------------------------------------------------------------


def filter_edges_point_in_time(edges: list[GraphEdge], snapshot_time: str) -> list[GraphEdge]:
    """Filter edges to only those available at or before the snapshot time.

    Returns only edges where ``edge_available_at <= snapshot_time``. This
    is the non-failing counterpart to :func:`validate_no_future_edge` —
    instead of raising on a future edge, it silently drops it. Used to
    build a PIT-safe graph snapshot from a raw edge stream.

    Args:
        edges: the list of :class:`GraphEdge` to filter.
        snapshot_time: the graph snapshot ISO datetime.

    Returns:
        A list of :class:`GraphEdge` whose ``edge_available_at`` <=
        ``snapshot_time``, preserving input order.
    """
    snap_epoch = _parse_temporal(snapshot_time)
    return [e for e in edges if _parse_temporal(e.edge_available_at) <= snap_epoch]
