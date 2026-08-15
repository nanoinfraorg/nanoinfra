# tests/gates/test_scrub_protocol.py
"""Item 39 (#41): the wire that carries one text to the scrubber and back.

The scrub wire is a second socket, and never a verb on the execute wire. The execute request
holds structured fields only, so a peer cannot smuggle a description of intent into the
executor (#14). A scrub request is free-form model text by definition, so it belongs on a
wire of its own.

Two properties matter here. The frame set is exact, so neither peer tolerates a field the
other one added. And the request carries the text plus one capability class, so nothing about
a secret name, a secret value, or a workspace rides on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.gates.executor.protocol import ProtocolError
from nanoinfra.gates.executor.scrub_protocol import (
    SCRUB_PROTOCOL_VERSION,
    ScrubRequest,
    ScrubResponse,
    decode_scrub_request,
    decode_scrub_response,
    default_scrub_socket_path,
    encode_scrub_request,
    encode_scrub_response,
)
from nanoinfra.gates.executor.supervisor import MAX_SOCKET_PATH_BYTES

# The kernel copies a socket path into sun_path, which holds 108 bytes on Linux.
_SUN_PATH_BYTES = 108


def test_a_request_round_trips() -> None:
    request = ScrubRequest(text="ran mysql -phunter2", capability_class="mutate.remote")

    assert decode_scrub_request(encode_scrub_request(request)) == request


def test_a_response_round_trips() -> None:
    response = ScrubResponse(ok=True, text="ran mysql -p[redacted]", error=None)

    assert decode_scrub_response(encode_scrub_response(response)) == response


def test_the_request_carries_the_text_and_one_capability_class() -> None:
    """Nothing else may ride on this wire.

    A secret name, a secret value, or a workspace path on the request would put part of the
    credential store back on the agent's side of the split.
    """
    assert set(ScrubRequest.__dataclass_fields__) == {"text", "capability_class"}


def test_the_response_carries_the_scrubbed_text_and_a_verdict() -> None:
    """No sentinel field, and no secret value, in either direction."""
    assert set(ScrubResponse.__dataclass_fields__) == {"ok", "text", "error"}


def test_a_frame_without_a_version_refuses() -> None:
    with pytest.raises(ProtocolError):
        decode_scrub_request(b'{"text": "x", "capability_class": ""}')


def test_a_frame_with_another_version_refuses() -> None:
    """A newer peer may carry a field this side would ignore, and that is the hole."""
    payload = (
        b'{"v": ' + str(SCRUB_PROTOCOL_VERSION + 1).encode()
        + b', "text": "x", "capability_class": ""}'
    )

    with pytest.raises(ProtocolError):
        decode_scrub_request(payload)


def test_a_frame_with_an_unknown_field_refuses() -> None:
    payload = (
        b'{"v": ' + str(SCRUB_PROTOCOL_VERSION).encode()
        + b', "text": "x", "capability_class": "", "sentinels": ["s3cr3t"]}'
    )

    with pytest.raises(ProtocolError):
        decode_scrub_request(payload)


def test_a_frame_with_a_missing_field_refuses() -> None:
    payload = b'{"v": ' + str(SCRUB_PROTOCOL_VERSION).encode() + b', "text": "x"}'

    with pytest.raises(ProtocolError):
        decode_scrub_request(payload)


def test_a_request_field_that_is_not_a_string_refuses() -> None:
    payload = (
        b'{"v": ' + str(SCRUB_PROTOCOL_VERSION).encode()
        + b', "text": 7, "capability_class": ""}'
    )

    with pytest.raises(ProtocolError):
        decode_scrub_request(payload)


def test_a_response_verdict_that_is_not_a_boolean_refuses() -> None:
    payload = (
        b'{"v": ' + str(SCRUB_PROTOCOL_VERSION).encode()
        + b', "ok": "yes", "text": "x", "error": null}'
    )

    with pytest.raises(ProtocolError):
        decode_scrub_response(payload)


def test_the_scrub_socket_sits_beside_the_execute_socket() -> None:
    """The agent reaches this socket, so it stays in the directory the agent reaches.

    The operator socket goes the other way (#38). It lives in a private subdirectory, because
    the agent must never answer its own approval. The scrub socket is the agent's own client,
    so it shares the execute socket's directory and its name stem.
    """
    derived = default_scrub_socket_path(Path("/run/nanoinfra/executor.sock"))

    assert derived == Path("/run/nanoinfra/executor.scrub.sock")


def test_two_executors_get_two_scrub_sockets() -> None:
    """The SDK names each execute socket after its process (#21).

    A fixed name would let the second executor unlink the first one's scrub socket.
    """
    first = default_scrub_socket_path(Path("/run/nanoinfra/executor-101.sock"))
    second = default_scrub_socket_path(Path("/run/nanoinfra/executor-102.sock"))

    assert first != second


def test_the_derived_path_still_fits_a_unix_socket() -> None:
    """The supervisor caps an execute path, and the derived name adds a few bytes.

    A path above the kernel limit fails at bind with a message that hides the cause. So the
    arithmetic is a test rather than a comment.
    """
    directory = "a" * (MAX_SOCKET_PATH_BYTES - len("//x.sock"))
    longest = Path(f"/{directory}/x.sock")
    assert len(str(longest).encode("utf-8")) == MAX_SOCKET_PATH_BYTES

    derived = default_scrub_socket_path(longest)

    assert len(str(derived).encode("utf-8")) <= _SUN_PATH_BYTES
