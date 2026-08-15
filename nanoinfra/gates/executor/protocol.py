"""The wire between the agent and the executor -- nanoinfraorg/nanoinfra#18.

The transport is a Unix domain socket. TCP would widen any egress policy to include our own
executor, and that holes the one rule that makes a narrow network policy useful.

Three properties shape this module:

- **Structured fields only.** A request has a fixed field set and no free-form member. #14
  keeps model text out of the approval prompt, and this keeps it off the request, so a peer
  cannot smuggle a description of intent into the executor.
- **Fail closed on anything unfamiliar.** An unknown field, a missing version, and a newer
  version all refuse. A tolerated extra field is an ambiguity between two peers, and an
  ambiguity in this position is a hole.
- **Length-prefixed frames, not lines.** A command may hold a newline, so newline framing
  would let one request read as two.

The response carries output and a verdict. It never carries a credential. The executor holds
the only plaintext, and returning one would undo the split.
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import asdict, dataclass, fields
from typing import Any, cast

PROTOCOL_VERSION = 1

# A peer controls the length prefix, so the reader caps it. 8 MiB is far above a command and
# far below a memory problem. Output is bounded separately by truncate_output.
MAX_FRAME_BYTES = 8 * 1024 * 1024

_LENGTH = struct.Struct(">I")


class ProtocolError(Exception):
    """A frame this side refuses to interpret. The caller must not guess past it."""


@dataclass(frozen=True, slots=True)
class ExecuteRequest:
    """One request to act on one server.

    ``command`` is the resolved command. The executor resolves the server, the scope, and the
    credential itself, so nothing about the target rides on the agent's word.

    ``token_nonce`` names an approval the executor verifies against its own store (#12). The
    token itself never crosses the wire, because a token the agent can read is a token the
    model can propose.
    """

    server_id_or_name: str
    command: str
    session_id: str | None
    execution_context: str
    preview_requested: bool
    timeout_s: str | None
    token_nonce: str | None


@dataclass(frozen=True, slots=True)
class ExecuteResponse:
    """The executor's answer. ``reason`` carries the gate's words for a refusal."""

    ok: bool
    output: str
    exit_code: int | None
    error: str | None
    reason: str


def encode_request(request: ExecuteRequest) -> bytes:
    payload = asdict(request)
    payload["v"] = PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def encode_response(response: ExecuteResponse) -> bytes:
    payload = asdict(response)
    payload["v"] = PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_request(payload: bytes) -> ExecuteRequest:
    return _decode(payload, ExecuteRequest)


def decode_response(payload: bytes) -> ExecuteResponse:
    return _decode(payload, ExecuteResponse)


def _decode(payload: bytes, kind: type[Any]) -> Any:
    """Parse one frame into *kind*, and refuse anything that does not match exactly."""
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
    not leave this side holding a partial request that happens to parse.
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
    "PROTOCOL_VERSION",
    "ExecuteRequest",
    "ExecuteResponse",
    "ProtocolError",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
    "read_frame",
    "write_frame",
]
