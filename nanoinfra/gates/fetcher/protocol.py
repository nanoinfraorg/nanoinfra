"""The wire between the agent and the fetcher -- nanoinfraorg/nanoinfra#19.

The transport is a Unix domain socket, and it copies #18 on purpose. A second process model
would drift from the first one, and two models mean two sets of mistakes. A TCP listener would
also let anything with network access ask the process with broad egress to fetch a URL.

Three properties shape this module, and they are the same three the executor wire holds:

- **Structured fields only.** A fetch and a search each have a fixed field set and no free-form
  member. So a peer cannot smuggle a directive into the fetcher, and the reply has a shape the
  agent renders without a guess.
- **Fail closed on anything unfamiliar.** An unknown field, a missing version, a newer version,
  and an unknown operation all refuse. An ambiguity between two peers in this position is a hole.
- **Length-prefixed frames, not lines.** A URL and a query may hold a newline, so newline framing
  would let one request read as two.

One difference from #18 is the frame cap. A command is short and 8 MiB is generous for one. A
fetch reply is not short: an image comes back as base64 content blocks, and base64 grows the
bytes by a third. So this wire caps a frame higher, and it still caps it, because the peer
controls the length prefix.

The reply carries content, a verdict, and nothing else. The fetcher holds the search provider
keys, because egress needs them, and a reply that could carry a key would hand it to the agent.
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import asdict, dataclass, fields
from typing import Any, cast

PROTOCOL_VERSION = 1

# A fetched image arrives as base64, and 32 MiB holds a browser-sized one. The cap stays because
# a peer controls the length prefix, and an uncapped prefix is an unbounded allocation.
MAX_FRAME_BYTES = 32 * 1024 * 1024

OP_FETCH = "fetch"
OP_SEARCH = "search"

_LENGTH = struct.Struct(">I")


class ProtocolError(Exception):
    """A frame this side refuses to interpret. The caller must not guess past it."""


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """One request to read one URL.

    The fetcher validates the URL itself, resolves it, and pins the address it validated. So
    nothing about the target rides on the agent's word.
    """

    url: str
    extract_mode: str
    max_chars: int | None


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """One request to search the web.

    Every field past ``query`` is a filter that some providers accept. The fetcher owns the
    provider choice and the key, so the request names neither.
    """

    query: str
    count: int | None
    time_range: str | None
    auth_level: int | None
    query_rewrite: bool | None
    freshness: str | None


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """The fetcher's answer for either operation.

    ``ok`` says the fetcher completed the operation. ``body`` and ``blocks`` then carry what the
    tool returns to the model. ``ok`` False means the fetcher refused or failed the frame itself,
    and ``error`` says why.

    ``is_error`` is separate, because it marks a tool-level error rather than a broken request.
    A rate-limited provider is a result the model must read as a failure, and the flag keeps that
    distinction across the wire.
    """

    ok: bool
    body: str
    blocks: list[dict[str, Any]] | None
    is_error: bool
    error: str | None


def encode_request(request: FetchRequest | SearchRequest) -> bytes:
    payload = asdict(request)
    payload["v"] = PROTOCOL_VERSION
    payload["op"] = OP_FETCH if isinstance(request, FetchRequest) else OP_SEARCH
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def encode_response(response: FetchResponse) -> bytes:
    payload = asdict(response)
    payload["v"] = PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_request(payload: bytes) -> FetchRequest | SearchRequest:
    """Parse one request frame, and refuse a frame that names no known operation."""
    data = _parse(payload)
    op = data.pop("op", None)
    if op is None:
        raise ProtocolError("frame names no operation")
    if op == OP_FETCH:
        return cast("FetchRequest", _build(data, FetchRequest))
    if op == OP_SEARCH:
        return cast("SearchRequest", _build(data, SearchRequest))
    raise ProtocolError(f"frame names an unknown operation: {op!r}")


def decode_response(payload: bytes) -> FetchResponse:
    return cast("FetchResponse", _build(_parse(payload), FetchResponse))


def _parse(payload: bytes) -> dict[str, Any]:
    """Read one frame as an object, and check the version before anything else."""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("frame is not an object")

    data: dict[str, Any] = cast("dict[str, Any]", raw)
    version = data.pop("v", None)
    if version is None:
        raise ProtocolError("frame carries no protocol version")
    if version != PROTOCOL_VERSION:
        # A newer peer may carry a field this side would ignore, and ignoring a field on this
        # wire is the hole. So a version mismatch refuses rather than degrades.
        raise ProtocolError(f"protocol version {version!r} is not {PROTOCOL_VERSION}")
    return data


def _build(data: dict[str, Any], kind: type[Any]) -> Any:
    """Make one *kind* from the parsed fields, and refuse anything that does not match."""
    expected = {field.name for field in fields(kind)}
    unknown = set(data) - expected
    if unknown:
        raise ProtocolError(f"frame carries unknown field(s): {sorted(unknown)}")
    missing = expected - set(data)
    if missing:
        raise ProtocolError(f"frame is missing field(s): {sorted(missing)}")
    return kind(**data)


def write_frame(conn: socket.socket, payload: bytes) -> None:
    """Send one length-prefixed frame."""
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(payload)} bytes exceeds {MAX_FRAME_BYTES}")
    conn.sendall(_LENGTH.pack(len(payload)) + payload)


def read_frame(conn: socket.socket) -> bytes:
    """Read one length-prefixed frame, or raise.

    A truncated frame raises rather than returns what arrived. A peer that dies mid-write must
    not leave this side with a partial request that happens to parse.
    """
    header = _read_exactly(conn, _LENGTH.size)
    (length,) = _LENGTH.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"peer announced {length} bytes, above {MAX_FRAME_BYTES}")
    return _read_exactly(conn, length)


def _read_exactly(conn: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ProtocolError(f"peer closed after {count - remaining} of {count} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


__all__ = [
    "MAX_FRAME_BYTES",
    "OP_FETCH",
    "OP_SEARCH",
    "PROTOCOL_VERSION",
    "FetchRequest",
    "FetchResponse",
    "ProtocolError",
    "SearchRequest",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
    "read_frame",
    "write_frame",
]
