# tests/webui/test_unreadable_transcript.py
"""A transcript nobody can read must not render as an empty chat.

This happened for real. While the executor's scrub socket was unreachable, every WebUI transcript
record was withheld -- including its own `event` name, because the withhold path replaced every
string. The replay then knew none of the events, and a 5 MB file of records rendered as a chat
with no messages in it, over a session whose canonical history still held every turn.

Two properties keep it from happening again: the withhold keeps the shape of a record, and a
replay that finds nothing falls back to the canonical history.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.agent.redaction import withheld_transcript_event


def _event() -> dict[str, Any]:
    return {
        "event": "message",
        "chat_id": "c1",
        "turn_id": "t1",
        "turn_phase": "answer",
        "kind": "progress",
        "text": "the uptime is 3 days, and the key is s3cr3t",
        "turn_seq": 7,
        "created_at_ms": 1787000000000,
        "tool_events": [
            {
                "version": 1,
                "phase": "end",
                "call_id": "execute_on_server_0",
                "name": "execute_on_server",
                "result": "up 3 days",
                "error": None,
            }
        ],
    }


def test_withholding_keeps_the_name_of_the_event() -> None:
    withheld = withheld_transcript_event(_event(), "the scrub socket was unreachable")

    # The discriminator the replay reads. Losing it is what made a whole chat vanish.
    assert withheld["event"] == "message"
    assert withheld["chat_id"] == "c1"
    assert withheld["turn_id"] == "t1"
    assert withheld["turn_phase"] == "answer"
    assert withheld["kind"] == "progress"
    # Numbers were never text.
    assert withheld["turn_seq"] == 7
    assert withheld["created_at_ms"] == 1787000000000


def test_withholding_keeps_the_shape_of_a_nested_tool_event() -> None:
    withheld = withheld_transcript_event(_event(), "no executor")

    [tool_event] = withheld["tool_events"]
    assert tool_event["version"] == 1
    assert tool_event["phase"] == "end"
    assert tool_event["call_id"] == "execute_on_server_0"
    assert tool_event["name"] == "execute_on_server"


def test_withholding_still_removes_every_text() -> None:
    """The point of the marker survives: nothing a turn produced is persisted."""
    withheld = withheld_transcript_event(_event(), "no executor")

    assert "s3cr3t" not in withheld["text"]
    assert "withheld this text" in withheld["text"]
    [tool_event] = withheld["tool_events"]
    assert "up 3 days" not in tool_event["result"]
    assert "withheld this text" in tool_event["result"]


def test_a_transcript_that_replays_to_nothing_falls_back_to_the_session_history(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The recovery half: an already-damaged transcript on disk still renders."""
    from nanoinfra.webui import transcript as transcript_module

    session_key = "websocket:chat-recover"
    marker = "[nanoinfra withheld this text. No executor scrubbed it]"
    # What the damaged file holds: records whose own event name was replaced.
    damaged = [
        {"event": marker, "text": marker, "turn_seq": index}
        for index in range(1, 6)
    ]
    monkeypatch.setattr(transcript_module, "read_transcript_lines", lambda _key: damaged)
    monkeypatch.setattr(
        transcript_module, "_annotate_replay_identities", lambda lines: list(lines)
    )

    payload = transcript_module.build_webui_thread_response(
        session_key,
        session_messages_loader=lambda: [
            {"role": "user", "content": "check the uptime"},
            {"role": "assistant", "content": "up 3 days"},
        ],
    )

    assert payload is not None
    texts = [str(message.get("text") or message.get("content") or "") for message in payload["messages"]]
    assert any("check the uptime" in text for text in texts), texts
    assert any("up 3 days" in text for text in texts), texts
