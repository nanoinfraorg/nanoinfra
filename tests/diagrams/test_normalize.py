from __future__ import annotations

import pytest

from nanoinfra.diagrams.normalize import DiagramValidationError, normalize_diagram


def test_normalize_minimal_diagram():
    diagram = normalize_diagram({"name": "My diagram"}, diagram_id="d1")
    assert diagram.id == "d1"
    assert diagram.name == "My diagram"
    assert diagram.targets == []
    assert diagram.nodes == []
    assert diagram.edges == []


def test_normalize_defaults_blank_name():
    diagram = normalize_diagram({"name": "   "}, diagram_id="d1")
    assert diagram.name == "Untitled diagram"

    diagram_no_name = normalize_diagram({}, diagram_id="d1")
    assert diagram_no_name.name == "Untitled diagram"


def test_normalize_truncates_long_name():
    diagram = normalize_diagram({"name": "x" * 500}, diagram_id="d1")
    assert len(diagram.name) == 120


def test_normalize_trims_and_drops_empty_targets():
    diagram = normalize_diagram({"targets": [" prod-web-01 ", "", None, "  "]}, diagram_id="d1")
    assert diagram.targets == ["prod-web-01"]


def test_normalize_non_list_targets_becomes_empty():
    diagram = normalize_diagram({"targets": "prod-web-01"}, diagram_id="d1")
    assert diagram.targets == []


def test_normalize_round_trips_nodes_and_edges():
    raw = {
        "name": "Web app",
        "targets": ["prod-web-01"],
        "nodes": [
            {"id": "web", "position": {"x": 20, "y": 380}, "data": {"label": "Web", "componentTypeId": "web_server", "providerId": "nginx", "config": {"image": "nginx:1.27"}}},
            {"id": "db", "position": {"x": -140, "y": 520}, "data": {"label": "DB", "componentTypeId": "database", "providerId": "postgres", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "web", "target": "db", "label": "Read/Write"},
        ],
    }
    diagram = normalize_diagram(raw, diagram_id="d1")
    assert [n.id for n in diagram.nodes] == ["web", "db"]
    assert diagram.nodes[0].data.component_type_id == "web_server"
    assert diagram.nodes[0].data.config == {"image": "nginx:1.27"}
    assert [e.id for e in diagram.edges] == ["e1"]
    assert diagram.edges[0].source == "web"
    assert diagram.edges[0].target == "db"

    # to_dict must be the reverse mapping (camelCase wire shape).
    as_dict = diagram.to_dict()
    assert as_dict["nodes"][0]["data"]["componentTypeId"] == "web_server"
    assert as_dict["edges"][0]["source"] == "web"


def test_normalize_drops_node_missing_id():
    raw = {"nodes": [{"position": {"x": 0, "y": 0}, "data": {}}]}
    diagram = normalize_diagram(raw, diagram_id="d1")
    assert diagram.nodes == []


def test_normalize_drops_non_dict_node():
    raw = {"nodes": ["not-a-node", 123, None]}
    diagram = normalize_diagram(raw, diagram_id="d1")
    assert diagram.nodes == []


def test_normalize_drops_edge_missing_source_or_target():
    raw = {
        "nodes": [{"id": "a", "position": {}, "data": {}}],
        "edges": [
            {"id": "e1", "source": "a"},  # no target
            {"id": "e2", "target": "a"},  # no source
            {"id": "e3"},  # neither
        ],
    }
    diagram = normalize_diagram(raw, diagram_id="d1")
    assert diagram.edges == []


def test_normalize_drops_dangling_edge_reference():
    raw = {
        "nodes": [{"id": "a", "position": {}, "data": {}}],
        "edges": [{"id": "e1", "source": "a", "target": "ghost"}],
    }
    diagram = normalize_diagram(raw, diagram_id="d1")
    assert diagram.edges == []


def test_normalize_keeps_valid_edge_between_real_nodes():
    raw = {
        "nodes": [
            {"id": "a", "position": {}, "data": {}},
            {"id": "b", "position": {}, "data": {}},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "b"}],
    }
    diagram = normalize_diagram(raw, diagram_id="d1")
    assert [e.id for e in diagram.edges] == ["e1"]


def test_normalize_clears_dangling_parent_id():
    raw = {
        "nodes": [
            {"id": "child", "position": {}, "data": {}, "parentId": "ghost-parent"},
        ],
    }
    diagram = normalize_diagram(raw, diagram_id="d1")
    assert diagram.nodes[0].parent_id is None


def test_normalize_keeps_valid_parent_id():
    raw = {
        "nodes": [
            {"id": "group", "type": "groupBox", "position": {}, "data": {}},
            {"id": "child", "position": {}, "data": {}, "parentId": "group"},
        ],
    }
    diagram = normalize_diagram(raw, diagram_id="d1")
    child = next(n for n in diagram.nodes if n.id == "child")
    assert child.parent_id == "group"


def test_normalize_rejects_non_object_payload():
    with pytest.raises(DiagramValidationError):
        normalize_diagram("not-a-diagram", diagram_id="d1")


def test_normalize_rejects_non_list_nodes():
    with pytest.raises(DiagramValidationError):
        normalize_diagram({"nodes": "not-a-list"}, diagram_id="d1")


def test_normalize_rejects_non_list_edges():
    with pytest.raises(DiagramValidationError):
        normalize_diagram({"edges": "not-a-list"}, diagram_id="d1")


def test_normalize_preserves_handles():
    raw = {
        "nodes": [
            {"id": "a", "position": {}, "data": {}},
            {"id": "b", "position": {}, "data": {}},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "b", "sourceHandle": "right", "targetHandle": "left"}],
    }
    diagram = normalize_diagram(raw, diagram_id="d1")
    edge = diagram.edges[0]
    assert edge.source_handle == "right"
    assert edge.target_handle == "left"
    assert edge.to_dict()["sourceHandle"] == "right"
