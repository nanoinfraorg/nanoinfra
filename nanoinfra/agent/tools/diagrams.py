"""Agent tools for reading and updating saved Infra Diagrams.

Pattern mirrors ``nanoinfra/agent/tools/image_generation.py``: schema via
``@tool_parameters``, ``create()`` builds from ``ctx.workspace``. The REST
route builders in ``nanoinfra/webui/diagrams_api.py`` are the same-shape
read-only precedent this reuses (``DiagramStore``, ``load_catalog``,
``normalize_diagram``) instead of re-implementing anything.
"""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanoinfra.diagrams.catalog import load_catalog
from nanoinfra.diagrams.lookup import resolve_diagram
from nanoinfra.diagrams.normalize import DiagramValidationError, normalize_diagram
from nanoinfra.diagrams.store import DiagramStore
from nanoinfra.diagrams.types import Diagram

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext

_NODE_DATA_SCHEMA = ObjectSchema(
    label=StringSchema("Display label shown on the node."),
    componentTypeId=StringSchema(
        "Component type id -- must be one returned by list_diagram_components. Never invent one."
    ),
    providerId=StringSchema(
        "Provider id under that component type -- must also come from list_diagram_components."
    ),
    config=ObjectSchema(
        description="Free-form key/value config for the selected provider's fields (see its `fields` list).",
        additional_properties={"type": "string"},
    ),
    locked=BooleanSchema(description="Whether the node is locked against edits in the visual editor."),
    required=["label", "componentTypeId", "providerId"],
)

_NODE_SCHEMA = ObjectSchema(
    id=StringSchema("Stable node id. Reuse the existing id for nodes you are not adding."),
    position=ObjectSchema(
        x=NumberSchema(description="X coordinate on the canvas."),
        y=NumberSchema(description="Y coordinate on the canvas."),
        required=["x", "y"],
    ),
    data=_NODE_DATA_SCHEMA,
    type=StringSchema("Node kind.", enum=["component", "groupBox"], nullable=True),
    parentId=StringSchema("Id of the containing group node, if this node is nested inside one.", nullable=True),
    style=ObjectSchema(
        description=(
            "Only meaningful for a groupBox node (its box width/height). Copy the existing "
            "value forward verbatim for any node you are not resizing -- dropping it silently "
            "resets a group to a tiny default size and overlaps its children."
        ),
        width=NumberSchema(description="Width in canvas pixels."),
        height=NumberSchema(description="Height in canvas pixels."),
        nullable=True,
    ),
    required=["id", "position", "data"],
)

_EDGE_SCHEMA = ObjectSchema(
    id=StringSchema("Stable edge id. Reuse the existing id for edges you are not adding."),
    source=StringSchema("Source node id."),
    target=StringSchema("Target node id."),
    label=StringSchema("Optional relationship label, e.g. 'connects_to'.", nullable=True),
    sourceHandle=StringSchema("Optional source connection point, e.g. 'left'/'right'.", nullable=True),
    targetHandle=StringSchema("Optional target connection point.", nullable=True),
    required=["id", "source", "target"],
)


# Generous on purpose: unlike the WebUI's own auto-layout (autoLayout.ts),
# which can read a node's real rendered `measured` size, this runs before a
# node is ever drawn in a browser -- there is no ground truth to fall back
# on, only a guess. A 2-line wrapped legend renders up to ~290px wide (the
# node's text column caps at 220px, plus icon/gap/padding), so undershooting
# this is exactly what produced real overlapping "pills" in practice.
_DEFAULT_NODE_WIDTH = 300.0
_DEFAULT_NODE_HEIGHT = 130.0
_LAYOUT_MARGIN = 40.0
_DEFAULT_CONTAINER_WIDTH = 900.0
_DEFAULT_GROUP_STYLE = {"width": 320.0, "height": 220.0}
# Vertical space a group's own header (label + provider + legend) occupies
# above its first child -- reserving only _LAYOUT_MARGIN there let a child
# visually overlap the header text.
_GROUP_HEADER_CLEARANCE = 90.0


def _node_footprint(node: Any) -> tuple[float, float]:
    if node.type == "groupBox" and node.style:
        return node.style.get("width", _DEFAULT_NODE_WIDTH), node.style.get("height", _DEFAULT_NODE_HEIGHT)
    return _DEFAULT_NODE_WIDTH, _DEFAULT_NODE_HEIGHT


def _auto_layout_new_nodes(nodes: list[Any], new_ids: set[str]) -> None:
    """Give every newly-added node a non-overlapping position instead of trusting
    whatever pixel coordinates the model guessed -- LLMs are unreliable at 2D
    spatial packing, so this is corrected deterministically, not by better
    prompting. Mutates ``nodes`` in place; existing nodes are never touched.

    Uses shelf packing (place left-to-right, wrap to a new row when a node
    would cross the container's right edge) rather than a fixed-column grid,
    since a fixed grid sized for the *default* footprint overlaps as soon as
    one sibling is wider than that default -- e.g. a same-level groupBox.
    """
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        if node.id in new_ids and node.type == "groupBox" and node.style is None:
            node.style = dict(_DEFAULT_GROUP_STYLE)

    by_parent: dict[str | None, list[Any]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)

    for parent_id, siblings in by_parent.items():
        new_siblings = [node for node in siblings if node.id in new_ids]
        if not new_siblings:
            continue
        existing_siblings = [node for node in siblings if node.id not in new_ids]

        container_width = _DEFAULT_CONTAINER_WIDTH
        parent = by_id.get(parent_id) if parent_id else None
        if parent is not None and parent.style:
            container_width = parent.style.get("width", _DEFAULT_CONTAINER_WIDTH)

        start_y = _GROUP_HEADER_CLEARANCE if parent_id else _LAYOUT_MARGIN
        if existing_siblings:
            start_y = _LAYOUT_MARGIN + max(
                sibling.position.get("y", 0.0) + _node_footprint(sibling)[1]
                for sibling in existing_siblings
            )

        cursor_x = _LAYOUT_MARGIN
        row_y = start_y
        row_height = 0.0
        for node in new_siblings:
            width, height = _node_footprint(node)
            if cursor_x > _LAYOUT_MARGIN and cursor_x + width > container_width:
                row_y += row_height + _LAYOUT_MARGIN
                cursor_x = _LAYOUT_MARGIN
                row_height = 0.0
            node.position = {"x": cursor_x, "y": row_y}
            cursor_x += width + _LAYOUT_MARGIN
            row_height = max(row_height, height)


def _boxes_overlap(a: Any, b: Any) -> bool:
    aw, ah = _node_footprint(a)
    bw, bh = _node_footprint(b)
    ax, ay = a.position.get("x", 0.0), a.position.get("y", 0.0)
    bx, by = b.position.get("x", 0.0), b.position.get("y", 0.0)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _has_overlap(siblings: list[Any]) -> bool:
    return any(_boxes_overlap(a, b) for i, a in enumerate(siblings) for b in siblings[i + 1 :])


def _resolve_overlaps(nodes: list[Any]) -> None:
    """Last-resort safety net, run after ``_auto_layout_new_nodes``: that
    function only repositions node ids it was told are brand-new, trusting
    everything else at face value. If the model *also* re-guessed a position
    for a node that already existed -- e.g. nudging siblings around to "make
    room" instead of copying them forward unchanged, as it was told to --
    those coordinates have no protection and can collide (this happened for
    real: a diagram needed a manual Auto Layout in the visual editor to
    un-overlap after an agent update).

    Only touches sibling groups that actually overlap; an already-sane
    layout is left alone. When one does, every sibling in it (not just the
    colliding pair) is re-shelf-packed in their existing top-to-bottom,
    left-to-right order, so the result stays recognizable rather than
    scrambled.
    """
    by_id = {node.id: node for node in nodes}
    by_parent: dict[str | None, list[Any]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)

    for parent_id, siblings in by_parent.items():
        if len(siblings) < 2 or not _has_overlap(siblings):
            continue
        siblings.sort(key=lambda node: (node.position.get("y", 0.0), node.position.get("x", 0.0)))

        container_width = _DEFAULT_CONTAINER_WIDTH
        parent = by_id.get(parent_id) if parent_id else None
        if parent is not None and parent.style:
            container_width = parent.style.get("width", _DEFAULT_CONTAINER_WIDTH)

        cursor_x = _LAYOUT_MARGIN
        row_y = _GROUP_HEADER_CLEARANCE if parent_id else _LAYOUT_MARGIN
        row_height = 0.0
        for node in siblings:
            width, height = _node_footprint(node)
            if cursor_x > _LAYOUT_MARGIN and cursor_x + width > container_width:
                row_y += row_height + _LAYOUT_MARGIN
                cursor_x = _LAYOUT_MARGIN
                row_height = 0.0
            node.position = {"x": cursor_x, "y": row_y}
            cursor_x += width + _LAYOUT_MARGIN
            row_height = max(row_height, height)


def _fit_groups_to_children(nodes: list[Any]) -> None:
    """Grow (never shrink) a group's style so it visually contains every child,
    since ``_auto_layout_new_nodes`` may have placed new children further out
    than whatever size the model guessed for the group."""
    children_by_parent: dict[str, list[Any]] = {}
    for node in nodes:
        if node.parent_id:
            children_by_parent.setdefault(node.parent_id, []).append(node)

    for node in nodes:
        if node.type != "groupBox":
            continue
        children = children_by_parent.get(node.id)
        if not children:
            continue
        required_width = _LAYOUT_MARGIN + max(
            child.position.get("x", 0.0) + _node_footprint(child)[0] for child in children
        )
        required_height = _LAYOUT_MARGIN + max(
            child.position.get("y", 0.0) + _node_footprint(child)[1] for child in children
        )
        current = node.style or dict(_DEFAULT_GROUP_STYLE)
        node.style = {
            "width": max(current.get("width", 0.0), required_width),
            "height": max(current.get("height", 0.0), required_height),
        }


def _catalog_pairs(catalog_types: list[Any]) -> set[tuple[str, str]]:
    return {
        (component_type.id, provider.id)
        for component_type in catalog_types
        for provider in component_type.providers
    }


def _unknown_component_errors(diagram: Diagram, catalog_types: list[Any]) -> list[str]:
    valid_pairs = _catalog_pairs(catalog_types)
    errors: list[str] = []
    for node in diagram.nodes:
        pair = (node.data.component_type_id, node.data.provider_id)
        if pair not in valid_pairs:
            errors.append(
                f"node {node.id!r} uses componentTypeId={node.data.component_type_id!r} "
                f"providerId={node.data.provider_id!r}, which is not in the catalog. "
                "Call list_diagram_components to see valid ids, or add a workspace catalog "
                "override file under <workspace>/diagrams/catalog/ first (see the "
                "infra-diagrams skill for the file shape)."
            )
    return errors


def _node_label(node: Any) -> str:
    return f"{node.data.label} ({node.data.component_type_id}/{node.data.provider_id})"


def _diff_summary(before: Diagram, after: Diagram) -> str:
    before_nodes = {n.id: n for n in before.nodes}
    after_nodes = {n.id: n for n in after.nodes}
    before_edges = {e.id: e for e in before.edges}
    after_edges = {e.id: e for e in after.edges}

    lines: list[str] = []
    for node_id in after_nodes.keys() - before_nodes.keys():
        lines.append(f"+ node {node_id!r}: {_node_label(after_nodes[node_id])}")
    for node_id in before_nodes.keys() - after_nodes.keys():
        lines.append(f"- node {node_id!r}: {_node_label(before_nodes[node_id])}")
    for node_id in after_nodes.keys() & before_nodes.keys():
        if before_nodes[node_id].to_dict() != after_nodes[node_id].to_dict():
            lines.append(f"~ node {node_id!r} modified")

    for edge_id in after_edges.keys() - before_edges.keys():
        edge = after_edges[edge_id]
        lines.append(f"+ edge {edge_id!r}: {edge.source} -> {edge.target}")
    for edge_id in before_edges.keys() - after_edges.keys():
        edge = before_edges[edge_id]
        lines.append(f"- edge {edge_id!r}: {edge.source} -> {edge.target}")
    for edge_id in after_edges.keys() & before_edges.keys():
        if before_edges[edge_id].to_dict() != after_edges[edge_id].to_dict():
            lines.append(f"~ edge {edge_id!r} modified")

    if not lines:
        lines.append("(no changes)")
    lines.append(f"Total: {len(after.nodes)} nodes, {len(after.edges)} edges.")
    return "\n".join(lines)


class ListDiagramsTool(Tool):
    """List saved Infra Diagrams (id, name, targets, node count, last update)."""

    capability_class = "read"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(DiagramStore(Path(ctx.workspace)))

    def __init__(self, store: DiagramStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "list_diagrams"

    @property
    def description(self) -> str:
        return (
            "List saved Infra Diagrams in this workspace (id, name, targets, node count, "
            "last updated). Use this to find a diagram's id when the user refers to it by "
            "name without having attached it via /infradiagrams first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return tool_parameters_schema()

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        summaries = [summary.to_dict() for summary in self.store.list_diagrams()]
        return json.dumps(summaries, ensure_ascii=False)


@tool_parameters(
    tool_parameters_schema(
        diagram_id_or_name=StringSchema(
            "Exact diagram id, or its name (case-insensitive; a name with spaces also "
            "matches as a whole-word prefix of a longer query).",
            min_length=1,
        ),
        required=["diagram_id_or_name"],
    )
)
class GetDiagramTool(Tool):
    """Fetch one saved diagram's full content (nodes, edges, targets)."""

    capability_class = "read"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(DiagramStore(Path(ctx.workspace)))

    def __init__(self, store: DiagramStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "get_diagram"

    @property
    def description(self) -> str:
        return (
            "Fetch a saved Infra Diagram's full current content: id, name, targets, and every "
            "node (position, style, component/provider, config) and edge. Always call this "
            "before proposing any change with update_diagram, so unchanged nodes/edges can be "
            "copied forward exactly instead of being reconstructed from memory."
        )

    async def execute(self, diagram_id_or_name: str, **kwargs: Any) -> Any:
        diagram = resolve_diagram(self.store, diagram_id_or_name)
        if diagram is None:
            return ToolResult.error(f"No saved diagram matches {diagram_id_or_name!r}.")
        return json.dumps(diagram.to_dict(), ensure_ascii=False)


class ListDiagramComponentsTool(Tool):
    """Expose the dynamic component catalog (valid componentTypeId/providerId pairs)."""

    capability_class = "read"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        workspace = Path(ctx.workspace)
        return cls(workspace)

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "list_diagram_components"

    @property
    def description(self) -> str:
        return (
            "List every component type and provider available for Infra Diagrams (the same "
            "catalog the visual editor's palette uses), including each provider's config "
            "fields. Call this before using any componentTypeId/providerId in update_diagram "
            "-- never invent one. If the user wants something not listed here, say so and "
            "offer to add it as a workspace catalog file instead of fabricating an id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return tool_parameters_schema()

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        component_types = load_catalog(self.workspace, skills_workspace_path=self.workspace)
        payload = {"componentTypes": [component_type.to_dict() for component_type in component_types]}
        return json.dumps(payload, ensure_ascii=False)


@tool_parameters(
    tool_parameters_schema(
        diagram_id=StringSchema("Id of the diagram to update (from list_diagrams/get_diagram).", min_length=1),
        name=StringSchema("New diagram name. Omit to keep the current name.", nullable=True),
        targets=ArraySchema(StringSchema(""), description="Full replacement target list.", nullable=True),
        nodes=ArraySchema(
            _NODE_SCHEMA,
            description=(
                "Full replacement node list -- not a delta. Include every node that should "
                "still exist, copying unchanged ones forward verbatim from get_diagram."
            ),
            min_items=0,
        ),
        edges=ArraySchema(
            _EDGE_SCHEMA,
            description="Full replacement edge list -- not a delta, same rule as nodes.",
            min_items=0,
        ),
        dry_run=BooleanSchema(
            description=(
                "Defaults to true: validate and return a preview without saving anything. "
                "Only pass dry_run=false, with the exact same nodes/edges, after the user has "
                "explicitly confirmed the preview."
            ),
            default=True,
        ),
        required=["diagram_id", "nodes", "edges"],
    )
)
class UpdateDiagramTool(Tool):
    """Preview (default) or persist a full-replacement update to a saved diagram."""

    capability_class = "mutate.local"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        workspace = Path(ctx.workspace)
        return cls(DiagramStore(workspace), workspace)

    def __init__(self, store: DiagramStore, workspace: Path) -> None:
        self.store = store
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "update_diagram"

    @property
    def description(self) -> str:
        return (
            "Replace a saved diagram's nodes/edges (and optionally name/targets). Defaults to "
            "dry_run=true, which validates and returns a plain-text diff without saving -- "
            "show that diff to the user and wait for their explicit confirmation before calling "
            "this again with dry_run=false and the same nodes/edges. Never set dry_run=false on "
            "the first call. Rejects any node whose componentTypeId/providerId is not in "
            "list_diagram_components, without saving or previewing. Position for a brand-new "
            "node is a required field but its value is ignored and auto-arranged to avoid "
            "overlap -- do not spend effort guessing pixel coordinates, any placeholder works."
        )

    async def execute(
        self,
        diagram_id: str,
        nodes: list[Any],
        edges: list[Any],
        name: str | None = None,
        targets: list[str] | None = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        current = self.store.get(diagram_id)
        if current is None:
            return ToolResult.error(f"No saved diagram with id {diagram_id!r}.")

        raw: dict[str, Any] = {
            "name": name if name is not None else current.name,
            "targets": targets if targets is not None else current.targets,
            "nodes": nodes,
            "edges": edges,
        }
        try:
            candidate = normalize_diagram(raw, diagram_id=diagram_id)
        except DiagramValidationError as exc:
            return ToolResult.error(f"Invalid diagram payload: {exc}")

        new_ids = {node.id for node in candidate.nodes} - {node.id for node in current.nodes}
        _auto_layout_new_nodes(candidate.nodes, new_ids)
        _resolve_overlaps(candidate.nodes)
        _fit_groups_to_children(candidate.nodes)

        catalog_types = load_catalog(self.workspace, skills_workspace_path=self.workspace)
        errors = _unknown_component_errors(candidate, catalog_types)
        if errors:
            return ToolResult.error("Not saved -- unknown component(s):\n" + "\n".join(errors))

        diff = _diff_summary(current, candidate)

        if dry_run:
            return (
                "Preview (not saved):\n"
                f"{diff}\n\n"
                "Not saved. Call update_diagram again with the same nodes/edges and "
                "dry_run=false only after the user confirms."
            )

        # Persist `candidate`, not the original `raw` -- it carries the
        # auto-arranged positions/group sizes computed above, which the
        # model's own (possibly overlapping) guesses must not overwrite.
        saved = self.store.update(diagram_id, candidate.to_dict())
        if saved is None:
            return ToolResult.error(f"No saved diagram with id {diagram_id!r}.")
        return f"Saved.\n{diff}"


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema("Name for the new diagram.", min_length=1),
        targets=ArraySchema(StringSchema(""), description="Target server names.", nullable=True),
        nodes=ArraySchema(_NODE_SCHEMA, description="Every node the new diagram should start with.", min_items=0),
        edges=ArraySchema(_EDGE_SCHEMA, description="Every edge the new diagram should start with.", min_items=0),
        dry_run=BooleanSchema(
            description=(
                "Defaults to true: validate and return a preview without creating anything. "
                "Only pass dry_run=false, with the exact same nodes/edges, after the user has "
                "explicitly confirmed the preview."
            ),
            default=True,
        ),
        required=["name", "nodes", "edges"],
    )
)
class CreateDiagramTool(Tool):
    """Preview (default) or create a brand-new saved diagram."""

    capability_class = "mutate.local"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        workspace = Path(ctx.workspace)
        return cls(DiagramStore(workspace), workspace)

    def __init__(self, store: DiagramStore, workspace: Path) -> None:
        self.store = store
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "create_diagram"

    @property
    def description(self) -> str:
        return (
            "Create a brand-new saved Infra Diagram from scratch. Defaults to dry_run=true, "
            "which validates and returns a preview without creating anything -- show that "
            "preview to the user and wait for their explicit confirmation before calling this "
            "again with dry_run=false and the same nodes/edges. Never set dry_run=false on the "
            "first call. Rejects any node whose componentTypeId/providerId is not in "
            "list_diagram_components, without creating or previewing. Position for a node is a "
            "required field but its value is ignored and auto-arranged to avoid overlap -- do "
            "not spend effort guessing pixel coordinates, any placeholder works. To add to an "
            "already-saved diagram instead of starting a new one, use update_diagram."
        )

    async def execute(
        self,
        name: str,
        nodes: list[Any],
        edges: list[Any],
        targets: list[str] | None = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        raw: dict[str, Any] = {"name": name, "targets": targets or [], "nodes": nodes, "edges": edges}
        try:
            # "pending" is a throwaway id for validation only -- store.create()
            # below mints the real one and re-normalizes independently.
            candidate = normalize_diagram(raw, diagram_id="pending")
        except DiagramValidationError as exc:
            return ToolResult.error(f"Invalid diagram payload: {exc}")

        new_ids = {node.id for node in candidate.nodes}
        _auto_layout_new_nodes(candidate.nodes, new_ids)
        _resolve_overlaps(candidate.nodes)
        _fit_groups_to_children(candidate.nodes)

        catalog_types = load_catalog(self.workspace, skills_workspace_path=self.workspace)
        errors = _unknown_component_errors(candidate, catalog_types)
        if errors:
            return ToolResult.error("Not created -- unknown component(s):\n" + "\n".join(errors))

        lines = [f"+ node {node.id!r}: {_node_label(node)}" for node in candidate.nodes]
        lines += [f"+ edge {edge.id!r}: {edge.source} -> {edge.target}" for edge in candidate.edges]
        if not lines:
            lines.append("(empty diagram)")
        lines.append(f"Total: {len(candidate.nodes)} nodes, {len(candidate.edges)} edges.")
        diff = "\n".join(lines)

        if dry_run:
            return (
                f"Preview (not created):\nName: {candidate.name}\n{diff}\n\n"
                "Not saved. Call create_diagram again with the same nodes/edges and "
                "dry_run=false only after the user confirms."
            )

        # Persist `candidate`, not the original `raw` -- it carries the
        # auto-arranged positions/group sizes computed above, which the
        # model's own (possibly overlapping) guesses must not overwrite.
        saved = self.store.create(candidate.to_dict())
        return f"Created diagram {saved.id!r} ({saved.name!r}).\n{diff}"


__all__ = [
    "CreateDiagramTool",
    "GetDiagramTool",
    "ListDiagramComponentsTool",
    "ListDiagramsTool",
    "UpdateDiagramTool",
]
