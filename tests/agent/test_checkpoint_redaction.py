# tests/agent/test_checkpoint_redaction.py
"""nanoinfraorg/nanoinfra#51: the runtime checkpoint holds no stored secret value.

#41 moved the transcript scrub into the executor, and #48 extended it to the reasoning fields.
Both act on the message record. The runtime checkpoint takes a second path into the same file,
and no redactor sat on it.

``AgentRunner`` emits a checkpoint on every turn that runs a tool. ``AgentLoop._checkpoint``
hands the payload to ``_set_runtime_checkpoint``, which writes it into ``session.metadata``. The
payload holds the resolved arguments of a pending tool call, the output of a completed tool, and
the reasoning of the assistant message. So a turn that the message path scrubbed correctly wrote
the same plaintext one line earlier in the same file.

The assertions read the session file. #51 names the file rather than the function, so an
in-memory object proves too little: the metadata line is the artifact an operator reads back.

Most tests here cross a real scrub socket through the ``scrub_service`` fixture, which runs the
executor's own answer path in a thread. One test starts a real executor child instead, the way
``tests/session/test_reasoning_session_file.py`` does for #48. That technique applies here for
the same reason: it breaks ``resolve_plaintext`` in the agent process, so a scrub that still
works proves the sentinels live in another address space (#41).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import pytest

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.redaction import (
    REASONING_SCRUB_MARKER_KEY,
    SecretSentinel,
    TranscriptRedactor,
    scrub_one_text,
)
from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.bus.queue import MessageBus
from nanoinfra.gates.executor.scrub_protocol import default_scrub_socket_path
from nanoinfra.gates.executor.supervisor import start_executor
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.session.manager import JsonlSessionStore

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"
SIGNATURE = "the-signature-the-provider-issued"
SESSION_KEY = "websocket:chat-1"

#: The one string #17 exists for: a command the agent resolved, with the credential inside it.
RESOLVED_COMMAND = f"mysql -h db1 -p{SECRET_VALUE} -e 'select 1'"

#: The ``scrub_service`` factory of ``tests/agent/conftest.py``. ``Any``, because one test
#: reads the request count off the service the factory returns.
_Scrubber = Callable[[Path], Any]


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)


def _stored_secret(workspace: Path) -> str:
    """Create one local secret, and return its name."""
    secret = SecretStore(workspace).create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )
    return secret.name


def _loop(workspace: Path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(), provider=MagicMock(), workspace=workspace, model="test-model"
    )


def _checkpoint_with_a_secret() -> dict[str, Any]:
    """The payload ``runner.py:624`` emits after a tool ran, with a credential in it.

    Every field #51 names is present: the reasoning of the assistant message, the output of a
    completed tool, and the resolved arguments of a tool call the turn had not finished.
    """
    reasoning = f"I connect with {RESOLVED_COMMAND} and then I read the table."
    return {
        "phase": "tools_completed",
        "iteration": 2,
        "model": "test-model",
        "assistant_message": {
            "role": "assistant",
            "content": "the table has 4 rows",
            "reasoning_content": reasoning,
            "thinking_blocks": [
                {"type": "thinking", "thinking": reasoning, "signature": SIGNATURE}
            ],
            "tool_calls": [
                {
                    "id": "call_done",
                    "type": "function",
                    "function": {
                        "name": "server_exec",
                        "arguments": json.dumps({"command": RESOLVED_COMMAND}),
                    },
                }
            ],
        },
        "completed_tool_results": [
            {
                "role": "tool",
                "tool_call_id": "call_done",
                "name": "server_exec",
                "content": f"$ {RESOLVED_COMMAND}\n1 row",
            }
        ],
        "pending_tool_calls": [
            {
                "id": "call_pending",
                "type": "function",
                "function": {
                    "name": "server_exec",
                    "arguments": json.dumps({"command": RESOLVED_COMMAND}),
                },
            }
        ],
    }


def _checkpoint_with_no_secret() -> dict[str, Any]:
    """The same shape, from a turn that resolved nothing. This one must not change."""
    return {
        "phase": "tools_completed",
        "iteration": 2,
        "model": "test-model",
        "assistant_message": {
            "role": "assistant",
            "content": "nginx is running",
            "reasoning_content": "I check the service on web1.",
            "thinking_blocks": [
                {
                    "type": "thinking",
                    "thinking": "I check the service on web1.",
                    "signature": SIGNATURE,
                }
            ],
        },
        "completed_tool_results": [
            {
                "role": "tool",
                "tool_call_id": "call_done",
                "name": "server_exec",
                "content": "active (running)",
            }
        ],
        "pending_tool_calls": [],
    }


def _saved_checkpoint(
    workspace: Path, payload: dict[str, Any], *, key: str = SESSION_KEY
) -> bytes:
    """Persist one checkpoint through the loop, and return the session file bytes."""
    loop = _loop(workspace)
    session = loop.sessions.get_or_create(key)
    loop._set_runtime_checkpoint(session, payload)
    return JsonlSessionStore(workspace).get_session_path(key).read_bytes()


def _metadata_line(raw: bytes) -> str:
    """The one line of the session file that carries ``session.metadata``."""
    for line in raw.decode("utf-8").splitlines():
        if line.strip() and json.loads(line).get("_type") == "metadata":
            return line
    raise AssertionError("the session file holds no metadata record")


def _stored_checkpoint(raw: bytes) -> dict[str, Any]:
    """The checkpoint as the file holds it, read back the way ``load`` reads it."""
    metadata: dict[str, Any] = json.loads(_metadata_line(raw))["metadata"]
    return metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY]


# -- the three fields the payload carries -----------------------------------


def test_a_resolved_command_in_a_pending_tool_call_never_reaches_the_file(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The case #17 exists for, on the path #51 found unguarded."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    assert SECRET_VALUE.encode("utf-8") not in raw
    calls = _stored_checkpoint(raw)["pending_tool_calls"]
    assert SECRET_NAME in calls[0]["function"]["arguments"]


def test_a_pending_tool_call_keeps_arguments_a_provider_can_parse(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The restore path replays these arguments, so they must stay JSON."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    arguments = _stored_checkpoint(raw)["pending_tool_calls"][0]["function"]["arguments"]
    assert isinstance(json.loads(arguments), dict)


def test_the_output_of_a_completed_tool_never_reaches_the_file(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """Remote output is the widest route for a credential into a durable file (#17)."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    assert SECRET_VALUE.encode("utf-8") not in raw
    result = _stored_checkpoint(raw)["completed_tool_results"][0]
    assert SECRET_NAME in result["content"]
    assert result["tool_call_id"] == "call_done"


def test_the_reasoning_content_of_the_checkpoint_never_reaches_the_file(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """#48 covered this field on the message path. The checkpoint held it as well."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    assert SECRET_VALUE.encode("utf-8") not in raw
    reasoning = _stored_checkpoint(raw)["assistant_message"]["reasoning_content"]
    assert SECRET_NAME in reasoning
    assert "and then I read the table." in reasoning


def test_a_thinking_block_of_the_checkpoint_never_reaches_the_file(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The field an Anthropic turn carries, on the second path into the same file."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    block = _stored_checkpoint(raw)["assistant_message"]["thinking_blocks"][0]
    assert SECRET_VALUE.encode("utf-8") not in raw
    assert SECRET_NAME in block["thinking"]


def test_a_scrubbed_thinking_block_in_the_checkpoint_loses_its_signature(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The #48 signature rule reaches this path too, through ``_unsigned_thinking_block``."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    block = _stored_checkpoint(raw)["assistant_message"]["thinking_blocks"][0]
    assert SIGNATURE.encode("utf-8") not in raw
    assert "signature" not in block
    assert "signature" in str(block[REASONING_SCRUB_MARKER_KEY])


def test_the_scrub_never_mutates_the_payload_of_the_live_turn(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The runner holds these dicts in its own message list, and the turn is still running."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    payload = _checkpoint_with_a_secret()

    _saved_checkpoint(tmp_path, payload)

    assert SECRET_VALUE in json.dumps(payload)
    assert payload["assistant_message"]["thinking_blocks"][0]["signature"] == SIGNATURE


# -- a restart in the middle of that turn -----------------------------------


def test_a_restart_mid_turn_restores_a_scrubbed_record(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The acceptance clause of #51 for the restore path.

    ``_restore_runtime_checkpoint`` materialises the payload into session history, so an
    unscrubbed checkpoint would put the plaintext back into the transcript a human reads. The
    record restores as itself, and it is already scrubbed, so no second scrub runs here.
    """
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    loop = _loop(tmp_path)
    session = loop.sessions.get_or_create(SESSION_KEY)
    loop._set_runtime_checkpoint(session, _checkpoint_with_a_secret())

    loop.sessions.invalidate(SESSION_KEY)
    restarted = loop.sessions.get_or_create(SESSION_KEY)
    assert loop._restore_runtime_checkpoint(restarted) is True

    restored = json.dumps(restarted.messages)
    assert SECRET_VALUE not in restored
    assert SECRET_NAME in restored
    assert SIGNATURE not in restored


# -- a turn that held no secret ---------------------------------------------


def test_a_checkpoint_that_held_no_secret_writes_the_same_bytes(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The pin #51 asks for. A scrub that changed nothing must change no byte.

    The expected text is the JSON of the metadata this loop would have written before #51,
    minus the outer braces, so the assertion reads the bytes of the file rather than an
    object the file was built from.
    """
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    payload = _checkpoint_with_no_secret()

    raw = _saved_checkpoint(tmp_path, payload)

    expected = json.dumps(
        {AgentLoop._RUNTIME_CHECKPOINT_KEY: payload}, ensure_ascii=False
    )[1:-1]
    assert expected.encode("utf-8") in raw


def test_a_checkpoint_that_held_no_secret_keeps_every_key(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """No marker, no dropped signature, and the phase fields untouched."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    payload = _checkpoint_with_no_secret()

    raw = _saved_checkpoint(tmp_path, payload)

    assert _stored_checkpoint(raw) == payload


def test_a_workspace_with_no_secret_asks_the_executor_nothing(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The common case stays cheap, which is the #41 rule for the message path too.

    A checkpoint lands on every turn that runs a tool, so the count is the test.
    """
    service = scrub_service(tmp_path)

    _saved_checkpoint(tmp_path, _checkpoint_with_no_secret())

    assert service.requests == 0


def test_a_workspace_with_no_secret_writes_the_same_bytes(tmp_path: Path) -> None:
    """The same pin for the common case, where no executor runs and none is needed.

    This workspace stored no secret, so the same payload holds no secret value in it. A
    sentinel comes from the store, and redaction is best-effort about a value the process
    never stored. The point of the test is the byte count of the change: zero.
    """
    payload = _checkpoint_with_a_secret()

    raw = _saved_checkpoint(tmp_path, payload)

    expected = json.dumps(
        {AgentLoop._RUNTIME_CHECKPOINT_KEY: payload}, ensure_ascii=False
    )[1:-1]
    assert expected.encode("utf-8") in raw


# -- a scrub that cannot run ------------------------------------------------


def test_no_executor_withholds_the_whole_checkpoint(tmp_path: Path) -> None:
    """Fail closed, which is the rule #41 set for the rest of the transcript.

    No scrub service runs here, so the socket client raises. The payload must reach the file
    as markers, and never as the text the turn produced.
    """
    _stored_secret(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    assert SECRET_VALUE.encode("utf-8") not in raw
    checkpoint = _stored_checkpoint(raw)
    assert "withheld" in checkpoint["assistant_message"]["reasoning_content"]
    assert "withheld" in checkpoint["completed_tool_results"][0]["content"]
    arguments = checkpoint["pending_tool_calls"][0]["function"]["arguments"]
    assert "withheld" in json.loads(arguments)


def test_a_withheld_checkpoint_keeps_the_shape_the_restore_path_reads(
    tmp_path: Path,
) -> None:
    """The restore path reads the phase, the ids, and the tool names, and none holds a value."""
    _stored_secret(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    checkpoint = _stored_checkpoint(raw)
    assert checkpoint["phase"] == "tools_completed"
    assert checkpoint["pending_tool_calls"][0]["id"] == "call_pending"
    assert checkpoint["completed_tool_results"][0]["name"] == "server_exec"


def test_a_withheld_thinking_block_in_the_checkpoint_loses_its_signature(
    tmp_path: Path,
) -> None:
    """The marker replaced the text, so the signature no longer matches it (#48)."""
    _stored_secret(tmp_path)

    raw = _saved_checkpoint(tmp_path, _checkpoint_with_a_secret())

    block = _stored_checkpoint(raw)["assistant_message"]["thinking_blocks"][0]
    assert SIGNATURE.encode("utf-8") not in raw
    assert "signature" not in block
    assert REASONING_SCRUB_MARKER_KEY in block


def test_a_scrub_that_raises_withholds_and_never_returns_the_raw_payload() -> None:
    """The unit behind the two tests above, with the failure forced rather than arranged."""

    def _explode(text: str, capability_class: str | None) -> str:
        raise RuntimeError("the scrub socket is gone")

    redactor = TranscriptRedactor(_explode)

    persisted = redactor.checkpoint(_checkpoint_with_a_secret())

    assert SECRET_VALUE not in json.dumps(persisted)
    assert "the scrub socket is gone" in json.dumps(persisted)


def test_one_failure_withholds_every_field_of_the_checkpoint() -> None:
    """A partial answer would mix a scrubbed field with an unscrubbed one.

    The scrub answers the assistant message and then raises. The whole record must go, the
    same way ``TranscriptRedactor.messages`` withholds a whole list on one failure.
    """
    sentinels = [SecretSentinel(name=SECRET_NAME, value=SECRET_VALUE)]
    answered = 0

    def _one_answer_then_fail(text: str, capability_class: str | None) -> str:
        nonlocal answered
        answered += 1
        if answered > 1:
            raise RuntimeError("the scrub socket closed mid record")
        return scrub_one_text(text, capability_class, sentinels)

    persisted = TranscriptRedactor(_one_answer_then_fail).checkpoint(
        _checkpoint_with_a_secret()
    )

    assert SECRET_VALUE not in json.dumps(persisted)
    assert "withheld" in persisted["assistant_message"]["content"]


# -- the agent process decrypts nothing -------------------------------------


class _Deployment:
    """One real executor child, plus the workspace whose secrets it can read."""

    def __init__(self, *, workspace: Path, handle: Any) -> None:
        self.workspace = workspace
        self.handle = handle


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Deployment]:
    """Start a real executor, so the sentinels live in another address space.

    The fixture is not autouse, so it runs after the autouse fixture that points
    ``EXECUTOR_SOCKET_ENV`` at an unused path, and its own value wins.
    """
    home = tmp_path / "home"
    (home / ".nanoinfra").mkdir(parents=True)
    (home / ".nanoinfra" / "config.json").write_text("{}", encoding="utf-8")
    # The child is a separate process, so HOME places its config and its audit root.
    monkeypatch.setenv("HOME", str(home))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _stored_secret(workspace)

    execute_socket = tmp_path / "r" / "e.sock"
    # The agent derives the scrub socket from this path, the way a deployment names it.
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(execute_socket))

    handle = start_executor(socket_path=execute_socket, workspace=workspace, timeout_s=30.0)
    try:
        assert handle.is_running(), handle.read_log_tail(tail=20)
        assert default_scrub_socket_path(execute_socket).exists(), handle.read_log_tail(tail=20)
        yield _Deployment(workspace=workspace, handle=handle)
    finally:
        handle.stop(timeout_s=10)


def test_the_agent_process_never_decrypts_a_secret_for_the_checkpoint(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint takes the #41 path, so this process builds no sentinel.

    ``resolve_plaintext`` raises here. The checkpoint still persists scrubbed, so the process
    that held the plaintext was the child.
    """

    def _refuse(self: SecretStore, secret_id: str) -> str | None:
        raise AssertionError("the agent process decrypted a secret")

    monkeypatch.setattr(SecretStore, "resolve_plaintext", _refuse)

    raw = _saved_checkpoint(deployment.workspace, _checkpoint_with_a_secret())

    assert SECRET_VALUE.encode("utf-8") not in raw
    assert SECRET_NAME.encode("utf-8") in raw


# -- the subagent checkpoint callback ---------------------------------------


def _subagent_checkpoint_callback() -> ast.AsyncFunctionDef:
    """The callback ``SubagentManager._run_subagent`` hands to the runner, as a syntax tree.

    A syntax tree rather than a call, because the claim is about which fields the callback
    reads. ``tests/agent/test_redaction_isolation.py`` reads the #41 rule the same way.
    """
    source = Path("nanoinfra/agent/subagent.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_checkpoint":
            return node
    raise AssertionError("nanoinfra/agent/subagent.py holds no _on_checkpoint callback")


def test_the_subagent_checkpoint_callback_reads_two_fields_that_hold_no_text() -> None:
    """#51 item 6: that callback reaches no store, and this pins the reason.

    It copies a phase, which the runner picks from a fixed set of words, and an iteration,
    which is a number. Both land on an in-memory ``SubagentStatus`` that the manager drops
    when the task ends. So no text that could hold a credential leaves the payload there, and
    the callback needs no redactor of its own. A field added later that reads a text field
    fails this test.
    """
    callback = _subagent_checkpoint_callback()

    read_keys = {
        node.args[0].value
        for node in ast.walk(callback)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "payload"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    payload_uses = [
        node for node in ast.walk(callback) if isinstance(node, ast.Name) and node.id == "payload"
    ]

    assert read_keys == {"phase", "iteration"}
    # One use per key. So the callback reads those two fields and nothing else from the payload.
    assert len(payload_uses) == len(read_keys)
