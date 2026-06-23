from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_SCHEMA_VERSION: Final = 1


class CausalNodeKind(StrEnum):
    SYMBOL = "symbol"
    SECTOR = "sector"
    EVENT = "event"
    REGIME = "regime"
    OUTCOME = "outcome"


class CausalEdgeKind(StrEnum):
    LEADS = "leads"
    LAGS = "lags"
    CORRELATES = "correlates"
    CAUSES = "causes"
    INFLUENCES = "influences"


class CausalNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    kind: CausalNodeKind
    label: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class CausalEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str
    source_id: str
    target_id: str
    kind: CausalEdgeKind
    strength: float
    lag_ns: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("strength")
    @classmethod
    def _strength_in_unit_interval(cls, value: float) -> float:
        if value < 0.0 or value > 1.0:
            raise ValueError("strength must be between 0.0 and 1.0")
        return value


class CausalGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: list[CausalNode] = Field(default_factory=list)
    edges: list[CausalEdge] = Field(default_factory=list)
    built_at_ns: int
    schema_version: int = DEFAULT_SCHEMA_VERSION

    def node_ids(self) -> set[str]:
        return {node.node_id for node in self.nodes}

    def edge_ids(self) -> set[str]:
        return {edge.edge_id for edge in self.edges}

    def neighbors(self, node_id: str) -> set[str]:
        connected: set[str] = set()
        for edge in self.edges:
            if edge.source_id == node_id:
                connected.add(edge.target_id)
            if edge.target_id == node_id:
                connected.add(edge.source_id)
        return connected

    def edges_from(self, node_id: str) -> list[CausalEdge]:
        return [edge for edge in self.edges if edge.source_id == node_id]

    def edges_to(self, node_id: str) -> list[CausalEdge]:
        return [edge for edge in self.edges if edge.target_id == node_id]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CausalGraphBuilder:
    def __init__(self, built_at_ns: int | None = None) -> None:
        self._nodes: dict[str, CausalNode] = {}
        self._edges: list[CausalEdge] = []
        self._edge_keys: set[tuple[str, str, CausalEdgeKind]] = set()
        self._built_at_ns = built_at_ns

    def add_node(self, node: CausalNode) -> CausalGraphBuilder:
        if node.node_id not in self._nodes:
            self._nodes[node.node_id] = node
        return self

    def add_edge(self, edge: CausalEdge) -> CausalGraphBuilder:
        key = (edge.source_id, edge.target_id, edge.kind)
        if key in self._edge_keys:
            raise ValueError("duplicate edge for source_id, target_id, and kind")
        self._edge_keys.add(key)
        self._edges.append(edge)
        return self

    def build(self) -> CausalGraph:
        built_at_ns = self._built_at_ns if self._built_at_ns is not None else time.time_ns()
        return CausalGraph(
            nodes=list(self._nodes.values()),
            edges=list(self._edges),
            built_at_ns=built_at_ns,
        )


def extract_features(graph: CausalGraph, node_id: str) -> dict[str, float]:
    if not graph.nodes or node_id not in graph.node_ids():
        return {}

    incident_edges = [
        edge for edge in graph.edges if edge.source_id == node_id or edge.target_id == node_id
    ]
    degree = len(graph.neighbors(node_id))
    possible_neighbors = max(len(graph.nodes) - 1, 1)
    weighted_degree = sum(edge.strength for edge in incident_edges)
    average_strength = weighted_degree / len(incident_edges) if incident_edges else 0.0
    lags = [float(edge.lag_ns) for edge in incident_edges if edge.lag_ns is not None]

    return {
        "degree_centrality": degree / possible_neighbors,
        "weighted_degree": weighted_degree,
        "average_neighbor_strength": average_strength,
        "lag_count": float(len(lags)),
        "average_lag_ns": sum(lags) / len(lags) if lags else 0.0,
        "min_lag_ns": min(lags) if lags else 0.0,
        "max_lag_ns": max(lags) if lags else 0.0,
    }


def explain_analogs(graph: CausalGraph, node_id: str, limit: int = 3) -> list[str]:
    incident_edges = [
        edge for edge in graph.edges if edge.source_id == node_id or edge.target_id == node_id
    ]
    sorted_edges = sorted(incident_edges, key=lambda edge: edge.strength, reverse=True)

    explanations: list[str] = []
    for edge in sorted_edges[:limit]:
        if edge.source_id == node_id:
            subject = node_id
            target = edge.target_id
        else:
            subject = edge.source_id
            target = node_id
        lag_text = f" after {edge.lag_ns} ns" if edge.lag_ns is not None else ""
        explanations.append(
            f"{subject} {edge.kind.value} {target} with strength {edge.strength:.3f}{lag_text}"
        )
    return explanations
