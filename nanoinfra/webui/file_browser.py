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

EntryKind = Literal["file", "directory", "symlink", "other"]


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
    max_entries: int = MAX_DIRECTORY_ENTRIES,
) -> dict[str, Any]:
    """``GET /api/webui/workspace/list`` -- one directory inside the session's workspace.

    An empty or missing *raw_path* means the workspace root, so the explorer has a
    starting point it does not have to know the absolute path of.
    """
    root = Path(scope.project_path)
    resolved = resolve_directory(raw_path, scope=scope)

    entries: list[DirectoryEntry] = []
    truncated = False
    try:
        with os.scandir(resolved) as scan:
            for item in scan:
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
    "DirectoryEntry",
    "WebUIFileBrowserError",
    "directory_listing_payload",
    "resolve_directory",
    "resolve_within_workspace",
]
