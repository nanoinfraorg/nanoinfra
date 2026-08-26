"""Bytes an operator can look at: images inline, PDFs in the browser's viewer, the rest downloadable.

The preview route is text by construction — a NUL byte in the first 4 KiB is a 415, in the payload
and in the availability probe — so a `.png` the agent just wrote never becomes a clickable chip.
This route answers for those files, and it keeps the workspace's containment rather than widening
the media resolver's roots.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest

from nanoinfra.security.workspace_access import WorkspaceSandboxStatus, WorkspaceScope
from nanoinfra.webui.media_api import b64url_encode
from nanoinfra.webui.workspace_asset import (
    MAX_WORKSPACE_ASSET_BYTES,
    asset_kind,
    serve_workspace_asset,
    sign_workspace_asset,
    sniff_media_type,
)

SECRET = b"a-test-signing-secret"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16
PDF = b"%PDF-1.7\n" + b"\x00" * 32


def _scope(root: Path) -> WorkspaceScope:
    return WorkspaceScope(
        project_path=root,
        access_mode="full",
        restrict_to_workspace=True,
        sandbox_status=WorkspaceSandboxStatus(
            restrict_to_workspace=True,
            workspace_root=str(root),
            level="none",
            enforced=False,
            provider="none",
            provider_label="none",
            summary="test scope",
        ),
    )


def _url(path: Path, root: Path) -> tuple[str, str]:
    url = sign_workspace_asset(path, secret=SECRET, workspace_root=root)
    assert url is not None
    _, _, _, sig, payload = url.split("/", 4)
    return sig, payload


def _headers(response) -> dict[str, str]:
    """The websockets Headers object is a multidict; raw_items is the readable view."""
    return dict(response.headers.raw_items())


def _serve(path: Path, root: Path, *, scope_root: Path | None = None, request=None):
    sig, payload = _url(path, root)
    return serve_workspace_asset(
        sig, payload, secret=SECRET, scope=_scope(scope_root or root), request=request
    )


# --- sniffing ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("prefix", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp"),
     (PDF, "application/pdf"), (b"just text", None), (b"\x00\x01\x02binary", None)],
)
def test_the_type_comes_from_the_bytes(prefix: bytes, expected: str | None) -> None:
    assert sniff_media_type(prefix) == expected


def test_the_kind_names_what_the_panel_should_do(tmp_path: Path) -> None:
    assert asset_kind(PNG) == "image"
    assert asset_kind(PDF) == "pdf"
    assert asset_kind(b"\x00\x01\x02") == "binary"
    assert asset_kind(b"hello") == "text"


# --- serving ----------------------------------------------------------------------------------

def test_an_image_in_the_workspace_is_served_with_its_sniffed_type(tmp_path: Path) -> None:
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    r = _serve(f, tmp_path)
    assert r.status_code == 200
    assert _headers(r)["Content-Type"] == "image/png"
    assert _headers(r)["Content-Disposition"] == "inline"
    assert _headers(r)["X-Content-Type-Options"] == "nosniff"
    assert r.body == PNG


def test_a_png_full_of_html_is_never_served_as_html(tmp_path: Path) -> None:
    f = tmp_path / "trap.png"
    f.write_bytes(b"<html><script>alert(1)</script></html>")
    r = _serve(f, tmp_path)
    # Sniffed, so the extension buys nothing: it is not an image, so it downloads.
    assert _headers(r)["Content-Type"] == "application/octet-stream"
    assert _headers(r)["Content-Disposition"].startswith("attachment")


def test_a_pdf_is_sandboxed_for_the_browser_viewer(tmp_path: Path) -> None:
    f = tmp_path / "plan.pdf"
    f.write_bytes(PDF)
    r = _serve(f, tmp_path)
    headers = _headers(r)
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"] == "inline"
    # A PDF is a document with script surface. The response says it has no origin to act on.
    assert headers["Content-Security-Policy"] == "sandbox"
    assert headers["Accept-Ranges"] == "bytes"


def test_any_other_binary_downloads_rather_than_disappearing(tmp_path: Path) -> None:
    f = tmp_path / "bundle.zip"
    f.write_bytes(b"PK\x03\x04" + b"\x00" * 16)
    r = _serve(f, tmp_path)
    assert _headers(r)["Content-Type"] == "application/octet-stream"
    assert 'filename="bundle.zip"' in _headers(r)["Content-Disposition"]


def test_a_file_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    # Signed for its own directory, then presented against a scope that does not contain it.
    r = _serve(outside, tmp_path, scope_root=root)
    assert r.status_code == 403


def test_a_url_minted_under_another_workspace_is_refused(tmp_path: Path) -> None:
    """v0.17.0 added a switcher, so a workspace is not one path per install any more."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    for d in (first, second):
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "shot.png").write_bytes(PNG)
    sig, payload = _url(first / "sub" / "shot.png", first)
    r = serve_workspace_asset(sig, payload, secret=SECRET, scope=_scope(second), request=None)
    assert r.status_code == 403


def test_a_wrong_signature_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    _, payload = _url(f, tmp_path)
    forged = b64url_encode(hmac.new(b"wrong", payload.encode(), hashlib.sha256).digest()[:16])
    r = serve_workspace_asset(forged, payload, secret=SECRET, scope=_scope(tmp_path), request=None)
    assert r.status_code == 401


def test_a_file_over_the_cap_is_refused_whole(tmp_path: Path) -> None:
    f = tmp_path / "huge.png"
    f.write_bytes(PNG + b"\x00" * (MAX_WORKSPACE_ASSET_BYTES + 1))
    r = _serve(f, tmp_path)
    assert r.status_code == 413
    # Not a partial image: nothing of the file is in the body.
    assert b"PNG" not in r.body


def test_a_missing_file_is_not_found(tmp_path: Path) -> None:
    f = tmp_path / "gone.png"
    f.write_bytes(PNG)
    sig, payload = _url(f, tmp_path)
    f.unlink()
    r = serve_workspace_asset(sig, payload, secret=SECRET, scope=_scope(tmp_path), request=None)
    assert r.status_code == 404


# --- the preview route stops refusing binaries -------------------------------------------------

def test_the_probe_says_which_viewer_opens_a_path(tmp_path: Path) -> None:
    """It answered 415 for anything with a NUL byte, so an image never became a clickable chip."""
    from nanoinfra.webui.file_preview import file_preview_availability_payload

    (tmp_path / "shot.png").write_bytes(PNG)
    (tmp_path / "plan.pdf").write_bytes(PDF)
    (tmp_path / "bundle.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 8)
    (tmp_path / "notes.md").write_text("# hello", encoding="utf-8")

    scope = _scope(tmp_path)
    kinds = {
        name: file_preview_availability_payload(str(tmp_path / name), scope=scope)
        for name in ("shot.png", "plan.pdf", "bundle.zip", "notes.md")
    }
    assert all(k["available"] for k in kinds.values())
    assert kinds["shot.png"]["kind"] == "image"
    assert kinds["plan.pdf"]["kind"] == "pdf"
    assert kinds["bundle.zip"]["kind"] == "binary"
    assert kinds["notes.md"]["kind"] == "text"


def test_a_binary_payload_carries_no_content_and_names_its_kind(tmp_path: Path) -> None:
    from nanoinfra.webui.file_preview import file_preview_payload

    (tmp_path / "shot.png").write_bytes(PNG)
    payload = file_preview_payload(str(tmp_path / "shot.png"), scope=_scope(tmp_path))

    assert payload["kind"] == "image"
    assert payload["content"] == ""
    # Not truncated: a picture is refused or served whole, never half.
    assert payload["truncated"] is False
    assert payload["size"] == len(PNG)


def test_a_text_payload_still_carries_its_text(tmp_path: Path) -> None:
    from nanoinfra.webui.file_preview import file_preview_payload

    (tmp_path / "notes.md").write_text("# hello\n", encoding="utf-8")
    payload = file_preview_payload(str(tmp_path / "notes.md"), scope=_scope(tmp_path))

    assert payload["kind"] == "text"
    assert payload["content"] == "# hello\n"


def test_the_signed_url_round_trips_through_the_route_shape(tmp_path: Path) -> None:
    """What the panel does: take the payload's asset_url, then fetch it."""
    (tmp_path / "shot.png").write_bytes(PNG)
    url = sign_workspace_asset(tmp_path / "shot.png", secret=SECRET, workspace_root=tmp_path)
    assert url is not None and url.startswith("/api/workspace-asset/")
    import re

    m = re.match(r"^/api/workspace-asset/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", url)
    assert m, f"the route pattern in ws_http must match what the signer emits: {url}"
    r = serve_workspace_asset(
        m.group(1), m.group(2), secret=SECRET, scope=_scope(tmp_path), request=None
    )
    assert r.status_code == 200
    assert r.body == PNG
