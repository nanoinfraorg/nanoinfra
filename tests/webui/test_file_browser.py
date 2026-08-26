from pathlib import Path

import pytest

from nanoinfra.security.workspace_access import default_workspace_scope
from nanoinfra.webui.file_browser import (
    WebUIFileBrowserError,
    create_directory,
    delete_entry,
    directory_listing_payload,
    move_entry,
    read_file_for_download,
    rename_entry,
    resolve_within_workspace,
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_no_path_lists_the_workspace_root(tmp_path: Path) -> None:
    """The explorer has a starting point without knowing an absolute path."""
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("hi", encoding="utf-8")
    (workspace / "src").mkdir()

    payload = directory_listing_payload(None, scope=default_workspace_scope(workspace, True))

    # Relative to the workspace, with "." for the root: the client joins this with an entry name,
    # so an absolute path here put the host's layout in every query string the explorer built.
    assert payload["path"] == "."
    assert payload["displayPath"] == ""
    assert payload["parent"] is None
    assert [e["name"] for e in payload["entries"]] == ["src", "notes.md"]


def test_directories_come_first_then_case_insensitive_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    for name in ("Zeta.txt", "alpha.txt", "Beta"):
        target = workspace / name
        target.mkdir() if name == "Beta" else target.write_text("x", encoding="utf-8")

    payload = directory_listing_payload("", scope=default_workspace_scope(workspace, True))

    assert [e["name"] for e in payload["entries"]] == ["Beta", "alpha.txt", "Zeta.txt"]


def test_entries_report_kind_size_and_mtime(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("hello", encoding="utf-8")
    (workspace / "src").mkdir()

    entries = {
        e["name"]: e
        for e in directory_listing_payload(
            None, scope=default_workspace_scope(workspace, True)
        )["entries"]
    }

    assert entries["notes.md"]["kind"] == "file"
    assert entries["notes.md"]["size"] == 5
    assert entries["notes.md"]["modified"]
    assert entries["src"]["kind"] == "directory"
    # A directory's byte size is an implementation detail of the filesystem, not
    # something to render in a column beside file sizes.
    assert entries["src"]["size"] is None


def test_a_subdirectory_reports_its_parent_and_relative_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src" / "deep").mkdir(parents=True)

    payload = directory_listing_payload("src/deep", scope=default_workspace_scope(workspace, True))

    assert payload["displayPath"] == "src/deep"
    assert payload["parent"] == "src"


def test_a_relative_escape_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (tmp_path / "outside").mkdir()

    with pytest.raises(WebUIFileBrowserError, match="outside the current workspace") as exc:
        directory_listing_payload("../outside", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 403


def test_an_absolute_path_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (tmp_path / "outside").mkdir()

    with pytest.raises(WebUIFileBrowserError) as exc:
        directory_listing_payload(str(tmp_path / "outside"), scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 403


def test_containment_does_not_read_restrict_to_workspace(tmp_path: Path) -> None:
    """The rule `file_preview.py` records, pinned for this module too.

    ``restrict_to_workspace`` governs the *agent's* file tools. If this listing
    read it, an operator relaxing a tool restriction would also grant an
    authenticated WebUI client a directory walk of everything the process user can
    open -- ``~/.nanoinfra/config.json``, with the provider keys, among it.
    """
    workspace = _workspace(tmp_path)
    (tmp_path / "outside").mkdir()
    unrestricted = default_workspace_scope(workspace, restrict_to_workspace=False)

    with pytest.raises(WebUIFileBrowserError) as exc:
        directory_listing_payload(str(tmp_path / "outside"), scope=unrestricted)

    assert exc.value.status == 403


def test_a_symlink_out_of_the_workspace_is_listed_but_not_followed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    scope = default_workspace_scope(workspace, True)

    listed = directory_listing_payload(None, scope=scope)["entries"]
    assert listed[0]["name"] == "escape"
    assert listed[0]["kind"] == "symlink"
    # Shown as unreachable rather than hidden: a name that silently vanishes is
    # more confusing than one that says why it cannot be opened.
    assert listed[0]["escapesWorkspace"] is True

    with pytest.raises(WebUIFileBrowserError) as exc:
        directory_listing_payload("escape", scope=scope)
    assert exc.value.status == 403


def test_a_symlink_inside_the_workspace_does_not_claim_to_escape(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "real").mkdir()
    (workspace / "link").symlink_to(workspace / "real", target_is_directory=True)

    entries = {
        e["name"]: e
        for e in directory_listing_payload(None, scope=default_workspace_scope(workspace, True))["entries"]
    }

    assert entries["link"]["escapesWorkspace"] is False


def test_a_file_is_not_a_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("hi", encoding="utf-8")

    with pytest.raises(WebUIFileBrowserError, match="not a directory") as exc:
        directory_listing_payload("notes.md", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 400


def test_a_missing_path_is_a_404(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(WebUIFileBrowserError) as exc:
        directory_listing_payload("nope", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 404


def test_an_overlong_path_is_refused_before_the_filesystem(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(WebUIFileBrowserError, match="too long") as exc:
        directory_listing_payload("a" * 5000, scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 400


def test_a_large_directory_is_cut_and_says_so(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    for index in range(6):
        (workspace / f"file-{index}.txt").write_text("x", encoding="utf-8")

    payload = directory_listing_payload(
        None, scope=default_workspace_scope(workspace, True), max_entries=4
    )

    assert len(payload["entries"]) == 4
    assert payload["truncated"] is True


def test_dot_entries_are_hidden_by_default_and_counted(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".env.example").write_text("KEY=", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / "notes.md").write_text("hi", encoding="utf-8")

    payload = directory_listing_payload(None, scope=default_workspace_scope(workspace, True))

    assert [e["name"] for e in payload["entries"]] == ["notes.md"]
    # Counted rather than dropped silently, so the toggle that asks for them can say
    # how many there are instead of looking like it does nothing.
    assert payload["hiddenCount"] == 2
    assert payload["includeHidden"] is False


def test_hidden_entries_are_listed_when_asked_for(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".env.example").write_text("KEY=", encoding="utf-8")
    (workspace / ".git").mkdir()

    payload = directory_listing_payload(
        None, scope=default_workspace_scope(workspace, True), include_hidden=True
    )

    assert [e["name"] for e in payload["entries"]] == [".git", ".env.example"]
    assert payload["includeHidden"] is True
    # Nothing left to reveal, so the UI never offers to reveal what it is showing.
    assert payload["hiddenCount"] == 0


def test_hidden_entries_do_not_spend_the_entry_budget(tmp_path: Path) -> None:
    """Why the filter is server-side rather than in the browser.

    A workspace under version control has a ``.git`` holding thousands of objects.
    Forwarding them would spend ``max_entries`` on names nobody asked to see, and
    report the directory as truncated while the files the operator came for sat
    outside the cut.
    """
    workspace = _workspace(tmp_path)
    for index in range(8):
        (workspace / f".object-{index}").write_text("x", encoding="utf-8")
    (workspace / "notes.md").write_text("hi", encoding="utf-8")
    (workspace / "README.md").write_text("hi", encoding="utf-8")

    payload = directory_listing_payload(
        None, scope=default_workspace_scope(workspace, True), max_entries=2
    )

    assert sorted(e["name"] for e in payload["entries"]) == ["README.md", "notes.md"]
    assert payload["truncated"] is False
    assert payload["hiddenCount"] == 8


def test_a_hidden_directory_can_still_be_opened_directly(tmp_path: Path) -> None:
    """Hidden is a listing default, not a boundary -- the resolver decides those."""
    workspace = _workspace(tmp_path)
    (workspace / ".git").mkdir()
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    payload = directory_listing_payload(".git", scope=default_workspace_scope(workspace, True))

    assert payload["displayPath"] == ".git"
    assert [e["name"] for e in payload["entries"]] == ["HEAD"]


def test_a_mutation_answers_in_the_view_the_caller_had(tmp_path: Path) -> None:
    """Otherwise renaming a file would fold the hidden entries away underneath the operator."""
    workspace = _workspace(tmp_path)
    (workspace / ".gitignore").write_text("x", encoding="utf-8")
    (workspace / "old.txt").write_text("x", encoding="utf-8")

    payload = rename_entry(
        None,
        "old.txt",
        "new.txt",
        scope=default_workspace_scope(workspace, True),
        include_hidden=True,
    )

    assert sorted(e["name"] for e in payload["entries"]) == [".gitignore", "new.txt"]
    assert payload["includeHidden"] is True


def test_the_resolver_accepts_a_path_that_does_not_exist_yet(tmp_path: Path) -> None:
    """What the mutating routes need: resolve a target before creating it."""
    workspace = _workspace(tmp_path)

    resolved = resolve_within_workspace("new/dir", scope=default_workspace_scope(workspace, True), must_exist=False)

    assert resolved == (workspace / "new" / "dir")


def test_the_resolver_refuses_a_nonexistent_path_outside_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(WebUIFileBrowserError) as exc:
        resolve_within_workspace(
            str(tmp_path / "outside" / "new.txt"),
            scope=default_workspace_scope(workspace, True),
            must_exist=False,
        )

    assert exc.value.status == 403


# -- Mutations ---------------------------------------------------------------


def test_mkdir_creates_one_folder_and_answers_the_fresh_listing(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    payload = create_directory(None, "src", scope=default_workspace_scope(workspace, True))

    assert (workspace / "src").is_dir()
    # The caller renders this without a second round trip.
    assert [e["name"] for e in payload["entries"]] == ["src"]


def test_mkdir_does_not_create_parents(tmp_path: Path) -> None:
    """Creating a directory the operator did not name is a different request."""
    workspace = _workspace(tmp_path)

    with pytest.raises(WebUIFileBrowserError) as exc:
        create_directory("missing", "child", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 404


def test_mkdir_refuses_an_existing_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()

    with pytest.raises(WebUIFileBrowserError) as exc:
        create_directory(None, "src", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 409


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "x\0y"])
def test_a_name_is_one_component_and_never_a_path(tmp_path: Path, name: str) -> None:
    """A name is not a path. The resolver would contain `../x` anyway, but accepting a
    separator in a field the UI fills from one directory entry means caller and server
    disagree about what was asked -- and that gap is where a bypass lives."""
    workspace = _workspace(tmp_path)

    with pytest.raises(WebUIFileBrowserError) as exc:
        create_directory(None, name, scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 400


def test_rename_moves_a_file_within_its_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "old.txt").write_text("body", encoding="utf-8")

    rename_entry(None, "old.txt", "new.txt", scope=default_workspace_scope(workspace, True))

    assert not (workspace / "old.txt").exists()
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "body"


def test_rename_refuses_to_clobber(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "b.txt").write_text("b", encoding="utf-8")

    with pytest.raises(WebUIFileBrowserError) as exc:
        rename_entry(None, "a.txt", "b.txt", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 409
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "b"


def test_rename_cannot_leave_its_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "sub").mkdir()
    (workspace / "sub" / "f.txt").write_text("x", encoding="utf-8")

    with pytest.raises(WebUIFileBrowserError) as exc:
        rename_entry("sub", "f.txt", "../f.txt", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 400
    assert (workspace / "sub" / "f.txt").exists()


def test_delete_removes_a_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "gone.txt").write_text("x", encoding="utf-8")

    payload = delete_entry(None, "gone.txt", recursive=False, scope=default_workspace_scope(workspace, True))

    assert not (workspace / "gone.txt").exists()
    assert payload["entries"] == []


def test_a_non_empty_folder_needs_recursive_said_out_loud(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "tree").mkdir()
    (workspace / "tree" / "child.txt").write_text("x", encoding="utf-8")
    scope = default_workspace_scope(workspace, True)

    with pytest.raises(WebUIFileBrowserError, match="not empty") as exc:
        delete_entry(None, "tree", recursive=False, scope=scope)
    assert exc.value.status == 409
    assert (workspace / "tree" / "child.txt").exists()

    delete_entry(None, "tree", recursive=True, scope=scope)
    assert not (workspace / "tree").exists()


def test_deleting_a_symlink_removes_the_link_and_not_its_target(tmp_path: Path) -> None:
    """The reason every mutation takes ``(parent, name)`` instead of one path.

    The path resolver must follow symlinks, or a link would be a way out of the
    workspace -- so resolving ``<dir>/link`` hands back the *target*, and a delete
    built on that would remove whatever the link pointed at.
    """
    workspace = _workspace(tmp_path)
    target = workspace / "real.txt"
    target.write_text("keep me", encoding="utf-8")
    (workspace / "link.txt").symlink_to(target)

    delete_entry(None, "link.txt", recursive=False, scope=default_workspace_scope(workspace, True))

    assert not (workspace / "link.txt").exists()
    assert target.read_text(encoding="utf-8") == "keep me"


def test_deleting_a_symlinked_directory_does_not_walk_into_it(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    delete_entry(None, "escape", recursive=True, scope=default_workspace_scope(workspace, True))

    assert not (workspace / "escape").exists()
    assert (outside / "secret.txt").exists()


def test_renaming_a_symlink_renames_the_link(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    target = workspace / "real.txt"
    target.write_text("body", encoding="utf-8")
    (workspace / "link.txt").symlink_to(target)

    rename_entry(None, "link.txt", "moved.txt", scope=default_workspace_scope(workspace, True))

    assert (workspace / "moved.txt").is_symlink()
    assert target.exists()


def test_the_workspace_root_cannot_be_deleted(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "sub").mkdir()

    # Reached the only way the root can be named as a child: from its own parent,
    # which the resolver refuses because it sits outside the workspace.
    with pytest.raises(WebUIFileBrowserError) as exc:
        delete_entry(str(tmp_path), workspace.name, recursive=True, scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 403
    assert workspace.is_dir()


def test_a_mutation_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(WebUIFileBrowserError) as exc:
        delete_entry(str(outside), "keep.txt", recursive=False, scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 403
    assert (outside / "keep.txt").exists()


def test_a_mutation_ignores_restrict_to_workspace_too(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    unrestricted = default_workspace_scope(workspace, restrict_to_workspace=False)

    with pytest.raises(WebUIFileBrowserError) as exc:
        delete_entry(str(outside), "keep.txt", recursive=False, scope=unrestricted)

    assert exc.value.status == 403
    assert (outside / "keep.txt").exists()


# -- Download ----------------------------------------------------------------


def test_download_returns_the_bytes_of_a_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "archive.bin").write_bytes(b"\x00\x01binary")

    resolved, body = read_file_for_download(
        "archive.bin", scope=default_workspace_scope(workspace, True)
    )

    assert resolved == (workspace / "archive.bin").resolve()
    assert body == b"\x00\x01binary"


def test_download_refuses_a_file_past_the_limit(tmp_path: Path) -> None:
    """Refused with a reason rather than truncated: half a tarball is not a smaller one."""
    workspace = _workspace(tmp_path)
    (workspace / "big.bin").write_bytes(b"x" * 64)

    with pytest.raises(WebUIFileBrowserError, match="download limit") as exc:
        read_file_for_download(
            "big.bin", scope=default_workspace_scope(workspace, True), max_bytes=32
        )

    assert exc.value.status == 413


def test_download_follows_a_link_that_stays_inside_the_workspace(tmp_path: Path) -> None:
    """Unlike a mutation, reading through a link is the useful behaviour."""
    workspace = _workspace(tmp_path)
    (workspace / "real.txt").write_text("body", encoding="utf-8")
    (workspace / "link.txt").symlink_to(workspace / "real.txt")

    _, body = read_file_for_download("link.txt", scope=default_workspace_scope(workspace, True))

    assert body == b"body"


def test_download_refuses_a_link_that_leaves_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "escape.txt").symlink_to(outside)

    with pytest.raises(WebUIFileBrowserError) as exc:
        read_file_for_download("escape.txt", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 403


def test_download_refuses_a_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()

    with pytest.raises(WebUIFileBrowserError, match="not a file") as exc:
        read_file_for_download("src", scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 400


# -- Move --------------------------------------------------------------------


def test_move_puts_a_file_in_another_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("body", encoding="utf-8")
    (workspace / "docs").mkdir()

    payload = move_entry(
        None, "notes.md", str(workspace / "docs"), scope=default_workspace_scope(workspace, True)
    )

    assert (workspace / "docs" / "notes.md").read_text(encoding="utf-8") == "body"
    assert not (workspace / "notes.md").exists()
    # The view the operator dragged from.
    assert [e["name"] for e in payload["entries"]] == ["docs"]


def test_move_into_the_directory_it_is_already_in_is_a_no_op(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("body", encoding="utf-8")

    payload = move_entry(None, "notes.md", None, scope=default_workspace_scope(workspace, True))

    assert (workspace / "notes.md").exists()
    assert [e["name"] for e in payload["entries"]] == ["notes.md"]


def test_a_folder_cannot_be_moved_into_its_own_subtree(tmp_path: Path) -> None:
    """`rename` would happily detach the whole branch, with no way back to it."""
    workspace = _workspace(tmp_path)
    (workspace / "outer" / "inner").mkdir(parents=True)
    (workspace / "outer" / "inner" / "keep.txt").write_text("x", encoding="utf-8")

    with pytest.raises(WebUIFileBrowserError, match="into itself") as exc:
        move_entry(
            None,
            "outer",
            str(workspace / "outer" / "inner"),
            scope=default_workspace_scope(workspace, True),
        )

    assert exc.value.status == 400
    assert (workspace / "outer" / "inner" / "keep.txt").exists()


def test_a_folder_cannot_be_moved_into_itself(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "outer").mkdir()

    with pytest.raises(WebUIFileBrowserError, match="into itself") as exc:
        move_entry(None, "outer", str(workspace / "outer"), scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 400
    assert (workspace / "outer").is_dir()


def test_move_refuses_to_clobber_at_the_destination(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("source", encoding="utf-8")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "notes.md").write_text("destination", encoding="utf-8")

    with pytest.raises(WebUIFileBrowserError, match="already exists there") as exc:
        move_entry(
            None, "notes.md", str(workspace / "docs"), scope=default_workspace_scope(workspace, True)
        )

    assert exc.value.status == 409
    assert (workspace / "docs" / "notes.md").read_text(encoding="utf-8") == "destination"
    assert (workspace / "notes.md").exists()


def test_move_cannot_leave_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "notes.md").write_text("body", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(WebUIFileBrowserError) as exc:
        move_entry(None, "notes.md", str(outside), scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 403
    assert (workspace / "notes.md").exists()
    assert list(outside.iterdir()) == []


def test_move_takes_a_symlink_as_the_link(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    target = workspace / "real.txt"
    target.write_text("body", encoding="utf-8")
    (workspace / "link.txt").symlink_to(target)
    (workspace / "docs").mkdir()

    move_entry(None, "link.txt", str(workspace / "docs"), scope=default_workspace_scope(workspace, True))

    assert (workspace / "docs" / "link.txt").is_symlink()
    assert target.read_text(encoding="utf-8") == "body"


def test_move_will_not_take_a_source_from_outside_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(WebUIFileBrowserError) as exc:
        move_entry(str(outside), "secret.txt", None, scope=default_workspace_scope(workspace, True))

    assert exc.value.status == 403
    assert (outside / "secret.txt").exists()


def test_a_relative_path_addresses_the_same_file_as_an_absolute_one(tmp_path: Path) -> None:
    """The listing hands out relative paths, so the routes have to take them back.

    An absolute path still resolves, because a caller that kept sending one — the chat's own
    preview route hands the agent's absolute paths straight through — must not break.
    """
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "note.md").write_text("hi", encoding="utf-8")
    scope = default_workspace_scope(workspace, True)

    from nanoinfra.webui.file_browser import resolve_within_workspace

    relative = resolve_within_workspace("docs/note.md", scope=scope, must_exist=True)
    dotted = resolve_within_workspace("./docs/note.md", scope=scope, must_exist=True)
    absolute = resolve_within_workspace(
        str(workspace / "docs" / "note.md"), scope=scope, must_exist=True
    )
    root_dot = resolve_within_workspace(".", scope=scope, must_exist=True)

    assert relative == dotted == absolute == (workspace / "docs" / "note.md").resolve()
    assert root_dot == workspace.resolve()


def test_the_listing_paths_never_name_the_host_layout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "docs" / "deep").mkdir(parents=True)
    scope = default_workspace_scope(workspace, True)

    from nanoinfra.webui.file_browser import directory_listing_payload

    root = directory_listing_payload(None, scope=scope, include_hidden=False)
    nested = directory_listing_payload("docs/deep", scope=scope, include_hidden=False)

    for payload in (root, nested):
        assert not str(payload["path"]).startswith("/"), payload["path"]
        assert str(tmp_path) not in str(payload["path"])
    assert root["path"] == "." and root["parent"] is None
    assert nested["path"] == "docs/deep" and nested["parent"] == "docs"
