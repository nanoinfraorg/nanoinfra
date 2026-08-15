# tests/session/test_responses_payload_session_file.py
"""nanoinfraorg/nanoinfra#54 end to end, against a real executor process.

#41, #48, #51 and #52 each closed one path into ``sessions/*.jsonl``. This module closes the
last one. #52 claimed the payload of a provider state "is a list of server-side identifiers
rather than text", and that claim is false: the Responses builders put message text straight
into it, and ``pending_messages`` is empty on that path, so #52 covered none of it.

The acceptance clause of #54 names the file rather than a function, so this module starts a real
executor child, saves one session whose Responses payload quotes a stored secret value, and
reads the bytes back from disk. It searches the whole file rather than one line.

A real child proves the #41 split as well. One test breaks ``resolve_plaintext`` in the pytest
process, the way ``tests/session/test_provider_state_session_file.py`` does, and the payload
still persists scrubbed. So another address space held the sentinels.

The connection count is a test of its own. A Responses payload holds one item per message plus
one per tool call, so the per-text wire of #41 would open one connection per item on every save.
The batch verb of #54 makes the same save cost one connection.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.redaction import TranscriptRedactor
from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.gates.executor.protocol import read_frame, write_frame
from nanoinfra.gates.executor.scrub_client import default_scrub_client
from nanoinfra.gates.executor.scrub_protocol import (
    ScrubBatchRequest,
    ScrubBatchResponse,
    ScrubResponse,
    decode_scrub_request_frame,
    default_scrub_socket_path,
    encode_scrub_batch_response,
    encode_scrub_response,
)
from nanoinfra.gates.executor.supervisor import start_executor
from nanoinfra.providers.base import ProviderConversationState
from nanoinfra.providers.openai_responses.state import (
    RESPONSES_STATE_KIND,
    build_responses_state,
    prepare_responses_input,
)
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.session.manager import JsonlSessionStore, Session, SessionManager

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"
SEAL = "gAAAAABo8Zk3-encrypted-reasoning"
MODEL = "gpt-5.6"
PROVIDER = "openai_compat:openai:https://api.openai.com/v1"

#: The command the model resolved. A resolved command routinely embeds a credential (#17).
RESOLVED_COMMAND = f"mysql --host=db1 --user=app -p{SECRET_VALUE} --database=app -e 'select 1'"


class _Deployment:
    """One executor child, plus the workspace whose secrets it can read."""

    def __init__(self, *, workspace: Path, root: Path, handle: Any) -> None:
        self.workspace = workspace
        self.root = root
        self.handle = handle


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory):
    """Start one real executor for this module, and stop it before the module ends."""
    patch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("responses_payload")
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
        yield _Deployment(workspace=workspace, root=root, handle=handle)
    finally:
        handle.stop(timeout_s=10)
        patch.undo()


def _payload_items() -> list[dict[str, Any]]:
    """Every named carrier of #54 at once, each one holding the resolved command."""
    return [
        {"role": "user", "content": [{"type": "input_text", "text": "check the database"}]},
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": SEAL,
            "summary": [{"type": "summary_text", "text": f"I will run {RESOLVED_COMMAND}"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "id": "msg_1",
            "content": [{"type": "output_text", "text": f"Running {RESOLVED_COMMAND} now"}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "execute_on_server",
            "arguments": json.dumps(
                {"server_id_or_name": "db1", "command": RESOLVED_COMMAND}, ensure_ascii=False
            ),
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": f"$ {RESOLVED_COMMAND}\n1 row in set",
        },
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": [{"type": "input_text", "text": f"cnf holds {SECRET_VALUE}"}],
        },
    ]


def _state(items: list[dict[str, Any]] | None = None) -> ProviderConversationState:
    return build_responses_state(
        provider=PROVIDER,
        model=MODEL,
        input_items=items if items is not None else _payload_items(),
        output_items=[],
        usage={"total_tokens": 4096},
    )


def _save(workspace: Path, key: str, state: ProviderConversationState) -> Path:
    session = Session(
        key=key,
        messages=[{"role": "user", "content": "check the database"}],
        provider_state=state,
    )
    SessionManager(workspace).save(session)
    return JsonlSessionStore(workspace).get_session_path(key)


def _provider_state_record(path: Path) -> dict[str, Any] | None:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    states = [record for record in records if record.get("_type") == "provider_state"]
    assert len(states) <= 1, records
    return states[0] if states else None


# -- the acceptance clause ---------------------------------------------------


def test_the_session_file_holds_no_plaintext_from_a_responses_payload(
    deployment: _Deployment,
) -> None:
    """The acceptance clause of #54, searched across the whole file at the bytes."""
    path = _save(deployment.workspace, "websocket:payload-1", _state())

    persisted = path.read_bytes()

    assert SECRET_VALUE.encode("utf-8") not in persisted
    assert SECRET_NAME.encode("utf-8") in persisted


def test_every_named_carrier_of_the_payload_scrubs(deployment: _Deployment) -> None:
    """One assertion per carrier, so a regression names the field it broke."""
    path = _save(deployment.workspace, "websocket:payload-2", _state())
    items = _provider_state_record(path)["state"]["payload"]["items"]  # type: ignore[index]
    by_index = {index: item for index, item in enumerate(items)}

    assert SECRET_VALUE not in by_index[1]["summary"][0]["text"]
    assert SECRET_VALUE not in by_index[2]["content"][0]["text"]
    assert SECRET_VALUE not in by_index[3]["arguments"]
    assert SECRET_VALUE not in by_index[4]["output"]
    assert SECRET_VALUE not in by_index[5]["output"][0]["text"]
    assert SECRET_NAME in by_index[3]["arguments"]


def test_the_payload_keeps_the_command_around_the_value(deployment: _Deployment) -> None:
    """A payload text scrubs value by value, so a reader still sees what the turn ran."""
    path = _save(deployment.workspace, "websocket:payload-3", _state())

    persisted = path.read_text(encoding="utf-8")

    assert "mysql --host=db1 --user=app -p" in persisted
    assert "--database=app" in persisted


def test_the_payload_keeps_every_identifier_it_carries(deployment: _Deployment) -> None:
    """An item id and a call id are the provider's own handles, so a replay still matches."""
    path = _save(deployment.workspace, "websocket:payload-4", _state())
    items = _provider_state_record(path)["state"]["payload"]["items"]  # type: ignore[index]

    assert [item.get("id") for item in items] == [None, "rs_1", "msg_1", "fc_1", None, None]
    assert items[3]["call_id"] == "call_1"
    assert items[4]["call_id"] == "call_1"
    assert items[5]["call_id"] == "call_2"


def test_a_changed_reasoning_item_reaches_the_file_with_no_seal(
    deployment: _Deployment,
) -> None:
    """The #48 rule on the field #54 found. A sealed copy of the removed text may not persist."""
    path = _save(deployment.workspace, "websocket:payload-5", _state())

    persisted = path.read_text(encoding="utf-8")

    assert SEAL not in persisted
    items = _provider_state_record(path)["state"]["payload"]["items"]  # type: ignore[index]
    assert "encrypted_content" not in items[1]
    assert items[1]["type"] == "reasoning"


def test_an_unchanged_reasoning_item_keeps_its_seal(deployment: _Deployment) -> None:
    """A turn whose reasoning held no secret replays exactly as it does today."""
    items = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": SEAL,
            "summary": [{"type": "summary_text", "text": "I read the row count"}],
        }
    ]
    path = _save(deployment.workspace, "websocket:payload-6", _state(items))

    assert SEAL in path.read_text(encoding="utf-8")


def test_the_persisted_payload_carries_no_marker_key(deployment: _Deployment) -> None:
    """The payload is request input, so a key the API does not define would fail the next turn."""
    path = _save(deployment.workspace, "websocket:payload-7", _state())

    assert "nanoinfra_scrubbed" not in path.read_text(encoding="utf-8")


def test_the_agent_process_never_decrypts_a_secret_for_a_payload(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#41's rule: the agent process holds no sentinel.

    ``resolve_plaintext`` raises in this process. The payload still persists scrubbed, so
    another address space held the sentinels.
    """

    def _refuse(self: SecretStore, secret_id: str) -> str | None:
        raise AssertionError("the agent process decrypted a secret")

    monkeypatch.setattr(SecretStore, "resolve_plaintext", _refuse)

    path = _save(deployment.workspace, "websocket:payload-8", _state())

    assert SECRET_VALUE not in path.read_text(encoding="utf-8")
    assert SECRET_NAME in path.read_text(encoding="utf-8")


# -- the replay --------------------------------------------------------------


def test_the_state_round_trips_and_still_builds_a_request(deployment: _Deployment) -> None:
    """A replay after the scrub must still reach the provider, or #54 broke the conversation."""
    manager = SessionManager(deployment.workspace)
    key = "websocket:payload-9"
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
    assert loaded.provider_state.kind == RESPONSES_STATE_KIND
    assert loaded.provider_state.provider == PROVIDER
    assert loaded.provider_state.model == MODEL

    instructions, items, replayed = prepare_responses_input(
        loaded.messages,
        state=loaded.provider_state,
        provider=PROVIDER,
        model=MODEL,
    )

    assert replayed is True
    assert len(items) == len(_payload_items())
    request_text = json.dumps(items, ensure_ascii=False)
    assert SECRET_VALUE not in request_text
    assert SECRET_NAME in request_text
    assert instructions == ""


# -- fail closed -------------------------------------------------------------


def test_a_scrub_that_cannot_run_persists_no_state(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A marker is wrong here, because a provider state is replay input.

    So the line goes and the session replays from its message history instead.
    """
    # No executor answers on this path, so every scrub attempt fails.
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(tmp_path / "absent" / "e.sock"))

    path = _save(deployment.workspace, "websocket:payload-10", _state())

    assert _provider_state_record(path) is None
    assert SECRET_VALUE not in path.read_text(encoding="utf-8")


def test_the_session_still_loads_with_no_provider_state_line(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-closed costs a provider-side cache and never the session."""
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(tmp_path / "absent" / "e.sock"))
    manager = SessionManager(deployment.workspace)
    key = "websocket:payload-11"
    manager.save(
        Session(
            key=key,
            messages=[{"role": "user", "content": "check the database"}],
            provider_state=_state(),
        )
    )
    manager.invalidate(key)

    loaded = manager.get_or_create(key)

    assert loaded.provider_state is None
    assert [message["content"] for message in loaded.messages] == ["check the database"]


# -- byte identity -----------------------------------------------------------


def test_a_workspace_with_no_secret_writes_the_bytes_it_writes_today(tmp_path: Path) -> None:
    """Item 6 of #54, pinned against the file bytes.

    This workspace stores no secret, so nothing could resolve a value that a scrub would remove.
    The expected line is the record ``to_private_record`` produces with no scrub at all, which is
    exactly what this file held before #54.
    """
    workspace = tmp_path / "clean"
    workspace.mkdir()
    state = _state()
    expected = json.dumps(
        {"_type": "provider_state", "state": state.to_private_record()}, ensure_ascii=False
    )

    path = _save(workspace, "websocket:clean-1", state)

    assert expected in path.read_text(encoding="utf-8").splitlines()


# -- the connection count ----------------------------------------------------


class _CountingScrubber:
    """A scrub socket a test drives. It answers either verb and counts the connections."""

    def __init__(self, socket_path: Path) -> None:
        self.connections = 0
        self.texts = 0
        self._stop = threading.Event()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # A short accept deadline, so the stop flag ends this thread. A close from another
        # thread does not always wake a blocked accept.
        self._listener.settimeout(0.05)
        self._listener.bind(str(socket_path))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            with conn:
                try:
                    request = decode_scrub_request_frame(read_frame(conn))
                except Exception:  # noqa: BLE001 -- a test double answers nothing here
                    return
                if not isinstance(request, ScrubBatchRequest):
                    self.texts += 1
                    write_frame(
                        conn,
                        encode_scrub_response(
                            ScrubResponse(ok=True, text=request.text, error=None)
                        ),
                    )
                    continue
                self.texts += len(request.items)
                write_frame(
                    conn,
                    encode_scrub_batch_response(
                        ScrubBatchResponse(
                            ok=True,
                            texts=[item.text for item in request.items],
                            error=None,
                        )
                    ),
                )

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self._listener.close()


@pytest.fixture()
def counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A workspace with a stored secret, and a scrub socket that counts what reaches it."""
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)
    workspace = tmp_path / "counted"
    workspace.mkdir()
    SecretStore(workspace).create(
        {"name": SECRET_NAME, "kind": "password", "providerId": "local", "value": SECRET_VALUE}
    )
    execute_socket = tmp_path / "run" / "e.sock"
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(execute_socket))
    scrubber = _CountingScrubber(default_scrub_socket_path(execute_socket))
    try:
        yield workspace, scrubber
    finally:
        scrubber.close()


def _many_item_state() -> ProviderConversationState:
    """A transcript long enough that one connection per item would be visible."""
    items: list[dict[str, Any]] = []
    for index in range(40):
        items.append(
            {
                "type": "function_call",
                "id": f"fc_{index}",
                "call_id": f"call_{index}",
                "name": "execute_on_server",
                "arguments": json.dumps({"command": f"uptime {index}"}, ensure_ascii=False),
            }
        )
        items.append(
            {
                "type": "function_call_output",
                "call_id": f"call_{index}",
                "output": f"load average {index}",
            }
        )
    return _state(items)


def test_a_save_of_a_long_transcript_opens_one_connection(counted: Any) -> None:
    """The acceptance clause of #54: one connection, not one per item."""
    workspace, scrubber = counted

    _save(workspace, "websocket:count-1", _many_item_state())

    assert scrubber.texts == 80
    assert scrubber.connections == 1


def test_the_per_text_wire_would_open_one_connection_per_text(counted: Any) -> None:
    """The cost #54 removes, measured with the same fixture.

    ``TranscriptRedactor`` with no batch scrubber is the #41 wire. It asks once per text, and the
    count is the reason the batch verb had to land before the walk.
    """
    workspace, scrubber = counted
    from nanoinfra.providers.openai_responses.redaction import scrub_responses_payload

    state = _many_item_state()
    redactor = TranscriptRedactor(default_scrub_client().scrub)
    redactor.in_one_batch(lambda scrub: scrub_responses_payload(state.payload, scrub))

    _ = workspace
    assert scrubber.texts == 80
    assert scrubber.connections == 80
