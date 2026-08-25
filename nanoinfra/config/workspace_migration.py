"""Move a pre-root default workspace under the workspaces root, once.

`~/.nanoinfra/workspace` was the default before `tools.workspacesRoot` existed, so
every install that never chose a workspace sits outside the root its own picker
reports against. This moves it to `~/.nanoinfra/workspaces/default` and rewrites the
one config key that names it.

Deliberately not part of ``loader._migrate_config``: that is a pure transform of the
config dict and runs on every ``load_config()``, in every process. Moving a
directory is neither pure nor repeatable, so it runs once, at startup, from a place
that can log what it did.

What moves with it is the reason this is careful rather than clever: the workspace
holds ``secrets/``, ``diagrams/``, ``servers/``, ``skills/``, ``triggers/`` and
``memory/`` as well as the operator's own files. Every guard below prefers doing
nothing over doing half of it — the old path keeps working either way, because
config naming a workspace is what makes it allowed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanoinfra.config.paths import default_workspace_path


@dataclass(frozen=True)
class WorkspaceMigration:
    """What the migration did, or why it did not."""

    moved: bool
    reason: str
    source: Path | None = None
    target: Path | None = None


def _legacy_workspace() -> Path:
    """The default before the workspaces root existed."""
    return Path.home() / ".nanoinfra" / "workspace"


def _is_empty(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except OSError:
        return False


def migrate_default_workspace(config_path: Path) -> WorkspaceMigration:
    """Move the pre-root default workspace under the root, and point config at it."""
    legacy = _legacy_workspace()
    target = default_workspace_path()

    if not config_path.is_file():
        return WorkspaceMigration(False, "no config file")

    try:
        parsed: object = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Not this module's problem to report: the loader answers a broken config
        # with a real error, and guessing at it here would move data on the strength
        # of a file we could not read.
        return WorkspaceMigration(False, "config unreadable")
    if not isinstance(parsed, dict):
        return WorkspaceMigration(False, "config unreadable")
    # Checked, then named: this is the untrusted-JSON edge, and everything below
    # works on a concrete type rather than carrying `Any` through the function.
    raw = cast(dict[str, Any], parsed)

    agents = raw.get("agents")
    raw_defaults = (
        cast(dict[str, Any], agents).get("defaults") if isinstance(agents, dict) else None
    )
    if not isinstance(raw_defaults, dict):
        return WorkspaceMigration(False, "config names no workspace")
    defaults = cast(dict[str, Any], raw_defaults)
    configured = defaults.get("workspace")
    if not isinstance(configured, str) or not configured.strip():
        return WorkspaceMigration(False, "config names no workspace")

    # Only the pre-root default. Any other path is a deliberate choice, and a
    # deliberate choice is not ours to relocate.
    if Path(configured).expanduser().resolve(strict=False) != legacy.resolve(strict=False):
        return WorkspaceMigration(False, "workspace is not the pre-root default")

    if legacy.is_symlink():
        # The link could name anything, including a directory that is not ours to
        # move. The same caution `SessionManager._migrate_from_workspace` takes.
        return WorkspaceMigration(False, "the workspace is a symlink")
    if not legacy.is_dir():
        return WorkspaceMigration(False, "the workspace does not exist")
    if target.exists() and not (target.is_dir() and _is_empty(target)):
        # Something is already there. Merging two workspaces is not a migration, and
        # picking a winner between two `secrets/` directories is not a decision code
        # gets to make.
        return WorkspaceMigration(False, "the destination already holds something")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()
        # Both sit under ~/.nanoinfra, so this is a rename: the contents are never
        # copied, and there is no window where they exist in two places.
        shutil.move(str(legacy), str(target))
    except (OSError, shutil.Error) as exc:
        logger.warning("Could not move {} to {}: {}", legacy, target, exc)
        return WorkspaceMigration(False, "the move failed")

    defaults["workspace"] = f"~/{target.relative_to(Path.home())}" if _under_home(target) else str(target)
    try:
        _write_json_atomic(config_path, raw)
    except OSError as exc:
        # The data moved and the config still names the old path, which would leave
        # the next start pointing at nothing. Put it back rather than leave that.
        logger.warning("Could not rewrite {}: {}; moving the workspace back", config_path, exc)
        try:
            shutil.move(str(target), str(legacy))
        except (OSError, shutil.Error):
            logger.error(
                "The workspace is at {} but config.json still names {}. Set "
                "agents.defaults.workspace to the new path by hand.",
                target,
                legacy,
            )
        return WorkspaceMigration(False, "the config rewrite failed")

    logger.info(
        "Moved the default workspace from {} to {} and updated {}. Everything in it "
        "moved with it: secrets, diagrams, servers, skills, triggers and memory.",
        legacy,
        target,
        config_path,
    )
    return WorkspaceMigration(True, "moved", source=legacy, target=target)


def _under_home(path: Path) -> bool:
    try:
        path.relative_to(Path.home())
        return True
    except ValueError:
        return False


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write via a sibling temp file and one rename, so a failure keeps the old file."""
    temp = path.with_name(f".{path.name}.nanoinfra-migrate")
    try:
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


__all__ = ["WorkspaceMigration", "migrate_default_workspace"]
