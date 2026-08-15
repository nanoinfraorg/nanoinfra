# tests/session/test_provider_state_replay.py
"""nanoinfraorg/nanoinfra#52: what a scrubbed provider state costs a replay.

The module above this one (``test_provider_state_session_file.py``) proves the scrub at the file
bytes, through a real executor. This module holds the three decisions around that scrub, and
each one needs no executor, so each one states its own condition instead:

1. A state with no secret serialises byte-identically. The workspace holds no secret record, so
   the redactor asks nothing and answers with the same text.
2. A scrubbed thinking block never replays. The scrub unsigned it (#48), and a provider needs a
   signature that matches the text, so a marked block reaches no provider.
3. A scrub that cannot run persists no state. The workspace holds a secret record and no
   executor listens, which is the real failure #41 describes rather than a patched function.

The child-process technique of ``test_reasoning_session_file.py`` does not apply to these three.
That technique proves WHERE a plaintext lives, and none of these three resolves a plaintext at
all: two of them run against a workspace with no secret, and the third one exists because
nothing scrubbed. The file-bytes module above carries the child instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanoinfra.agent.redaction import (
    REASONING_SCRUB_MARKER_KEY,
    REASONING_SCRUBBED_MARKER,
    REASONING_WITHHELD_MARKER,
    SCRUB_UNAVAILABLE_MARKER,
)
from nanoinfra.providers.base import LLMProvider, ProviderConversationState
from nanoinfra.providers.conversation_state import ProviderConversationStateController
from nanoinfra.providers.openai_responses.state import prepare_responses_input
from nanoinfra.session.manager import JsonlSessionStore, Session, SessionManager

MODEL = "gpt-5.6"
PROVIDER = "openai_compat:openai:https://api.openai.com/v1"
SIGNATURE = "the-signature-the-provider-issued"
SECRET_VALUE = "hunter2-correct-horse-battery"

_SIGNED_BLOCK: dict[str, Any] = {
    "type": "thinking",
    "thinking": "I restart nginx on web1",
    "signature": SIGNATURE,
}
_SCRUBBED_BLOCK: dict[str, Any] = {
    "type": "thinking",
    "thinking": "I run mysql -p[redacted secret: prod-db-password] on db1",
    REASONING_SCRUB_MARKER_KEY: REASONING_SCRUBBED_MARKER,
}
_WITHHELD_BLOCK: dict[str, Any] = {
    "type": "thinking",
    "thinking": "[nanoinfra withheld this text...]",
    REASONING_SCRUB_MARKER_KEY: REASONING_WITHHELD_MARKER,
}

_OPAQUE_PAYLOAD: dict[str, Any] = {
    "items": [{"type": "reasoning", "id": "rs_0af3", "encrypted_content": "gAAAAABo8Zk3"}],
    "context_tokens": 2048,
}


def _state(*, pending: list[dict[str, Any]] | None = None) -> ProviderConversationState:
    return ProviderConversationState(
        kind="openai_responses",
        provider=PROVIDER,
        model=MODEL,
        version=1,
        payload=json.loads(json.dumps(_OPAQUE_PAYLOAD)),
        pending_messages=pending if pending is not None else [],
    )


def _pending_assistant(*blocks: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "I run the command on db1",
            "thinking_blocks": list(blocks),
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "execute_on_server", "content": "ok"},
    ]


def _rich_pending() -> list[dict[str, Any]]:
    """Every shape a pending message can carry, so byte-identity means something.

    A block list, an internal ``_meta`` the controller reads, serialized tool arguments, a tool
    result, and both reasoning fields. The scrub touches each one of these on the message path,
    so each one is a chance for a key to move or a value to change.
    """
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": "restart nginx"}],
            "_meta": {"provider_state_boundary": True},
        },
        {
            "role": "assistant",
            "content": "done",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "execute_on_server",
                        "arguments": '{"server_id_or_name": "web1", "command": "systemctl start x"}',
                    },
                }
            ],
            "reasoning_content": "I restart nginx on web1",
            "thinking_blocks": [_SIGNED_BLOCK],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "execute_on_server", "content": "ok"},
    ]


def _quiet_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace that holds no secret, so no scrub asks the executor anything."""
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)
    workspace = tmp_path / "quiet"
    workspace.mkdir()
    return workspace


def _saved_lines(workspace: Path, session: Session) -> list[str]:
    SessionManager(workspace).save(session)
    path = JsonlSessionStore(workspace).get_session_path(session.key)
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _provider_state_line(lines: list[str]) -> str | None:
    for line in lines:
        if json.loads(line).get("_type") == "provider_state":
            return line
    return None


def _replayed(session: Session) -> list[dict[str, Any]]:
    """What the next request carries for a loaded state, through the real controller."""
    assert session.provider_state is not None
    provider = MagicMock(spec=LLMProvider)
    provider.can_resume_conversation_state.return_value = True
    controller = ProviderConversationStateController(
        provider=provider,
        model=MODEL,
        messages=session.messages,
        state=session.provider_state,
    )
    context = controller.prepare_request(session.messages, context_window_tokens=200_000)
    assert context is not None
    assert context.conversation_state is not None
    return context.conversation_state.pending_messages


def test_a_state_with_no_secret_serialises_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case must reach the file unchanged, and this pins the exact bytes."""
    workspace = _quiet_workspace(tmp_path, monkeypatch)
    state = _state(pending=_rich_pending())
    session = Session(
        key="websocket:quiet",
        messages=[{"role": "user", "content": "restart nginx"}],
        provider_state=state,
    )

    line = _provider_state_line(_saved_lines(workspace, session))

    assert line == json.dumps(
        {"_type": "provider_state", "state": state.to_private_record()}, ensure_ascii=False
    )


def test_a_state_with_no_pending_message_serialises_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state that carries no message of ours has nothing to scrub, and costs no round trip."""
    workspace = _quiet_workspace(tmp_path, monkeypatch)
    state = _state()
    session = Session(key="websocket:empty", messages=[], provider_state=state)

    line = _provider_state_line(_saved_lines(workspace, session))

    assert line == json.dumps(
        {"_type": "provider_state", "state": state.to_private_record()}, ensure_ascii=False
    )


def test_a_scrubbed_thinking_block_never_replays_from_a_provider_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signature that no longer matches its text is worse than no block at all (#48)."""
    workspace = _quiet_workspace(tmp_path, monkeypatch)
    manager = SessionManager(workspace)
    key = "websocket:scrubbed"
    manager.save(
        Session(
            key=key,
            messages=[{"role": "user", "content": "run it"}],
            provider_state=_state(pending=_pending_assistant(_SCRUBBED_BLOCK)),
        )
    )
    manager.invalidate(key)

    replayed = _replayed(manager.get_or_create(key))

    assert "thinking_blocks" not in replayed[0]
    assert replayed[0]["content"] == "done"
    assert [message["role"] for message in replayed] == ["assistant", "tool"]


def test_a_withheld_thinking_block_never_replays_from_a_provider_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block nobody scrubbed holds a marker in place of its text, so it replays no better."""
    workspace = _quiet_workspace(tmp_path, monkeypatch)
    manager = SessionManager(workspace)
    key = "websocket:withheld-block"
    manager.save(
        Session(
            key=key,
            messages=[{"role": "user", "content": "run it"}],
            provider_state=_state(pending=_pending_assistant(_WITHHELD_BLOCK)),
        )
    )
    manager.invalidate(key)

    replayed = _replayed(manager.get_or_create(key))

    assert "thinking_blocks" not in replayed[0]


def test_a_signed_thinking_block_still_replays_from_a_provider_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that held no secret changes in no way."""
    workspace = _quiet_workspace(tmp_path, monkeypatch)
    manager = SessionManager(workspace)
    key = "websocket:signed"
    manager.save(
        Session(
            key=key,
            messages=[{"role": "user", "content": "restart nginx"}],
            provider_state=_state(pending=_pending_assistant(_SIGNED_BLOCK, _SCRUBBED_BLOCK)),
        )
    )
    manager.invalidate(key)

    replayed = _replayed(manager.get_or_create(key))

    assert replayed[0]["thinking_blocks"] == [_SIGNED_BLOCK]


def _loud_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace that holds a secret record, with no executor to scrub for it.

    The record is a file rather than a real secret. ``workspace_may_hold_a_secret`` counts
    records and decrypts nothing, so one file is enough to make the scrub a round trip, and the
    round trip then fails because nothing listens on the socket.
    """
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)
    workspace = tmp_path / "loud"
    (workspace / "secrets").mkdir(parents=True)
    (workspace / "secrets" / "one.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NANOINFRA_EXECUTOR_SOCKET", str(tmp_path / "nowhere" / "executor.sock"))
    return workspace


def test_a_scrub_that_cannot_run_persists_no_provider_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed costs a cache and never a session, so no state beats a raw one."""
    workspace = _loud_workspace(tmp_path, monkeypatch)
    session = Session(
        key="websocket:no-scrub",
        messages=[{"role": "user", "content": "run it"}],
        provider_state=_state(
            pending=[
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "execute_on_server",
                    "content": f"mysql -p{SECRET_VALUE}",
                }
            ]
        ),
    )

    lines = _saved_lines(workspace, session)

    assert _provider_state_line(lines) is None
    assert SECRET_VALUE not in "\n".join(lines)


def test_a_session_still_loads_after_a_scrub_could_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing provider state degrades to a normal replay, so the session survives."""
    workspace = _loud_workspace(tmp_path, monkeypatch)
    manager = SessionManager(workspace)
    key = "websocket:degraded"
    manager.save(
        Session(
            key=key,
            messages=[{"role": "user", "content": "run it"}],
            provider_state=_state(pending=[{"role": "tool", "content": "ok"}]),
        )
    )
    manager.invalidate(key)

    loaded = manager.get_or_create(key)

    assert loaded.provider_state is None
    assert loaded.messages == [{"role": "user", "content": "run it"}]


def test_no_provider_state_degrades_to_a_normal_replay() -> None:
    """The claim fail-closed rests on: a missing state costs a cache and never a session.

    ``prepare_responses_input`` is the code that pays that cost for the Responses API. With no
    state it converts the whole message history instead, so the conversation continues.
    """
    messages = [
        {"role": "user", "content": "check the database"},
        {"role": "assistant", "content": "1 row in set"},
    ]

    _, items, resumed = prepare_responses_input(
        messages, state=None, provider=PROVIDER, model=MODEL
    )

    assert resumed is False
    replayed = json.dumps(items, ensure_ascii=False)
    assert "check the database" in replayed
    assert "1 row in set" in replayed


def test_a_withheld_marker_never_reaches_the_provider_state_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message path persists a marker (#41). A provider state persists nothing instead.

    A marker is a readable answer for a transcript a human reads. A provider state is replay
    input, so a marker there would send the model a sentence it never wrote.
    """
    workspace = _loud_workspace(tmp_path, monkeypatch)
    session = Session(
        key="websocket:no-marker",
        messages=[],
        provider_state=_state(pending=[{"role": "tool", "content": "ok"}]),
    )

    lines = _saved_lines(workspace, session)

    marker_prefix = SCRUB_UNAVAILABLE_MARKER.split("{reason}", 1)[0]
    assert marker_prefix not in "\n".join(lines)
