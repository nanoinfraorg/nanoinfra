# tests/gates/test_mcp_host_protocol.py
"""Item 20 (#22): the wire between the agent and the MCP host.

A stdio MCP server is a subprocess. The fetcher cannot exec. The MCP host process holds that
exec right on its own account, and this wire is how the agent reaches it.

Three properties matter here, and each one has a test below:

- The request names a configured server. It never names a program. The host resolves the
  command from its own config, so the agent cannot choose what runs.
- Anything unfamiliar refuses. A missing version, a newer version, an unknown field, and an
  unknown operation all raise.
- One frame stays one frame. A tool argument may hold a newline, so the length prefix frames
  the payload rather than the newline.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct

import pytest

from nanoinfra.gates.mcp_host.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    CallToolRequest,
    GetPromptRequest,
    HostResponse,
    ListPromptsRequest,
    ListResourcesRequest,
    ListToolsRequest,
    OpenRequest,
    ProtocolError,
    ReadResourceRequest,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    read_frame,
    write_frame,
)


async def _stream_pair() -> tuple[
    asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamWriter
]:
    left, right = socket.socketpair()
    left_reader, left_writer = await asyncio.open_connection(sock=left)
    right_reader, right_writer = await asyncio.open_connection(sock=right)
    return left_reader, left_writer, right_reader, right_writer


def _close(*writers: asyncio.StreamWriter) -> None:
    for writer in writers:
        writer.close()


# ------------------------------------------------------------------ round trips


@pytest.mark.parametrize(
    "request_object",
    [
        OpenRequest(request_id=1, server_name="github"),
        ListToolsRequest(request_id=2),
        ListResourcesRequest(request_id=3),
        ListPromptsRequest(request_id=4),
        CallToolRequest(request_id=5, tool_name="search", arguments={"query": "line\nbreak"}),
        ReadResourceRequest(request_id=6, uri="file:///tmp/data.txt"),
        GetPromptRequest(request_id=7, prompt_name="review", arguments={"topic": "gates"}),
    ],
)
def test_every_request_kind_survives_one_round_trip(request_object: object) -> None:
    assert decode_request(encode_request(request_object)) == request_object


def test_the_response_survives_one_round_trip() -> None:
    response = HostResponse(
        request_id=9,
        ok=True,
        result={"tools": [{"name": "search"}]},
        error=None,
        error_data=None,
    )

    assert decode_response(encode_response(response)) == response


def test_the_encoded_request_carries_the_version_and_the_operation() -> None:
    payload = json.loads(encode_request(OpenRequest(request_id=1, server_name="github")))

    assert payload["v"] == PROTOCOL_VERSION
    assert payload["op"] == "open"


# --------------------------------------------------- the agent names no program


def test_the_open_request_names_a_server_and_never_a_command() -> None:
    """The security property of this wire.

    A command field here would hand the agent an exec right through the host. The host reads
    the command from its own config instead.
    """
    assert set(OpenRequest.__dataclass_fields__) == {"request_id", "server_name"}


def test_no_request_kind_carries_a_command_field() -> None:
    forbidden = {"command", "args", "argv", "env", "cwd", "url", "executable", "program"}
    offences: list[str] = []
    for kind in (
        OpenRequest,
        ListToolsRequest,
        ListResourcesRequest,
        ListPromptsRequest,
        CallToolRequest,
        ReadResourceRequest,
        GetPromptRequest,
    ):
        offences += [
            f"{kind.__name__}.{name}"
            for name in kind.__dataclass_fields__
            if name in forbidden
        ]

    assert offences == []


# ------------------------------------------------------------- fail closed


def test_a_frame_without_a_version_refuses() -> None:
    with pytest.raises(ProtocolError):
        decode_request(json.dumps({"op": "open", "server_name": "x"}).encode("utf-8"))


def test_a_newer_version_refuses_rather_than_degrades() -> None:
    payload = json.dumps(
        {"v": PROTOCOL_VERSION + 1, "op": "open", "request_id": 1, "server_name": "x"}
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_request(payload)


def test_a_frame_with_an_unknown_field_refuses() -> None:
    payload = json.dumps(
        {"v": PROTOCOL_VERSION, "op": "open", "request_id": 1, "server_name": "x", "extra": 1}
    ).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_request(payload)


def test_a_frame_with_a_missing_field_refuses() -> None:
    payload = json.dumps({"v": PROTOCOL_VERSION, "op": "open", "request_id": 1}).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_request(payload)


def test_a_frame_without_an_operation_refuses() -> None:
    payload = json.dumps({"v": PROTOCOL_VERSION, "request_id": 1}).encode("utf-8")

    with pytest.raises(ProtocolError):
        decode_request(payload)


def test_an_unknown_operation_refuses() -> None:
    payload = json.dumps({"v": PROTOCOL_VERSION, "op": "exec", "request_id": 1}).encode("utf-8")

    with pytest.raises(ProtocolError) as caught:
        decode_request(payload)
    assert "exec" in str(caught.value)


def test_a_frame_that_is_not_json_refuses() -> None:
    with pytest.raises(ProtocolError):
        decode_request(b"\xff\xfe not json")


def test_a_frame_that_is_not_an_object_refuses() -> None:
    with pytest.raises(ProtocolError):
        decode_response(b"[1, 2, 3]")


# ------------------------------------------------------------------- framing


async def test_one_frame_with_a_newline_reads_as_one_frame() -> None:
    left_reader, left_writer, right_reader, right_writer = await _stream_pair()
    request = CallToolRequest(request_id=1, tool_name="t", arguments={"body": "a\nb\nc"})
    try:
        await write_frame(left_writer, encode_request(request))
        received = decode_request(await read_frame(right_reader))
    finally:
        _close(left_writer, right_writer)

    assert received == request


async def test_two_frames_stay_apart_on_one_connection() -> None:
    left_reader, left_writer, right_reader, right_writer = await _stream_pair()
    first = ListToolsRequest(request_id=1)
    second = ListPromptsRequest(request_id=2)
    try:
        await write_frame(left_writer, encode_request(first))
        await write_frame(left_writer, encode_request(second))
        received = [
            decode_request(await read_frame(right_reader)),
            decode_request(await read_frame(right_reader)),
        ]
    finally:
        _close(left_writer, right_writer)

    assert received == [first, second]


async def test_a_payload_above_the_cap_refuses_before_the_send() -> None:
    _left_reader, left_writer, _right_reader, right_writer = await _stream_pair()
    try:
        with pytest.raises(ProtocolError):
            await write_frame(left_writer, b"x" * (MAX_FRAME_BYTES + 1))
    finally:
        _close(left_writer, right_writer)


async def test_an_announced_length_above_the_cap_refuses_before_the_read() -> None:
    left_reader, left_writer, right_reader, right_writer = await _stream_pair()
    try:
        left_writer.write(struct.pack(">I", MAX_FRAME_BYTES + 1))
        await left_writer.drain()
        with pytest.raises(ProtocolError):
            await read_frame(right_reader)
    finally:
        _close(left_writer, right_writer)


async def test_a_truncated_frame_raises_rather_than_returns_a_part() -> None:
    left_reader, left_writer, right_reader, right_writer = await _stream_pair()
    try:
        left_writer.write(struct.pack(">I", 40) + b"half")
        await left_writer.drain()
        left_writer.close()
        with pytest.raises(ProtocolError):
            await read_frame(right_reader)
    finally:
        _close(right_writer)


async def test_a_peer_that_hangs_up_raises() -> None:
    left_reader, left_writer, right_reader, right_writer = await _stream_pair()
    left_writer.close()
    try:
        with pytest.raises(ProtocolError):
            await read_frame(right_reader)
    finally:
        _close(right_writer)


# ------------------------------------------------------------------ the reply


def test_the_reply_carries_no_credential_field() -> None:
    """The reply carries MCP content and a verdict. It has nowhere to put a secret."""
    assert set(HostResponse.__dataclass_fields__) == {
        "request_id",
        "ok",
        "result",
        "error",
        "error_data",
    }
