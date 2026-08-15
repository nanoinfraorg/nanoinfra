# tests/gates/test_executor_protocol.py
"""Item 15 (#18): the wire between the agent and the executor.

The transport is a Unix domain socket. TCP would widen any egress policy to include our own
executor, and that holes the one rule that makes a narrow network policy useful.

The wire carries structured fields only. No free-form field exists for the model to fill, so a
request cannot smuggle a command the executor did not resolve itself.
"""

from __future__ import annotations

import json

import pytest

from nanoinfra.gates.executor.protocol import (
    PROTOCOL_VERSION,
    ExecuteRequest,
    ExecuteResponse,
    ProtocolError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    read_frame,
    write_frame,
)


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, object] = {
        "server_id_or_name": "prod-web-01",
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)  # pyright: ignore[reportArgumentType]


def test_a_request_round_trips() -> None:
    request = _request()

    assert decode_request(encode_request(request)) == request


def test_a_response_round_trips() -> None:
    response = ExecuteResponse(ok=True, output="up 3 days", exit_code=0, error=None, reason="")

    assert decode_response(encode_response(response)) == response


def test_a_request_carries_no_free_form_field() -> None:
    """A field the model could fill with prose is a field it could use to describe intent.

    #14 keeps model text out of the approval prompt. The wire keeps it off the request.
    """
    allowed = set(ExecuteRequest.__dataclass_fields__)

    assert allowed == {
        "server_id_or_name",
        "command",
        "session_id",
        "execution_context",
        "preview_requested",
        "timeout_s",
        "token_nonce",
    }


def test_an_unknown_field_is_refused() -> None:
    """Fail closed on a wire the peer does not share. A tolerated extra is an ambiguity."""
    payload = json.loads(encode_request(_request()))
    payload["extra"] = "surprise"

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_missing_version_is_refused() -> None:
    payload = json.loads(encode_request(_request()))
    del payload["v"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_future_version_is_refused() -> None:
    """A newer peer may mean a field this one ignores, and ignoring a field is the hole."""
    payload = json.loads(encode_request(_request()))
    payload["v"] = PROTOCOL_VERSION + 1

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_non_json_frame_is_refused() -> None:
    with pytest.raises(ProtocolError):
        decode_request(b"not json at all")


def test_a_frame_round_trips_over_a_socket_pair() -> None:
    import socket

    left, right = socket.socketpair()
    try:
        write_frame(left, encode_request(_request()))
        payload = read_frame(right)
    finally:
        left.close()
        right.close()

    assert decode_request(payload) == _request()


def test_two_frames_do_not_run_together() -> None:
    """Length prefixing, not newline framing. A command may hold a newline."""
    import socket

    left, right = socket.socketpair()
    try:
        write_frame(left, encode_request(_request(command="line one\nline two")))
        write_frame(left, encode_request(_request(command="second")))
        first = decode_request(read_frame(right))
        second = decode_request(read_frame(right))
    finally:
        left.close()
        right.close()

    assert first.command == "line one\nline two"
    assert second.command == "second"


def test_an_oversized_frame_is_refused() -> None:
    """A length prefix a peer controls must not let it ask for an unbounded allocation."""
    import socket

    from nanoinfra.gates.executor.protocol import MAX_FRAME_BYTES

    left, right = socket.socketpair()
    try:
        left.sendall((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        with pytest.raises(ProtocolError):
            read_frame(right)
    finally:
        left.close()
        right.close()


def test_a_truncated_frame_is_refused() -> None:
    """A peer that dies mid-write must not leave the reader with a partial request."""
    import socket

    left, right = socket.socketpair()
    try:
        payload = encode_request(_request())
        left.sendall(len(payload).to_bytes(4, "big"))
        left.sendall(payload[:5])
        left.close()
        with pytest.raises(ProtocolError):
            read_frame(right)
    finally:
        right.close()


def test_a_response_carries_no_secret_field() -> None:
    """The executor returns output and a verdict. It never returns a credential."""
    fields = set(ExecuteResponse.__dataclass_fields__)

    assert "secret" not in fields
    assert "secret_value" not in fields
    assert fields == {"ok", "output", "exit_code", "error", "reason"}
