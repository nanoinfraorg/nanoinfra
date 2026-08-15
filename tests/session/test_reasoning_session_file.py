# tests/session/test_reasoning_session_file.py
"""nanoinfraorg/nanoinfra#48 end to end, against a real executor process.

The acceptance clause of #48 names the file, and not the function. So this module starts a real
executor child, saves one turn whose reasoning quotes a stored secret value, and reads
``sessions/*.jsonl`` back from disk.

A real child proves the split as well. The sentinels live in the executor, and the agent sends
one text over a socket. The persisted reasoning then holds the secret name and never the value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.gates.executor.scrub_protocol import default_scrub_socket_path
from nanoinfra.gates.executor.supervisor import start_executor
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.session.manager import JsonlSessionStore, Session, SessionManager

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"
SIGNATURE = "the-signature-the-provider-issued"
SESSION_KEY = "websocket:chat-1"


class _Deployment:
    """One executor child, plus the workspace whose secrets it can read."""

    def __init__(self, *, workspace: Path, handle: Any) -> None:
        self.workspace = workspace
        self.handle = handle


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory):
    """Start one real executor for this module, and stop it before the module ends."""
    patch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("reasoning")
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


def _reasoning_turn() -> list[dict[str, Any]]:
    """The turn #48 describes. The model wrote the resolved command in its reasoning."""
    reasoning = f"I connect with mysql -p{SECRET_VALUE} and then I read the table."
    return [
        {"role": "user", "content": "check the database"},
        {
            "role": "assistant",
            "content": "the table has 4 rows",
            "reasoning_content": reasoning,
            "thinking_blocks": [
                {"type": "thinking", "thinking": reasoning, "signature": SIGNATURE}
            ],
        },
    ]


def _loop(workspace: Path) -> AgentLoop:
    """The save stage of one loop, with the two fields it reads.

    ``tests/agent/test_loop_save_turn.py`` builds a loop this way. A full loop registers every
    tool, and this module exercises the save stage rather than the registry. The real loop that
    scrubs its reasoning lives in ``tests/agent/test_reasoning_redaction.py``.
    """
    from nanoinfra.config.schema import AgentDefaults

    loop = AgentLoop.__new__(AgentLoop)
    loop.max_tool_result_chars = AgentDefaults().max_tool_result_chars
    loop.workspace = workspace
    return loop


def _saved_session_file(workspace: Path, key: str) -> str:
    """Save one reasoning turn through the loop, and return the session file text."""
    session = Session(key=key, messages=[])
    _loop(workspace)._save_turn(session, _reasoning_turn(), 0)
    SessionManager(workspace).save(session)
    return JsonlSessionStore(workspace).get_session_path(key).read_text(encoding="utf-8")


def test_the_session_file_holds_no_secret_value_from_the_reasoning(
    deployment: _Deployment,
) -> None:
    """The acceptance clause of #48, at the file the operator reads back."""
    persisted = _saved_session_file(deployment.workspace, SESSION_KEY)

    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted


def test_the_session_file_keeps_the_reasoning_around_the_value(
    deployment: _Deployment,
) -> None:
    """Reasoning scrubs value by value, so an operator still reads what the turn planned."""
    persisted = _saved_session_file(deployment.workspace, "websocket:chat-2")

    assert "I connect with mysql -p" in persisted
    assert "and then I read the table." in persisted


def test_the_session_file_holds_no_signature_for_a_scrubbed_block(
    deployment: _Deployment,
) -> None:
    """A scrubbed block plus its old signature is a mismatched pair, so the signature goes."""
    persisted = _saved_session_file(deployment.workspace, "websocket:chat-3")

    assert SIGNATURE not in persisted
    assert "nanoinfra_scrubbed" in persisted


def test_the_agent_process_never_decrypts_a_secret_for_the_reasoning(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reasoning text takes the #41 path, so this process builds no sentinel.

    ``resolve_plaintext`` raises here. The reasoning still persists scrubbed, so another
    address space held the sentinels.
    """

    def _refuse(self: SecretStore, secret_id: str) -> str | None:
        raise AssertionError("the agent process decrypted a secret")

    monkeypatch.setattr(SecretStore, "resolve_plaintext", _refuse)

    persisted = _saved_session_file(deployment.workspace, "websocket:chat-4")

    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted
