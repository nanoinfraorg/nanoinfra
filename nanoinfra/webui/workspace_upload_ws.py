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
from pathlib import Path
from typing import Any

from nanoinfra.security.workspace_access import WorkspaceScope
from nanoinfra.webui.file_browser import (
    WebUIFileBrowserError,
    directory_listing_payload,
    resolve_child,
)

_MAX_REQUEST_ID_LENGTH = 80

#: Decoded bytes one upload may carry.
#:
#: The binding constraint above this is the transport: ``WebSocketConfig.max_message_bytes``
#: (36 MB by default) is handed to ``websockets`` as ``max_size``, and a larger frame is
#: dropped with close code 1009 before any of this runs -- which the client already
#: surfaces as ``message_too_big``. Base64 inflates by 4/3, so 20 MB of file is ~27 MB of
#: frame, comfortably inside that default. An operator who lowers ``max_message_bytes``
#: makes the transport the limit again, and learns so from the close code.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

_DATA_URL_RE = re.compile(r"^data:[^;,]*(?:;[^;,]*)*;base64,(.*)$", re.DOTALL)


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


def webui_workspace_upload_event(
    envelope: dict[str, Any],
    *,
    scope: WorkspaceScope,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[str, dict[str, Any]]:
    """Return the WS event name and payload for one WebUI workspace upload."""
    request_id = envelope.get("request_id")
    valid_request_id = isinstance(request_id, str) and 0 < len(request_id) <= _MAX_REQUEST_ID_LENGTH

    def error(detail: str) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {"detail": detail}
        if valid_request_id:
            payload["request_id"] = request_id
        return "workspace_upload_error", payload

    if not valid_request_id:
        return error("invalid_request")

    raw_parent = envelope.get("parent")
    parent = raw_parent.strip() or None if isinstance(raw_parent, str) else None
    data = _decode_data_url(envelope.get("data_url"))
    if data is None:
        return error("invalid_payload")
    if len(data) > max_bytes:
        return error(f"file is larger than the {max_bytes // (1024 * 1024)} MB upload limit")

    try:
        target = resolve_child(parent, str(envelope.get("name") or ""), scope=scope)
    except WebUIFileBrowserError as exc:
        return error(exc.message)

    # Refused rather than overwritten. Replacing a file the operator did not name in
    # this request is a different action from adding one, and the explorer can rename
    # or delete deliberately.
    if target.exists() or target.is_symlink():
        return error("a file or folder with that name already exists")

    try:
        _write_bytes_atomic(target, data)
    except PermissionError:
        return error("not permitted to write here")
    except OSError:
        return error("failed to write the file")

    try:
        # Answering in the view the client is showing, so an upload made with hidden
        # entries revealed does not silently fold them away again.
        listing = directory_listing_payload(
            parent, scope=scope, include_hidden=envelope.get("include_hidden") is True
        )
    except WebUIFileBrowserError as exc:
        return error(exc.message)
    return "workspace_upload_result", {"request_id": request_id, "listing": listing}


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    """Write via a sibling temp file and one rename.

    An upload that fails halfway would otherwise leave a truncated file under the
    name the operator chose, which reads as a complete one. The temp file is a
    sibling so the rename stays on one filesystem, and it is removed if the write
    or the rename fails.
    """
    temp = target.with_name(f".{target.name}.nanoinfra-upload")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


__all__ = ["MAX_UPLOAD_BYTES", "webui_workspace_upload_event"]
