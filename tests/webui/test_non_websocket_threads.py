"""A conversation held anywhere is readable in the WebUI (#216).

Driving nanoinfra from the Codex CLI or from Hermes works, and it is the deployment's own agent:
its workspace, its memory, its gate, its connectors. But the conversation appeared nowhere in the
UI, which is half the reason to use nanoinfra rather than a model endpoint — the deployment keeps
the record and the operator could not read it. A gated turn made that concrete: the approval lands
in the WebUI inbox with no conversation behind it.

The record was never missing. `api:` sessions sit in `sessions/<identity>/`, their cost in
`llm-usage.sqlite3`, their decisions in `gates/`. Two narrow cuts hid them: the listing skipped any
key that was not `websocket:`, and the thread was read from a transcript only that channel writes.

What did *not* move is pinned here too: rendering is on read rather than persisted, and writing
stays closed.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.webui.transcript import (
    build_webui_thread_response,
    read_transcript_lines,
    session_messages_as_transcript_rows,
)


def _history() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "what is the uptime on barrahome?"},
        {"role": "assistant", "content": "barrahome is up 27 weeks, 5 days."},
        {"role": "user", "content": "thank you!"},
        {"role": "assistant", "content": "you're welcome."},
    ]


def test_a_session_from_another_channel_renders_its_rows() -> None:
    rows = session_messages_as_transcript_rows("api:alberto", _history())

    assert [row["event"] for row in rows] == ["user", "message", "user", "message"]
    assert rows[0]["chat_id"] == "alberto"
    assert rows[1]["text"].startswith("barrahome is up")


def test_the_chat_id_comes_from_the_key_whatever_the_channel() -> None:
    """It used to be `None` for anything but websocket, so every row grouped under nothing."""
    assert session_messages_as_transcript_rows("cron:nightly", _history())[0]["chat_id"] == (
        "nightly"
    )


def test_the_thread_is_built_from_history_when_no_transcript_exists(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("nanoinfra.config.paths.get_data_dir", lambda: tmp_path)

    data = build_webui_thread_response(
        "api:alberto",
        session_messages_loader=_history,
    )

    assert data is not None
    texts = [m.get("content") for m in data["messages"]]
    assert "what is the uptime on barrahome?" in texts
    assert any("barrahome is up" in str(t) for t in texts)


def test_rendering_writes_no_transcript(tmp_path, monkeypatch) -> None:
    """Persisting it would freeze the view: the next turn arrives over the API, which writes no
    transcript, so the file would answer with an old conversation and look authoritative doing
    it. The session history stays the record."""
    monkeypatch.setattr("nanoinfra.config.paths.get_data_dir", lambda: tmp_path)

    build_webui_thread_response("api:alberto", session_messages_loader=_history)

    assert read_transcript_lines("api:alberto") == []


def test_a_session_with_no_history_and_no_transcript_has_no_thread(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("nanoinfra.config.paths.get_data_dir", lambda: tmp_path)

    assert build_webui_thread_response("api:empty", session_messages_loader=lambda: []) is None


def test_a_real_transcript_still_wins(tmp_path, monkeypatch) -> None:
    """A websocket thread has turn phases, activity segments and per-step usage that history
    cannot reconstruct. History is the fallback, never an override."""
    monkeypatch.setattr("nanoinfra.config.paths.get_data_dir", lambda: tmp_path)
    from nanoinfra.webui.transcript import append_transcript_object

    append_transcript_object(
        "websocket:chat-1",
        {"event": "user", "chat_id": "chat-1", "text": "from the transcript"},
    )

    data = build_webui_thread_response(
        "websocket:chat-1",
        session_messages_loader=_history,
    )

    assert data is not None
    texts = [str(m.get("content")) for m in data["messages"]]
    assert "from the transcript" in texts
    assert not any("barrahome" in t for t in texts)


def test_machinery_sessions_are_not_conversations() -> None:
    """`dream:` is memory consolidation talking to itself. Ten of those rows would bury the chats
    in the sidebar, and none of them is a conversation anybody had."""
    from nanoinfra.webui.ws_http import _is_readable_session_key

    assert _is_readable_session_key("api:alberto") is True
    assert _is_readable_session_key("cron:nightly") is True
    assert _is_readable_session_key("telegram:123") is True
    assert _is_readable_session_key("dream:20260902-011437") is False
    assert _is_readable_session_key("no-colon") is False
