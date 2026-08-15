"""The wire between the agent and the MCP host -- nanoinfraorg/nanoinfra#22.

The transport is a Unix domain socket, and it copies #18 and #19 on purpose. A second process
model would drift from the first one, and two models mean two sets of mistakes. TCP would also
widen an egress policy to include our own host process.

Four properties shape this module:

- **The agent names a server, never a program.** ``OpenRequest`` carries a configured server
  name. The host reads the command, the arguments, the environment, and the working directory
  from its own config. An agent that could name a command would hold the exec right that this
  split moves away from it.
- **Fail closed on anything unfamiliar.** An unknown field, a missing version, a newer version,
  and an unknown operation all refuse. An ambiguity between two peers here is a hole.
- **Length-prefixed frames, not lines.** A tool argument and a tool result may hold a newline,
  so newline framing would let one request read as two.
- **Each request carries an id, and each reply echoes it.** A tool call can time out on the
  agent side while the host still works. The id lets the agent drop a late reply rather than
  read it as the answer to the next call.

One field is free-form on this wire, and that is deliberate. ``arguments`` holds what the model
passed to an MCP tool, and the tool needs those values. The host never interprets them: it
forwards them to the named tool of the one server this connection opened.

The frame cap matches #19 rather than #18. An MCP tool may return an image as a base64 content
block, and base64 grows the bytes by a third.

Both peers are asyncio streams. The MCP SDK is async, and the agent calls a tool from inside its
own event loop, so a blocking socket on either side would need a thread to bridge it.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import asdict, dataclass, fields
from typing import Any, cast

PROTOCOL_VERSION = 1

# An MCP tool result can carry an image as base64, and 32 MiB holds a browser-sized one. The cap
# stays because a peer controls the length prefix, and an uncapped prefix is an unbounded
# allocation.
MAX_FRAME_BYTES = 32 * 1024 * 1024

OP_OPEN = "open"
OP_LIST_TOOLS = "list_tools"
OP_LIST_RESOURCES = "list_resources"
OP_LIST_PROMPTS = "list_prompts"
OP_CALL_TOOL = "call_tool"
OP_READ_RESOURCE = "read_resource"
OP_GET_PROMPT = "get_prompt"

_LENGTH = struct.Struct(">I")


class ProtocolError(Exception):
    """A frame this side refuses to interpret. The caller must not guess past it."""


@dataclass(frozen=True, slots=True)
class OpenRequest:
    """Start one configured stdio MCP server and initialize its session.

    ``server_name`` is a key of ``config.tools.mcpServers``. The host resolves the command from
    its own config, so nothing about the program rides on the agent's word.
    """

    request_id: int
    server_name: str


@dataclass(frozen=True, slots=True)
class ListToolsRequest:
    """Ask the open server for its tools."""

    request_id: int


@dataclass(frozen=True, slots=True)
class ListResourcesRequest:
    """Ask the open server for its resources."""

    request_id: int


@dataclass(frozen=True, slots=True)
class ListPromptsRequest:
    """Ask the open server for its prompts."""

    request_id: int


@dataclass(frozen=True, slots=True)
class CallToolRequest:
    """Call one tool of the open server.

    ``tool_name`` is a name that server advertised. ``arguments`` holds the model's values, and
    the host forwards them without a change.
    """

    request_id: int
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReadResourceRequest:
    """Read one resource of the open server."""

    request_id: int
    uri: str


@dataclass(frozen=True, slots=True)
class GetPromptRequest:
    """Fill one prompt template of the open server."""

    request_id: int
    prompt_name: str
    arguments: dict[str, str]


@dataclass(frozen=True, slots=True)
class HostResponse:
    """The host's answer to one request.

    ``ok`` True means the host completed the operation, and ``result`` holds the MCP result
    model as JSON. ``ok`` False means the host refused or failed, and ``error`` says why.

    ``error_data`` carries an MCP ``ErrorData`` when the server itself reported one. The agent
    then raises the same ``McpError`` the SDK would raise in one process, so the retry and the
    reconnect paths behave as they did before the split.
    """

    request_id: int
    ok: bool
    result: dict[str, Any] | None
    error: str | None
    error_data: dict[str, Any] | None


HostRequest = (
    OpenRequest
    | ListToolsRequest
    | ListResourcesRequest
    | ListPromptsRequest
    | CallToolRequest
    | ReadResourceRequest
    | GetPromptRequest
)

_OP_OF_KIND: dict[type[Any], str] = {
    OpenRequest: OP_OPEN,
    ListToolsRequest: OP_LIST_TOOLS,
    ListResourcesRequest: OP_LIST_RESOURCES,
    ListPromptsRequest: OP_LIST_PROMPTS,
    CallToolRequest: OP_CALL_TOOL,
    ReadResourceRequest: OP_READ_RESOURCE,
    GetPromptRequest: OP_GET_PROMPT,
}

_KIND_OF_OP: dict[str, type[Any]] = {op: kind for kind, op in _OP_OF_KIND.items()}


def encode_request(request: HostRequest) -> bytes:
    """Turn one request into one frame payload."""
    op = _OP_OF_KIND.get(type(request))
    if op is None:
        raise ProtocolError(f"{type(request).__name__} is not a request kind of this wire")
    payload = asdict(request)
    payload["v"] = PROTOCOL_VERSION
    payload["op"] = op
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def encode_response(response: HostResponse) -> bytes:
    """Turn one reply into one frame payload."""
    payload = asdict(response)
    payload["v"] = PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_request(payload: bytes) -> HostRequest:
    """Parse one request frame, and refuse a frame that names no known operation."""
    data = _parse(payload)
    op = data.pop("op", None)
    if op is None:
        raise ProtocolError("frame names no operation")
    kind = _KIND_OF_OP.get(op) if isinstance(op, str) else None
    if kind is None:
        raise ProtocolError(f"frame names an unknown operation: {op!r}")
    return cast("HostRequest", _build(data, kind))


def decode_response(payload: bytes) -> HostResponse:
    """Parse one reply frame."""
    return cast("HostResponse", _build(_parse(payload), HostResponse))


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


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    """Send one length-prefixed frame."""
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(payload)} bytes exceeds {MAX_FRAME_BYTES}")
    writer.write(_LENGTH.pack(len(payload)) + payload)
    try:
        await writer.drain()
    except OSError as exc:
        raise ProtocolError(f"peer closed before the frame left: {exc}") from exc


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one length-prefixed frame, or raise.

    A truncated frame raises rather than returns what arrived. A peer that dies mid-write must
    not leave this side with a partial request that happens to parse.
    """
    header = await _read_exactly(reader, _LENGTH.size)
    (length,) = _LENGTH.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"peer announced {length} bytes, above {MAX_FRAME_BYTES}")
    return await _read_exactly(reader, length)


async def _read_exactly(reader: asyncio.StreamReader, count: int) -> bytes:
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError(
            f"peer closed after {len(exc.partial)} of {count} bytes"
        ) from exc
    except OSError as exc:
        raise ProtocolError(f"the connection failed after {count} bytes were asked for: {exc}") from exc


__all__ = [
    "MAX_FRAME_BYTES",
    "OP_CALL_TOOL",
    "OP_GET_PROMPT",
    "OP_LIST_PROMPTS",
    "OP_LIST_RESOURCES",
    "OP_LIST_TOOLS",
    "OP_OPEN",
    "OP_READ_RESOURCE",
    "PROTOCOL_VERSION",
    "CallToolRequest",
    "GetPromptRequest",
    "HostRequest",
    "HostResponse",
    "ListPromptsRequest",
    "ListResourcesRequest",
    "ListToolsRequest",
    "OpenRequest",
    "ProtocolError",
    "ReadResourceRequest",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
    "read_frame",
    "write_frame",
]
