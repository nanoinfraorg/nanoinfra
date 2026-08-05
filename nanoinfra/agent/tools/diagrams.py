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
            "list_diagram_components, without saving or previewing."
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

        saved = self.store.update(diagram_id, raw)
        if saved is None:
            return ToolResult.error(f"No saved diagram with id {diagram_id!r}.")
        return f"Saved.\n{diff}"


__all__ = [
    "GetDiagramTool",
    "ListDiagramComponentsTool",
    "ListDiagramsTool",
    "UpdateDiagramTool",
]
