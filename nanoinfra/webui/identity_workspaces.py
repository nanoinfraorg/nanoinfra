"""One identity, one directory under the workspaces root.

The name of that directory is derived and not chosen, because both obvious choices
are wrong. A directory named from the email address is a directory that a rename
orphans and that a reassigned address inherits -- and inheriting someone's files
because a Workspace domain handed you their old address is the failure worth
designing out. A directory named from the raw subject claim is unreadable and
provider-shaped, and a path that looks like an identifier invites a reader to
trust it.

So the key is the pair ``(issuer, subject)``, which no provider reuses, and the
name is a truncated digest of it. The index file is what makes the disk legible:
it records which identity was last seen behind each directory, so an operator
reading the root can tell whose it is without asking the gateway.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, cast

from loguru import logger

# The prefix exists so a personal directory can never be confused with `default`,
# or with a workspace an operator created by hand.
IDENTITY_DIR_PREFIX = "u-"
# Ten base32 characters of a SHA-256 is 50 bits. A collision needs ~2^25 identities
# on one deployment before it becomes likely, and the index below would show it.
IDENTITY_DIR_DIGEST_CHARS = 10
IDENTITY_INDEX_NAME = ".identities.json"
_MAX_INDEX_BYTES = 512 * 1024


def identity_workspace_key(issuer: str, subject: str) -> str:
    """The storage key for one person at one provider.

    Both halves are needed. Two providers can name the same person, and admitting
    them into one directory would let an account at either one reach the other's
    files, so the answer fails closed: two issuers are two people.
    """
    issuer = (issuer or "").strip()
    subject = (subject or "").strip()
    if not subject:
        return ""
    return f"{issuer}\n{subject}"


def identity_dirname(key: str) -> str:
    """The directory name for a storage key. Stable, opaque, and filesystem-safe."""
    if not key:
        raise ValueError("an empty identity key names no directory")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower()
    return IDENTITY_DIR_PREFIX + encoded[:IDENTITY_DIR_DIGEST_CHARS]


def is_identity_dirname(name: str) -> bool:
    """Whether a directory name was produced by this module."""
    if not name.startswith(IDENTITY_DIR_PREFIX):
        return False
    tail = name[len(IDENTITY_DIR_PREFIX) :]
    return len(tail) == IDENTITY_DIR_DIGEST_CHARS and all(
        c in "abcdefghijklmnopqrstuvwxyz234567" for c in tail
    )


def identity_workspace_path(root: Path, key: str) -> Path:
    """Where this identity's workspace lives, whether or not it exists yet."""
    return root / identity_dirname(key)


def index_path(root: Path) -> Path:
    return root / IDENTITY_INDEX_NAME


def read_identity_index(root: Path) -> dict[str, Any]:
    """The directory-to-identity map, or an empty one.

    A damaged index is not an authentication failure and must not become one: the
    directory name comes from the key every time, so this file is a label and
    never an authority. An unreadable one is logged and replaced.
    """
    path = index_path(root)
    try:
        if not path.is_file() or path.stat().st_size > _MAX_INDEX_BYTES:
            return {}
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        logger.warning("workspaces: identity index unreadable at {}: {}", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = cast(dict[str, Any], raw)
    return entries


def _write_identity_index(root: Path, entries: dict[str, Any]) -> None:
    path = index_path(root)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("workspaces: identity index not written at {}: {}", path, exc)
        temp.unlink(missing_ok=True)


def record_identity(root: Path, key: str, *, name: str) -> None:
    """Note which identity is behind a directory, and when it was last seen.

    The subject is written, because it is opaque and it is what the directory name
    was derived from -- an operator who has to prove that a directory belongs to a
    person needs both halves of the key, not a hash they cannot reverse.
    """
    issuer, _, subject = key.partition("\n")
    dirname = identity_dirname(key)
    entries = read_identity_index(root)
    previous = entries.get(dirname)
    entry = {
        "issuer": issuer,
        "subject": subject,
        "identity": name,
        "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if isinstance(previous, dict):
        first_seen = cast(dict[str, Any], previous).get("first_seen")
        entry["first_seen"] = first_seen if isinstance(first_seen, str) else entry["last_seen"]
    else:
        entry["first_seen"] = entry["last_seen"]
    entries[dirname] = entry
    _write_identity_index(root, entries)


IDENTITY_DEFAULT_WORKSPACE = "default"


def ensure_identity_workspace(root: Path, key: str, *, name: str) -> Path:
    """Create this identity's own root if it is not there, and return it.

    The person's first workspace is ``default`` **inside** it, mirroring the shared
    posture one level down: a root holds workspaces, and one of them is the default.
    The identity directory is the root and not the workspace, because a switcher
    lists what is under a root -- point both at the same directory and a person's
    own workspace is the one thing the picker cannot show them.

    It is seeded the way ``create_workspace`` seeds one, through the same
    ``sync_workspace_templates``: ``AGENTS.md``, ``HEARTBEAT.md``, ``SOUL.md``,
    ``USER.md``, ``memory/`` and ``prompts/``. That function's own docstring says
    why, and it applies here word for word -- a bare directory is a workspace the
    agent reads no instructions from and keeps no memory in, and the word would
    mean two different things depending on which surface made it.

    The store directories (``secrets/``, ``servers/``, ``diagrams/``,
    ``triggers/``) are deliberately not created and deliberately not copied. Each
    store makes its own on first write, an empty one would claim the feature is in
    use, and copying a credential store would be the worst of the three.

    The mode is the one the container's entrypoint gives ``workspaces/default``:
    the agent account owns the directory and nothing else on the host reads it.
    A creation that fails is raised rather than swallowed, because the caller's
    alternative -- the shared default -- would hand one person another's files.
    """
    from nanoinfra.utils.helpers import sync_workspace_templates

    path = identity_workspace_path(root, key)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = path / IDENTITY_DEFAULT_WORKSPACE
    fresh = not workspace.exists()
    workspace.mkdir(exist_ok=True, mode=0o700)
    if fresh:
        # silent: this runs on a handshake, and no console is watching.
        sync_workspace_templates(workspace, silent=True)
    record_identity(root, key, name=name)
    return path
