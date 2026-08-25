"""The workspaces root: where workspaces live, and what a client may name.

`tools.workspacesRoot` holds the workspaces, and every workspace a *client* asks
for must be that root, something under it, or the configured
`agents.defaults.workspace`.

That last exception is the point of this module rather than a hole in it. Config is
git-reviewed and widens deliberately; a path arriving from a browser does not. It
is the same split `file_preview.py` records for reads, applied to the choice of
workspace: an operator who wants their projects reachable sets the root to their
parent directory, and does not get there by typing a path into the composer.

Consequence, stated plainly: a project outside the root can no longer be selected
from the WebUI. Before this, any path the process could open was fair game.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanoinfra.webui.file_browser import WebUIFileBrowserError, validate_component

#: Workspaces listed in one response. A bound on the answer, not on the directory.
MAX_WORKSPACES = 500


@dataclass(frozen=True)
class WorkspaceEntry:
    """One workspace under the root, or the configured one beside it."""

    name: str
    path: str
    modified: str | None
    #: True for ``agents.defaults.workspace``, which is allowed wherever it sits.
    is_default: bool
    #: True when it does not live under the root, which only the default can be.
    outside_root: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "modified": self.modified,
            "isDefault": self.is_default,
            "outsideRoot": self.outside_root,
        }


def _modified(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        return None


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def workspaces_payload_for_root(
    root: Path,
    default_workspace: Path,
    *,
    max_entries: int = MAX_WORKSPACES,
) -> dict[str, Any]:
    """``GET /api/webui/workspace/projects`` -- the workspaces a client may choose.

    The root is created on demand, so a fresh install lists an empty root rather
    than an error about a directory nobody has made yet.
    """
    resolved_root = root.expanduser()
    resolved_default = default_workspace.expanduser()
    entries: list[WorkspaceEntry] = []

    if not _is_within(resolved_default, resolved_root):
        entries.append(
            WorkspaceEntry(
                name=resolved_default.name or str(resolved_default),
                path=str(resolved_default),
                modified=_modified(resolved_default),
                is_default=True,
                outside_root=True,
            )
        )

    try:
        resolved_root.mkdir(parents=True, exist_ok=True)
        children = sorted(
            (child for child in resolved_root.iterdir() if child.is_dir()),
            key=lambda child: child.name.casefold(),
        )
    except OSError:
        children = []

    for child in children[:max_entries]:
        entries.append(
            WorkspaceEntry(
                name=child.name,
                path=str(child),
                modified=_modified(child),
                is_default=child == resolved_default,
                outside_root=False,
            )
        )

    return {
        "root": str(resolved_root),
        "defaultWorkspace": str(resolved_default),
        "workspaces": [entry.to_dict() for entry in entries],
    }


def resolve_client_workspace(
    raw_path: str | None,
    *,
    root: Path,
    default_workspace: Path,
) -> Path:
    """The one gate for a workspace a client named. Returns it, or raises.

    Empty means the configured default, so a client that has chosen nothing gets
    what the operator configured rather than a refusal.
    """
    resolved_root = root.expanduser()
    resolved_default = default_workspace.expanduser()
    text = (raw_path or "").strip()
    if not text:
        return resolved_default

    try:
        candidate = Path(text).expanduser().resolve(strict=False)
    except OSError as exc:
        raise WebUIFileBrowserError(400, "invalid path") from exc

    # Resolved on both sides before comparing, so a symlink into the root cannot
    # stand for a workspace outside it, and a root reached through one still matches.
    if candidate == resolved_default.resolve(strict=False):
        return resolved_default
    if not _is_within(candidate, resolved_root.resolve(strict=False)):
        raise WebUIFileBrowserError(
            403,
            "that workspace is outside the workspaces root — "
            "set tools.workspacesRoot to a directory that holds it",
        )
    if not candidate.is_dir():
        raise WebUIFileBrowserError(404, "workspace not found")
    return candidate


def create_workspace(root: Path, name: str) -> dict[str, Any]:
    """``POST /api/webui/workspace/projects/create`` -- one new workspace.

    A single component under the root, validated the same way a new folder in the
    explorer is: a name is not a path, and this one becomes a directory a client
    then gets to browse.
    """
    resolved_root = root.expanduser()
    target = resolved_root / validate_component(name)
    if target.exists() or target.is_symlink():
        raise WebUIFileBrowserError(409, "a workspace with that name already exists")
    try:
        resolved_root.mkdir(parents=True, exist_ok=True)
        target.mkdir()
    except PermissionError as exc:
        raise WebUIFileBrowserError(403, "not permitted to create a workspace there") from exc
    except OSError as exc:
        raise WebUIFileBrowserError(500, "failed to create the workspace") from exc
    return {"workspace": str(target)}


__all__ = [
    "MAX_WORKSPACES",
    "WorkspaceEntry",
    "create_workspace",
    "resolve_client_workspace",
    "workspaces_payload_for_root",
]
