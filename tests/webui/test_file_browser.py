from pathlib import Path

import pytest

from nanoinfra.security.workspace_access import default_workspace_scope
from nanoinfra.webui.file_browser import (
    WebUIFileBrowserError,
    directory_listing_payload,
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

    assert Path(payload["path"]) == workspace.resolve()
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
    assert Path(payload["parent"]) == (workspace / "src").resolve()


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


def test_hidden_files_are_listed(tmp_path: Path) -> None:
    """A workspace's dotfiles are the point of browsing it, not noise to filter."""
    workspace = _workspace(tmp_path)
    (workspace / ".env.example").write_text("KEY=", encoding="utf-8")

    names = [
        e["name"]
        for e in directory_listing_payload(None, scope=default_workspace_scope(workspace, True))["entries"]
    ]

    assert ".env.example" in names


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
