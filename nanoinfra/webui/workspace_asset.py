"""Serve the workspace files the text preview cannot: images, PDFs, and anything else as a download.

Three routes could have carried this, and two of them must not.

Not the **preview** route: it is text by construction. A NUL byte in the first 4 KiB is a 415 in the
payload *and* in the availability probe (``file_preview.py``), so a ``.png`` the agent just wrote is
not merely unrendered -- its path never becomes a clickable chip, because the probe rejects it first.

Not the **media** route: its signer resolves against the media dir, while these files live in the
session workspace, whose containment is a deliberately separate capability. The comment in
``file_preview._resolve_preview_path`` records what happened the last time one setting answered both
questions: turning off a tool restriction granted a remote read of ``~/.nanoinfra/config.json``. So
this route resolves through the same ``resolve_allowed_path`` call the preview route makes, and it
widens nothing.

What it adds is a workspace binding in the signature. A workspace used to be one path per install;
since the workspaces root and its switcher, it is not -- so the signed payload names the root the
path resolved under, and a URL minted under one workspace is refused after a switch to another.
That is the one guarantee a signature over the path alone could not give.

The type comes from the bytes, never from the extension. A ``.png`` whose contents are HTML must not
come back as ``text/html``, and the way to be sure is to not ask the name.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
from pathlib import Path
from typing import Any, Final

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanoinfra.security.workspace_access import WorkspaceScope
from nanoinfra.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from nanoinfra.webui.http_utils import http_error, http_response
from nanoinfra.webui.media_api import (
    b64url_decode,
    b64url_encode,
    parse_single_byte_range,
)

ROUTE_PREFIX: Final = "/api/workspace-asset"

# Memory, not modesty: the whole file is read to answer a request, and a capped image is refused
# rather than truncated, because half a picture is not a smaller picture.
MAX_WORKSPACE_ASSET_BYTES: Final = 32 * 1024 * 1024

# Sniffed from magic bytes. Kept short on purpose: every entry is a type a browser renders without
# a plugin, and a type that renders is the entire reason this route exists.
_MAGIC: Final = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

_INLINE_TYPES: Final = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "application/pdf"}
)


def sniff_media_type(prefix: bytes) -> str | None:
    """Return the media type these bytes actually are, or ``None`` if it is not one we serve."""
    for magic, media_type in _MAGIC:
        if prefix.startswith(magic):
            return media_type
    # RIFF containers name their form four bytes in, so WEBP needs both ends checked.
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    # ISO-BMFF: `ftyp` at offset 4, brand after it. AVIF and its sequence form.
    if prefix[4:8] == b"ftyp" and prefix[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


def asset_kind(prefix: bytes) -> str:
    """What the panel should do with these bytes: ``image``, ``pdf``, ``binary`` or ``text``.

    The probe calls this instead of answering 415, so a path becomes a chip either way and the
    client knows which viewer to open rather than guessing from a name.
    """
    media_type = sniff_media_type(prefix)
    if media_type == "application/pdf":
        return "pdf"
    if media_type is not None:
        return "image"
    return "binary" if b"\0" in prefix else "text"


def sign_workspace_asset(
    abs_path: Path,
    *,
    secret: bytes,
    workspace_root: Path,
) -> str | None:
    """Return a signed URL for a file inside *workspace_root*, or ``None`` if it is outside."""
    try:
        root = workspace_root.resolve()
        rel = abs_path.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    payload = b64url_encode(f"{root.as_posix()}\n{rel.as_posix()}".encode())
    mac = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
    return f"{ROUTE_PREFIX}/{b64url_encode(mac)}/{payload}"


def attach_asset_url(
    payload: dict[str, Any],
    *,
    secret: bytes,
    workspace_root: Path,
) -> dict[str, Any]:
    """Give a non-text preview payload the signed URL its viewer loads.

    Every route that returns a preview payload calls this. The first cut signed inside one route
    handler, and the explorer's own preview route -- which reuses the same payload builder with a
    different scope -- returned `kind: "image"` with no URL, so the panel said the file could not be
    served from this workspace. Two callers, one of them patched. A test scans for the call sites
    rather than listing them, because a list is what missed the second one.

    Text is left alone: it carries its content already, and a URL for it would be a second way to
    read the same bytes.
    """
    if payload.get("kind") in (None, "text"):
        return payload
    raw_path = payload.get("path")
    if not isinstance(raw_path, str):
        return payload
    payload["asset_url"] = sign_workspace_asset(
        Path(raw_path), secret=secret, workspace_root=workspace_root
    )
    return payload


def serve_workspace_asset(
    sig: str,
    payload: str,
    *,
    secret: bytes,
    scope: WorkspaceScope,
    request: WsRequest | None = None,
) -> Response:
    """Serve one workspace file: images and PDFs inline, everything else as a download."""
    try:
        provided_mac = b64url_decode(sig)
    except (ValueError, binascii.Error):
        return http_error(401, "invalid signature")
    expected_mac = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(expected_mac, provided_mac):
        return http_error(401, "invalid signature")

    try:
        signed_root, _, rel = b64url_decode(payload).decode("utf-8").partition("\n")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return http_error(400, "invalid payload")
    if not rel:
        return http_error(400, "invalid payload")

    # The workspace this URL was minted under has to be the workspace that is open. A switcher
    # exists, so a signature over the path alone would outlive the workspace it belonged to.
    try:
        if Path(signed_root).resolve(strict=False) != scope.project_path.resolve(strict=False):
            return http_error(403, "the file belongs to another workspace")
    except OSError:
        return http_error(403, "the file belongs to another workspace")

    try:
        resolved = resolve_allowed_path(
            rel,
            workspace=scope.project_path,
            allowed_root=scope.project_path,
            strict=True,
        )
    except FileNotFoundError:
        return http_error(404, "not found")
    except WorkspaceBoundaryError:
        return http_error(403, "the file is outside the current workspace")
    except OSError:
        return http_error(400, "invalid path")
    if not resolved.is_file():
        return http_error(404, "not found")

    try:
        size = resolved.stat().st_size
    except OSError:
        return http_error(500, "read error")
    if size > MAX_WORKSPACE_ASSET_BYTES:
        return http_error(413, "file is too large to serve")

    try:
        with resolved.open("rb") as fh:
            prefix = fh.read(4096)
    except OSError:
        return http_error(500, "read error")

    media_type = sniff_media_type(prefix) or "application/octet-stream"
    inline = media_type in _INLINE_TYPES
    headers: list[tuple[str, str]] = [
        ("Accept-Ranges", "bytes"),
        ("Cache-Control", "private, no-store"),
        ("X-Content-Type-Options", "nosniff"),
        (
            "Content-Disposition",
            "inline" if inline else f'attachment; filename="{_safe_filename(resolved.name)}"',
        ),
    ]
    if media_type == "application/pdf":
        # A PDF is a document that can hold script, and the browser's viewer is what renders it.
        # An empty sandbox leaves that script no origin to act on and no way to reach the frame
        # around it, which is what makes embedding the viewer a smaller decision than it looks.
        headers.append(("Content-Security-Policy", "sandbox"))

    range_header = _range_header(request)
    if range_header:
        try:
            start, end = parse_single_byte_range(range_header, size)
        except ValueError:
            return http_response(
                b"range not satisfiable",
                status=416,
                extra_headers=[("Accept-Ranges", "bytes"), ("Content-Range", f"bytes */{size}")],
            )
        try:
            with resolved.open("rb") as fh:
                fh.seek(start)
                body = fh.read(end - start + 1)
        except OSError:
            return http_error(500, "read error")
        return http_response(
            body,
            status=206,
            content_type=media_type,
            extra_headers=[*headers, ("Content-Range", f"bytes {start}-{end}/{size}")],
        )

    try:
        body = resolved.read_bytes()
    except OSError:
        return http_error(500, "read error")
    return http_response(body, content_type=media_type, extra_headers=headers)


def _safe_filename(name: str) -> str:
    """A filename a header can carry: no quotes, no newlines, no path separators."""
    cleaned = "".join(c for c in name if c.isprintable() and c not in '"\\/\r\n')
    return cleaned or "download"


def _range_header(request: WsRequest | None) -> str:
    if request is None:
        return ""
    for key, value in request.headers.raw_items():
        if key.lower() == "range":
            return value
    return ""


