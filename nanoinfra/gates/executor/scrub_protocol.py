"""The scrub wire between the agent and the executor -- nanoinfraorg/nanoinfra#41.

#18 moved the credential store behind the executor. ``workspace_secret_sentinels`` then
decrypted every secret of the workspace inside the agent process, on every turn that persisted
a transcript. So the process the model runs in held the whole credential store. This wire moves
the scrub to the side that owns the store, and the agent sends text instead.

**Why a second socket, and not a verb on the execute wire.** The execute request holds
structured fields only, and ``protocol.py`` says why: #14 keeps model text out of the approval
prompt, and a free-form member on that request would let a peer smuggle a description of intent
into the executor. A scrub request is nothing but free-form model text, so it does not belong
there. Two more reasons follow the first. An execute connection blocks for as long as an
operator takes to answer an approval (#38), and a persist must not queue behind that wait. And
the two frames share no field, so one strict decoder would have to accept two shapes, which is
the ambiguity the execute wire refuses by design.

**What a blocked or slow scrub costs a turn.** The client holds a short deadline, and a timeout
reads the same as an unreachable socket. The caller then persists a marker in place of the
text. So a slow scrubber costs a turn its transcript text, never the turn itself, and never a
raw credential value in a durable file.

**What the wire carries.** One text, and the capability class of the tool that produced it. The
class is on the frame because #17 drops a ``credential.access`` result whole rather than scrub
it value by value, and only the side with the sentinels can name which secret matched. Nothing
else rides here. No secret name, no secret value, no workspace path, and no request id. The
reply carries the scrubbed text and a verdict.

**No record holds the text.** Neither side logs it, and the executor keeps no request record.
The text exists in the two process memories for the length of one call.

**The class is a hint, and never an authorization.** A compromised agent can name any class, and
the worst it buys is a value-by-value scrub of a result that should have dropped whole. Such an
agent already holds the text it sent, so it gains nothing it did not have. The decisions that
matter stay with the executor: which values to remove, and which name to write.

**The honest limit: this socket confirms an exact value.** A caller can send a guess and read
whether the reply names a secret. The scrub replaces a whole value and never a prefix, and it
ignores a value below eight characters, so the answer cannot be searched piece by piece. A guess
has to be the entire value. State this plainly in a review, and compare it with what the same
process held before #41, which was every plaintext at once.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, cast

from nanoinfra.gates.executor.protocol import ProtocolError

SCRUB_PROTOCOL_VERSION = 1

# What the scrub socket adds to the execute socket's name. The two live in one directory,
# because the agent reaches both. The operator socket goes the other way (#38): it sits in a
# private subdirectory, since the agent must never answer its own approval.
#
# The name costs six bytes more than the execute name. The supervisor caps an execute path at
# 100 bytes, and Linux copies 108 bytes into ``sun_path``, so the derived path still binds.
SCRUB_SOCKET_SUFFIX = ".scrub.sock"


@dataclass(frozen=True, slots=True)
class ScrubRequest:
    """One text to scrub, and the class of the tool that produced it.

    ``capability_class`` is empty when the caller knows none. An empty class asks for a
    value-by-value scrub, which is the answer for every class other than ``credential.access``.
    """

    text: str
    capability_class: str


@dataclass(frozen=True, slots=True)
class ScrubResponse:
    """The scrubbed text, or a refusal.

    ``ok`` false means the scrub did not run. ``text`` is empty then, because a failure must
    not echo the text it could not scrub. The caller persists a marker instead.
    """

    ok: bool
    text: str
    error: str | None


def encode_scrub_request(request: ScrubRequest) -> bytes:
    payload = asdict(request)
    payload["v"] = SCRUB_PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def encode_scrub_response(response: ScrubResponse) -> bytes:
    payload = asdict(response)
    payload["v"] = SCRUB_PROTOCOL_VERSION
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_scrub_request(payload: bytes) -> ScrubRequest:
    data = _frame_fields(payload, ScrubRequest)
    for name in ("text", "capability_class"):
        if not isinstance(data[name], str):
            raise ProtocolError(f"field {name!r} is not a string")
    return ScrubRequest(**cast("dict[str, str]", data))


def decode_scrub_response(payload: bytes) -> ScrubResponse:
    data = _frame_fields(payload, ScrubResponse)
    if not isinstance(data["ok"], bool):
        raise ProtocolError("field 'ok' is not a boolean")
    if not isinstance(data["text"], str):
        raise ProtocolError("field 'text' is not a string")
    error = data["error"]
    if error is not None and not isinstance(error, str):
        raise ProtocolError("field 'error' is not a string or null")
    return ScrubResponse(ok=data["ok"], text=data["text"], error=error)


def _frame_fields(payload: bytes, kind: type[Any]) -> dict[str, Any]:
    """Parse one frame, and refuse anything that does not match *kind* exactly.

    The rules are the execute wire's rules, for the reasons that wire gives. A missing version
    refuses, another version refuses, and an extra field refuses. A tolerated extra field is an
    ambiguity between two peers, and an ambiguity in this position is a hole.
    """
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
    if version != SCRUB_PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version {version!r} is not {SCRUB_PROTOCOL_VERSION}")

    expected = {field.name for field in fields(kind)}
    unknown = set(data) - expected
    if unknown:
        raise ProtocolError(f"frame carries unknown field(s): {sorted(unknown)}")
    missing = expected - set(data)
    if missing:
        raise ProtocolError(f"frame is missing field(s): {sorted(missing)}")
    return data


def default_scrub_socket_path(execute_socket_path: Path | str) -> Path:
    """Where the scrub socket lives beside one execute socket.

    The name carries the execute socket's own stem. Two executors can share one run directory,
    because the SDK names each execute socket after its process (#21). A fixed name would let
    the second executor unlink the first one's scrub socket.
    """
    execute = Path(execute_socket_path)
    return execute.parent / f"{execute.name.removesuffix('.sock')}{SCRUB_SOCKET_SUFFIX}"


__all__ = [
    "SCRUB_PROTOCOL_VERSION",
    "SCRUB_SOCKET_SUFFIX",
    "ScrubRequest",
    "ScrubResponse",
    "decode_scrub_request",
    "decode_scrub_response",
    "default_scrub_socket_path",
    "encode_scrub_request",
    "encode_scrub_response",
]
