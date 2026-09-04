"""The WebUI half of device memory (#229): read, append as the operator, edit the whole file."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from nanoinfra.servers.notes import AUTHOR_AGENT, ServerNotesError, ServerNotesStore
from nanoinfra.servers.store import ServerStore
from nanoinfra.webui.servers_api import (
    append_webui_server_note,
    save_webui_server_notes,
    webui_server_notes_archive_payload,
    webui_server_notes_payload,
)
from nanoinfra.webui.ws_http import server_notes_headers


def _server(tmp_path: Path) -> tuple[ServerStore, ServerNotesStore, str]:
    store = ServerStore(tmp_path)
    server = store.create({"name": "barrahome", "providerId": "ssh"})
    return store, ServerNotesStore(tmp_path), server.id


def test_the_payload_carries_both_the_text_and_the_parsed_entries(tmp_path: Path) -> None:
    store, notes, server_id = _server(tmp_path)
    notes.append(server_id, author="sre", kind=AUTHOR_AGENT, title="quirk", body="something true")

    payload = webui_server_notes_payload(store, notes, server_id)

    assert payload is not None
    assert payload["name"] == "barrahome"
    assert payload["notesUpdatedAt"]
    assert "something true" in payload["text"]
    assert payload["entries"] == [
        {
            "when": payload["entries"][0]["when"],
            "author": "sre",
            "title": "quirk",
            "body": "something true",
            "isOperator": False,
        }
    ]
    assert payload["hasArchive"] is False


def test_a_missing_server_is_a_404_and_not_an_empty_file(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    notes = ServerNotesStore(tmp_path)
    assert webui_server_notes_payload(store, notes, "0" * 32) is None
    assert webui_server_notes_archive_payload(store, notes, "0" * 32) is None
    assert append_webui_server_note(
        store, notes, "0" * 32, {"title": "t", "body": "b"}, author="webui"
    ) is None
    assert save_webui_server_notes(store, notes, "0" * 32, "text") is None


def test_an_appended_note_is_marked_operator_with_the_verified_identity(tmp_path: Path) -> None:
    """The author comes from the call site, which reads it from the request identity (#228)."""
    store, notes, server_id = _server(tmp_path)

    payload = append_webui_server_note(
        store,
        notes,
        server_id,
        {"title": "journald is deliberate", "body": "Do not change it.", "author": "impostor"},
        author="webui:alberto",
    )

    assert payload is not None
    assert payload["appended"]["isOperator"] is True
    assert payload["appended"]["author"] == "webui:alberto"
    # A payload-supplied author is ignored rather than merged.
    assert "impostor" not in payload["text"]
    assert "webui:alberto (operator)" in payload["text"]


def test_an_operator_note_is_not_credential_screened(tmp_path: Path) -> None:
    store, notes, server_id = _server(tmp_path)
    payload = append_webui_server_note(
        store,
        notes,
        server_id,
        {"title": "legacy config", "body": "The token= line in /etc/app.conf is dead."},
        author="webui",
    )
    assert payload is not None and "token=" in payload["text"]


def test_an_empty_note_is_refused_with_a_reason(tmp_path: Path) -> None:
    store, notes, server_id = _server(tmp_path)
    with pytest.raises(ServerNotesError):
        append_webui_server_note(store, notes, server_id, {"title": "", "body": "b"}, author="w")
    with pytest.raises(ServerNotesError):
        append_webui_server_note(store, notes, server_id, {"title": "t", "body": ""}, author="w")


def test_saving_the_whole_file_replaces_it_and_stamps_the_record(tmp_path: Path) -> None:
    store, notes, server_id = _server(tmp_path)
    notes.append(server_id, author="sre", kind=AUTHOR_AGENT, title="old", body="stale")

    payload = save_webui_server_notes(
        store,
        notes,
        server_id,
        "Hand-written context.\n\n## 2026-01-01 · alberto (operator) · owner\nAsk #infra first.\n",
    )

    assert payload is not None
    assert "old" not in payload["text"]
    assert payload["entries"] == [
        {
            "when": "2026-01-01",
            "author": "alberto",
            "title": "owner",
            "body": "Ask #infra first.",
            "isOperator": True,
        }
    ]
    stored = store.get(server_id)
    assert stored is not None and stored.notes_updated_at


def test_the_archive_payload_reads_what_rotation_moved_out(tmp_path: Path) -> None:
    store, notes, server_id = _server(tmp_path)
    for index in range(30):
        notes.append(
            server_id, author="sre", kind=AUTHOR_AGENT, title=f"f{index}", body="x " * 900
        )

    live = webui_server_notes_payload(store, notes, server_id)
    archive = webui_server_notes_archive_payload(store, notes, server_id)

    assert live is not None and live["hasArchive"] is True
    assert archive is not None and archive["entries"]
    assert archive["entries"][0]["title"] == "f0"


def test_the_notes_headers_round_trip_through_the_chunked_reader() -> None:
    """One long file must survive the split, because this transport exposes no body."""
    from nanoinfra.webui import ws_http

    payload = {"text": "## a · b · c\n" + ("x" * 40_000)}
    headers = server_notes_headers(payload)

    class _Request:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    assert int(headers["X-Nanoinfra-Server-Notes-Chunks"]) > 1
    read = ws_http._server_notes_values_from_request(_Request(headers))  # pyright: ignore[reportPrivateUsage]
    assert read == payload
    # Every chunk line stays under the transport's request-line limit.
    assert all(len(f"{name}: {value}") < 8192 for name, value in headers.items())

    # A dropped chunk is an invalid payload, never a partial save.
    truncated = dict(headers)
    del truncated["X-Nanoinfra-Server-Notes-1"]
    assert ws_http._server_notes_values_from_request(_Request(truncated)) is None  # pyright: ignore[reportPrivateUsage]

    # And a single unchunked header still reads, the way the diagram path's does.
    from urllib.parse import quote

    single = {"X-Nanoinfra-Server-Notes": quote(json.dumps({"text": "hi"}), safe="")}
    assert ws_http._server_notes_values_from_request(_Request(single)) == {"text": "hi"}  # pyright: ignore[reportPrivateUsage]
    assert unquote(single["X-Nanoinfra-Server-Notes"]) == '{"text": "hi"}'
