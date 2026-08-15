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

**A second verb carries many texts at once (nanoinfraorg/nanoinfra#54).** One text per
connection was affordable while only a message list crossed this wire. A Responses payload holds
one item per message plus one per tool call, so a walk over it would open one connection per item
and read the credential store once per item, on every save. ``scrub_many`` carries the whole
walk in one frame.

Four decisions shape that verb, and this module states each one.

1. **The frame names its verb.** The two request shapes share no discriminating field, and a
   decoder that sniffed the shape would be guessing. So the envelope carries ``op`` beside ``v``,
   and a frame that names no known verb refuses. The single verb keeps its own name, so a caller
   with one text sends one text and never a list of one.
2. **The version rose to 2**, because the envelope changed. ``protocol.py`` gives the rule this
   follows: a newer peer may carry a field this side would ignore, and ignoring a field on this
   wire is the hole, so a mismatch refuses rather than degrades. The agent and the executor ship
   in one package and the supervisor starts the child from the same install, so the refusal
   costs no deployment a rolling upgrade.
3. **The answer is a list, and never a map.** Position is the whole pairing: the caller collected
   the texts by walking named fields, and it writes the answers back by walking the same fields
   in the same order. A map keyed by text would collapse two carriers that hold the same string,
   which a transcript produces often, and the caller could not tell which field lost its answer.
   A map keyed by index is a list with extra steps, and a missing key would read as a partial
   answer.
4. **A count that does not match is a protocol error.** The client refuses the whole batch. A
   caller that paired what arrived would write one field's scrub over another field's text, and
   a transcript with one field's credential under another field's name is worse than no scrub.

**A batch item carries no version and no verb of its own.** One frame is one version and one
verb. A per-item version would let a single frame mean two things at once, which is the
ambiguity this wire refuses everywhere else.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, cast

from nanoinfra.gates.executor.protocol import MAX_FRAME_BYTES, ProtocolError

SCRUB_PROTOCOL_VERSION = 2

#: The verb of a frame that carries one text (#41).
SCRUB_OP_ONE = "scrub_one"

#: The verb of a frame that carries many texts (#54).
SCRUB_OP_MANY = "scrub_many"

_VERSION_KEY = "v"
_OP_KEY = "op"

#: Room for the envelope of a batch frame, above the bytes of the items themselves. The keys,
#: the brackets, and the separators cost far less than this, and a frame that overshoots the
#: kernel's limit costs a whole save its provider state.
_BATCH_ENVELOPE_HEADROOM_BYTES = 1024

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


@dataclass(frozen=True, slots=True)
class ScrubBatchRequest:
    """Many texts to scrub, each one with the class of the tool that produced it (#54).

    The element type is ``ScrubRequest`` itself rather than two parallel lists. One element
    schema then serves both verbs, so a reviewer reads the pairing of a text with its class in
    one place. Two parallel lists could also arrive at unequal length, and this shape cannot.
    """

    items: list[ScrubRequest]


@dataclass(frozen=True, slots=True)
class ScrubBatchResponse:
    """The scrubbed texts in the order they were asked, or a refusal (#54).

    ``ok`` false means the scrub did not run for any of them. ``texts`` is empty then, for the
    reason ``ScrubResponse`` gives: a failure must not echo a text it could not scrub.

    One verdict covers the whole batch. A per-text verdict would invite a partial answer, and a
    partial answer is what the count check exists to make impossible.
    """

    ok: bool
    texts: list[str]
    error: str | None


def encode_scrub_request(request: ScrubRequest) -> bytes:
    return _encode(asdict(request), op=SCRUB_OP_ONE)


def encode_scrub_response(response: ScrubResponse) -> bytes:
    return _encode(asdict(response), op=None)


def encode_scrub_batch_request(request: ScrubBatchRequest) -> bytes:
    return _encode(asdict(request), op=SCRUB_OP_MANY)


def encode_scrub_batch_response(response: ScrubBatchResponse) -> bytes:
    return _encode(asdict(response), op=None)


def decode_scrub_request(payload: bytes) -> ScrubRequest:
    """Decode one single-text request frame, and refuse any other verb."""
    data, op = _frame_envelope(payload)
    if op != SCRUB_OP_ONE:
        raise ProtocolError(f"frame names verb {op!r}, and not {SCRUB_OP_ONE!r}")
    return _one_request(data)


def decode_scrub_batch_request(payload: bytes) -> ScrubBatchRequest:
    """Decode one batch request frame, and refuse any other verb."""
    data, op = _frame_envelope(payload)
    if op != SCRUB_OP_MANY:
        raise ProtocolError(f"frame names verb {op!r}, and not {SCRUB_OP_MANY!r}")
    return _batch_request(data)


def decode_scrub_request_frame(payload: bytes) -> ScrubRequest | ScrubBatchRequest:
    """Decode whichever request verb arrived. The service uses this.

    The verb comes off the frame, and the shape is never sniffed. A frame that names no known
    verb refuses, so a peer that invents a third verb gets a refusal and not a guess.
    """
    data, op = _frame_envelope(payload)
    if op == SCRUB_OP_ONE:
        return _one_request(data)
    if op == SCRUB_OP_MANY:
        return _batch_request(data)
    raise ProtocolError(f"frame names no known scrub verb: {op!r}")


def decode_scrub_response(payload: bytes) -> ScrubResponse:
    data, _ = _frame_envelope(payload)
    _exact_fields(data, ScrubResponse)
    if not isinstance(data["ok"], bool):
        raise ProtocolError("field 'ok' is not a boolean")
    if not isinstance(data["text"], str):
        raise ProtocolError("field 'text' is not a string")
    return ScrubResponse(ok=data["ok"], text=data["text"], error=_error_field(data["error"]))


def decode_scrub_batch_response(payload: bytes) -> ScrubBatchResponse:
    data, _ = _frame_envelope(payload)
    _exact_fields(data, ScrubBatchResponse)
    if not isinstance(data["ok"], bool):
        raise ProtocolError("field 'ok' is not a boolean")
    raw_texts = data["texts"]
    if not isinstance(raw_texts, list):
        raise ProtocolError("field 'texts' is not a list")
    texts = cast("list[Any]", raw_texts)
    for index, text in enumerate(texts):
        if not isinstance(text, str):
            raise ProtocolError(f"answer {index} is not a string")
    return ScrubBatchResponse(
        ok=data["ok"],
        texts=cast("list[str]", texts),
        error=_error_field(data["error"]),
    )


def split_scrub_batch(items: Sequence[ScrubRequest]) -> Iterator[list[ScrubRequest]]:
    """Cut *items* into batches that each fit one frame.

    A peer controls the length prefix, so ``protocol.py`` caps a frame at ``MAX_FRAME_BYTES``.
    A whole transcript of tool output can pass that cap, and a frame the writer refuses costs
    the save its provider state. So the client sends a few frames rather than one oversized one.

    The trade-off is stated rather than hidden: each frame resolves the sentinels again, so a
    split costs one more store read. That is the safe direction, because a sentinel set is only
    ever more current than the one before it.

    One item above the whole budget still goes alone. Such a frame fails at the writer, and the
    caller then persists no state, which is the fail-closed answer for a text nobody scrubbed.
    """
    budget = MAX_FRAME_BYTES - _BATCH_ENVELOPE_HEADROOM_BYTES
    batch: list[ScrubRequest] = []
    used = 0
    for item in items:
        # The exact encoded cost of this item, plus one byte for the separator that joins it.
        cost = len(json.dumps(asdict(item), ensure_ascii=False).encode("utf-8")) + 1
        if batch and used + cost > budget:
            yield batch
            batch, used = [], 0
        batch.append(item)
        used += cost
    if batch:
        yield batch


def _encode(payload: dict[str, Any], *, op: str | None) -> bytes:
    payload[_VERSION_KEY] = SCRUB_PROTOCOL_VERSION
    if op is not None:
        # Only a request names a verb. The caller of a response already knows what it asked, and
        # the two response shapes differ in their own fields, so a verb there would be decoration.
        payload[_OP_KEY] = op
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _frame_envelope(payload: bytes) -> tuple[dict[str, Any], str | None]:
    """Parse one frame, check the version, and return the rest plus the named verb.

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
    version = data.pop(_VERSION_KEY, None)
    if version is None:
        raise ProtocolError("frame carries no protocol version")
    if version != SCRUB_PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version {version!r} is not {SCRUB_PROTOCOL_VERSION}")

    op = data.pop(_OP_KEY, None)
    if op is not None and not isinstance(op, str):
        raise ProtocolError("field 'op' is not a string")
    return data, op


def _exact_fields(data: dict[str, Any], kind: type[Any]) -> None:
    """Refuse a frame body that does not match *kind* exactly."""
    expected = {field.name for field in fields(kind)}
    unknown = set(data) - expected
    if unknown:
        raise ProtocolError(f"frame carries unknown field(s): {sorted(unknown)}")
    missing = expected - set(data)
    if missing:
        raise ProtocolError(f"frame is missing field(s): {sorted(missing)}")


def _one_request(data: dict[str, Any]) -> ScrubRequest:
    _exact_fields(data, ScrubRequest)
    return _item(data, where="frame")


def _batch_request(data: dict[str, Any]) -> ScrubBatchRequest:
    _exact_fields(data, ScrubBatchRequest)
    raw_items = data["items"]
    if not isinstance(raw_items, list):
        raise ProtocolError("field 'items' is not a list")
    items: list[ScrubRequest] = []
    for index, raw in enumerate(cast("list[Any]", raw_items)):
        if not isinstance(raw, dict):
            raise ProtocolError(f"batch item {index} is not an object")
        # A copy, because _exact_fields reads the keys of one item and the caller keeps the frame.
        fields_of_item = dict(cast("dict[str, Any]", raw))
        _exact_fields(fields_of_item, ScrubRequest)
        items.append(_item(fields_of_item, where=f"batch item {index}"))
    return ScrubBatchRequest(items=items)


def _item(data: dict[str, Any], *, where: str) -> ScrubRequest:
    for name in ("text", "capability_class"):
        if not isinstance(data[name], str):
            raise ProtocolError(f"{where}: field {name!r} is not a string")
    return ScrubRequest(**cast("dict[str, str]", data))


def _error_field(error: Any) -> str | None:
    if error is not None and not isinstance(error, str):
        raise ProtocolError("field 'error' is not a string or null")
    return error


def default_scrub_socket_path(execute_socket_path: Path | str) -> Path:
    """Where the scrub socket lives beside one execute socket.

    The name carries the execute socket's own stem. Two executors can share one run directory,
    because the SDK names each execute socket after its process (#21). A fixed name would let
    the second executor unlink the first one's scrub socket.
    """
    execute = Path(execute_socket_path)
    return execute.parent / f"{execute.name.removesuffix('.sock')}{SCRUB_SOCKET_SUFFIX}"


__all__ = [
    "SCRUB_OP_MANY",
    "SCRUB_OP_ONE",
    "SCRUB_PROTOCOL_VERSION",
    "SCRUB_SOCKET_SUFFIX",
    "ScrubBatchRequest",
    "ScrubBatchResponse",
    "ScrubRequest",
    "ScrubResponse",
    "decode_scrub_batch_request",
    "decode_scrub_batch_response",
    "decode_scrub_request",
    "decode_scrub_request_frame",
    "decode_scrub_response",
    "default_scrub_socket_path",
    "encode_scrub_batch_request",
    "encode_scrub_batch_response",
    "encode_scrub_request",
    "encode_scrub_response",
    "split_scrub_batch",
]
