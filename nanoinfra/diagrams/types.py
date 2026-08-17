"""Persistent types for infra diagrams.

Field names mirror ``webui/src/components/diagrams/diagramTypes.ts`` — the
``to_dict``/``from_dict`` pairs are the untrusted-JSON boundary, so the JSON
shape stays camelCase (matching the WebUI wire format) while Python code
gets ordinary snake_case attributes, the same split ``LocalTrigger``
(``nanoinfra/triggers/local_types.py``) uses for its own on-disk records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from nanoinfra.utils.dict_keys import get_camel_snake as _get


def _as_dict(raw: Any) -> dict[str, Any]:
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def _as_str_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    items = cast(dict[Any, Any], raw)
    return {str(k): str(v) for k, v in items.items()}


def _as_optional_str(raw: Any) -> str | None:
    text = str(raw).strip() if raw else ""
    return text or None


@dataclass
class DiagramNodeData:
    """The component a node represents: its catalog type, provider, and config."""

    label: str
    component_type_id: str
    provider_id: str
    config: dict[str, str] = field(default_factory=dict)
    locked: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiagramNodeData":
        return cls(
            label=str(data.get("label") or ""),
            component_type_id=str(_get(data, "componentTypeId", "component_type_id", "")),
            provider_id=str(_get(data, "providerId", "provider_id", "")),
            config=_as_str_dict(data.get("config")),
            locked=bool(data.get("locked", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "componentTypeId": self.component_type_id,
            "providerId": self.provider_id,
            "config": self.config,
            "locked": self.locked,
        }


def _coerce_xy(raw: Any) -> dict[str, float]:
    box = _as_dict(raw)
    try:
        return {"x": float(box.get("x", 0)), "y": float(box.get("y", 0))}
    except (TypeError, ValueError):
        return {"x": 0.0, "y": 0.0}


def _coerce_size(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    box = _as_dict(raw)
    try:
        return {"width": float(box.get("width", 0)), "height": float(box.get("height", 0))}
    except (TypeError, ValueError):
        return None


@dataclass
class DiagramNode:
    """One component or group box placed on the canvas."""

    id: str
    position: dict[str, float]
    data: DiagramNodeData
    type: str | None = None  # "component" | "groupBox"
    parent_id: str | None = None
    style: dict[str, float] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiagramNode":
        raw_type = data.get("type")
        return cls(
            id=str(data["id"]),
            position=_coerce_xy(data.get("position")),
            data=DiagramNodeData.from_dict(_as_dict(data.get("data"))),
            type=raw_type if isinstance(raw_type, str) else None,
            parent_id=_as_optional_str(_get(data, "parentId", "parent_id")),
            style=_coerce_size(data.get("style")),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "position": self.position,
            "data": self.data.to_dict(),
        }
        if self.type is not None:
            result["type"] = self.type
        if self.parent_id is not None:
            result["parentId"] = self.parent_id
        if self.style is not None:
            result["style"] = self.style
        return result


@dataclass
class DiagramEdge:
    """One connection between two nodes."""

    id: str
    source: str
    target: str
    label: str = ""
    source_handle: str | None = None
    target_handle: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiagramEdge":
        return cls(
            id=str(data["id"]),
            source=str(data["source"]),
            target=str(data["target"]),
            label=str(data.get("label") or ""),
            source_handle=_as_optional_str(_get(data, "sourceHandle", "source_handle")),
            target_handle=_as_optional_str(_get(data, "targetHandle", "target_handle")),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "label": self.label,
        }
        if self.source_handle is not None:
            result["sourceHandle"] = self.source_handle
        if self.target_handle is not None:
            result["targetHandle"] = self.target_handle
        return result


@dataclass
class Diagram:
    """One saved infra diagram — the full, editable document."""

    id: str
    name: str
    targets: list[str] = field(default_factory=list)
    nodes: list[DiagramNode] = field(default_factory=list)
    edges: list[DiagramEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "targets": self.targets,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }


@dataclass
class DiagramSummary:
    """The lightweight listing shape shown in the Diagrams gallery."""

    id: str
    name: str
    targets: list[str]
    node_count: int
    updated_at: str
    #: ``ok``, ``modified_outside`` or ``unreadable`` (#96).
    #:
    #: Four writers reach ``diagrams/*.json`` and three of them are not the store: an ungated
    #: ``write_file``, ``exec``, and the seed. That cannot be prevented from inside the store, so the
    #: gallery reports it instead of rendering a foreign write as the operator's own -- and a file the
    #: store cannot parse appears as an error rather than vanishing, which is the worst answer
    #: available.
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "targets": self.targets,
            "nodeCount": self.node_count,
            "updatedAt": self.updated_at,
            "status": self.status,
        }
