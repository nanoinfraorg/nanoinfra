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

**Version 2 carries the origin path (#38).** #13 decides path independence, and it cannot do
that without the path the request arrived on. So the field is mandatory, and a version 1 frame
gets a refusal. An agent that does not state its path must not execute. The client and the
server ship together in one package, so no rolling deploy needs the older frame.

**Version 3 carries the origin identity (#47, item 10).** ``gates.identityIndependence`` lets a
second person on one path replace a second path, and that rule needs the person the origin path
authenticated. The field set changed, so the version rose. A version 2 frame gets a refusal
rather than a default, because this side would otherwise read the absent field as "no identity",
which is a value #13 accepts, and the frame would execute on a fact the peer never sent.

The executor treats ``origin_path`` as the agent's assertion about itself. A compromised agent
can state any path. That is why the *answer* arrives on a separate socket the executor owns,
and why the approver set lives in git-reviewed config rather than in a reachability list.
``origin_actor`` inherits that trust model exactly: a compromised agent can claim that the
request came from another person. ``gates.identityIndependence`` therefore defaults to false,
and ``nanoinfra/gates/approvals.py`` states what a deployment gives up when it turns the flag
on.

**Version 5 carries a second request kind: a data connector call.** A connector call is
performed in the executor for the same reasons a command is -- the credential lives here, the
approval socket is here, and the audit record is written here -- so it needs a frame. The kind
travels in the envelope beside the version, and an unknown kind refuses: a frame this side
cannot name must not fall through to the one it can.

**Version 6 carries a third kind: a secret write.** The store belongs to the executor account,
so the gateway could not write the file at all -- the Secrets page failed with a permission
error on every deployment with the privilege split. The gateway holds the encryption key, so it
encrypts and this wire carries **ciphertext**: no plaintext crosses the socket, and the write
lands with the ownership the executor needs by construction. Making the directory group-writable
would have been the ten-minute answer and would have let the process the model steers *replace*
a credential, which is worse than reading one.

``ConnectorRequest.arguments_json`` is the one field that carries caller-shaped content, and it
is not the free-form member this wire refuses. Every key in it must appear in the operation's
own declared parameter schema, which the executor reads from the installed package rather than
from the frame, and a key that does not appear is refused before anything is sent. So the
bound is the manifest in this repository, not the sender's word.
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import asdict, dataclass, field, fields
from typing import Any, cast

PROTOCOL_VERSION = 6

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
    model can propose. #38 issues every nonce inside the executor, so the agent holds none, and
    the executor refuses a request that carries one.

    ``origin_path`` names the channel that raised the request, in the vocabulary of
    ``Approver.channel``. #13 refuses an approval that arrives on this same path, so the field
    is what makes path independence checkable. The wire requires it, and the default here
    serves in-process construction only, where a missing path fails closed at the gate.

    ``origin_actor`` names the person that path authenticated, in the vocabulary of
    ``Approver.sender``. ``gates.identityIndependence`` reads it (#47, item 11). ``None`` means
    the channel authenticated nobody, and it is never a wildcard that matches every person: #13
    falls back to the path rule alone for that case. ``None`` and the empty string are different
    facts on this wire, so a channel with no sender sends null rather than empty text.
    """

    server_id_or_name: str
    command: str
    session_id: str | None
    execution_context: str
    preview_requested: bool
    timeout_s: str | None
    token_nonce: str | None
    origin_path: str | None = None
    origin_actor: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorRequest:
    """One request to perform one declared operation of one active connector.

    ``connector`` and ``operation`` are names, never a URL and never a method: the executor
    reads the method, the path and the capability class out of the installed manifest, so the
    agent cannot name a call the package did not declare. That is the same rule
    ``server_id_or_name`` follows -- nothing about the target rides on the agent's word.

    ``arguments_json`` is a JSON object of the call arguments. The executor validates it against
    the operation's declared schema and refuses an undeclared key, so the frame carries content
    without carrying a free-form member.

    ``token_nonce`` exists for one reason: to be refused. The executor issues every nonce and
    hands none to the agent, so a nonce arriving on this wire came from model-visible text.
    """

    connector: str
    operation: str
    arguments_json: str
    session_id: str | None
    execution_context: str
    preview_requested: bool
    token_nonce: str | None
    origin_path: str | None = None
    origin_actor: str | None = None


# What a secret write may do. Three verbs and no fourth: a rename is an update, and there is no
# "read", because reading a plaintext is the one thing this wire has never carried.
SECRET_VERBS = frozenset({"create", "update", "delete"})


@dataclass(frozen=True, slots=True)
class SecretWriteRequest:
    """One write to the credential store, performed by the account that owns it.

    ``ciphertext_b64`` is already encrypted by the caller. The executor writes bytes it cannot
    read, which is the property that makes this wire safe to add: it moves a *file write* across
    the boundary and not a secret.

    ``verb`` is checked against ``SECRET_VERBS``. A delete carries no ciphertext and no name; a
    create and an update carry both.
    """

    verb: str
    secret_id: str
    name: str
    # `secret_kind` and not `kind`: the envelope's discriminator is `kind`, and the decoder
    # strips envelope keys before matching fields, so a field of that name went missing on
    # every frame. Found by round-tripping one.
    secret_kind: str
    provider_id: str
    ciphertext_b64: str
    created_at: str
    updated_at: str
    origin_path: str | None = None
    origin_actor: str | None = None


@dataclass(frozen=True, slots=True)
class ExecuteResponse:
    """The executor's answer. ``reason`` carries the gate's words for a refusal."""

    ok: bool
    output: str
    exit_code: int | None
    error: str | None
    reason: str
    # Whether this refusal ends the retry loop for the session (#15). The default is terminal,
    # because a response that says nothing must still stop a brute force. The executor marks a
    # refusal that describes the deployment rather than the action, and the tool then leaves the
    # latch alone: a configuration gap gives the agent nothing to change, and an operator who
    # clears a latch as routine stops reading it (#42).
    terminal: bool = True
    # What the gate would answer for this action, filled on a preview only (#179). A preview
    # asks nobody and opens no secret store, so every field below is a hypothetical: it says
    # what a real run would meet, and authorizes nothing. `preview_hosts` and `preview_command`
    # are the two halves of the standing grant that would permit it, in the form config takes.
    preview_outcome: str | None = None
    preview_reason: str = ""
    preview_grant_id: str | None = None
    preview_scope: str | None = None
    preview_hosts: list[str] = field(default_factory=list[str])
    preview_command: str = ""
    preview_credential_outcome: str | None = None
    preview_credential_reason: str = ""


# What each request kind is called on the wire. The name travels in the envelope, so one
# socket serves two shapes and neither can be read as the other.
KIND_EXECUTE = "execute"
KIND_CONNECTOR = "connector"
KIND_SECRET_WRITE = "secret_write"

_REQUEST_KINDS: dict[str, type[Any]] = {
    KIND_EXECUTE: ExecuteRequest,
    KIND_CONNECTOR: ConnectorRequest,
    KIND_SECRET_WRITE: SecretWriteRequest,
}
_KIND_NAMES: dict[type[Any], str] = {value: key for key, value in _REQUEST_KINDS.items()}

# The envelope keys, which are not fields of any request. A response carries the version alone.
_REQUEST_ENVELOPE = frozenset({"v", "kind"})
_RESPONSE_ENVELOPE = frozenset({"v"})

Request = ExecuteRequest | ConnectorRequest | SecretWriteRequest


def encode_request(request: Request) -> bytes:
    kind = _KIND_NAMES.get(type(request))
    if kind is None:
        raise ProtocolError(f"{type(request).__name__} is not a request kind on this wire")
    payload = asdict(request)
    payload["v"] = PROTOCOL_VERSION
    payload["kind"] = kind
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def encode_response(response: ExecuteResponse) -> bytes:
    payload = asdict(response)
    payload["v"] = PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_request(payload: bytes) -> Request:
    """Parse one request frame, choosing the shape from the kind it declares."""
    kind = _peek_kind(payload)
    target = _REQUEST_KINDS.get(kind)
    if target is None:
        raise ProtocolError(f"request kind {kind!r} is not one of {sorted(_REQUEST_KINDS)}")
    return cast("Request", _decode(payload, target, envelope=_REQUEST_ENVELOPE))


def decode_response(payload: bytes) -> ExecuteResponse:
    return _decode(payload, ExecuteResponse)


def _peek_kind(payload: bytes) -> str:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("frame is not an object")
    kind = cast("dict[str, Any]", raw).get("kind")
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("request frame carries no kind")
    return kind


def _decode(
    payload: bytes, kind: type[Any], *, envelope: frozenset[str] = _RESPONSE_ENVELOPE
) -> Any:
    """Parse one frame into *kind*, and refuse anything that does not match exactly."""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("frame is not an object")

    data: dict[str, Any] = cast("dict[str, Any]", raw)
    for key in envelope - {"v"}:
        data.pop(key, None)
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
    "KIND_CONNECTOR",
    "KIND_EXECUTE",
    "KIND_SECRET_WRITE",
    "SECRET_VERBS",
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "ConnectorRequest",
    "ExecuteRequest",
    "ExecuteResponse",
    "Request",
    "SecretWriteRequest",
    "ProtocolError",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
    "read_frame",
    "write_frame",
]
