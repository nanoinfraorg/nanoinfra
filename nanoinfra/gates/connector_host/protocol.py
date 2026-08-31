"""The wire between the executor and the connector host (#195, part 4).

The same construction as the MCP host's wire, and copied on purpose: a second process model would
drift from the first, and two models mean two sets of mistakes. Unix socket, length-prefixed
frames, an exact field set, a version that refuses rather than degrades.

What crosses, and what deliberately does not, is the whole design:

**Crosses.** The package directory name, the operation name, the arguments the executor already
validated against the operation's own schema, the rendered URL, one short-lived access token, and a
deadline.

**Does not cross.** The refresh token, the credential id, the secrets directory, the config, the
session key, and the gate's decision. The host receives a request it may make and a token it may
make it with, and it learns nothing about why it was allowed.

The order matters more than the fields: the executor gates the action and mints the token
**afterwards**, so a refusal never produces a token at all. A gate decision taken in a process that
holds no policy would be a gate in name only, which is why this wire carries a rendered request
rather than a question.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, cast

PROTOCOL_VERSION = 1

#: A connector response is JSON that the engine projects down to declared fields, so this is far
#: below the MCP host's cap. The cap stays because a peer controls the length prefix, and an
#: uncapped prefix is an unbounded allocation.
MAX_FRAME_BYTES = 8 * 1024 * 1024

OP_CALL = "call"

_LENGTH = struct.Struct(">I")


class ProtocolError(Exception):
    """A frame this side refuses to interpret. The caller must not guess past it."""


@dataclass(frozen=True, slots=True)
class ConnectorHostRequest:
    """One declared operation of one installed package, already gated and already rendered.

    `package` is a directory name under the workspace's `connectors/`, never a path: the host
    resolves it against its own root, so nothing about where code lives rides on the caller's word.

    `access_token` is short-lived and scoped to the operation's own capability class. A compromised
    host holds a token for those scopes, for minutes -- not the refresh token, not the store, and
    not another connector's credential.
    """

    request_id: int
    package: str
    operation: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict[str, str])
    query: dict[str, str] = field(default_factory=dict[str, str])
    body: dict[str, Any] | None = None
    access_token: str = ""
    timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class ConnectorHostResponse:
    """The host's answer.

    `ok` False is a refusal or a failure, never a partial result: a projection built from an
    error body would put an API's error prose into the model's context as though it were data.
    """

    request_id: int
    ok: bool
    status: int | None
    payload: dict[str, Any] | None
    error: str | None
    retryable: bool = False


HostRequest = ConnectorHostRequest

_OP_OF_KIND: dict[type[Any], str] = {ConnectorHostRequest: OP_CALL}
_KIND_OF_OP: dict[str, type[Any]] = {op: kind for kind, op in _OP_OF_KIND.items()}


def encode_request(request: HostRequest) -> bytes:
    op = _OP_OF_KIND.get(type(request))
    if op is None:
        raise ProtocolError(f"{type(request).__name__} is not a request kind of this wire")
    payload = asdict(request)
    payload["v"] = PROTOCOL_VERSION
    payload["op"] = op
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def encode_response(response: ConnectorHostResponse) -> bytes:
    payload = asdict(response)
    payload["v"] = PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_request(payload: bytes) -> HostRequest:
    data = _parse(payload)
    op = data.pop("op", None)
    if op is None:
        raise ProtocolError("frame names no operation")
    kind = _KIND_OF_OP.get(op) if isinstance(op, str) else None
    if kind is None:
        raise ProtocolError(f"frame names an unknown operation: {op!r}")
    return cast("HostRequest", _build(data, kind))


def decode_response(payload: bytes) -> ConnectorHostResponse:
    return cast("ConnectorHostResponse", _build(_parse(payload), ConnectorHostResponse))


def _parse(payload: bytes) -> dict[str, Any]:
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
        # A newer peer may carry a field this side would ignore, and ignoring a field on a wire
        # that hands out tokens is the hole. A mismatch refuses rather than degrades.
        raise ProtocolError(f"protocol version {version!r} is not {PROTOCOL_VERSION}")
    return data


def _build(data: dict[str, Any], kind: type[Any]) -> Any:
    expected = {member.name for member in fields(kind)}
    unknown = set(data) - expected
    if unknown:
        raise ProtocolError(f"frame carries unknown field(s): {sorted(unknown)}")
    missing = expected - set(data)
    # A field with a default is optional on the wire; one without is not. The MCP host's wire
    # has no optional fields, so it can require every one -- this one carries a body only for a
    # writing method.
    required = {
        member.name
        for member in fields(kind)
        if member.default is MISSING and member.default_factory is MISSING
    }
    if missing & required:
        raise ProtocolError(f"frame is missing field(s): {sorted(missing & required)}")
    return kind(**data)


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(payload)} bytes exceeds {MAX_FRAME_BYTES}")
    writer.write(_LENGTH.pack(len(payload)) + payload)
    try:
        await writer.drain()
    except OSError as exc:
        raise ProtocolError(f"peer closed before the frame left: {exc}") from exc


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await _read_exactly(reader, _LENGTH.size)
    (length,) = _LENGTH.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"peer announced {length} bytes, above {MAX_FRAME_BYTES}")
    return await _read_exactly(reader, length)


async def _read_exactly(reader: asyncio.StreamReader, count: int) -> bytes:
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError(f"peer closed after {len(exc.partial)} of {count} bytes") from exc
    except OSError as exc:
        raise ProtocolError(
            f"the connection failed after {count} bytes were asked for: {exc}"
        ) from exc


__all__ = [
    "MAX_FRAME_BYTES",
    "OP_CALL",
    "PROTOCOL_VERSION",
    "ConnectorHostRequest",
    "ConnectorHostResponse",
    "HostRequest",
    "ProtocolError",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
    "read_frame",
    "write_frame",
]
