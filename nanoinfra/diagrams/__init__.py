"""Infra diagram persistence — a workspace-scoped store for the WebUI Diagram Designer."""

from __future__ import annotations

from nanoinfra.diagrams.catalog import (
    ComponentProvider,
    ComponentType,
    ProviderField,
    ProviderIntegration,
    load_catalog,
)
from nanoinfra.diagrams.changes import (
    DiagramChange,
    notify_diagram_change,
    subscribe_diagram_changes,
)
from nanoinfra.diagrams.lookup import resolve_diagram
from nanoinfra.diagrams.normalize import DiagramValidationError, normalize_diagram
from nanoinfra.diagrams.runtime_context import diagram_runtime_context
from nanoinfra.diagrams.store import DiagramStore
from nanoinfra.diagrams.types import (
    Diagram,
    DiagramEdge,
    DiagramNode,
    DiagramNodeData,
    DiagramSummary,
)

__all__ = [
    "ComponentProvider",
    "ComponentType",
    "Diagram",
    "DiagramChange",
    "DiagramEdge",
    "DiagramNode",
    "DiagramNodeData",
    "DiagramStore",
    "DiagramSummary",
    "DiagramValidationError",
    "ProviderField",
    "ProviderIntegration",
    "diagram_runtime_context",
    "load_catalog",
    "normalize_diagram",
    "notify_diagram_change",
    "resolve_diagram",
    "subscribe_diagram_changes",
]
