"""WebUI workspace upload envelope handling.

Shaped after ``transcription_ws.py``, and over the socket for the same reason the
message attachments are: this transport exposes no HTTP body (see the chunked
diagram headers in ``ws_http.py``), and bulk bytes do not belong in header lines.

The WebSocket channel owns transport and fan-out; this module owns the one action.
Containment is not re-implemented here -- ``file_browser.resolve_child`` is the
single gate, so an upload lands under the same rules a listing or a delete does.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanoinfra.security.workspace_access import WorkspaceScope
from nanoinfra.webui.file_browser import (
    WebUIFileBrowserError,
    directory_listing_payload,
    ensure_relative_directory,
    resolve_child,
)

_MAX_REQUEST_ID_LENGTH = 80

#: Decoded bytes one *chunk* may carry, which is a bound on one frame and no longer a
#: bound on a file.
#:
#: The binding constraint is the transport: ``WebSocketConfig.max_message_bytes``
#: (36 MB by default) is handed to ``websockets`` as ``max_size``, and a larger frame is
#: dropped with close code 1009 before any of this runs. Base64 inflates by 4/3, so this
#: is what a frame can hold with room to spare. A file larger than this arrives as
#: several chunks instead of being refused -- a 20 MB ceiling on a file was an artefact
#: of the transport leaking into the feature.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: Bytes one whole file may accumulate across its chunks.
#:
#: A real limit rather than an artefact: the old 20 MB was the transport's frame size
#: leaking into the feature, and chunking removed that. This one is a bound on what a
#: single client can make this process write while a session is open, and it is the
#: number to change -- together with its twin in `webui/src/lib/dropped-files.ts`,
#: which a test pins to this one.
MAX_UPLOAD_TOTAL_BYTES = 100 * 1024 * 1024

#: Chunks one upload may be split into. With ``MAX_UPLOAD_BYTES`` per chunk this is far
#: past ``MAX_UPLOAD_TOTAL_BYTES``, so the total is what actually binds; this only keeps
#: a client from opening a session that claims an absurd number of parts.
MAX_UPLOAD_CHUNKS = 4096

_DATA_URL_RE = re.compile(r"^data:[^;,]*(?:;[^;,]*)*;base64,(.*)$", re.DOTALL)

#: Directory levels one upload may create beneath its parent. A bound on what a single
#: browser-supplied path can make the server do, not a judgement about how deep a real
#: tree goes -- `webkitRelativePath` comes off an untrusted client like anything else.
MAX_RELATIVE_DEPTH = 32


def _decode_data_url(raw: Any) -> bytes | None:
    """The payload of a ``data:<mime>;base64,<payload>`` URL, or ``None`` if malformed.

    The mime is parsed and discarded on purpose: the name on disk comes from the
    operator, so a claimed content type would decide nothing here and trusting it
    to pick an extension is how an upload gets a name nobody asked for.
    """
    if not isinstance(raw, str):
        return None
    match = _DATA_URL_RE.match(raw)
    if not match:
        return None
    try:
        return base64.b64decode(match.group(1), validate=True)
    except Exception:
        return None


@dataclass
class UploadSession:
    """One in-progress chunked upload.

    The resolved target is stored, not re-derived per chunk: containment is decided
    once, on the first chunk, so a later chunk cannot name somewhere else and have
    the bytes already written follow it there.
    """

    target: Path
    temp: Path
    chunk_count: int
    received: int = 0
    written: int = 0
    parent: str | None = None
    include_hidden: bool = False
    seen: set[int] = field(default_factory=set)

    def discard(self) -> None:
        self.temp.unlink(missing_ok=True)


def _abandon(sessions: dict[str, UploadSession], upload_id: str) -> None:
    session = sessions.pop(upload_id, None)
    if session is not None:
        session.discard()


def discard_upload_sessions(sessions: dict[str, UploadSession]) -> None:
    """Drop every session and its temp file -- what a disconnect owes the disk."""
    for upload_id in list(sessions):
        _abandon(sessions, upload_id)


def webui_workspace_upload_event(
    envelope: dict[str, Any],
    *,
    scope: WorkspaceScope,
    sessions: dict[str, UploadSession] | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
    max_total_bytes: int = MAX_UPLOAD_TOTAL_BYTES,
) -> tuple[str, dict[str, Any]]:
    """Return the WS event name and payload for one WebUI workspace upload.

    A file arrives as one or more chunks. *sessions* is the caller's per-connection
    state: keyed by the client's ``upload_id``, and owned by one connection so a
    second client cannot append to an upload it did not start. A caller that passes
    none accepts single-chunk uploads only.
    """
    request_id = envelope.get("request_id")
    valid_request_id = isinstance(request_id, str) and 0 < len(request_id) <= _MAX_REQUEST_ID_LENGTH
    live: dict[str, UploadSession] = {} if sessions is None else sessions

    def error(detail: str) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {"detail": detail}
        if valid_request_id:
            payload["request_id"] = request_id
        return "workspace_upload_error", payload

    if not valid_request_id:
        return error("invalid_request")

    data = _decode_data_url(envelope.get("data_url"))
    if data is None:
        return error("invalid_payload")
    if len(data) > max_bytes:
        return error(f"chunk is larger than the {max_bytes // (1024 * 1024)} MB frame limit")

    chunk_count = _positive_int(envelope.get("chunk_count"), default=1)
    chunk_index = _positive_int(envelope.get("chunk_index"), default=0, allow_zero=True)
    if chunk_count is None or chunk_index is None:
        return error("invalid_chunk")
    if chunk_count > MAX_UPLOAD_CHUNKS or chunk_index >= chunk_count:
        return error("invalid_chunk")

    raw_upload_id = envelope.get("upload_id")
    upload_id = (
        raw_upload_id.strip()
        if isinstance(raw_upload_id, str) and 0 < len(raw_upload_id.strip()) <= _MAX_REQUEST_ID_LENGTH
        else None
    )
    if chunk_count > 1 and upload_id is None:
        return error("invalid_request")

    session = live.get(upload_id) if upload_id is not None else None

    if session is None:
        # First chunk: everything is decided here, once.
        if chunk_index != 0:
            return error("upload session not found — start again")
        raw_parent = envelope.get("parent")
        parent = raw_parent.strip() or None if isinstance(raw_parent, str) else None
        # A folder upload sends the path the file had inside the dropped folder
        # (``docs/img/logo.png``); a single file sends only its name. The intermediate
        # directories are created here rather than in a round trip per level from the
        # client, which for a real tree is hundreds of requests to say something the
        # client already knows.
        raw_relative = envelope.get("relative_path")
        relative = (
            [part for part in str(raw_relative).split("/") if part]
            if isinstance(raw_relative, str)
            else []
        )
        if len(relative) > MAX_RELATIVE_DEPTH:
            return error(f"path is deeper than the {MAX_RELATIVE_DEPTH} level upload limit")
        try:
            if relative:
                directory = ensure_relative_directory(parent, relative[:-1], scope=scope)
                target = resolve_child(str(directory), relative[-1], scope=scope)
            else:
                target = resolve_child(parent, str(envelope.get("name") or ""), scope=scope)
        except WebUIFileBrowserError as exc:
            return error(exc.message)

        # Refused rather than overwritten. Replacing a file the operator did not name
        # in this request is a different action from adding one, and the explorer can
        # rename or delete deliberately.
        if target.exists() or target.is_symlink():
            return error("a file or folder with that name already exists")

        session = UploadSession(
            target=target,
            # A sibling, so the finishing rename stays on one filesystem. Dotted, so
            # the explorer's own default hides a partial upload rather than showing a
            # file that is not there yet.
            temp=target.with_name(f".{target.name}.nanoinfra-upload"),
            chunk_count=chunk_count,
            parent=parent,
            include_hidden=envelope.get("include_hidden") is True,
        )
        session.temp.unlink(missing_ok=True)
        if chunk_count > 1 and upload_id is not None:
            live[upload_id] = session
    elif session.chunk_count != chunk_count:
        _abandon(live, upload_id or "")
        return error("chunk count changed mid-upload")
    elif chunk_index in session.seen:
        # A retry of a chunk already written would corrupt the file, because this
        # appends. Refusing is honest: the client knows which chunk it resent.
        _abandon(live, upload_id or "")
        return error("chunk already received — start again")

    if session.written + len(data) > max_total_bytes:
        _abandon(live, upload_id or "")
        return error(f"upload is larger than the {max_total_bytes // (1024 * 1024)} MB limit")

    try:
        _append_bytes(session.temp, data)
    except PermissionError:
        _abandon(live, upload_id or "")
        return error("not permitted to write here")
    except OSError:
        _abandon(live, upload_id or "")
        return error("failed to write the file")

    session.received += 1
    session.written += len(data)
    session.seen.add(chunk_index)

    if session.received < session.chunk_count:
        # Acknowledged, so the client sends the next one. No listing yet: the file is
        # not there, and answering with one would show a directory that does not hold
        # what the caller is about to be told about.
        return "workspace_upload_chunk", {
            "request_id": request_id,
            "upload_id": upload_id,
            "received": session.received,
            "chunk_count": session.chunk_count,
        }

    try:
        _finish(session)
    except OSError:
        _abandon(live, upload_id or "")
        return error("failed to write the file")
    if upload_id is not None:
        live.pop(upload_id, None)

    try:
        # Answering in the view the client is showing, so an upload made with hidden
        # entries revealed does not silently fold them away again.
        listing = directory_listing_payload(
            session.parent, scope=scope, include_hidden=session.include_hidden
        )
    except WebUIFileBrowserError as exc:
        return error(exc.message)
    return "workspace_upload_result", {"request_id": request_id, "listing": listing}


def _positive_int(raw: Any, *, default: int, allow_zero: bool = False) -> int | None:
    """An int off the wire, or ``None`` when it is not one this may act on."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw < 0 or (raw == 0 and not allow_zero):
        return None
    return raw


def _append_bytes(temp: Path, data: bytes) -> None:
    """Append one chunk, durably.

    Appending rather than holding the file in memory is the point of chunking: the
    process never holds more than one chunk of it.
    """
    with open(temp, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _finish(session: UploadSession) -> None:
    """One rename, so the name never exists holding a partial file."""
    if session.target.exists() or session.target.is_symlink():
        # Something took the name while the chunks were arriving. The upload does not
        # get to overwrite it after the fact.
        session.discard()
        raise OSError("the name was taken while the upload was in flight")
    os.replace(session.temp, session.target)


__all__ = [
    "MAX_UPLOAD_BYTES",
    "MAX_UPLOAD_CHUNKS",
    "MAX_UPLOAD_TOTAL_BYTES",
    "UploadSession",
    "discard_upload_sessions",
    "webui_workspace_upload_event",
]
