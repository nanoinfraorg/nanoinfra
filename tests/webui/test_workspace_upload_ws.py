"""One WebUI workspace upload, carried over the socket."""

from __future__ import annotations

import base64
from pathlib import Path

from nanoinfra.security.workspace_access import default_workspace_scope
from nanoinfra.webui.workspace_upload_ws import webui_workspace_upload_event


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _data_url(payload: bytes, mime: str = "text/plain") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _envelope(**overrides: object) -> dict[str, object]:
    return {
        "request_id": "req-1",
        "parent": None,
        "name": "notes.md",
        "data_url": _data_url(b"hello"),
        **overrides,
    }


def test_an_upload_lands_and_answers_the_fresh_listing(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(), scope=default_workspace_scope(workspace, True)
    )

    assert event == "workspace_upload_result"
    assert payload["request_id"] == "req-1"
    assert (workspace / "notes.md").read_bytes() == b"hello"
    assert [e["name"] for e in payload["listing"]["entries"]] == ["notes.md"]


def test_an_upload_into_a_subdirectory_uses_it_as_the_parent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "docs").mkdir()

    event, payload = webui_workspace_upload_event(
        _envelope(parent=str(workspace / "docs")),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_result"
    assert (workspace / "docs" / "notes.md").is_file()
    assert payload["listing"]["displayPath"] == "docs"


def test_a_request_without_an_id_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(request_id=None), scope=default_workspace_scope(workspace, True)
    )

    assert (event, payload) == ("workspace_upload_error", {"detail": "invalid_request"})
    assert list(workspace.iterdir()) == []


def test_a_malformed_payload_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(data_url="not-a-data-url"), scope=default_workspace_scope(workspace, True)
    )

    assert event == "workspace_upload_error"
    assert payload["detail"] == "invalid_payload"
    assert list(workspace.iterdir()) == []


def test_an_oversized_upload_is_refused_with_the_limit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(data_url=_data_url(b"x" * 64)),
        scope=default_workspace_scope(workspace, True),
        max_bytes=32,
    )

    assert event == "workspace_upload_error"
    assert "upload limit" in str(payload["detail"])
    assert list(workspace.iterdir()) == []


def test_an_upload_will_not_replace_an_existing_file(tmp_path: Path) -> None:
    """Replacing a file is a different action from adding one."""
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("keep me", encoding="utf-8")

    event, payload = webui_workspace_upload_event(
        _envelope(), scope=default_workspace_scope(workspace, True)
    )

    assert event == "workspace_upload_error"
    assert "already exists" in str(payload["detail"])
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "keep me"


def test_a_name_that_is_a_path_is_refused(tmp_path: Path) -> None:
    """Containment is `file_browser.resolve_child`'s, not re-implemented here."""
    workspace = _workspace(tmp_path)
    (workspace / "docs").mkdir()

    event, payload = webui_workspace_upload_event(
        _envelope(name="../escaped.md"), scope=default_workspace_scope(workspace, True)
    )

    assert event == "workspace_upload_error"
    assert "separator" in str(payload["detail"])
    assert not (tmp_path / "escaped.md").exists()


def test_a_parent_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    event, payload = webui_workspace_upload_event(
        _envelope(parent=str(outside)), scope=default_workspace_scope(workspace, True)
    )

    assert event == "workspace_upload_error"
    assert "outside the current workspace" in str(payload["detail"])
    assert list(outside.iterdir()) == []


def test_the_parent_is_refused_even_with_restriction_relaxed(tmp_path: Path) -> None:
    """`restrict_to_workspace` governs the agent's tools, never this route."""
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    event, _ = webui_workspace_upload_event(
        _envelope(parent=str(outside)),
        scope=default_workspace_scope(workspace, restrict_to_workspace=False),
    )

    assert event == "workspace_upload_error"
    assert list(outside.iterdir()) == []


def test_no_temp_file_is_left_behind(tmp_path: Path) -> None:
    """The write goes through a sibling temp file and one rename, so a failure
    cannot leave a truncated file under the name the operator chose."""
    workspace = _workspace(tmp_path)

    webui_workspace_upload_event(_envelope(), scope=default_workspace_scope(workspace, True))

    assert sorted(p.name for p in workspace.iterdir()) == ["notes.md"]


def test_a_binary_payload_round_trips_byte_for_byte(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    body = bytes(range(256))

    webui_workspace_upload_event(
        _envelope(name="blob.bin", data_url=_data_url(body, "application/octet-stream")),
        scope=default_workspace_scope(workspace, True),
    )

    assert (workspace / "blob.bin").read_bytes() == body


# -- Recursive folder upload -------------------------------------------------


def test_a_relative_path_creates_the_directories_it_needs(tmp_path: Path) -> None:
    """What a dropped folder sends: the path the file had inside it."""
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(name="logo.png", relative_path="docs/img/logo.png"),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_result"
    assert (workspace / "docs" / "img" / "logo.png").read_bytes() == b"hello"
    # The answer is still the listing of the directory the upload was aimed at.
    assert [e["name"] for e in payload["listing"]["entries"]] == ["docs"]


def test_existing_directories_are_reused_rather_than_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "docs").mkdir()
    (workspace / "docs" / "keep.md").write_text("keep", encoding="utf-8")

    event, _ = webui_workspace_upload_event(
        _envelope(name="new.md", relative_path="docs/new.md"),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_result"
    assert (workspace / "docs" / "keep.md").exists()
    assert (workspace / "docs" / "new.md").exists()


def test_a_relative_path_cannot_walk_out_of_the_workspace(tmp_path: Path) -> None:
    """`webkitRelativePath` comes off the browser and is untrusted like anything else."""
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(name="escaped.md", relative_path="../../escaped.md"),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_error"
    assert "invalid name" in str(payload["detail"])
    assert not (tmp_path / "escaped.md").exists()
    assert not (tmp_path.parent / "escaped.md").exists()


def test_a_relative_path_cannot_use_a_backslash_to_hide_a_component(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(name="x.md", relative_path="docs\\..\\..\\x.md"),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_error"
    assert "separator" in str(payload["detail"])


def test_a_file_in_the_way_of_a_directory_is_reported(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "docs").write_text("i am a file", encoding="utf-8")

    event, payload = webui_workspace_upload_event(
        _envelope(name="new.md", relative_path="docs/new.md"),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_error"
    assert "in the way" in str(payload["detail"])
    assert (workspace / "docs").read_text(encoding="utf-8") == "i am a file"


def test_a_link_is_not_followed_to_place_an_upload(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "docs").symlink_to(outside, target_is_directory=True)

    event, payload = webui_workspace_upload_event(
        _envelope(name="new.md", relative_path="docs/new.md"),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_error"
    assert "is a link" in str(payload["detail"])
    assert list(outside.iterdir()) == []


def test_an_absurdly_deep_path_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    deep = "/".join(f"level{index}" for index in range(40)) + "/file.md"

    event, payload = webui_workspace_upload_event(
        _envelope(name="file.md", relative_path=deep),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_error"
    assert "upload limit" in str(payload["detail"])
    assert list(workspace.iterdir()) == []


def test_a_relative_path_of_one_component_behaves_like_a_plain_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, _ = webui_workspace_upload_event(
        _envelope(name="notes.md", relative_path="notes.md"),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_result"
    assert (workspace / "notes.md").read_bytes() == b"hello"


def test_an_upload_into_a_subdirectory_keeps_its_relative_tree(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "existing").mkdir()

    event, payload = webui_workspace_upload_event(
        _envelope(
            parent=str(workspace / "existing"),
            name="logo.png",
            relative_path="assets/logo.png",
        ),
        scope=default_workspace_scope(workspace, True),
    )

    assert event == "workspace_upload_result"
    assert (workspace / "existing" / "assets" / "logo.png").is_file()
    assert payload["listing"]["displayPath"] == "existing"
