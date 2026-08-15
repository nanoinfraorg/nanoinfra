# tests/gates/test_fetcher_protocol.py
"""Item 16 (#19): the wire between the agent and the fetcher.

The transport is a Unix domain socket. The fetcher is the process with broad egress, so a TCP
listener on it would let anything that reaches the network ask it to fetch. A socket file keeps
the caller local.

The wire carries structured fields only. Untrusted content comes back through it, so the reply
has a fixed shape the agent can render without a guess.
"""

from __future__ import annotations

import json
import socket

import pytest

from nanoinfra.gates.fetcher.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    FetchRequest,
    FetchResponse,
    ProtocolError,
    SearchRequest,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    read_frame,
    write_frame,
)


def _fetch(**over: object) -> FetchRequest:
    fields: dict[str, object] = {
        "url": "https://example.com/page",
        "extract_mode": "markdown",
        "max_chars": 50000,
    }
    fields.update(over)
    return FetchRequest(**fields)  # pyright: ignore[reportArgumentType]


def _search(**over: object) -> SearchRequest:
    fields: dict[str, object] = {
        "query": "nanoinfra",
        "count": 5,
        "time_range": None,
        "auth_level": None,
        "query_rewrite": None,
        "freshness": None,
    }
    fields.update(over)
    return SearchRequest(**fields)  # pyright: ignore[reportArgumentType]


def _response(**over: object) -> FetchResponse:
    fields: dict[str, object] = {
        "ok": True,
        "body": "Results for: nanoinfra",
        "blocks": None,
        "is_error": False,
        "error": None,
    }
    fields.update(over)
    return FetchResponse(**fields)  # pyright: ignore[reportArgumentType]


def test_a_fetch_request_round_trips() -> None:
    assert decode_request(encode_request(_fetch())) == _fetch()


def test_a_search_request_round_trips() -> None:
    assert decode_request(encode_request(_search())) == _search()


def test_a_response_round_trips() -> None:
    assert decode_response(encode_response(_response())) == _response()


def test_image_blocks_round_trip() -> None:
    """A fetched image comes back as content blocks, so the blocks must survive the wire."""
    blocks = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "(Image fetched from: https://example.com/i.png)"},
    ]
    response = _response(body="", blocks=blocks)

    assert decode_response(encode_response(response)) == response


def test_a_request_carries_no_free_form_field() -> None:
    """A field the model could fill with prose is a field it could use to describe intent.

    #14 keeps model text out of the approval prompt. The wire keeps it off the request. So the
    two request shapes are closed sets, and both hold only what a fetch or a search needs.
    """
    assert set(FetchRequest.__dataclass_fields__) == {"url", "extract_mode", "max_chars"}
    assert set(SearchRequest.__dataclass_fields__) == {
        "query",
        "count",
        "time_range",
        "auth_level",
        "query_rewrite",
        "freshness",
    }


def test_a_response_carries_no_credential_field() -> None:
    """The fetcher returns content and a verdict. It never returns a key.

    The fetcher holds the search provider keys, because egress needs them. A reply that could
    carry one would hand the agent, and so the model, a key the split keeps away from it.
    """
    fields = set(FetchResponse.__dataclass_fields__)

    assert fields == {"ok", "body", "blocks", "is_error", "error"}
    for name in ("secret", "secret_value", "api_key", "token", "authorization"):
        assert name not in fields


def test_an_unknown_field_is_refused() -> None:
    """Fail closed on a wire the peer does not share. A tolerated extra is an ambiguity."""
    payload = json.loads(encode_request(_fetch()))
    payload["extra"] = "surprise"

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_missing_field_is_refused() -> None:
    payload = json.loads(encode_request(_fetch()))
    del payload["extract_mode"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_missing_version_is_refused() -> None:
    payload = json.loads(encode_request(_search()))
    del payload["v"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_future_version_is_refused() -> None:
    """A newer peer may mean a field this one ignores, and ignoring a field is the hole."""
    payload = json.loads(encode_request(_search()))
    payload["v"] = PROTOCOL_VERSION + 1

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_missing_operation_is_refused() -> None:
    """Two operations share one wire, so a frame that names neither is not interpretable."""
    payload = json.loads(encode_request(_fetch()))
    del payload["op"]

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_an_unknown_operation_is_refused() -> None:
    payload = json.loads(encode_request(_fetch()))
    payload["op"] = "exec"

    with pytest.raises(ProtocolError):
        decode_request(json.dumps(payload).encode())


def test_a_non_json_frame_is_refused() -> None:
    with pytest.raises(ProtocolError):
        decode_request(b"not json at all")


def test_a_frame_round_trips_over_a_socket_pair() -> None:
    left, right = socket.socketpair()
    try:
        write_frame(left, encode_request(_fetch()))
        payload = read_frame(right)
    finally:
        left.close()
        right.close()

    assert decode_request(payload) == _fetch()


def test_two_frames_do_not_run_together() -> None:
    """Length prefixing, not newline framing. A query and a URL may hold a newline."""
    left, right = socket.socketpair()
    try:
        write_frame(left, encode_request(_search(query="line one\nline two")))
        write_frame(left, encode_request(_search(query="second")))
        first = decode_request(read_frame(right))
        second = decode_request(read_frame(right))
    finally:
        left.close()
        right.close()

    assert isinstance(first, SearchRequest)
    assert isinstance(second, SearchRequest)
    assert first.query == "line one\nline two"
    assert second.query == "second"


def test_an_oversized_frame_is_refused() -> None:
    """A length prefix a peer controls must not let it ask for an unbounded allocation."""
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
    left, right = socket.socketpair()
    try:
        payload = encode_request(_fetch())
        left.sendall(len(payload).to_bytes(4, "big"))
        left.sendall(payload[:5])
        left.close()
        with pytest.raises(ProtocolError):
            read_frame(right)
    finally:
        right.close()


def test_the_frame_cap_holds_a_whole_page_and_an_image() -> None:
    """A page and an image both cross this wire, and base64 grows an image by a third.

    The executor caps a frame at 8 MiB, because a command is short. A fetch reply is not: a
    fetched image arrives as base64 content blocks. A cap below a browser-sized image would turn
    an ordinary fetch into a refusal, so this wire carries a larger cap and still holds one.
    """
    assert MAX_FRAME_BYTES >= 32 * 1024 * 1024
