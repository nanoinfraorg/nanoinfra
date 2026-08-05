"""Tests for the agent tools that read/update saved Infra Diagrams."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent.tools.diagrams import (
    GetDiagramTool,
    ListDiagramComponentsTool,
    ListDiagramsTool,
    UpdateDiagramTool,
    _node_footprint,
)
from nanoinfra.agent.tools.loader import ToolLoader
from nanoinfra.diagrams.store import DiagramStore


def _boxes_overlap(a, b) -> bool:
    aw, ah = _node_footprint(a)
    bw, bh = _node_footprint(b)
    ax, ay = a.position["x"], a.position["y"]
    bx, by = b.position["x"], b.position["y"]
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _assert_no_overlaps(nodes) -> None:
    by_parent: dict[str | None, list] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    for siblings in by_parent.values():
        for i, a in enumerate(siblings):
            for b in siblings[i + 1 :]:
                assert not _boxes_overlap(a, b), f"{a.id} overlaps {b.id}"


def _decode(value: object) -> object:
    return json.loads(str(value))


def _seed_diagram(store: DiagramStore) -> str:
    diagram = store.create({
        "name": "Web app",
        "targets": ["prod-web-01"],
        "nodes": [
            {
                "id": "web",
                "position": {"x": 0, "y": 0},
                "data": {"label": "Web", "componentTypeId": "web_server", "providerId": "nginx", "config": {}},
            },
            {
                "id": "group",
                "type": "groupBox",
                "position": {"x": 600, "y": 0},
                "style": {"width": 320.0, "height": 220.0},
                "data": {"label": "Cluster", "componentTypeId": "__group__", "providerId": "kubernetes", "config": {}},
            },
        ],
        "edges": [],
    })
    return diagram.id


def test_diagram_tools_are_discovered() -> None:
    names = {tool.__name__ for tool in ToolLoader().discover()}
    assert {
        "ListDiagramsTool",
        "GetDiagramTool",
        "ListDiagramComponentsTool",
        "UpdateDiagramTool",
    } <= names


@pytest.mark.asyncio
async def test_list_diagrams_returns_summaries(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)

    result = _decode(await ListDiagramsTool(store).execute())

    assert isinstance(result, list)
    assert result[0]["id"] == diagram_id
    assert result[0]["name"] == "Web app"
    assert result[0]["nodeCount"] == 2


@pytest.mark.asyncio
async def test_get_diagram_by_exact_id(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)

    result = _decode(await GetDiagramTool(store).execute(diagram_id_or_name=diagram_id))

    assert result["id"] == diagram_id
    node_ids = {n["id"] for n in result["nodes"]}
    assert node_ids == {"web", "group"}


@pytest.mark.asyncio
async def test_get_diagram_by_fuzzy_name(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    _seed_diagram(store)

    result = _decode(await GetDiagramTool(store).execute(diagram_id_or_name="web app"))

    assert result["name"] == "Web app"


@pytest.mark.asyncio
async def test_get_diagram_unknown_returns_error(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)

    result = await GetDiagramTool(store).execute(diagram_id_or_name="ghost")

    assert result.is_error
    assert "No saved diagram matches" in result


@pytest.mark.asyncio
async def test_list_diagram_components_includes_builtin_type(tmp_path: Path) -> None:
    result = _decode(await ListDiagramComponentsTool(tmp_path).execute())

    types_by_id = {t["id"]: t for t in result["componentTypes"]}
    assert "compute" in types_by_id
    provider_ids = {p["id"] for p in types_by_id["compute"]["providers"]}
    assert "nvidia-docker" in provider_ids


@pytest.mark.asyncio
async def test_update_diagram_dry_run_does_not_persist(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    new_nodes = [n.to_dict() for n in before.nodes] + [{
        "id": "cache-1",
        "position": {"x": 50, "y": 50},
        "data": {"label": "Cache", "componentTypeId": "cache", "providerId": "redis", "config": {}},
    }]

    tool = UpdateDiagramTool(store, tmp_path)
    result = await tool.execute(diagram_id=diagram_id, nodes=new_nodes, edges=[])

    assert not getattr(result, "is_error", False)
    assert "Preview (not saved)" in result
    assert "+ node 'cache-1'" in result
    assert "Not saved" in result

    after = store.get(diagram_id)
    assert {n.id for n in after.nodes} == {"web", "group"}


@pytest.mark.asyncio
async def test_update_diagram_persists_when_dry_run_false(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    new_nodes = [n.to_dict() for n in before.nodes] + [{
        "id": "cache-1",
        "position": {"x": 50, "y": 50},
        "data": {"label": "Cache", "componentTypeId": "cache", "providerId": "redis", "config": {}},
    }]

    tool = UpdateDiagramTool(store, tmp_path)
    result = await tool.execute(diagram_id=diagram_id, nodes=new_nodes, edges=[], dry_run=False)

    assert not getattr(result, "is_error", False)
    assert "Saved" in result

    after = store.get(diagram_id)
    assert {n.id for n in after.nodes} == {"web", "group", "cache-1"}


@pytest.mark.asyncio
async def test_update_diagram_rejects_unknown_component_without_persisting(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    new_nodes = [n.to_dict() for n in before.nodes] + [{
        "id": "fake-1",
        "position": {"x": 0, "y": 0},
        "data": {"label": "Fake", "componentTypeId": "gpu_cluster_v2", "providerId": "made-up", "config": {}},
    }]

    tool = UpdateDiagramTool(store, tmp_path)
    result = await tool.execute(diagram_id=diagram_id, nodes=new_nodes, edges=[], dry_run=False)

    assert result.is_error
    assert "gpu_cluster_v2" in result
    assert "list_diagram_components" in result

    after = store.get(diagram_id)
    assert {n.id for n in after.nodes} == {"web", "group"}


@pytest.mark.asyncio
async def test_update_diagram_unknown_diagram_id_returns_error(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    tool = UpdateDiagramTool(store, tmp_path)

    result = await tool.execute(diagram_id="does-not-exist", nodes=[], edges=[])

    assert result.is_error
    assert "does-not-exist" in result


@pytest.mark.asyncio
async def test_update_diagram_auto_arranges_new_nodes_ignoring_model_positions(tmp_path: Path) -> None:
    """The model is unreliable at 2D spatial packing -- regression guard for a real
    incident where several agent-added nodes landed overlapping because their
    guessed pixel positions were taken at face value. New nodes must be placed
    in a non-overlapping grid regardless of whatever position the model sent."""
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    group = next(n for n in before.nodes if n.id == "group")
    group_dict = group.to_dict()
    group_dict["style"] = {"width": 700.0, "height": 220.0}

    def _new_child(node_id: str) -> dict:
        return {
            "id": node_id,
            "parentId": "group",
            # Every new node claims the exact same colliding position on purpose.
            "position": {"x": 0.0, "y": 0.0},
            "data": {"label": node_id, "componentTypeId": "monitoring", "providerId": "prometheus", "config": {}},
        }

    nodes = (
        [n.to_dict() for n in before.nodes if n.id != "group"]
        + [group_dict]
        + [_new_child("new-a"), _new_child("new-b"), _new_child("new-c")]
    )

    tool = UpdateDiagramTool(store, tmp_path)
    result = await tool.execute(diagram_id=diagram_id, nodes=nodes, edges=[], dry_run=False)
    assert not getattr(result, "is_error", False)

    after = store.get(diagram_id)
    by_id = {n.id: n for n in after.nodes}

    positions = {node_id: (by_id[node_id].position["x"], by_id[node_id].position["y"]) for node_id in ("new-a", "new-b", "new-c")}
    assert len(set(positions.values())) == 3, f"new nodes must not collide: {positions}"
    assert positions == {
        "new-a": (40.0, 90.0),
        "new-b": (380.0, 90.0),
        "new-c": (40.0, 260.0),
    }

    # The group must grow to actually contain its new children.
    resized_group = by_id["group"]
    assert resized_group.style == {"width": 720.0, "height": 430.0}


@pytest.mark.asyncio
async def test_update_diagram_auto_layout_handles_wide_group_alongside_plain_nodes(tmp_path: Path) -> None:
    """A fixed-column grid sized for the default node footprint overlaps as soon
    as a same-level sibling is wider than that default -- e.g. a big new groupBox
    placed next to plain nodes. Regression guard for exactly that mix, checked
    generically (no overlaps) rather than against one hardcoded layout."""
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    nodes = [n.to_dict() for n in before.nodes] + [
        {
            "id": "wide-group",
            "type": "groupBox",
            "position": {"x": 0.0, "y": 0.0},
            "style": {"width": 900.0, "height": 300.0},
            "data": {"label": "Wide Group", "componentTypeId": "__group__", "providerId": "generic", "config": {}},
        },
        {
            "id": "plain-a",
            "position": {"x": 0.0, "y": 0.0},
            "data": {"label": "A", "componentTypeId": "monitoring", "providerId": "prometheus", "config": {}},
        },
        {
            "id": "plain-b",
            "position": {"x": 0.0, "y": 0.0},
            "data": {"label": "B", "componentTypeId": "monitoring", "providerId": "prometheus", "config": {}},
        },
    ]

    tool = UpdateDiagramTool(store, tmp_path)
    result = await tool.execute(diagram_id=diagram_id, nodes=nodes, edges=[], dry_run=False)
    assert not getattr(result, "is_error", False)

    after = store.get(diagram_id)
    _assert_no_overlaps(after.nodes)


@pytest.mark.asyncio
async def test_update_diagram_gives_new_untyped_group_a_default_size(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    nodes = [n.to_dict() for n in before.nodes] + [{
        "id": "new-group",
        "type": "groupBox",
        "position": {"x": 0.0, "y": 0.0},
        "data": {"label": "New Group", "componentTypeId": "__group__", "providerId": "generic", "config": {}},
    }]

    tool = UpdateDiagramTool(store, tmp_path)
    result = await tool.execute(diagram_id=diagram_id, nodes=nodes, edges=[], dry_run=False)
    assert not getattr(result, "is_error", False)

    after = store.get(diagram_id)
    new_group = next(n for n in after.nodes if n.id == "new-group")
    assert new_group.style == {"width": 320.0, "height": 220.0}


@pytest.mark.asyncio
async def test_update_diagram_preserves_group_style_when_not_touched(tmp_path: Path) -> None:
    """Regression guard: the WebUI once dropped a group's `style` on save, collapsing
    it to a tiny default size. Copying a node forward unchanged must not lose it."""
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    unchanged_nodes = [n.to_dict() for n in before.nodes]

    tool = UpdateDiagramTool(store, tmp_path)
    result = await tool.execute(diagram_id=diagram_id, nodes=unchanged_nodes, edges=[], dry_run=False)
    assert not getattr(result, "is_error", False)

    after = store.get(diagram_id)
    group = next(n for n in after.nodes if n.id == "group")
    assert group.style == {"width": 320.0, "height": 220.0}
