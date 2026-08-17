"""Validation/normalization for untrusted Diagram payloads.

Individual malformed nodes/edges, and dangling ``edge.source``/``edge.target``
or ``node.parentId`` references, are dropped with a logged warning rather
than failing the whole save — the same lenient, defensive style
``LocalTriggerStore`` uses for corrupt entries (nanoinfra/triggers/local_store.py).
Only structurally-wrong top-level payloads (not an object, ``nodes``/``edges``
not lists) raise, since those mean the client sent something a human editing
a JSON file couldn't have produced by accident through the normal UI.
"""

from __future__ import annotations

from typing import Any, cast

from loguru import logger

from nanoinfra.diagrams.types import Diagram, DiagramEdge, DiagramNode

_MAX_NAME_LENGTH = 120


class DiagramValidationError(ValueError):
    """Raised when a diagram payload has a structural problem the client must fix."""


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        return "Untitled diagram"
    return name[:_MAX_NAME_LENGTH]


def _clean_targets(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    items = cast(list[Any], raw)
    cleaned: list[str] = []
    for item in items:
        target = str(item or "").strip()
        if target:
            cleaned.append(target)
    return cleaned


def _normalize_nodes(raw_nodes: list[Any]) -> list[DiagramNode]:
    nodes: list[DiagramNode] = []
    for raw_entry in raw_nodes:
        if not isinstance(raw_entry, dict):
            logger.warning("Dropping malformed diagram node: {}", raw_entry)
            continue
        entry = cast(dict[str, Any], raw_entry)
        if not entry.get("id"):
            logger.warning("Dropping malformed diagram node: {}", entry)
            continue
        try:
            nodes.append(DiagramNode.from_dict(entry))
        except (KeyError, TypeError, ValueError):
            logger.warning("Dropping malformed diagram node: {}", entry)

    _refuse_duplicate_ids(nodes)
    node_ids = {node.id for node in nodes}
    for node in nodes:
        if node.parent_id is not None and node.parent_id not in node_ids:
            logger.warning("Dropping dangling parentId {!r} on node {!r}", node.parent_id, node.id)
            node.parent_id = None
    _refuse_parent_cycles(nodes)
    return nodes


def _refuse_duplicate_ids(nodes: list[DiagramNode]) -> None:
    """Two nodes cannot share an id (#101).

    ``_diff_summary`` keys nodes by id, so a duplicate collapsed into one line of the diff an operator
    approves: they approved "one node modified" and got a document with two nodes under one id. A diff
    that cannot show two nodes cannot describe the write, and this is the last place that can say no.
    """
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            raise DiagramValidationError(
                f"duplicate node id {node.id!r}: two nodes cannot share an id, because a preview "
                "keyed by id cannot show them both"
            )
        seen.add(node.id)


def _refuse_parent_cycles(nodes: list[DiagramNode]) -> None:
    """A node cannot be its own ancestor (#101).

    ``parentId == id`` and an ``A -> B -> A`` chain both passed, because the check was only that the
    parent exists. Neither is a document a canvas can draw.
    """
    parents = {node.id: node.parent_id for node in nodes}
    for start in parents:
        seen = {start}
        current = parents[start]
        while current is not None:
            if current in seen:
                raise DiagramValidationError(
                    f"node {start!r} is inside its own parent chain, which no canvas can draw"
                )
            seen.add(current)
            current = parents.get(current)


def _normalize_edges(raw_edges: list[Any], node_ids: set[str]) -> list[DiagramEdge]:
    edges: list[DiagramEdge] = []
    for raw_entry in raw_edges:
        if not isinstance(raw_entry, dict):
            logger.warning("Dropping malformed diagram edge: {}", raw_entry)
            continue
        entry = cast(dict[str, Any], raw_entry)
        if not entry.get("id") or not entry.get("source") or not entry.get("target"):
            logger.warning("Dropping malformed diagram edge: {}", entry)
            continue
        try:
            edge = DiagramEdge.from_dict(entry)
        except (KeyError, TypeError, ValueError):
            logger.warning("Dropping malformed diagram edge: {}", entry)
            continue
        if edge.source not in node_ids or edge.target not in node_ids:
            logger.warning("Dropping dangling edge {!r} (source/target not found)", edge.id)
            continue
        edges.append(edge)
    return edges


def normalize_diagram(raw: Any, *, diagram_id: str) -> Diagram:
    """Validate/coerce an untrusted value into a :class:`Diagram`."""
    if not isinstance(raw, dict):
        raise DiagramValidationError("diagram payload must be an object")
    payload = cast(dict[str, Any], raw)

    raw_nodes_value = payload.get("nodes", [])
    raw_edges_value = payload.get("edges", [])
    if not isinstance(raw_nodes_value, list):
        raise DiagramValidationError("nodes must be a list")
    if not isinstance(raw_edges_value, list):
        raise DiagramValidationError("edges must be a list")
    raw_nodes = cast(list[Any], raw_nodes_value)
    raw_edges = cast(list[Any], raw_edges_value)

    nodes = _normalize_nodes(raw_nodes)
    edges = _normalize_edges(raw_edges, {node.id for node in nodes})

    return Diagram(
        id=diagram_id,
        name=_clean_name(payload.get("name")),
        targets=_clean_targets(payload.get("targets")),
        nodes=nodes,
        edges=edges,
    )
