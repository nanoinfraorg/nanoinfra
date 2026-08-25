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


def test_a_single_frame_past_the_limit_is_refused_as_a_frame(tmp_path: Path) -> None:
    """The per-chunk cap is about the transport, and says so.

    A file this size is not refused any more -- it arrives in several chunks. What
    cannot happen is one frame carrying more than the socket allows.
    """
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(data_url=_data_url(b"x" * 64)),
        scope=default_workspace_scope(workspace, True),
        max_bytes=32,
    )

    assert event == "workspace_upload_error"
    assert "frame limit" in str(payload["detail"])
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


def test_the_browser_and_the_server_agree_on_the_upload_limits() -> None:
    """A comment naming the other side is not enough; the two must be checked.

    Two numbers cross the wire here. The file cap has to match, or the browser
    promises something the server refuses after reading the bytes. And the browser's
    chunk size has to stay within the server's per-frame cap, or every chunk of every
    large upload is rejected -- and past the transport's own limit the socket closes
    instead of answering, which is not an error message at all.
    """
    import re

    from nanoinfra.webui.workspace_upload_ws import MAX_UPLOAD_BYTES, MAX_UPLOAD_TOTAL_BYTES

    source = Path(__file__).parents[2] / "webui" / "src" / "lib" / "dropped-files.ts"
    text = source.read_text(encoding="utf-8")

    file_cap = re.search(r"MAX_UPLOAD_BYTES = (\d+) \* 1024 \* 1024", text)
    chunk = re.search(r"UPLOAD_CHUNK_BYTES = (\d+) \* 1024 \* 1024", text)
    assert file_cap is not None, "the browser no longer declares MAX_UPLOAD_BYTES in MB"
    assert chunk is not None, "the browser no longer declares UPLOAD_CHUNK_BYTES in MB"

    browser_file_cap = int(file_cap.group(1)) * 1024 * 1024
    browser_chunk = int(chunk.group(1)) * 1024 * 1024
    assert browser_file_cap == MAX_UPLOAD_TOTAL_BYTES, (
        f"the browser refuses a file past {browser_file_cap} bytes and the server past "
        f"{MAX_UPLOAD_TOTAL_BYTES}"
    )
    assert browser_chunk <= MAX_UPLOAD_BYTES, (
        f"the browser sends {browser_chunk}-byte chunks and the server accepts "
        f"{MAX_UPLOAD_BYTES}"
    )


# -- Chunked uploads ---------------------------------------------------------


def _chunk(index: int, count: int, payload: bytes, **overrides: object) -> dict[str, object]:
    return _envelope(
        request_id=f"req-{index}",
        upload_id="up-1",
        chunk_index=index,
        chunk_count=count,
        data_url=_data_url(payload),
        **overrides,
    )


def test_a_file_arrives_in_several_chunks_and_is_assembled_in_order(tmp_path: Path) -> None:
    """What removes the frame size from the limit a person sees."""
    workspace = _workspace(tmp_path)
    scope = default_workspace_scope(workspace, True)
    sessions: dict[str, object] = {}

    first = webui_workspace_upload_event(_chunk(0, 3, b"aaa"), scope=scope, sessions=sessions)
    second = webui_workspace_upload_event(_chunk(1, 3, b"bbb"), scope=scope, sessions=sessions)
    third = webui_workspace_upload_event(_chunk(2, 3, b"ccc"), scope=scope, sessions=sessions)

    # No listing until the file exists: answering one earlier would describe a
    # directory that does not hold what the caller is about to be told about.
    assert first[0] == "workspace_upload_chunk"
    assert first[1]["received"] == 1
    assert second[0] == "workspace_upload_chunk"
    assert third[0] == "workspace_upload_result"
    assert (workspace / "notes.md").read_bytes() == b"aaabbbccc"
    assert sessions == {}


def test_a_partial_upload_never_holds_the_final_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    scope = default_workspace_scope(workspace, True)
    sessions: dict[str, object] = {}

    webui_workspace_upload_event(_chunk(0, 2, b"aaa"), scope=scope, sessions=sessions)

    # The bytes are on disk, under a dotted sibling the explorer hides by default,
    # and the name the operator chose does not exist yet.
    assert not (workspace / "notes.md").exists()
    assert (workspace / ".notes.md.nanoinfra-upload").read_bytes() == b"aaa"


def test_a_repeated_chunk_is_refused_rather_than_appended_twice(tmp_path: Path) -> None:
    """Appending a chunk already written would corrupt the file silently."""
    workspace = _workspace(tmp_path)
    scope = default_workspace_scope(workspace, True)
    sessions: dict[str, object] = {}
    webui_workspace_upload_event(_chunk(0, 2, b"aaa"), scope=scope, sessions=sessions)

    event, payload = webui_workspace_upload_event(_chunk(0, 2, b"aaa"), scope=scope, sessions=sessions)

    assert event == "workspace_upload_error"
    assert "already received" in str(payload["detail"])
    # The session is abandoned with its temp file, so a retry starts clean.
    assert sessions == {}
    assert not (workspace / ".notes.md.nanoinfra-upload").exists()


def test_a_chunk_without_a_session_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _chunk(1, 2, b"bbb"), scope=default_workspace_scope(workspace, True), sessions={}
    )

    assert event == "workspace_upload_error"
    assert "session not found" in str(payload["detail"])


def test_a_later_chunk_cannot_redirect_the_upload(tmp_path: Path) -> None:
    """Containment is decided on the first chunk, once.

    Otherwise a client could open a session at a legitimate path and point the last
    chunk somewhere else, with the bytes already written following it there.
    """
    workspace = _workspace(tmp_path)
    (workspace / "docs").mkdir()
    scope = default_workspace_scope(workspace, True)
    sessions: dict[str, object] = {}
    webui_workspace_upload_event(_chunk(0, 2, b"aaa"), scope=scope, sessions=sessions)

    event, _ = webui_workspace_upload_event(
        _chunk(1, 2, b"bbb", name="elsewhere.md", parent=str(workspace / "docs")),
        scope=scope,
        sessions=sessions,
    )

    assert event == "workspace_upload_result"
    # The name and the directory from the first chunk are what got written.
    assert (workspace / "notes.md").read_bytes() == b"aaabbbb"[:6]
    assert not (workspace / "docs" / "elsewhere.md").exists()


def test_a_changed_chunk_count_abandons_the_upload(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    scope = default_workspace_scope(workspace, True)
    sessions: dict[str, object] = {}
    webui_workspace_upload_event(_chunk(0, 3, b"aaa"), scope=scope, sessions=sessions)

    event, payload = webui_workspace_upload_event(_chunk(1, 5, b"bbb"), scope=scope, sessions=sessions)

    assert event == "workspace_upload_error"
    assert "changed mid-upload" in str(payload["detail"])
    assert sessions == {}


def test_a_chunk_past_the_frame_limit_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _chunk(0, 2, b"x" * 64),
        scope=default_workspace_scope(workspace, True),
        sessions={},
        max_bytes=32,
    )

    assert event == "workspace_upload_error"
    assert "frame limit" in str(payload["detail"])


def test_the_total_is_enforced_across_chunks(tmp_path: Path) -> None:
    """A file cannot get past the cap by arriving in pieces."""
    workspace = _workspace(tmp_path)
    scope = default_workspace_scope(workspace, True)
    sessions: dict[str, object] = {}
    webui_workspace_upload_event(
        _chunk(0, 3, b"x" * 20), scope=scope, sessions=sessions, max_total_bytes=32
    )

    event, payload = webui_workspace_upload_event(
        _chunk(1, 3, b"x" * 20), scope=scope, sessions=sessions, max_total_bytes=32
    )

    assert event == "workspace_upload_error"
    assert "larger than" in str(payload["detail"])
    assert sessions == {}
    assert not (workspace / "notes.md").exists()
    assert not (workspace / ".notes.md.nanoinfra-upload").exists()


def test_a_name_taken_while_chunks_were_in_flight_is_not_overwritten(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    scope = default_workspace_scope(workspace, True)
    sessions: dict[str, object] = {}
    webui_workspace_upload_event(_chunk(0, 2, b"aaa"), scope=scope, sessions=sessions)
    (workspace / "notes.md").write_text("someone else", encoding="utf-8")

    event, _ = webui_workspace_upload_event(_chunk(1, 2, b"bbb"), scope=scope, sessions=sessions)

    assert event == "workspace_upload_error"
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "someone else"
    assert sessions == {}


def test_discarding_sessions_removes_their_temp_files(tmp_path: Path) -> None:
    """What a disconnect owes the disk."""
    from nanoinfra.webui.workspace_upload_ws import discard_upload_sessions

    workspace = _workspace(tmp_path)
    sessions: dict[str, object] = {}
    webui_workspace_upload_event(
        _chunk(0, 2, b"aaa"), scope=default_workspace_scope(workspace, True), sessions=sessions
    )
    assert (workspace / ".notes.md.nanoinfra-upload").exists()

    discard_upload_sessions(sessions)  # type: ignore[arg-type]

    assert not (workspace / ".notes.md.nanoinfra-upload").exists()
    assert sessions == {}


def test_a_multi_chunk_upload_needs_an_upload_id(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    event, payload = webui_workspace_upload_event(
        _envelope(chunk_index=0, chunk_count=2, upload_id=None),
        scope=default_workspace_scope(workspace, True),
        sessions={},
    )

    assert (event, payload["detail"]) == ("workspace_upload_error", "invalid_request")


def test_a_nonsense_chunk_index_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    for bad in ({"chunk_index": 5, "chunk_count": 2}, {"chunk_index": -1, "chunk_count": 2},
                {"chunk_index": "0", "chunk_count": 2}, {"chunk_index": 0, "chunk_count": 0}):
        event, payload = webui_workspace_upload_event(
            _envelope(upload_id="up-1", **bad),
            scope=default_workspace_scope(workspace, True),
            sessions={},
        )
        assert event == "workspace_upload_error", bad
        assert payload["detail"] in {"invalid_chunk", "invalid_request"}, bad
