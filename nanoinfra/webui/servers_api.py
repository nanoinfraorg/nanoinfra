"""REST-facing payload builders for the WebUI Servers module.

Pattern: nanoinfra/webui/diagrams_api.py -- small pure functions the
gateway HTTP dispatcher (ws_http.py) calls into, gated by
check_api_token at the call site, not in here.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.servers.store import ServerStore


def webui_servers_payload(store: ServerStore) -> dict[str, Any]:
    """``GET /api/webui/servers`` -- the saved-server gallery + TargetPicker's data source."""
    return {"servers": [summary.to_dict() for summary in store.list_servers()]}


def webui_server_detail_payload(store: ServerStore, server_id: str) -> dict[str, Any] | None:
    """``GET /api/webui/servers/<id>`` -- ``None`` means 404."""
    server = store.get(server_id)
    if server is None:
        return None
    return {"server": server.to_dict()}


def create_webui_server(store: ServerStore, raw: dict[str, Any]) -> dict[str, Any]:
    """Raises ServerValidationError (-> 400 at the call site)."""
    server = store.create(raw)
    return {"server": server.to_dict()}


def update_webui_server(store: ServerStore, server_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    server = store.update(server_id, raw)
    if server is None:
        return None
    return {"server": server.to_dict()}


def delete_webui_server(store: ServerStore, server_id: str) -> bool:
    return store.delete(server_id)


__all__ = [
    "create_webui_server",
    "delete_webui_server",
    "update_webui_server",
    "webui_server_detail_payload",
    "webui_servers_payload",
]
