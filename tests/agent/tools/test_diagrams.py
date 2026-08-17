"""Tests for the agent tools that read/update saved Infra Diagrams."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent.tools.diagrams import (
    CreateDiagramTool,
    GetDiagramTool,
    ListDiagramComponentsTool,
    ListDiagramsTool,
    UpdateDiagramTool,
    _boxes_overlap,
    _resolve_overlaps,
)
from nanoinfra.agent.tools.loader import ToolLoader
from nanoinfra.diagrams.store import DiagramStore
from nanoinfra.diagrams.types import DiagramNode, DiagramNodeData


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
        "CreateDiagramTool",
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
    # An apply carries the preview that authorized it (#95).
    await tool.execute(diagram_id=diagram_id, nodes=new_nodes, edges=[], dry_run=True)
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
    # An apply carries the preview that authorized it (#95).
    await tool.execute(diagram_id=diagram_id, nodes=new_nodes, edges=[], dry_run=True)
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
    # An apply carries the preview that authorized it (#95).
    await tool.execute(diagram_id=diagram_id, nodes=nodes, edges=[], dry_run=True)
    result = await tool.execute(diagram_id=diagram_id, nodes=nodes, edges=[], dry_run=False)
    assert not getattr(result, "is_error", False)

    after = store.get(diagram_id)
    by_id = {n.id: n for n in after.nodes}

    positions = {node_id: (by_id[node_id].position["x"], by_id[node_id].position["y"]) for node_id in ("new-a", "new-b", "new-c")}
    assert len(set(positions.values())) == 3, f"new nodes must not collide: {positions}"
    # A pitch of 220 + 40, from the footprint the browser actually draws (#91). These read 380 and
    # 260 before, from a 300x130 guess that made hand-arranged layouts register as overlapping.
    assert positions == {
        "new-a": (40.0, 90.0),
        "new-b": (300.0, 90.0),
        "new-c": (40.0, 220.0),
    }

    # The group must grow to actually contain its new children. Smaller than it was, because the
    # children are the size the canvas draws them rather than a 300x130 guess (#91).
    resized_group = by_id["group"]
    assert resized_group.style == {"width": 700.0, "height": 350.0}


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
    # An apply carries the preview that authorized it (#95).
    await tool.execute(diagram_id=diagram_id, nodes=nodes, edges=[], dry_run=True)
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
    # An apply carries the preview that authorized it (#95).
    await tool.execute(diagram_id=diagram_id, nodes=nodes, edges=[], dry_run=True)
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
    # An apply carries the preview that authorized it (#95).
    await tool.execute(diagram_id=diagram_id, nodes=unchanged_nodes, edges=[], dry_run=True)
    result = await tool.execute(diagram_id=diagram_id, nodes=unchanged_nodes, edges=[], dry_run=False)
    assert not getattr(result, "is_error", False)

    after = store.get(diagram_id)
    group = next(n for n in after.nodes if n.id == "group")
    assert group.style == {"width": 320.0, "height": 220.0}


@pytest.mark.asyncio
async def test_create_diagram_dry_run_does_not_persist(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    nodes = [{
        "id": "web",
        "position": {"x": 0, "y": 0},
        "data": {"label": "Web", "componentTypeId": "web_server", "providerId": "nginx", "config": {}},
    }]

    tool = CreateDiagramTool(store, tmp_path)
    result = await tool.execute(name="New app", nodes=nodes, edges=[])

    assert not getattr(result, "is_error", False)
    assert "Preview (not created)" in result
    assert "+ node 'web'" in result
    assert "Not saved" in result
    assert store.list_diagrams() == []


@pytest.mark.asyncio
async def test_create_diagram_persists_when_dry_run_false(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    nodes = [{
        "id": "web",
        "position": {"x": 0, "y": 0},
        "data": {"label": "Web", "componentTypeId": "web_server", "providerId": "nginx", "config": {}},
    }]

    tool = CreateDiagramTool(store, tmp_path)
    result = await tool.execute(name="New app", targets=["prod-01"], nodes=nodes, edges=[], dry_run=False)

    assert not getattr(result, "is_error", False)
    assert "Created diagram" in result

    summaries = store.list_diagrams()
    assert len(summaries) == 1
    saved = store.get(summaries[0].id)
    assert saved.name == "New app"
    assert saved.targets == ["prod-01"]
    assert {n.id for n in saved.nodes} == {"web"}


@pytest.mark.asyncio
async def test_create_diagram_rejects_unknown_component_without_persisting(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)
    nodes = [{
        "id": "fake-1",
        "position": {"x": 0, "y": 0},
        "data": {"label": "Fake", "componentTypeId": "gpu_cluster_v2", "providerId": "made-up", "config": {}},
    }]

    tool = CreateDiagramTool(store, tmp_path)
    result = await tool.execute(name="Bad diagram", nodes=nodes, edges=[], dry_run=False)

    assert result.is_error
    assert "gpu_cluster_v2" in result
    assert "list_diagram_components" in result
    assert store.list_diagrams() == []


@pytest.mark.asyncio
async def test_create_diagram_auto_arranges_new_nodes(tmp_path: Path) -> None:
    store = DiagramStore(tmp_path)

    def _node(node_id: str) -> dict:
        return {
            "id": node_id,
            # Every node claims the exact same colliding position on purpose.
            "position": {"x": 0.0, "y": 0.0},
            "data": {"label": node_id, "componentTypeId": "monitoring", "providerId": "prometheus", "config": {}},
        }

    tool = CreateDiagramTool(store, tmp_path)
    result = await tool.execute(
        name="Layout test",
        nodes=[_node("a"), _node("b"), _node("c")],
        edges=[],
        dry_run=False,
    )
    assert not getattr(result, "is_error", False)

    summaries = store.list_diagrams()
    saved = store.get(summaries[0].id)
    positions = {node.id: (node.position["x"], node.position["y"]) for node in saved.nodes}
    assert len(set(positions.values())) == 3, f"nodes must not collide: {positions}"


@pytest.mark.asyncio
async def test_update_diagram_resolves_overlap_caused_by_repositioned_existing_node(tmp_path: Path) -> None:
    """Regression guard for a real incident: _auto_layout_new_nodes only ever
    protects node ids it's told are brand-new -- an *existing* node's position
    is trusted verbatim, on the assumption the model copied it forward
    unchanged from get_diagram. When it didn't (nudged an old sibling instead
    of leaving it alone, e.g. to "make room" for a new one), that collision
    had no safety net and needed a manual Auto Layout in the visual editor to
    fix. _resolve_overlaps is the net: it only kicks in when siblings actually
    overlap, and re-packs that whole sibling set deterministically."""
    store = DiagramStore(tmp_path)
    diagram_id = _seed_diagram(store)
    before = store.get(diagram_id)
    assert before is not None

    web = next(n.to_dict() for n in before.nodes if n.id == "web")
    group = next(n.to_dict() for n in before.nodes if n.id == "group")
    # The model moves the *existing* "web" node on top of "group" instead of
    # copying its position forward unchanged, while adding one new sibling.
    web["position"] = dict(group["position"])
    new_nodes = [web, group, {
        "id": "cache-1",
        "position": {"x": 0, "y": 0},
        "data": {"label": "Cache", "componentTypeId": "cache", "providerId": "redis", "config": {}},
    }]

    tool = UpdateDiagramTool(store, tmp_path)
    # An apply carries the preview that authorized it (#95).
    await tool.execute(diagram_id=diagram_id, nodes=new_nodes, edges=[], dry_run=True)
    result = await tool.execute(diagram_id=diagram_id, nodes=new_nodes, edges=[], dry_run=False)
    assert not getattr(result, "is_error", False)

    after = store.get(diagram_id)
    _assert_no_overlaps(after.nodes)


def _plain_node(node_id: str, x: float, y: float) -> DiagramNode:
    return DiagramNode(
        id=node_id,
        position={"x": x, "y": y},
        data=DiagramNodeData(label=node_id, component_type_id="cache", provider_id="redis"),
    )


def test_resolve_overlaps_leaves_a_non_overlapping_layout_untouched() -> None:
    nodes = [_plain_node("a", 40.0, 40.0), _plain_node("b", 500.0, 40.0)]
    original = [dict(n.position) for n in nodes]

    _resolve_overlaps(nodes, {node.id for node in nodes})

    assert [dict(n.position) for n in nodes] == original


def test_resolve_overlaps_repacks_only_the_colliding_sibling_set() -> None:
    overlapping = [_plain_node("a", 0.0, 0.0), _plain_node("b", 0.0, 0.0)]
    untouched = [_plain_node("c", 40.0, 40.0)]
    untouched[0].parent_id = "other-group"
    nodes = overlapping + untouched

    _resolve_overlaps(nodes, {node.id for node in nodes})

    assert not _boxes_overlap(overlapping[0], overlapping[1])
    assert untouched[0].position == {"x": 40.0, "y": 40.0}


def _valid(node_id: str, x: float, y: float) -> dict:
    """A node the catalog accepts, so these tests exercise layout and not validation."""
    return {
        "id": node_id,
        "position": {"x": x, "y": y},
        "data": {
            "label": node_id.upper(),
            "componentTypeId": "monitoring",
            "providerId": "prometheus",
            "config": {},
        },
    }


class TestAnApplyCarriesWhatItRead:
    """A preview then an operator save then an apply -- nanoinfraorg/nanoinfra#93.

    The agent previews adding a node, the operator adds a load balancer in the browser and saves, the
    user says yes, and the agent applies what it previewed. The operator's node and edge were gone,
    and the only trace was a diff printed after the write.
    """

    @pytest.mark.asyncio
    async def test_an_apply_after_a_concurrent_save_is_refused(self, tmp_path) -> None:
        from nanoinfra.agent.tools.diagrams import UpdateDiagramTool
        from nanoinfra.diagrams.store import DiagramStore

        store = DiagramStore(tmp_path)
        diagram = store.create({
            "name": "Topology",
            "nodes": [_valid("web", 0, 0)],
            "edges": [],
        })
        tool = UpdateDiagramTool(store, tmp_path)

        preview = await tool.execute(
            diagram_id=diagram.id,
            nodes=[
                _valid("web", 0, 0),
                _valid("cache", 0, 0),
            ],
            edges=[],
            dry_run=True,
        )
        assert "Preview" in str(preview)

        # The operator saves in the browser while the user is deciding.
        store.update(diagram.id, {
            "nodes": [
                _valid("web", 0, 0),
                _valid("lb", 400, 0),
            ],
            "edges": [{"id": "e2", "source": "lb", "target": "web"}],
        })

        applied = await tool.execute(
            diagram_id=diagram.id,
            nodes=[
                _valid("web", 0, 0),
                _valid("cache", 0, 0),
            ],
            edges=[],
            dry_run=False,
        )

        assert "changed" in str(applied).lower()
        loaded = store.get(diagram.id)
        assert loaded is not None
        assert [node.id for node in loaded.nodes] == ["web", "lb"], "the operator's save survives"

    @pytest.mark.asyncio
    async def test_an_apply_with_no_concurrent_write_succeeds(self, tmp_path) -> None:
        from nanoinfra.agent.tools.diagrams import UpdateDiagramTool
        from nanoinfra.diagrams.store import DiagramStore

        store = DiagramStore(tmp_path)
        diagram = store.create({
            "name": "Topology",
            "nodes": [_valid("web", 0, 0)],
            "edges": [],
        })
        tool = UpdateDiagramTool(store, tmp_path)
        nodes = [
            _valid("web", 0, 0),
            _valid("cache", 0, 0),
        ]

        await tool.execute(diagram_id=diagram.id, nodes=nodes, edges=[], dry_run=True)
        applied = await tool.execute(diagram_id=diagram.id, nodes=nodes, edges=[], dry_run=False)

        assert "Saved" in str(applied)
        loaded = store.get(diagram.id)
        assert loaded is not None
        assert sorted(node.id for node in loaded.nodes) == ["cache", "web"]


class TestTheOperatorsLayoutIsADecision:
    """An update re-packed every node -- nanoinfraorg/nanoinfra#91.

    Python assumed a node footprint of 300x130 and the browser draws 220x90 with a horizontal pitch
    of 270, so a layout the product's own Auto Layout produced read as overlapping. All seven seeded
    diagrams tripped the detector, and renaming one label moved 11 of 11 nodes. There is no undo:
    `diagrams/` is not versioned and the write is a full replace.
    """

    @pytest.mark.asyncio
    async def test_a_rename_moves_nothing(self, tmp_path) -> None:
        from nanoinfra.agent.tools.diagrams import UpdateDiagramTool
        from nanoinfra.diagrams.store import DiagramStore

        store = DiagramStore(tmp_path)
        placed = [
            _valid("client", 40, 20),
            _valid("dns", 20, 140),
            _valid("lb", 20, 260),
            _valid("web", 20, 380),
        ]
        diagram = store.create({"name": "Flow", "nodes": placed, "edges": []})
        tool = UpdateDiagramTool(store, tmp_path)

        await tool.execute(diagram_id=diagram.id, nodes=placed, edges=[], dry_run=True)
        await tool.execute(
            diagram_id=diagram.id,
            nodes=[
                {**node, "data": {**node["data"], "label": node["data"]["label"] + "!"}}
                for node in placed
            ],
            edges=[],
            dry_run=False,
        )

        loaded = store.get(diagram.id)
        assert loaded is not None
        after = {node.id: (node.position["x"], node.position["y"]) for node in loaded.nodes}
        before = {node["id"]: (node["position"]["x"], node["position"]["y"]) for node in placed}
        assert after == before, "a node the operator placed is a decision, not a suggestion"

    @pytest.mark.asyncio
    async def test_a_new_node_is_still_placed_clear_of_the_others(self, tmp_path) -> None:
        from nanoinfra.agent.tools.diagrams import UpdateDiagramTool
        from nanoinfra.diagrams.store import DiagramStore

        store = DiagramStore(tmp_path)
        placed = [_valid("web", 40, 40)]
        diagram = store.create({"name": "Flow", "nodes": placed, "edges": []})
        tool = UpdateDiagramTool(store, tmp_path)

        await tool.execute(
            diagram_id=diagram.id,
            nodes=[*placed, _valid("cache", 40, 40)],
            edges=[],
            dry_run=True,
        )
        await tool.execute(
            diagram_id=diagram.id,
            nodes=[*placed, _valid("cache", 40, 40)],
            edges=[],
            dry_run=False,
        )

        loaded = store.get(diagram.id)
        assert loaded is not None
        positions = {node.id: (node.position["x"], node.position["y"]) for node in loaded.nodes}
        assert positions["web"] == (40, 40), "the existing node did not move"
        assert positions["cache"] != (40, 40), "and the new one was placed somewhere else"


def test_the_python_footprint_matches_what_the_browser_draws() -> None:
    """One footprint, checked rather than commented -- #91.

    A comment naming the browser's values is not enough: the two already disagreed, by 80x40 pixels,
    which is what made a hand-arranged layout read as overlapping.
    """
    import re as _re
    from pathlib import Path as _Path

    from nanoinfra.agent.tools.diagrams import _DEFAULT_NODE_HEIGHT, _DEFAULT_NODE_WIDTH

    source = _Path(__file__).parents[3] / "webui" / "src" / "components" / "diagrams" / "autoLayout.ts"
    text = source.read_text(encoding="utf-8")
    width = int(_re.search(r"const NODE_WIDTH = (\d+)", text).group(1))
    height = int(_re.search(r"const NODE_HEIGHT = (\d+)", text).group(1))

    assert (_DEFAULT_NODE_WIDTH, _DEFAULT_NODE_HEIGHT) == (float(width), float(height)), (
        f"the browser draws {width}x{height} and Python assumes "
        f"{_DEFAULT_NODE_WIDTH}x{_DEFAULT_NODE_HEIGHT}"
    )
