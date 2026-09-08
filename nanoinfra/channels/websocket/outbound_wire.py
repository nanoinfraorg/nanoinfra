"""Project outbound events onto the WebUI wire protocol.

This module answers one question and refuses every other: *what does a frame
look like*. It builds bodies. It does not know that a connection exists, it
does not know that delivery can fail, and it never awaits anything.

That separation is the reason the module exists. The channel's runtime used to
hold both halves -- deciding a frame's shape and pushing it down a socket -- and
the file grew past two thousand lines because every new frame type and every new
delivery concern landed in the same place. Per-connection delivery state
(`outbound_delivery.py`) now sits on one side of the seam and frame projection on
the other, so a change to how bytes leave cannot change what the bytes say.

Two contracts a caller has to honour, both learned from the code this replaces:

* **The returned dict is mutable on purpose.** The transcript writer
  (`WebUITranscripts.prepare_event`) annotates the event in place -- it is what
  puts `turn_id` and `source` on the frame -- and the annotated dict is what
  gets serialised. So the order is always: encode, persist, serialise, deliver.
  Encoding into a frozen or typed-and-narrowed shape would mean copying it back
  out again at every call site, which is a third representation to keep in step
  with the two that already have to agree (the encoder and the transcript
  reader). A `dict[str, Any]` here is the honest type.

* **An absent key is not the same as a zero.** Every encoder omits a field whose
  value means nothing rather than sending a default. That is what keeps a
  `cache_read_tokens` no provider reported from rendering as "0% cached", and a
  model name the deployment never configured from rendering as a guess. Several
  encoders return `None` for the whole frame on the same principle: a
  `runtime_model_updated` with no model in it is not a frame worth a socket
  write.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from nanoinfra.bus.events import OutboundMessage
    from nanoinfra.bus.outbound_events import ProgressEvent
    from nanoinfra.providers.base import LLMUsage

from nanoinfra.bus.events import OUTBOUND_META_AGENT_UI
from nanoinfra.webui.transcript import WEBUI_TRANSCRIPT_INCOMPLETE_KEY

#: A frame body, ready to be persisted and then serialised. See the note on
#: mutability in the module docstring.
WireFrame = dict[str, Any]


class MediaRewriter(Protocol):
    """The two media operations frame projection needs, and no others.

    Declared as a protocol rather than imported as a concrete class so this
    module has no dependency on the gateway's service graph: the encoders can be
    exercised with a stub that returns its input.
    """

    def rewrite_local_markdown_images(self, text: str) -> str: ...

    def sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None: ...


class StreamTextBuffers:
    """Accumulate a stream's deltas so its ``stream_end`` can carry whole text.

    A ``delta`` frame is one fragment as the model produced it, which is the
    wrong unit for rewriting a markdown image reference: the path can straddle
    two fragments. So the fragments are kept here until the stream closes, and
    the rewrite runs once over the joined text.

    Keyed by ``(chat_id, stream_id)`` and not by ``chat_id`` alone, because a
    chat can have a stream open while a merged continuation opens the next one.
    ``merge_next`` keeps the buffer alive across exactly that boundary -- the
    stream ends on the wire, but the text carries on into the segment after it.

    This is projection state, not delivery state: it decides what a frame
    *says*, so it lives here rather than beside the send queues.
    """

    def __init__(self) -> None:
        self._buffers: dict[tuple[str, str], list[str]] = {}

    def key(self, chat_id: str, stream_id: str | None) -> tuple[str, str]:
        return (chat_id, str(stream_id or ""))

    def append(self, key: tuple[str, str], delta: str) -> None:
        self._buffers.setdefault(key, []).append(delta)

    def take(self, key: tuple[str, str], *, keep: bool) -> list[str]:
        """The fragments buffered under *key*, retaining them when ``keep``."""
        if keep:
            return self._buffers.setdefault(key, [])
        return self._buffers.pop(key, [])

    def discard(self, key: tuple[str, str]) -> None:
        self._buffers.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._buffers


# -- Control frames ----------------------------------------------------------


def encode_control_event(event: str, fields: dict[str, Any]) -> WireFrame:
    """One control frame (``attached``, ``error``, ``ready``, ...).

    Unlike the application frames below, a control frame's fields are decided by
    its caller, so there is nothing to validate here. It still goes through an
    encoder so that *every* frame on this wire is built in one module.
    """
    body: WireFrame = {"event": event}
    body.update(fields)
    return body


# -- Chat frames -------------------------------------------------------------


def encode_message(
    msg: OutboundMessage,
    *,
    media: MediaRewriter,
    progress: ProgressEvent | None,
) -> tuple[WireFrame, str]:
    """A conversational reply or an intermediate breadcrumb.

    Returns the frame and the *original* text beside it. They differ: the wire
    text has local image paths rewritten into fetchable URLs, and the transcript
    has to keep the original, or a replay would embed URLs whose signatures have
    since expired.
    """
    text = msg.content
    body: WireFrame = {
        "event": "message",
        "chat_id": msg.chat_id,
        "text": media.rewrite_local_markdown_images(text),
    }
    if msg.media:
        body["media"] = msg.media
        urls: list[dict[str, str]] = []
        for entry in msg.media:
            signed = media.sign_or_stage_media_path(Path(entry))
            if signed is not None:
                urls.append(signed)
        if urls:
            body["media_urls"] = urls
    if msg.reply_to:
        body["reply_to"] = msg.reply_to
    lat = msg.metadata.get("latency_ms")
    if isinstance(lat, (int, float)):
        body["latency_ms"] = int(lat)
    if progress and progress.tool_events:
        body["tool_events"] = progress.tool_events
    agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
    if agent_ui is not None:
        body["agent_ui"] = agent_ui
    # Mark intermediate agent breadcrumbs (tool-call hints, generic progress
    # strings) so WS clients can render them as subordinate trace rows rather
    # than conversational replies.
    if progress and progress.tool_hint:
        body["kind"] = "tool_hint"
    elif progress:
        body["kind"] = "progress"
    return body, text


def message_phase(body: WireFrame) -> str:
    """Which transcript phase a ``message`` frame belongs to.

    Derived from the frame rather than from the event, because ``kind`` is the
    thing that decides it and ``kind`` is set during encoding.
    """
    return "activity" if body.get("kind") in ("tool_hint", "progress") else "answer"


def encode_reasoning_delta(
    chat_id: str,
    delta: str,
    *,
    stream_id: str | None,
) -> WireFrame:
    """One chunk of model reasoning.

    Mirrors ``encode_delta``'s shape so clients receive a stream that opens,
    updates in place, and closes -- rendered above the active assistant bubble
    with a shimmer header until the matching ``reasoning_end`` arrives.
    """
    body: WireFrame = {
        "event": "reasoning_delta",
        "chat_id": chat_id,
        "text": delta,
    }
    if stream_id is not None:
        body["stream_id"] = stream_id
    return body


def encode_reasoning_end(chat_id: str, *, stream_id: str | None) -> WireFrame:
    """Close the current reasoning stream segment for in-place renderers."""
    body: WireFrame = {"event": "reasoning_end", "chat_id": chat_id}
    if stream_id is not None:
        body["stream_id"] = stream_id
    return body


def encode_file_edit(chat_id: str, edits: list[dict[str, Any]]) -> WireFrame:
    """The file edits one turn performed, for the diff strip."""
    return {"event": "file_edit", "chat_id": chat_id, "edits": edits}


def encode_delta(
    chat_id: str,
    delta: str,
    *,
    buffers: StreamTextBuffers,
    media: MediaRewriter,
    stream_id: str | None,
    stream_end: bool,
    resuming: bool,
    merge_next: bool,
    step_usage: LLMUsage | None,
    step_ms: int | None,
) -> WireFrame:
    """One streaming segment: a ``delta``, or the ``stream_end`` that closes it.

    ``step_usage`` and ``step_ms`` describe the single provider call this segment
    came from (#208). They are accepted here and nowhere else because this is
    the channel with a surface that can show them.
    """
    key = buffers.key(chat_id, stream_id)
    if stream_end:
        body: WireFrame = {"event": "stream_end", "chat_id": chat_id}
        buffered = buffers.take(key, keep=merge_next)
        if delta:
            buffered.append(delta)
        full_text = "".join(buffered)
        rewritten = media.rewrite_local_markdown_images(full_text)
        if delta or rewritten != full_text:
            body["text"] = rewritten
    else:
        body = {"event": "delta", "chat_id": chat_id, "text": delta}
        buffers.append(key, delta)
    if stream_id is not None:
        body["stream_id"] = stream_id
    if stream_end and resuming:
        body["resuming"] = True
    if stream_end and merge_next:
        body["merge_next"] = True
    if stream_end and step_usage is not None:
        # The same projection the turn's usage takes, so one reader parses both.
        # A key is absent when its number means nothing -- which is what keeps a
        # `cache_read_tokens` the provider never reported from rendering as 0%
        # cached.
        body["usage"] = step_usage.to_turn_dict()
    if stream_end and step_ms is not None:
        body["duration_ms"] = int(step_ms)
    return body


def encode_turn_end(
    chat_id: str,
    *,
    latency_ms: int | None,
    goal_state: dict[str, Any] | None,
    usage: LLMUsage | None,
    prompt_manifest: dict[str, Any] | None,
    agent: str | None,
) -> WireFrame:
    """The agent has fully finished processing the current turn.

    No ``turn_id`` is set here. The transcript writer adds it during
    persistence, and the frame that goes on the wire is the annotated one -- see
    the module docstring on why encoding hands back a mutable dict.
    """
    body: WireFrame = {"event": "turn_end", "chat_id": chat_id}
    if latency_ms is not None:
        body["latency_ms"] = int(latency_ms)
    if goal_state is not None:
        body["goal_state"] = goal_state
    if usage is not None:
        # Beside the latency, and persisted with it -- so a reloaded thread shows
        # the same number as a live one rather than losing it on refresh (#202).
        body["usage"] = usage.to_turn_dict()
    if prompt_manifest:
        # Names and sizes. A manifest that carried the prompt's text would be a
        # second copy of the conversation persisted where nobody expects one
        # (#203).
        body["prompt"] = prompt_manifest
    if agent:
        # Persisted with the rest of this body, so a reloaded thread says which
        # agent answered each turn (#248). Omitted for the default agent: a name
        # the deployment never configured would be a guess, and every turn today
        # is the default one.
        body["agent"] = agent
    return body


def turn_end_transcript_overrides(
    *,
    prior_persistence_failure: bool,
) -> dict[str, Any] | None:
    """The durable "this transcript is incomplete" marker, when one is owed.

    A completion that follows a failed write of the turn's own answer is still a
    completion, but the persisted thread is missing text. The marker travels
    with the record rather than the frame, so the HTTP replay path can recover
    it from session history after a gateway restart.
    """
    return {WEBUI_TRANSCRIPT_INCOMPLETE_KEY: True} if prior_persistence_failure else None


def encode_goal_state(chat_id: str, blob: dict[str, Any]) -> WireFrame:
    """A persisted goal-state snapshot for one chat (multi-chat isolation)."""
    return {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}


def encode_goal_status(
    chat_id: str,
    status: str,
    *,
    started_at: float | None,
    turn_id: str | None,
) -> WireFrame:
    """A turn started or finished, with the wall-clock hint the strip needs."""
    body: WireFrame = {"event": "goal_status", "chat_id": chat_id, "status": status}
    if status == "running" and started_at is not None:
        body["started_at"] = started_at
    if turn_id:
        body["turn_id"] = turn_id
    return body


def encode_session_updated(chat_id: str, *, scope: str | None) -> WireFrame:
    """A session row should refresh itself from the HTTP routes."""
    body: WireFrame = {"event": "session_updated", "chat_id": chat_id}
    if scope:
        body["scope"] = scope
    return body


def encode_runtime_model_updated(
    *,
    model_name: Any,
    model_preset: Any = None,
) -> WireFrame | None:
    """The runtime's model changed, for every open connection.

    ``None`` when the update names no model: the values arrive off the bus as
    ``Any``, and a frame announcing an empty model name would make every client
    render a blank where a model should be.
    """
    if not isinstance(model_name, str) or not model_name.strip():
        return None
    body: WireFrame = {
        "event": "runtime_model_updated",
        "model_name": model_name.strip(),
    }
    if isinstance(model_preset, str) and model_preset.strip():
        body["model_preset"] = model_preset.strip()
    return body


def encode_diagram_updated(
    *,
    diagram_id: Any,
    kind: Any = "updated",
    revision: Any = None,
) -> WireFrame | None:
    """A saved diagram changed, for every open connection.

    Not scoped to a chat, the same as ``encode_runtime_model_updated``: the
    Diagrams view has no chat to attach to. The frame carries the id and the
    revision only -- a client that cares refetches through the authenticated
    REST route, so no diagram body (and no config field) travels here.
    """
    if not isinstance(diagram_id, str) or not diagram_id.strip():
        return None
    body: WireFrame = {
        "event": "diagram_updated",
        "diagram_id": diagram_id.strip(),
        "kind": kind if isinstance(kind, str) and kind.strip() else "updated",
    }
    if isinstance(revision, int) and not isinstance(revision, bool):
        body["revision"] = revision
    return body


def encode_turn_model_updated(chat_id: str, *, model_name: Any) -> WireFrame | None:
    """Which model is handling one chat's current request."""
    if not isinstance(model_name, str) or not model_name.strip():
        return None
    return {
        "event": "turn_model_updated",
        "chat_id": chat_id,
        "model_name": model_name.strip(),
    }
