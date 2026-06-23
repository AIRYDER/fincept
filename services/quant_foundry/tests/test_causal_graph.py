from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError
from quant_foundry.causal_graph import (
    CausalEdge,
    CausalEdgeKind,
    CausalGraph,
    CausalGraphBuilder,
    CausalNode,
    CausalNodeKind,
    explain_analogs,
    extract_features,
)


def _node(node_id: str, kind: CausalNodeKind = CausalNodeKind.SYMBOL) -> CausalNode:
    return CausalNode(node_id=node_id, kind=kind, label=node_id.upper())


def _edge(
    source_id: str,
    target_id: str,
    kind: CausalEdgeKind = CausalEdgeKind.LEADS,
    strength: float = 0.5,
    lag_ns: int | None = None,
) -> CausalEdge:
    return CausalEdge(
        edge_id=f"{source_id}-{kind.value}-{target_id}",
        source_id=source_id,
        target_id=target_id,
        kind=kind,
        strength=strength,
        lag_ns=lag_ns,
    )


class TestCausalGraphModels:
    def test_module_imports_cleanly(self) -> None:
        assert CausalNodeKind.SYMBOL.value == "symbol"
        assert CausalEdgeKind.CAUSES.value == "causes"

    def test_node_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            CausalNode.model_validate(
                {
                    "node_id": "AAPL",
                    "kind": CausalNodeKind.SYMBOL,
                    "label": "Apple",
                    "unexpected": "field",
                }
            )

    def test_edge_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            CausalEdge.model_validate(
                {
                    "edge_id": "e1",
                    "source_id": "AAPL",
                    "target_id": "recession",
                    "kind": CausalEdgeKind.CORRELATES,
                    "strength": 0.4,
                    "extra": "blocked",
                }
            )

    def test_edge_strength_must_be_between_zero_and_one(self) -> None:
        for bad_strength in (-0.01, 1.01):
            with pytest.raises(ValueError):
                _edge("AAPL", "recession", strength=bad_strength)


class TestCausalGraphBuilder:
    def test_builder_deduplicates_nodes_by_node_id(self) -> None:
        builder = CausalGraphBuilder(built_at_ns=123)
        builder.add_node(_node("AAPL", CausalNodeKind.SYMBOL))
        builder.add_node(CausalNode(node_id="AAPL", kind=CausalNodeKind.SYMBOL, label="Apple Inc."))

        graph = builder.build()

        assert graph.node_ids() == {"AAPL"}
        assert graph.nodes[0].label == "AAPL"

    def test_builder_rejects_duplicate_edges_by_source_target_kind(self) -> None:
        builder = CausalGraphBuilder(built_at_ns=123)
        builder.add_node(_node("AAPL"))
        builder.add_node(_node("recession", CausalNodeKind.EVENT))
        builder.add_edge(_edge("AAPL", "recession", CausalEdgeKind.CORRELATES))

        with pytest.raises(ValueError, match="duplicate edge"):
            builder.add_edge(
                CausalEdge(
                    edge_id="second-id",
                    source_id="AAPL",
                    target_id="recession",
                    kind=CausalEdgeKind.CORRELATES,
                    strength=0.8,
                )
            )


class TestCausalGraphQueries:
    def test_neighbors_returns_connected_node_ids(self) -> None:
        graph = CausalGraph(
            built_at_ns=123,
            nodes=[
                _node("AAPL"),
                _node("tech", CausalNodeKind.SECTOR),
                _node("recession", CausalNodeKind.EVENT),
            ],
            edges=[
                _edge("AAPL", "tech", CausalEdgeKind.INFLUENCES),
                _edge("recession", "AAPL", CausalEdgeKind.CAUSES),
            ],
        )

        assert graph.neighbors("AAPL") == {"tech", "recession"}
        assert [edge.target_id for edge in graph.edges_from("AAPL")] == ["tech"]
        assert [edge.source_id for edge in graph.edges_to("AAPL")] == ["recession"]

    def test_to_dict_round_trips_through_json(self) -> None:
        graph = CausalGraph(
            built_at_ns=123,
            nodes=[_node("AAPL")],
            edges=[_edge("AAPL", "AAPL", CausalEdgeKind.CORRELATES, strength=0.1)],
        )

        round_tripped = CausalGraph.model_validate(json.loads(json.dumps(graph.to_dict())))

        assert round_tripped == graph


class TestResearchFeatures:
    def test_extract_features_returns_degree_weight_and_average_strength(self) -> None:
        graph = CausalGraph(
            built_at_ns=123,
            nodes=[_node("AAPL"), _node("tech", CausalNodeKind.SECTOR), _node("recession", CausalNodeKind.EVENT)],
            edges=[
                _edge("AAPL", "tech", CausalEdgeKind.INFLUENCES, strength=0.8, lag_ns=10),
                _edge("recession", "AAPL", CausalEdgeKind.CAUSES, strength=0.4, lag_ns=30),
            ],
        )

        features = extract_features(graph, "AAPL")

        assert features["degree_centrality"] == 1.0
        assert features["weighted_degree"] == pytest.approx(1.2)
        assert features["average_neighbor_strength"] == pytest.approx(0.6)
        assert features["average_lag_ns"] == 20.0

    def test_empty_graph_extract_features_returns_empty_dict(self) -> None:
        graph = CausalGraph(built_at_ns=123, nodes=[], edges=[])

        assert extract_features(graph, "AAPL") == {}

    def test_analog_explanations_describe_strongest_relationships(self) -> None:
        graph = CausalGraph(
            built_at_ns=123,
            nodes=[_node("AAPL"), _node("recession", CausalNodeKind.REGIME)],
            edges=[_edge("recession", "AAPL", CausalEdgeKind.CAUSES, strength=0.9, lag_ns=50)],
        )

        explanations = explain_analogs(graph, "AAPL", limit=1)

        assert explanations == ["recession causes AAPL with strength 0.900 after 50 ns"]


class TestTradingBoundaryInvariant:
    def test_no_sig_predict_bus_producer_or_order_fields(self) -> None:
        forbidden_names = {
            "sig_predict",
            "sig_predict_writer",
            "bus_producer",
            "producer",
            "order",
            "orders",
            "order_id",
            "submit_order",
            "write_order",
        }
        classes: tuple[type[Any], ...] = (CausalNode, CausalEdge, CausalGraph, CausalGraphBuilder)

        for cls in classes:
            exposed = set(dir(cls)) | set(getattr(cls, "model_fields", {}))
            assert forbidden_names.isdisjoint(exposed)
