"""REST-facing payload builders for the WebUI Servers module.

Pattern: nanoinfra/webui/diagrams_api.py -- small pure functions the
gateway HTTP dispatcher (ws_http.py) calls into, gated by
check_api_token at the call site, not in here.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.servers.notes import (
    AUTHOR_OPERATOR,
    ServerNotesError,
    ServerNotesStore,
    parse_notes,
)
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


def webui_server_notes_payload(
    store: ServerStore,
    notes: ServerNotesStore,
    server_id: str,
) -> dict[str, Any] | None:
    """``GET /api/webui/servers/<id>/notes`` -- the box's memory, rendered and parsed (#229).

    Both halves, because the panel needs both: the parsed entries to show newest first with
    authorship, and the raw markdown because a human owns this file as much as the agent does and
    editing it means editing the text.
    """
    server = store.get(server_id)
    if server is None:
        return None
    archive = notes.archive_path(server_id)
    text = notes.read(server_id)
    return {
        "serverId": server.id,
        "name": server.name,
        "notesUpdatedAt": server.notes_updated_at,
        "text": text,
        "entries": [entry.to_dict() for entry in notes.entries(server_id)],
        "hasArchive": archive is not None and archive.is_file(),
    }


def webui_server_notes_archive_payload(
    store: ServerStore,
    notes: ServerNotesStore,
    server_id: str,
) -> dict[str, Any] | None:
    """``GET /api/webui/servers/<id>/notes/archive`` -- what rotation moved out (#227)."""
    server = store.get(server_id)
    if server is None:
        return None
    text = notes.read_archive(server_id)
    return {
        "serverId": server.id,
        "text": text,
        "entries": [entry.to_dict() for entry in parse_notes(text).entries],
    }


def append_webui_server_note(
    store: ServerStore,
    notes: ServerNotesStore,
    server_id: str,
    values: dict[str, Any],
    *,
    author: str,
) -> dict[str, Any] | None:
    """Append one operator entry. Raises ServerNotesError (-> 400 at the call site).

    Both halves of the attribution come from outside the payload (#228): the *kind* is fixed here,
    because this route is behind the API token and so the writer is a person, and the *name* is
    passed in by the call site from ``operator_actor``, which is the identity a verified assertion
    established. A request body that could name its own author could sign an agent's note
    ``(operator)`` and outrank the person it quotes.
    """
    if store.get(server_id) is None:
        return None
    entry = notes.append(
        server_id,
        author=author,
        kind=AUTHOR_OPERATOR,
        title=str(values.get("title") or ""),
        body=str(values.get("body") or ""),
    )
    payload = webui_server_notes_payload(store, notes, server_id)
    if payload is None:  # pragma: no cover -- the record was checked above
        raise ServerNotesError("server disappeared while its note was written")
    payload["appended"] = entry.to_dict()
    return payload


def save_webui_server_notes(
    store: ServerStore,
    notes: ServerNotesStore,
    server_id: str,
    text: str,
) -> dict[str, Any] | None:
    """Replace the whole file, for a human editing it. Raises ServerNotesError (-> 400)."""
    if store.get(server_id) is None:
        return None
    notes.replace(server_id, text)
    return webui_server_notes_payload(store, notes, server_id)


__all__ = [
    "append_webui_server_note",
    "create_webui_server",
    "delete_webui_server",
    "save_webui_server_notes",
    "update_webui_server",
    "webui_server_detail_payload",
    "webui_server_notes_archive_payload",
    "webui_server_notes_payload",
    "webui_servers_payload",
]
