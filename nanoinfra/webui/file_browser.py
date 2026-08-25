"""Workspace-scoped directory listings for the WebUI Workspaces explorer.

Sibling of ``file_preview.py`` and bound by the same rule, for the same reason:
containment is **unconditional** and does not read
``scope.restrict_to_workspace``. That setting governs the agent's own file
tools; this module decides what an authenticated WebUI client may enumerate off
the host. Reading one setting to answer both questions is how a relaxed tool
restriction would turn into a remote directory walk of everything the process
user can open, ``~/.nanoinfra/config.json`` among it.

Reads are allowed in the project path (the media directory is *not* browsable:
nothing asks to navigate it, and `file_preview.py` already reaches an attachment
by exact path). An operator who needs a wider root sets a wider workspace,
which is the capability-specific mechanism `.agent/security.md` requires.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from nanoinfra.security.workspace_access import WorkspaceScope
from nanoinfra.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path

#: A bound on one response, not a claim about how many files a directory may hold. A
#: workspace with a ``node_modules`` in it reaches five figures, and the payload is
#: built in memory and rendered as a list. The response says it was cut.
MAX_DIRECTORY_ENTRIES = 1000

#: Same shape of bound as ``file_preview.py``'s path limit, and the same reason: this
#: value came off the wire.
MAX_PATH_CHARS = 4096

#: A download is read into memory and answered as one response, because this transport
#: builds a response body rather than streaming one. So the cap is not a policy about
#: what an operator may take off the host -- it is the largest file this path can carry
#: without the gateway holding a copy of something unbounded. A file past it is refused
#: with that reason rather than truncated, since half a tarball is not a smaller tarball.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

EntryKind = Literal["file", "directory", "symlink", "other"]

#: Names a single path component may not be or contain. A name is not a path: the
#: resolver below would contain ``../x`` anyway, but accepting a separator in a field
#: the UI fills from one directory entry means the caller and the server disagree
#: about what was asked for, and that gap is where a bypass lives.
_REFUSED_NAMES = {"", ".", ".."}


class WebUIFileBrowserError(ValueError):
    """Raised when a path cannot be browsed through the WebUI.

    Carries the HTTP status the route answers with, the same contract
    ``WebUIFilePreviewError`` has. Kept separate rather than shared so neither
    module's failure vocabulary is constrained by the other's.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class DirectoryEntry:
    """One child of a listed directory."""

    name: str
    kind: EntryKind
    size: int | None
    modified: str | None
    #: True for a symlink whose target leaves the workspace. Listed rather than hidden,
    #: because a name that vanishes is more confusing than one that says why it is
    #: unreachable -- and following it is refused by the same resolver either way.
    escapes_workspace: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "modified": self.modified,
            "escapesWorkspace": self.escapes_workspace,
        }


def directory_listing_payload(
    raw_path: str | None,
    *,
    scope: WorkspaceScope,
    include_hidden: bool = False,
    max_entries: int = MAX_DIRECTORY_ENTRIES,
) -> dict[str, Any]:
    """``GET /api/webui/workspace/list`` -- one directory inside the session's workspace.

    An empty or missing *raw_path* means the workspace root, so the explorer has a
    starting point it does not have to know the absolute path of.

    Dot-entries are filtered here rather than in the browser, and that is not only a
    presentation choice: a workspace under version control has a ``.git`` holding
    thousands of objects, and forwarding them would spend the whole ``max_entries``
    budget on names nobody asked to see -- reporting the directory as truncated
    while the files the operator came for sat outside the cut. Their number is
    still counted and reported, so the toggle that asks for them says how many
    there are instead of appearing to do nothing.
    """
    root = Path(scope.project_path)
    resolved = resolve_directory(raw_path, scope=scope)

    entries: list[DirectoryEntry] = []
    hidden_count = 0
    truncated = False
    try:
        with os.scandir(resolved) as scan:
            for item in scan:
                if item.name.startswith(".") and not include_hidden:
                    # Counted, not listed -- and deliberately not against max_entries.
                    hidden_count += 1
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    break
                entries.append(_entry_for(item, root=root))
    except PermissionError as exc:
        raise WebUIFileBrowserError(403, "directory is not readable") from exc
    except OSError as exc:
        raise WebUIFileBrowserError(500, "failed to read directory") from exc

    entries.sort(key=lambda e: (e.kind != "directory", e.name.casefold()))
    return {
        "path": str(resolved),
        "displayPath": _display_path(resolved, root),
        "projectPath": str(root),
        # ``None`` at the root, so the UI can render "up" as unavailable instead of
        # offering a step the resolver would refuse.
        "parent": None if resolved == root else str(resolved.parent),
        "entries": [entry.to_dict() for entry in entries],
        "truncated": truncated,
        "includeHidden": include_hidden,
        # Zero when they are already in ``entries``, so the UI never offers to reveal
        # what it is showing.
        "hiddenCount": hidden_count,
    }


def resolve_directory(raw_path: str | None, *, scope: WorkspaceScope) -> Path:
    """Resolve *raw_path* to a directory inside the workspace, or raise."""
    resolved = resolve_within_workspace(raw_path, scope=scope, must_exist=True)
    if not resolved.is_dir():
        raise WebUIFileBrowserError(400, "path is not a directory")
    return resolved


def resolve_within_workspace(
    raw_path: str | None,
    *,
    scope: WorkspaceScope,
    must_exist: bool,
) -> Path:
    """The single containment gate for this module.

    Every path that reaches the filesystem from a Workspaces route goes through
    here, including the ones the mutating routes build. ``strict=True`` resolves
    symlinks before the containment check, so a link inside the workspace cannot
    name a target outside it.
    """
    root = Path(scope.project_path)
    text = (raw_path or "").strip()
    if len(text) > MAX_PATH_CHARS:
        raise WebUIFileBrowserError(400, "path is too long")
    if not text:
        return root

    try:
        resolved = resolve_allowed_path(
            text,
            workspace=root,
            allowed_root=root,
            strict=must_exist,
        )
    except FileNotFoundError as exc:
        raise WebUIFileBrowserError(404, "path not found") from exc
    except WorkspaceBoundaryError as exc:
        raise WebUIFileBrowserError(403, "path is outside the current workspace") from exc
    except OSError as exc:
        raise WebUIFileBrowserError(400, "invalid path") from exc

    if must_exist and not resolved.exists():
        raise WebUIFileBrowserError(404, "path not found")
    return resolved


def resolve_child(
    parent_path: str | None,
    name: str,
    *,
    scope: WorkspaceScope,
) -> Path:
    """``(parent, name)`` -> a path whose parent is contained and whose last component is *not* followed.

    Every mutation takes this shape rather than a single path, and that is the whole
    defence for a symlinked entry. ``resolve_within_workspace`` resolves symlinks
    (it must, or a link would be a way out of the workspace), so resolving
    ``<dir>/link`` hands back the *target*: a delete would then remove whatever the
    link pointed at instead of the link. Resolving only the parent and joining a
    validated component leaves the final name untouched, and ``unlink``/``rename``
    then act on the link itself.
    """
    parent = resolve_directory(parent_path, scope=scope)
    return parent / validate_component(name)


def validate_component(name: str) -> str:
    """Return *name* if it is one ordinary path component, else raise."""
    text = (name or "").strip()
    if text in _REFUSED_NAMES:
        raise WebUIFileBrowserError(400, "invalid name")
    if "/" in text or "\\" in text or "\0" in text:
        raise WebUIFileBrowserError(400, "a name may not contain a path separator")
    if len(text) > 255:
        raise WebUIFileBrowserError(400, "name is too long")
    return text


def create_directory(
    parent_path: str | None,
    name: str,
    *,
    scope: WorkspaceScope,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """``POST /api/webui/workspace/mkdir`` -- one new directory, no parents."""
    target = resolve_child(parent_path, name, scope=scope)
    if target.exists() or target.is_symlink():
        raise WebUIFileBrowserError(409, "a file or folder with that name already exists")
    try:
        # parents=False: creating an intermediate directory the operator did not name
        # is a different request from the one they made.
        target.mkdir()
    except PermissionError as exc:
        raise WebUIFileBrowserError(403, "not permitted to write here") from exc
    except OSError as exc:
        raise WebUIFileBrowserError(500, "failed to create the folder") from exc
    return directory_listing_payload(parent_path, scope=scope, include_hidden=include_hidden)


def rename_entry(
    parent_path: str | None,
    name: str,
    new_name: str,
    *,
    scope: WorkspaceScope,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """``POST /api/webui/workspace/rename`` -- rename inside one directory.

    Deliberately not a move: a rename that may also change directory is a second
    capability with its own failure modes (across filesystems, into itself), and
    nothing here needs it yet.
    """
    source = resolve_child(parent_path, name, scope=scope)
    target = source.parent / validate_component(new_name)
    if not source.exists() and not source.is_symlink():
        raise WebUIFileBrowserError(404, "path not found")
    if target == source:
        return directory_listing_payload(parent_path, scope=scope, include_hidden=include_hidden)
    if target.exists() or target.is_symlink():
        raise WebUIFileBrowserError(409, "a file or folder with that name already exists")
    try:
        source.rename(target)
    except PermissionError as exc:
        raise WebUIFileBrowserError(403, "not permitted to rename here") from exc
    except OSError as exc:
        raise WebUIFileBrowserError(500, "failed to rename") from exc
    return directory_listing_payload(parent_path, scope=scope, include_hidden=include_hidden)


def delete_entry(
    parent_path: str | None,
    name: str,
    *,
    recursive: bool,
    scope: WorkspaceScope,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """``POST /api/webui/workspace/delete`` -- one entry of one directory.

    A non-empty directory needs *recursive* set explicitly. The flag is not a
    formality: it is the difference between what the operator saw when they clicked
    (one folder) and what the call actually removes (everything under it), and the
    route makes the client say which one it meant.
    """
    target = resolve_child(parent_path, name, scope=scope)
    root = Path(scope.project_path)
    if target == root:
        raise WebUIFileBrowserError(400, "the workspace root cannot be deleted")
    if not target.exists() and not target.is_symlink():
        raise WebUIFileBrowserError(404, "path not found")

    try:
        # Checked before is_dir(), because a symlink to a directory answers True there
        # and must be unlinked rather than walked.
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            if any(target.iterdir()) and not recursive:
                raise WebUIFileBrowserError(409, "the folder is not empty")
            shutil.rmtree(target) if recursive else target.rmdir()
        else:
            target.unlink()
    except WebUIFileBrowserError:
        raise
    except PermissionError as exc:
        raise WebUIFileBrowserError(403, "not permitted to delete here") from exc
    except OSError as exc:
        raise WebUIFileBrowserError(500, "failed to delete") from exc
    return directory_listing_payload(parent_path, scope=scope, include_hidden=include_hidden)


def read_file_for_download(
    raw_path: str | None,
    *,
    scope: WorkspaceScope,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> tuple[Path, bytes]:
    """``GET /api/webui/workspace/download`` -- one file's bytes, contained as ever.

    Unlike the mutations, this follows a symlink on purpose: reading through a link
    that stays inside the workspace is the useful behaviour, and one that leaves it
    is refused by the same resolver.
    """
    resolved = resolve_within_workspace(raw_path, scope=scope, must_exist=True)
    if not resolved.is_file():
        raise WebUIFileBrowserError(400, "path is not a file")
    try:
        size = resolved.stat().st_size
        if size > max_bytes:
            raise WebUIFileBrowserError(
                413,
                f"file is larger than the {max_bytes // (1024 * 1024)} MB download limit",
            )
        return resolved, resolved.read_bytes()
    except WebUIFileBrowserError:
        raise
    except PermissionError as exc:
        raise WebUIFileBrowserError(403, "file is not readable") from exc
    except OSError as exc:
        raise WebUIFileBrowserError(500, "failed to read file") from exc


def _entry_for(item: os.DirEntry[str], *, root: Path) -> DirectoryEntry:
    is_symlink = item.is_symlink()
    escapes = False
    try:
        if is_symlink:
            target = Path(item.path).resolve()
            escapes = not _is_within(target, root)
        # follow_symlinks=False keeps a link's own timestamps and refuses to touch its
        # target, which may not exist or may sit outside the workspace.
        stat = item.stat(follow_symlinks=False)
        size = stat.st_size
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    except OSError:
        # A child that cannot be stat'ed is still a name the operator should see.
        size = None
        modified = None

    if is_symlink:
        kind: EntryKind = "symlink"
    elif item.is_dir(follow_symlinks=False):
        kind = "directory"
    elif item.is_file(follow_symlinks=False):
        kind = "file"
    else:
        kind = "other"

    return DirectoryEntry(
        name=item.name,
        kind=kind,
        size=None if kind == "directory" else size,
        modified=modified,
        escapes_workspace=escapes,
    )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _display_path(path: Path, root: Path) -> str:
    """The workspace-relative path, which is what the breadcrumb shows."""
    if path == root:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "MAX_DIRECTORY_ENTRIES",
    "MAX_DOWNLOAD_BYTES",
    "DirectoryEntry",
    "WebUIFileBrowserError",
    "create_directory",
    "delete_entry",
    "directory_listing_payload",
    "read_file_for_download",
    "rename_entry",
    "resolve_child",
    "resolve_directory",
    "resolve_within_workspace",
    "validate_component",
]
