"""Decide which files under ``knowledge/`` may be read at all (#244).

Three refusals live here, and each one is a rule the index must not be able to talk itself out
of: the operator's exclude list, the workspace boundary, and the size caps. A file this module
refuses is reported with its reason -- a skipped file nobody is told about is indistinguishable
from a file that was indexed and never matched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from nanoinfra.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path

#: Our own index, which must never index itself.
INDEX_DIR_NAME = ".index"

#: How much of a file the text check looks at.
SNIFF_BYTES = 4096

REASON_TOO_LARGE = "too_large"
REASON_TOTAL_BUDGET = "total_budget"
REASON_NOT_TEXT = "not_text"
REASON_OUTSIDE_WORKSPACE = "outside_workspace"
REASON_UNREADABLE = "unreadable"

_REASON_TEXT = {
    REASON_TOO_LARGE: "larger than the per-file limit",
    REASON_TOTAL_BUDGET: "the total index size limit was already reached",
    REASON_NOT_TEXT: "not a text document",
    REASON_OUTSIDE_WORKSPACE: "a symlink leaving the knowledge folder",
    REASON_UNREADABLE: "could not be read",
}


@dataclass(frozen=True)
class Candidate:
    """A file that passed every refusal in this module."""

    rel: str
    path: Path
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class Skip:
    """A file that did not, and why. This is the *reported* half of "skipped and reported".

    ``mtime_ns`` and ``size`` are set only for a refusal that came from reading the file. They
    are what lets the next pass remember the refusal instead of opening the file again, and a
    refusal decided from stat alone leaves them at zero because recomputing it is free.
    """

    rel: str
    reason: str
    detail: str = ""
    mtime_ns: int = 0
    size: int = 0

    def describe(self) -> str:
        base = _REASON_TEXT.get(self.reason, self.reason)
        return f"{self.rel}: {base}" + (f" ({self.detail})" if self.detail else "")


def is_excluded(rel: str, patterns: list[str]) -> bool:
    """Whether *rel* (a POSIX path relative to the knowledge root) is excluded.

    Four matches per pattern, because an operator writing ``secrets/**`` means "any secrets
    folder" and one writing ``.env`` means "any .env". Matching only the full relative path would
    make ``.env`` exclude the file at the root and index the one in a subfolder, which is the
    worse half of a secret exclusion working.
    """
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if not pattern:
            continue
        if (
            fnmatch(name, pattern)
            or fnmatch(rel, pattern)
            or fnmatch(rel, f"*/{pattern}")
            or fnmatch(f"{rel}/", pattern)
        ):
            return True
    return False


def looks_like_text(prefix: bytes) -> bool:
    """Whether a file's first bytes read as text.

    Asked of the bytes and never of the extension, the same rule ``workspace_asset`` applies: a
    ``.md`` holding a JPEG is a JPEG, and its bytes would otherwise become index terms.
    """
    if b"\x00" in prefix:
        return False
    try:
        prefix.decode("utf-8")
    except UnicodeDecodeError:
        # A cut multi-byte character at the read boundary is not a binary file.
        return prefix.decode("utf-8", errors="ignore") != ""
    return True


def iter_candidates(
    root: Path,
    *,
    exclude: list[str],
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[list[Candidate], list[Skip]]:
    """Walk *root* and return what may be indexed, plus every refusal and its reason.

    No file is opened here, only stat-ed. This walk runs on *every* search as the freshness pass,
    so a knowledge base of 500 documents must not cost 500 file reads to answer "nothing
    changed". Whether the bytes are text is decided by the caller, which has to read the file
    anyway to index it.
    """
    candidates: list[Candidate] = []
    skips: list[Skip] = []
    if not root.is_dir():
        return candidates, skips

    total = 0
    # followlinks=False is the containment rule for directories: a symlinked folder is never
    # descended, so a link to ``/etc`` cannot become a subtree of the knowledge base. Files are
    # checked one at a time below, because a symlinked *file* is worth following when it stays
    # inside the folder and must be refused when it does not.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = "" if here == root else here.relative_to(root).as_posix()
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name != INDEX_DIR_NAME
            and not is_excluded(f"{rel_dir}/{name}" if rel_dir else name, exclude)
        )
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if is_excluded(rel, exclude):
                continue
            path = here / name
            resolved = _contained(path, root)
            if resolved is None:
                skips.append(Skip(rel, REASON_OUTSIDE_WORKSPACE))
                continue
            try:
                stat = resolved.stat()
            except OSError as exc:
                skips.append(Skip(rel, REASON_UNREADABLE, exc.strerror or ""))
                continue
            if not resolved.is_file():
                continue
            if stat.st_size > max_file_bytes:
                skips.append(Skip(rel, REASON_TOO_LARGE, f"{stat.st_size} bytes"))
                continue
            if total + stat.st_size > max_total_bytes:
                skips.append(Skip(rel, REASON_TOTAL_BUDGET, f"{stat.st_size} bytes"))
                continue
            total += stat.st_size
            candidates.append(
                Candidate(rel=rel, path=resolved, mtime_ns=stat.st_mtime_ns, size=stat.st_size)
            )
    return candidates, skips


def _contained(path: Path, root: Path) -> Path | None:
    """Resolve *path* and require it to stay under *root*, or return None.

    ``root`` is the knowledge folder rather than the workspace, which is stricter than the file
    browser's rule and deliberately so: the workspace holds the secrets store, so a link from
    ``knowledge/`` to ``../secrets`` has to be refused by the same check that refuses a link to
    ``/etc``. One rule, no list of sensitive paths to keep current.
    """
    try:
        return resolve_allowed_path(path, allowed_root=root, strict=True)
    except (WorkspaceBoundaryError, FileNotFoundError, OSError):
        return None
