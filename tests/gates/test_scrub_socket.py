# tests/gates/test_scrub_socket.py
"""Item 39 (#41): the executor scrubs, and the agent sends text.

The credential store lives behind the executor (#18). So the sentinel build lives here too,
and the agent holds no plaintext at any point. This module drives the service half: the
sentinels, the two placeholders, and the socket that answers one text at a time.

The scrub keeps the secret name and drops the value (#31). A ``credential.access`` result
drops whole, because such a result is credential material by definition (#17).
"""

from __future__ import annotations

import ast
import socket
import threading
from pathlib import Path

import pytest
from loguru import logger

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.gates.executor.protocol import read_frame, write_frame
from nanoinfra.gates.executor.scrub import (
    answer_scrub,
    bind_scrub_socket,
    serve_scrub_socket,
    workspace_secret_sentinels,
)
from nanoinfra.gates.executor.scrub_protocol import (
    ScrubRequest,
    decode_scrub_response,
    encode_scrub_request,
)
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)


def _stored_secret(workspace: Path) -> str:
    secret = SecretStore(workspace).create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )
    return secret.id


# -- the sentinels ----------------------------------------------------------


def test_the_sentinels_come_from_the_workspace_store(tmp_path: Path) -> None:
    _stored_secret(tmp_path)

    found = workspace_secret_sentinels(tmp_path)

    assert [(s.name, s.value) for s in found] == [(SECRET_NAME, SECRET_VALUE)]


def test_no_key_yields_no_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No key means no turn resolved a secret either. So there is nothing to scrub."""
    _stored_secret(tmp_path)
    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)

    assert workspace_secret_sentinels(tmp_path) == []


def test_one_unreadable_secret_does_not_stop_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value nobody can decrypt is a value nobody can leak either."""
    _stored_secret(tmp_path)

    def _explode(self: SecretStore, secret_id: str) -> str | None:
        raise RuntimeError("this secret is unreadable")

    monkeypatch.setattr(SecretStore, "resolve_plaintext", _explode)

    assert workspace_secret_sentinels(tmp_path) == []


def test_a_broken_store_raises_rather_than_answers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store this side cannot read must not read as "no secret exists".

    That answer is fail open on the one path #17 exists to close. The caller withholds the
    text instead, so the build raises and the socket reports the failure.
    """
    _stored_secret(tmp_path)

    def _explode(self: SecretStore) -> list[object]:
        raise RuntimeError("store is broken")

    monkeypatch.setattr(SecretStore, "list_secrets", _explode)

    with pytest.raises(RuntimeError):
        workspace_secret_sentinels(tmp_path)


# -- one answer -------------------------------------------------------------


def test_a_stored_value_is_replaced_by_its_name(tmp_path: Path) -> None:
    _stored_secret(tmp_path)

    answer = answer_scrub(
        ScrubRequest(text=f"DB_PASSWORD={SECRET_VALUE}", capability_class=MUTATE_REMOTE),
        workspace=tmp_path,
    )

    assert answer.ok
    assert SECRET_VALUE not in answer.text
    assert SECRET_NAME in answer.text


def test_text_with_no_secret_comes_back_unchanged(tmp_path: Path) -> None:
    _stored_secret(tmp_path)

    answer = answer_scrub(
        ScrubRequest(text="restart nginx please", capability_class=""), workspace=tmp_path
    )

    assert answer.ok
    assert answer.text == "restart nginx please"


def test_a_credential_access_result_drops_whole(tmp_path: Path) -> None:
    """Keep the name, drop the body. #17 does not scrub such a result value by value."""
    _stored_secret(tmp_path)

    answer = answer_scrub(
        ScrubRequest(
            text=f"value: {SECRET_VALUE}", capability_class=CREDENTIAL_ACCESS
        ),
        workspace=tmp_path,
    )

    assert answer.ok
    assert SECRET_VALUE not in answer.text
    assert SECRET_NAME in answer.text
    assert "value:" not in answer.text


def test_a_credential_access_result_drops_even_with_no_match(tmp_path: Path) -> None:
    """No sentinel matched. The body still goes, with an unnamed reference."""
    answer = answer_scrub(
        ScrubRequest(text="some unknown credential", capability_class=CREDENTIAL_ACCESS),
        workspace=tmp_path,
    )

    assert answer.ok
    assert "some unknown credential" not in answer.text


def test_a_broken_store_answers_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller reads a failure and withholds the text. It never reads a false success."""
    _stored_secret(tmp_path)

    def _explode(self: SecretStore) -> list[object]:
        raise RuntimeError("store is broken")

    monkeypatch.setattr(SecretStore, "list_secrets", _explode)

    answer = answer_scrub(
        ScrubRequest(text=f"used {SECRET_VALUE}", capability_class=""), workspace=tmp_path
    )

    assert not answer.ok
    assert answer.error
    assert answer.text == ""


def test_a_failed_answer_carries_no_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure must not echo the text it could not scrub back onto the wire."""
    _stored_secret(tmp_path)

    def _explode(self: SecretStore) -> list[object]:
        raise RuntimeError("store is broken")

    monkeypatch.setattr(SecretStore, "list_secrets", _explode)

    answer = answer_scrub(
        ScrubRequest(text=f"used {SECRET_VALUE}", capability_class=""), workspace=tmp_path
    )

    assert SECRET_VALUE not in answer.text
    assert SECRET_VALUE not in (answer.error or "")


def test_the_service_logs_no_plaintext(tmp_path: Path) -> None:
    """A log line that quotes the text would make the leak durable in a second file."""
    _stored_secret(tmp_path)
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG")
    try:
        answer_scrub(
            ScrubRequest(text=f"used {SECRET_VALUE}", capability_class=MUTATE_REMOTE),
            workspace=tmp_path,
        )
    finally:
        logger.remove(sink)

    assert SECRET_VALUE not in "".join(lines)


def test_a_failure_logs_no_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dangerous log line is the one a failure writes.

    loguru prints the local variables of each frame by default, and one of those locals is the
    text under scrub. So the service turns that off for its own exception records.
    """
    _stored_secret(tmp_path)

    def _explode(self: SecretStore) -> list[object]:
        raise RuntimeError("store is broken")

    monkeypatch.setattr(SecretStore, "list_secrets", _explode)
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", diagnose=True, backtrace=True)
    try:
        answer_scrub(
            ScrubRequest(text=f"used {SECRET_VALUE}", capability_class=MUTATE_REMOTE),
            workspace=tmp_path,
        )
    finally:
        logger.remove(sink)

    assert SECRET_VALUE not in "".join(lines)


# -- the socket -------------------------------------------------------------


def _serve_once(socket_path: Path, workspace: Path) -> threading.Thread:
    listener = bind_scrub_socket(socket_path)
    thread = threading.Thread(
        target=serve_scrub_socket,
        args=(listener, workspace),
        kwargs={"max_requests": 1},
        daemon=True,
    )
    thread.start()
    return thread


def _ask(socket_path: Path, payload: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10.0)
        conn.connect(str(socket_path))
        write_frame(conn, payload)
        return read_frame(conn)


def test_the_socket_answers_one_scrub(tmp_path: Path) -> None:
    _stored_secret(tmp_path)
    socket_path = tmp_path / "run" / "e.scrub.sock"
    thread = _serve_once(socket_path, tmp_path)

    reply = decode_scrub_response(
        _ask(
            socket_path,
            encode_scrub_request(
                ScrubRequest(text=f"used {SECRET_VALUE}", capability_class=MUTATE_REMOTE)
            ),
        )
    )
    thread.join(timeout=10)

    assert reply.ok
    assert SECRET_VALUE not in reply.text
    assert SECRET_NAME in reply.text


def test_a_malformed_frame_gets_a_refusal(tmp_path: Path) -> None:
    """A peer that speaks nonsense must not take the scrubber down."""
    socket_path = tmp_path / "run" / "e.scrub.sock"
    thread = _serve_once(socket_path, tmp_path)

    reply = decode_scrub_response(_ask(socket_path, b"not a frame"))
    thread.join(timeout=10)

    assert not reply.ok
    assert reply.error


def test_the_service_writes_no_record() -> None:
    """No record holds the text, and that is structural rather than careful.

    The service imports no audit store and no job store, so no path through it can write the
    text of a transcript to a second durable file.
    """
    tree = ast.parse(Path("nanoinfra/gates/executor/scrub.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "nanoinfra.gates.audit" not in imported
    assert "nanoinfra.servers.job_store" not in imported
