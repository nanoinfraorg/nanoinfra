# tests/agent/test_redaction_boundaries.py
"""Item 14 (#17), the two boundaries the first pass left unwired.

`memory/history.jsonl` and the subagent transcripts were covered in d3cbdd1b. The main chat
transcript is neither of those. It is `sessions/*.jsonl`, written through the loop's save
stage, plus a second durable file for the WebUI reasoning pane. Both still held a resolved
credential, so the acceptance clause about the reasoning pane was not met.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import pytest

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.bus.queue import MessageBus
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.session.manager import Session
from nanoinfra.webui.transcript import append_transcript_object

_SECRET = "s3cr3t-key-material"

# The executor performs the scrub after #41, so each test here starts one. The service runs the
# executor's own answer path, and the text crosses a real socket.
_Scrubber = Callable[[Path], object]


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _stored_secret(workspace: Path) -> str:
    """A real Secret, because redaction resolves sentinels from the workspace store."""
    secret = SecretStore(workspace).create(
        # kind="password", because the value is a password-shaped string. An ssh_key
        # secret has to hold a PEM private key.
        {"name": "web-key", "kind": "password", "providerId": "local", "value": _SECRET}
    )
    return secret.name


def _loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


def test_a_saved_turn_carries_no_secret_value(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    name = _stored_secret(tmp_path)
    scrub_service(tmp_path)
    loop = _loop(tmp_path)
    session = Session(key="s1", messages=[])
    # The assistant declaration has to be there. Persistence drops a tool result whose
    # tool_call_id no session message declares.
    messages = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "execute_on_server", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "execute_on_server",
            "content": f"connected with {_SECRET} and ran uptime",
        },
    ]

    loop._save_turn(session, messages, 0)

    persisted = json.dumps(session.messages)
    assert _SECRET not in persisted
    assert name in persisted


def test_a_saved_turn_withholds_its_text_when_no_executor_answers(tmp_path: Path) -> None:
    """#41 at the chat transcript. No scrub service runs in this test.

    The old code persisted the turn unscrubbed and logged a warning. The turn now persists with
    a marker in place of each text, so the file holds no value that nobody scrubbed.
    """
    _stored_secret(tmp_path)
    loop = _loop(tmp_path)
    session = Session(key="s1", messages=[])
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "execute_on_server", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "execute_on_server",
            "content": f"connected with {_SECRET} and ran uptime",
        },
    ]

    loop._save_turn(session, messages, 0)

    persisted = json.dumps(session.messages)
    assert _SECRET not in persisted
    assert "withheld" in persisted
    assert '"tool_call_id": "call-1"' in persisted


def test_a_saved_turn_keeps_ordinary_content(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """Redaction must remove the value and nothing else."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    loop = _loop(tmp_path)
    session = Session(key="s1", messages=[])

    loop._save_turn(session, [{"role": "user", "content": "restart nginx please"}], 0)

    assert "restart nginx please" in json.dumps(session.messages)


def test_a_saved_turn_scrubs_a_tool_call_argument(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """A resolved command rides in tool_calls arguments, which sensitive_params never covered."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    loop = _loop(tmp_path)
    session = Session(key="s1", messages=[])
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": json.dumps({"command": f"mysql -p{_SECRET}"}),
                    },
                }
            ],
        }
    ]

    loop._save_turn(session, messages, 0)

    assert _SECRET not in json.dumps(session.messages)


def test_the_reasoning_pane_transcript_carries_no_secret_value(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The second durable file. `/api/sessions/{key}/webui-thread` reads it back."""
    name = _stored_secret(tmp_path)
    scrub_service(tmp_path)

    append_transcript_object(
        "websocket:chat-1",
        {"event": "tool_result", "text": f"connected with {_SECRET}"},
        workspace=tmp_path,
    )

    from nanoinfra.webui.transcript import read_transcript_lines

    written = json.dumps(read_transcript_lines("websocket:chat-1"))
    assert _SECRET not in written
    assert name in written


def test_the_reasoning_pane_transcript_keeps_ordinary_text(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    append_transcript_object(
        "websocket:chat-2", {"event": "assistant", "text": "nginx restarted"}, workspace=tmp_path
    )

    from nanoinfra.webui.transcript import read_transcript_lines

    assert "nginx restarted" in json.dumps(read_transcript_lines("websocket:chat-2"))


def test_the_transcript_writer_works_without_a_workspace(tmp_path: Path) -> None:
    """Callers outside a workspace scope must keep working, and they redact nothing.

    A missing workspace cannot resolve sentinels. That is a real limit and it is stated, so
    the caller that has a workspace passes it.
    """
    append_transcript_object("websocket:chat-3", {"event": "assistant", "text": "hello"})

    from nanoinfra.webui.transcript import read_transcript_lines

    assert "hello" in json.dumps(read_transcript_lines("websocket:chat-3"))


def test_the_recorder_passes_its_workspace_to_the_writer() -> None:
    """A default of None means production would silently write unredacted events.

    That is the same trap #33 hit, so the wiring gets its own test: the gateway must build the
    recorder with a workspace, and the recorder must forward it.
    """
    import inspect

    from nanoinfra.webui import gateway_services
    from nanoinfra.webui.transcript import WebUITranscriptRecorder

    assert "workspace" in inspect.signature(WebUITranscriptRecorder.__init__).parameters
    # The recorder redacts before it calls the funnel, so the funnel keeps its two-argument
    # call. Tests replace that function with a double, and a new keyword at the call site
    # would break every one of those doubles.
    assert "_redacted_transcript_record(dup, self._workspace)" in inspect.getsource(
        WebUITranscriptRecorder.append
    )
    assert "workspace=workspace_path" in inspect.getsource(gateway_services)


def test_the_recorder_redacts_through_its_own_append(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    name = _stored_secret(tmp_path)
    scrub_service(tmp_path)
    from nanoinfra.webui.transcript import WebUITranscriptRecorder, read_transcript_lines

    recorder = WebUITranscriptRecorder(workspace=tmp_path)
    recorder.append("chat-4", {"event": "tool_result", "text": f"used {_SECRET}"})

    written = json.dumps(read_transcript_lines("websocket:chat-4"))
    assert _SECRET not in written
    assert name in written
