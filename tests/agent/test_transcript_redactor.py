# tests/agent/test_transcript_redactor.py
"""Item 39 (#41): the agent asks the executor, and it withholds what nobody scrubbed.

Three rules meet in this module.

The agent holds no sentinel, so it sends each text to the executor and reads the answer back.
A workspace with no secret asks nothing, so the common case costs no round trip. And a scrub
that cannot run withholds the text, because the old code returned an empty sentinel list on
every failure and the caller then persisted the text unscrubbed. That is fail open on the one
path #17 exists to close.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.redaction import (
    TranscriptRedactor,
    workspace_may_hold_a_secret,
)
from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS
from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.gates.executor.protocol import read_frame, write_frame
from nanoinfra.gates.executor.scrub_protocol import (
    ScrubResponse,
    decode_scrub_request,
    default_scrub_socket_path,
    encode_scrub_response,
)
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"


@pytest.fixture(autouse=True)
def _secrets_and_socket_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Keep every socket this module names inside tmp_path.

    Without this the client would resolve the operator's own run directory, and a test would
    reach the executor of the workstation it runs on.
    """
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)
    execute_socket = tmp_path / "run" / "e.sock"
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(execute_socket))
    return execute_socket


def _stored_secret(workspace: Path) -> None:
    SecretStore(workspace).create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )


class _CountingScrubber:
    """A scrub socket a test drives. It counts the connections it accepted."""

    def __init__(self, socket_path: Path, *, answer: ScrubResponse | None = None) -> None:
        self.connections = 0
        self.seen: list[tuple[str, str]] = []
        self._answer = answer
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
                    request = decode_scrub_request(read_frame(conn))
                except Exception:  # noqa: BLE001 -- a test double answers nothing here
                    return
                self.seen.append((request.text, request.capability_class))
                answer = self._answer or ScrubResponse(
                    ok=True, text=f"scrubbed:{request.text}", error=None
                )
                write_frame(conn, encode_scrub_response(answer))

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self._listener.close()


def _tool_message(content: str, name: str = "execute_on_server") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "call_1", "name": name, "content": content}


# -- the cheap common case --------------------------------------------------


def test_a_workspace_with_no_secret_needs_no_round_trip(tmp_path: Path) -> None:
    """The common case must stay cheap, so the count is the test."""
    scrubber = _CountingScrubber(default_scrub_socket_path(tmp_path / "run" / "e.sock"))
    try:
        redactor = TranscriptRedactor.for_workspace(tmp_path)
        result = redactor.messages([_tool_message("exit code 0")])
    finally:
        scrubber.close()

    assert scrubber.connections == 0
    assert result[0]["content"] == "exit code 0"


def test_a_workspace_with_a_secret_asks_the_executor(tmp_path: Path) -> None:
    _stored_secret(tmp_path)
    scrubber = _CountingScrubber(default_scrub_socket_path(tmp_path / "run" / "e.sock"))
    try:
        redactor = TranscriptRedactor.for_workspace(tmp_path)
        result = redactor.messages([_tool_message(f"used {SECRET_VALUE}")])
    finally:
        scrubber.close()

    assert scrubber.connections == 1
    assert result[0]["content"] == f"scrubbed:used {SECRET_VALUE}"


def test_the_request_carries_the_capability_class(tmp_path: Path) -> None:
    """#17 drops a ``credential.access`` result whole, so the class rides with the text."""
    _stored_secret(tmp_path)
    scrubber = _CountingScrubber(default_scrub_socket_path(tmp_path / "run" / "e.sock"))
    try:
        TranscriptRedactor.for_workspace(tmp_path).messages(
            [_tool_message("value", name="read_secret")],
            capability_of=lambda _name: CREDENTIAL_ACCESS,
        )
    finally:
        scrubber.close()

    assert scrubber.seen == [("value", CREDENTIAL_ACCESS)]


def test_a_postgres_backend_counts_as_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared backend holds the secrets that the workspace directory does not."""
    monkeypatch.setenv("NANOINFRA_SECRETS_POSTGRES_DSN", "postgresql://example")

    assert workspace_may_hold_a_secret(tmp_path)


def test_an_empty_workspace_holds_no_secret(tmp_path: Path) -> None:
    assert not workspace_may_hold_a_secret(tmp_path)


def test_a_stored_secret_makes_the_workspace_ask(tmp_path: Path) -> None:
    """The check reads the store's own layout, so a store that moves fails this test."""
    _stored_secret(tmp_path)

    assert workspace_may_hold_a_secret(tmp_path)
    assert SecretStore(tmp_path).root == tmp_path / "secrets"


# -- a scrub that cannot run ------------------------------------------------


def test_no_scrubber_withholds_a_tool_result(tmp_path: Path) -> None:
    """The defect this item closes. The old code persisted the text unscrubbed."""
    _stored_secret(tmp_path)

    result = TranscriptRedactor.for_workspace(tmp_path).messages(
        [_tool_message(f"used {SECRET_VALUE}")]
    )

    assert SECRET_VALUE not in json.dumps(result)
    assert "withheld" in str(result[0]["content"])


def test_the_marker_says_why_and_names_the_socket(tmp_path: Path) -> None:
    """An operator reads this line months later, so it carries its own diagnosis."""
    _stored_secret(tmp_path)

    content = str(
        TranscriptRedactor.for_workspace(tmp_path).messages(
            [_tool_message(f"used {SECRET_VALUE}")]
        )[0]["content"]
    )

    assert "executor" in content
    assert "e.scrub.sock" in content
    assert "\n" not in content


def test_an_error_answer_also_withholds(tmp_path: Path) -> None:
    """A reachable executor that refuses is still a scrub that did not run."""
    _stored_secret(tmp_path)
    scrubber = _CountingScrubber(
        default_scrub_socket_path(tmp_path / "run" / "e.sock"),
        answer=ScrubResponse(ok=False, text="", error="the secret store is broken"),
    )
    try:
        result = TranscriptRedactor.for_workspace(tmp_path).messages(
            [_tool_message(f"used {SECRET_VALUE}")]
        )
    finally:
        scrubber.close()

    assert SECRET_VALUE not in json.dumps(result)
    assert "the secret store is broken" in str(result[0]["content"])


def test_a_withheld_message_keeps_its_structure(tmp_path: Path) -> None:
    """A transcript stays readable and replayable. Only the text goes."""
    _stored_secret(tmp_path)

    result = TranscriptRedactor.for_workspace(tmp_path).messages(
        [_tool_message(f"used {SECRET_VALUE}")]
    )[0]

    assert result["role"] == "tool"
    assert result["name"] == "execute_on_server"
    assert result["tool_call_id"] == "call_1"


def test_a_withheld_tool_call_keeps_parseable_arguments(tmp_path: Path) -> None:
    """Session history replays to a provider, so the arguments stay valid JSON."""
    _stored_secret(tmp_path)
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "execute_on_server",
                    "arguments": json.dumps({"command": f"mysql -p{SECRET_VALUE}"}),
                },
            }
        ],
    }

    result = TranscriptRedactor.for_workspace(tmp_path).messages([message])[0]

    calls: Any = result["tool_calls"]
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert SECRET_VALUE not in json.dumps(result)
    assert calls[0]["function"]["name"] == "execute_on_server"
    assert "withheld" in json.dumps(arguments)


def test_no_scrubber_withholds_a_mapping_value(tmp_path: Path) -> None:
    _stored_secret(tmp_path)

    result = TranscriptRedactor.for_workspace(tmp_path).mapping(
        {"event": "tool_result", "text": f"used {SECRET_VALUE}"}
    )

    assert SECRET_VALUE not in json.dumps(result)
    assert "withheld" in str(result["text"])


def test_no_scrubber_withholds_one_text(tmp_path: Path) -> None:
    _stored_secret(tmp_path)

    result = TranscriptRedactor.for_workspace(tmp_path).text(f"used {SECRET_VALUE}")

    assert SECRET_VALUE not in result
    assert "withheld" in result


def test_the_input_is_never_mutated(tmp_path: Path) -> None:
    """The live turn keeps the real values, whatever the scrub answered."""
    _stored_secret(tmp_path)
    message = _tool_message(f"used {SECRET_VALUE}")

    TranscriptRedactor.for_workspace(tmp_path).messages([message])

    assert message["content"] == f"used {SECRET_VALUE}"


def test_no_workspace_scrubs_nothing(tmp_path: Path) -> None:
    """A caller outside a workspace scope has nothing to resolve sentinels from.

    That is a stated limit rather than a failure, so the text persists. Every caller that
    holds a workspace passes it.
    """
    del tmp_path

    result = TranscriptRedactor.for_workspace(None).text("hello")

    assert result == "hello"
