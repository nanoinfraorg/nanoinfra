# tests/session/test_provider_state_session_file.py
"""nanoinfraorg/nanoinfra#52 end to end, against a real executor process.

The acceptance clause of #52 names the ``provider_state`` line of ``sessions/*.jsonl``, and not
a function. So this module starts a real executor child, saves one session whose provider state
carries pending messages that quote a stored secret value, and reads the file back from disk.

A real child proves the #41 split as well. The sentinels live in the executor, and the agent
sends one text over a socket. One test breaks ``resolve_plaintext`` in this process, the way
``tests/session/test_reasoning_session_file.py`` does for the message path, and the pending
messages still persist scrubbed. So another address space held the sentinels.

**The scope of this module is the ``pending_messages`` half.** A provider state holds two kinds
of thing. ``pending_messages`` are Chat-style messages this repository builds, and #52 scrubs
them. ``payload`` is the provider's own handle, and #52 left it as it was.

#54 closed the payload half, and ``tests/session/test_responses_payload_session_file.py`` covers
it. ``OPAQUE_PAYLOAD`` below therefore still reaches the file unchanged, and for a reason that is
now narrower than #52 stated: every value in it is either a name #54 does not scrub or a text that
holds no stored secret. A failure in this module names the pending-message half, which is what it
is for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.gates.executor.scrub_protocol import default_scrub_socket_path
from nanoinfra.gates.executor.supervisor import start_executor
from nanoinfra.providers.base import LLMProvider, ProviderConversationState
from nanoinfra.providers.conversation_state import ProviderConversationStateController
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.session.manager import JsonlSessionStore, Session, SessionManager

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"
SIGNATURE = "the-signature-the-provider-issued"
MODEL = "gpt-5.6"
PROVIDER = "openai_compat:openai:https://api.openai.com/v1"

#: The command the model resolved. A resolved command routinely embeds a credential (#17).
RESOLVED_COMMAND = f"mysql --host=db1 --user=app -p{SECRET_VALUE} --database=app -e 'select 1'"

#: The provider's own handle. Every value in this fixture is an item id, a server-side call id, an
#: encrypted reasoning blob, or an empty argument object. A wrong edit to any of them breaks a
#: replay the operator cannot recover. The fixture holds no stored secret on purpose, so a failure
#: below names the pending-message half and never the payload scrub #54 added.
OPAQUE_PAYLOAD: dict[str, Any] = {
    "items": [
        {"type": "reasoning", "id": "rs_0af3", "encrypted_content": "gAAAAABo8Zk3encrypted"},
        {
            "type": "function_call",
            "id": "fc_0af4",
            "call_id": "call_1",
            "name": "execute_on_server",
            "arguments": "{}",
        },
    ],
    "context_tokens": 4096,
}


class _Deployment:
    """One executor child, plus the workspace whose secrets it can read."""

    def __init__(self, *, workspace: Path, handle: Any) -> None:
        self.workspace = workspace
        self.handle = handle


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory):
    """Start one real executor for this module, and stop it before the module ends."""
    patch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("provider_state")
    home = root / "home"
    (home / ".nanoinfra").mkdir(parents=True)
    (home / ".nanoinfra" / "config.json").write_text("{}", encoding="utf-8")
    # The child is a separate process, so HOME places its config and its audit root.
    patch.setenv("HOME", str(home))
    patch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    patch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)

    workspace = root / "ws"
    workspace.mkdir()
    SecretStore(workspace).create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )

    execute_socket = root / "r" / "e.sock"
    # The agent derives the scrub socket from this path, the way a deployment names it.
    patch.setenv(EXECUTOR_SOCKET_ENV, str(execute_socket))

    handle = start_executor(socket_path=execute_socket, workspace=workspace, timeout_s=30.0)
    try:
        # The scrub socket binds before the execute socket, so a handle proves both are up.
        assert handle.is_running(), handle.read_log_tail(tail=20)
        assert default_scrub_socket_path(execute_socket).exists(), handle.read_log_tail(tail=20)
        yield _Deployment(workspace=workspace, handle=handle)
    finally:
        handle.stop(timeout_s=10)
        patch.undo()


def _pending_messages() -> list[dict[str, Any]]:
    """The delta #52 describes: a turn that resolved a secret and then ran a command."""
    reasoning = f"I run {RESOLVED_COMMAND} and then I read the row count."
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "execute_on_server",
                        "arguments": json.dumps(
                            {"server_id_or_name": "db1", "command": RESOLVED_COMMAND},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
            "reasoning_content": reasoning,
            "thinking_blocks": [
                {"type": "thinking", "thinking": reasoning, "signature": SIGNATURE}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "execute_on_server",
            "content": f"$ {RESOLVED_COMMAND}\n1 row in set",
        },
    ]


def _state() -> ProviderConversationState:
    return ProviderConversationState(
        kind="openai_responses",
        provider=PROVIDER,
        model=MODEL,
        version=1,
        payload=json.loads(json.dumps(OPAQUE_PAYLOAD)),
        pending_messages=_pending_messages(),
    )


def _saved_session_file(workspace: Path, key: str) -> str:
    """Save one session that carries a provider state, and return the file text."""
    session = Session(
        key=key,
        messages=[{"role": "user", "content": "check the database"}],
        provider_state=_state(),
    )
    SessionManager(workspace).save(session)
    return JsonlSessionStore(workspace).get_session_path(key).read_text(encoding="utf-8")


def _provider_state_record(persisted: str) -> dict[str, Any]:
    """The one ``provider_state`` line of the file, decoded."""
    records = [json.loads(line) for line in persisted.splitlines() if line]
    states = [record for record in records if record.get("_type") == "provider_state"]
    assert len(states) == 1, records
    return states[0]


def test_the_provider_state_line_holds_no_secret_from_a_pending_message(
    deployment: _Deployment,
) -> None:
    """The acceptance clause of #52, at the file bytes the operator reads back."""
    persisted = _saved_session_file(deployment.workspace, "websocket:state-1")

    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted


def test_the_provider_state_line_keeps_the_command_around_the_value(
    deployment: _Deployment,
) -> None:
    """A pending message scrubs value by value, so the replay keeps the rest of the command."""
    persisted = _saved_session_file(deployment.workspace, "websocket:state-2")

    assert "mysql --host=db1 --user=app -p" in persisted
    assert "--database=app" in persisted


def test_a_payload_with_no_stored_secret_survives_the_scrub(deployment: _Deployment) -> None:
    """A field #54 does not name, and a text that holds no secret, reach the file unchanged."""
    persisted = _saved_session_file(deployment.workspace, "websocket:state-3")

    assert _provider_state_record(persisted)["state"]["payload"] == OPAQUE_PAYLOAD


def test_a_scrubbed_thinking_block_in_a_pending_message_loses_its_signature(
    deployment: _Deployment,
) -> None:
    """A scrubbed block plus its old signature is a mismatched pair (#48), so the signature goes."""
    persisted = _saved_session_file(deployment.workspace, "websocket:state-4")

    assert SIGNATURE not in persisted
    assert "nanoinfra_scrubbed" in persisted


def test_the_agent_process_never_decrypts_a_secret_for_a_pending_message(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending messages take the #41 path, so this process builds no sentinel.

    ``resolve_plaintext`` raises here. The pending messages still persist scrubbed, so another
    address space held the sentinels.
    """

    def _refuse(self: SecretStore, secret_id: str) -> str | None:
        raise AssertionError("the agent process decrypted a secret")

    monkeypatch.setattr(SecretStore, "resolve_plaintext", _refuse)

    persisted = _saved_session_file(deployment.workspace, "websocket:state-5")

    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted


def test_the_state_still_reaches_the_provider_after_the_scrub(deployment: _Deployment) -> None:
    """A replay after a scrub continues the conversation, which is the cost #52 must not pay."""
    manager = SessionManager(deployment.workspace)
    key = "websocket:state-6"
    manager.save(
        Session(
            key=key,
            messages=[{"role": "user", "content": "check the database"}],
            provider_state=_state(),
        )
    )
    manager.invalidate(key)

    loaded = manager.get_or_create(key)
    assert loaded.provider_state is not None
    assert loaded.provider_state.kind == "openai_responses"
    assert loaded.provider_state.provider == PROVIDER
    assert loaded.provider_state.model == MODEL
    assert loaded.provider_state.version == 1
    assert loaded.provider_state.payload == OPAQUE_PAYLOAD

    provider = MagicMock(spec=LLMProvider)
    provider.can_resume_conversation_state.return_value = True
    controller = ProviderConversationStateController(
        provider=provider,
        model=MODEL,
        messages=loaded.messages,
        state=loaded.provider_state,
    )
    context = controller.prepare_request(loaded.messages, context_window_tokens=200_000)

    assert context is not None
    assert context.conversation_state is not None
    assert context.conversation_state.payload == OPAQUE_PAYLOAD
    replayed = context.conversation_state.pending_messages
    assert [message["role"] for message in replayed] == ["assistant", "tool"]
    replayed_text = json.dumps(replayed, ensure_ascii=False)
    assert SECRET_VALUE not in replayed_text
    assert SECRET_NAME in replayed_text
