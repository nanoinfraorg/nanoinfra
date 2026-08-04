"""REST-facing payload builders for the WebUI Diagrams module.

Pattern: ``nanoinfra/webui/skills_api.py``/``mcp_presets_api.py`` — small
pure functions the gateway HTTP dispatcher (``ws_http.py``) calls into,
gated by ``check_api_token`` at the call site, not in here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanoinfra.diagrams.catalog import load_catalog
from nanoinfra.diagrams.store import DiagramStore


def webui_diagrams_payload(store: DiagramStore) -> dict[str, Any]:
    """``GET /api/webui/diagrams`` — the saved-diagram gallery."""
    return {"diagrams": [summary.to_dict() for summary in store.list_diagrams()]}


def webui_diagram_detail_payload(store: DiagramStore, diagram_id: str) -> dict[str, Any] | None:
    """``GET /api/webui/diagrams/<id>`` — one full diagram, or ``None`` (-> 404)."""
    diagram = store.get(diagram_id)
    if diagram is None:
        return None
    return {"diagram": diagram.to_dict()}


def create_webui_diagram(store: DiagramStore, raw: dict[str, Any]) -> dict[str, Any]:
    """Create a diagram, assigning its id server-side.

    Raises :class:`nanoinfra.diagrams.normalize.DiagramValidationError` for a
    structurally-invalid payload — the caller maps that to a 400 response.
    """
    diagram = store.create(raw)
    return {"diagram": diagram.to_dict()}


def update_webui_diagram(store: DiagramStore, diagram_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    """Replace an existing diagram's content, or ``None`` if it doesn't exist (-> 404)."""
    diagram = store.update(diagram_id, raw)
    if diagram is None:
        return None
    return {"diagram": diagram.to_dict()}


def delete_webui_diagram(store: DiagramStore, diagram_id: str) -> bool:
    """Delete a diagram. ``False`` means it didn't exist (-> 404)."""
    return store.delete(diagram_id)


def webui_diagram_catalog_payload(
    workspace_path: Path,
    *,
    skills_workspace_path: Path | None = None,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any]:
    """``GET /api/webui/diagrams/catalog`` — the dynamic component palette."""
    component_types = load_catalog(
        workspace_path,
        skills_workspace_path=skills_workspace_path,
        disabled_skills=disabled_skills,
    )
    return {"componentTypes": [component_type.to_dict() for component_type in component_types]}


__all__ = [
    "create_webui_diagram",
    "delete_webui_diagram",
    "update_webui_diagram",
    "webui_diagram_catalog_payload",
    "webui_diagram_detail_payload",
    "webui_diagrams_payload",
]
