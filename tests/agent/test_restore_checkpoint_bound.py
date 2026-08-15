# tests/agent/test_restore_checkpoint_bound.py
"""nanoinfraorg/nanoinfra#55: a restored tool result carries the transcript bound.

``_save_turn`` bounds the content of a ``role="tool"`` record before it reaches
``sessions/*.jsonl``. ``_restore_runtime_checkpoint`` appended the same kind of record straight
into ``session.messages``, and no bound ran there. So a turn that a restart interrupted wrote a
result far longer than any other record in the same file.

The in-flight budget is not the transcript budget. ``ContextGovernor.normalize_tool_result``
exempts ``read_file`` from the offload, so that result stays whole in the message list of the
running turn. The normal persist path bounds it. The restore path did not.

This is a size problem and not a value leak. #51 scrubs the checkpoint, so a restored result
carries no credential.

The assertions read the session file. The two call sites of the restore
(``nanoinfra/agent/loop.py:1361`` and ``:1693``) both save the session right after, so the file
is the artifact an operator reads back.

One test pins the opposite direction: the checkpoint itself keeps every character it holds. A
bound at the checkpoint write is the obvious fix and the wrong one, because #51 requires that a
checkpoint holding no secret stays byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.bus.queue import MessageBus
from nanoinfra.session.manager import JsonlSessionStore, Session
from nanoinfra.utils.helpers import truncate_text

SESSION_KEY = "websocket:chat-1"

#: A bound far below the default of 16000, so a test payload stays small and the assertion
#: reads the loop's own number rather than a number this file repeats.
BOUND = 200

#: A result longer than the bound. ``read_file`` returns a whole file, and the in-flight path
#: exempts that tool from the offload, so this length is reachable.
OVERSIZED_RESULT = "x" * (BOUND * 4)


def _loop(workspace: Path, *, bound: int = BOUND) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=MagicMock(),
        workspace=workspace,
        model="test-model",
        max_tool_result_chars=bound,
    )


def _checkpoint(content: Any = OVERSIZED_RESULT) -> dict[str, Any]:
    """One interrupted turn, with a completed tool result the transcript must bound."""
    return {
        "phase": "tools_completed",
        "iteration": 1,
        "model": "test-model",
        "assistant_message": {
            "role": "assistant",
            "content": "I read the file.",
            "tool_calls": [
                {
                    "id": "call_done",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "app.log"}),
                    },
                }
            ],
        },
        "completed_tool_results": [
            {
                "role": "tool",
                "tool_call_id": "call_done",
                "name": "read_file",
                "content": content,
            }
        ],
        "pending_tool_calls": [],
    }


def _restored_session_file(workspace: Path, checkpoint: dict[str, Any]) -> bytes:
    """Restore one checkpoint through the loop, and return the session file bytes.

    The save mirrors both call sites of the restore, which save the session as soon as the
    restore reports it changed the history.
    """
    loop = _loop(workspace)
    session = loop.sessions.get_or_create(SESSION_KEY)
    session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = checkpoint
    assert loop._restore_runtime_checkpoint(session) is True
    loop.sessions.save(session)
    return JsonlSessionStore(workspace).get_session_path(SESSION_KEY).read_bytes()


def _tool_records(raw: bytes) -> list[dict[str, Any]]:
    """Every ``role="tool"`` record the session file holds, in file order."""
    records: list[dict[str, Any]] = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        record: dict[str, Any] = json.loads(line)
        if record.get("role") == "tool":
            records.append(record)
    return records


# -- the restore applies the bound ------------------------------------------


def test_a_restored_tool_result_carries_the_transcript_bound(tmp_path: Path) -> None:
    """The defect #55 reports. The record reaches the file at the bound, with the marker.

    The marker text is written out here rather than imported. ``truncate_text`` is the function
    the production path calls, so an assertion that only compares against it would pass for a
    marker that changed on both sides at once.
    """
    raw = _restored_session_file(tmp_path, _checkpoint())

    records = _tool_records(raw)
    assert len(records) == 1
    content: str = records[0]["content"]
    assert content.endswith("\n... (truncated)")
    assert len(content) == BOUND + len("\n... (truncated)")
    assert content == truncate_text(OVERSIZED_RESULT, BOUND)


def test_the_restore_reads_the_bound_of_the_loop_that_runs_it(tmp_path: Path) -> None:
    """The bound comes from one place. A retyped number would ignore this loop's setting."""
    loop = _loop(tmp_path, bound=64)
    session = loop.sessions.get_or_create(SESSION_KEY)
    session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = _checkpoint()

    assert loop._restore_runtime_checkpoint(session) is True

    restored = [m for m in session.messages if m.get("role") == "tool"]
    assert restored[0]["content"] == truncate_text(OVERSIZED_RESULT, 64)


def test_a_restored_result_matches_what_the_normal_path_writes(tmp_path: Path) -> None:
    """The acceptance of #55. A checkpoint must not change what a result persists as."""
    checkpoint = _checkpoint()
    normal = _loop(tmp_path)
    normal_session = Session(key="websocket:chat-2")
    normal._save_turn(
        normal_session,
        [
            checkpoint["assistant_message"],
            checkpoint["completed_tool_results"][0],
        ],
        skip=0,
    )

    restored_loop = _loop(tmp_path)
    restored_session = Session(key="websocket:chat-3")
    restored_session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = checkpoint
    assert restored_loop._restore_runtime_checkpoint(restored_session) is True

    normal_tool = [m for m in normal_session.messages if m.get("role") == "tool"][0]
    restored_tool = [m for m in restored_session.messages if m.get("role") == "tool"][0]
    assert restored_tool["content"] == normal_tool["content"]


def test_a_restored_block_list_result_carries_the_same_bound(tmp_path: Path) -> None:
    """A tool result can hold blocks rather than one string, and the bound covers both.

    ``maybe_persist_tool_result`` accepts a block list, so this shape reaches a checkpoint.
    """
    blocks = [
        {"type": "text", "text": OVERSIZED_RESULT},
        {"type": "text", "text": "short tail"},
    ]

    raw = _restored_session_file(tmp_path, _checkpoint(blocks))

    records = _tool_records(raw)
    assert records[0]["content"][0]["text"] == truncate_text(OVERSIZED_RESULT, BOUND)
    assert records[0]["content"][1]["text"] == "short tail"


def test_a_result_inside_the_bound_restores_unchanged(tmp_path: Path) -> None:
    """The common case pays nothing. A short result keeps every character and no marker."""
    raw = _restored_session_file(tmp_path, _checkpoint("active (running)"))

    records = _tool_records(raw)
    assert records[0]["content"] == "active (running)"


# -- the checkpoint write keeps its bytes -----------------------------------


def test_the_checkpoint_write_applies_no_bound(tmp_path: Path) -> None:
    """The trade-off #55 names. The bound belongs to the restore, never to the write.

    #51 pins that a checkpoint holding no secret is byte-identical. A bound at the write would
    break that pin, and it would also shorten a checkpoint no secret ever touched. This test
    fails if somebody moves the bound to the obvious place.
    """
    loop = _loop(tmp_path)
    session = loop.sessions.get_or_create(SESSION_KEY)

    loop._set_runtime_checkpoint(session, _checkpoint())

    raw = JsonlSessionStore(tmp_path).get_session_path(SESSION_KEY).read_bytes()
    stored: dict[str, Any] | None = None
    for line in raw.decode("utf-8").splitlines():
        if line.strip() and json.loads(line).get("_type") == "metadata":
            stored = json.loads(line)["metadata"][AgentLoop._RUNTIME_CHECKPOINT_KEY]
    assert stored is not None
    assert stored["completed_tool_results"][0]["content"] == OVERSIZED_RESULT


# -- the fields the normal path leaves alone --------------------------------


def test_an_assistant_message_restores_at_the_length_the_normal_path_writes(
    tmp_path: Path,
) -> None:
    """The normal path bounds a ``role="tool"`` record and nothing else (#55, item 4).

    ``_save_turn`` reads the bound inside its ``role == "tool"`` branch, so an assistant
    content persists whole. The restore must match that, so this test fails on a bound
    invented for the restore alone as well as on a bound that goes missing.
    """
    checkpoint = _checkpoint()
    checkpoint["assistant_message"]["content"] = OVERSIZED_RESULT
    normal = _loop(tmp_path)
    normal_session = Session(key="websocket:chat-4")
    normal._save_turn(normal_session, [checkpoint["assistant_message"]], skip=0)

    restored_loop = _loop(tmp_path)
    restored_session = Session(key="websocket:chat-5")
    restored_session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = checkpoint
    assert restored_loop._restore_runtime_checkpoint(restored_session) is True

    normal_assistant = [m for m in normal_session.messages if m.get("role") == "assistant"][0]
    restored_assistant = [
        m for m in restored_session.messages if m.get("role") == "assistant"
    ][0]
    assert normal_assistant["content"] == OVERSIZED_RESULT
    assert restored_assistant["content"] == OVERSIZED_RESULT


def test_a_pending_tool_call_restores_with_its_arguments_unread(tmp_path: Path) -> None:
    """A pending call needs no bound, because the restore writes its own short text.

    The record the restore appends for an unfinished call is a fixed sentence. The arguments of
    the call never reach the transcript, so no bound applies to them.
    """
    checkpoint = _checkpoint()
    checkpoint["pending_tool_calls"] = [
        {
            "id": "call_pending",
            "type": "function",
            "function": {
                "name": "server_exec",
                "arguments": json.dumps({"command": OVERSIZED_RESULT}),
            },
        }
    ]

    raw = _restored_session_file(tmp_path, checkpoint)

    pending = [r for r in _tool_records(raw) if r.get("tool_call_id") == "call_pending"][0]
    assert "interrupted before this tool finished" in pending["content"].lower()
    assert OVERSIZED_RESULT not in raw.decode("utf-8")
