"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from nanoinfra.utils.helpers import ensure_dir


def get_config_path() -> Path:
    """Get the configuration file path (lazy import to break circular dependency).

    Delegates to ``nanoinfra.config.loader.get_config_path`` at call time so
    that importing this module never triggers a circular import during startup.
    """
    from nanoinfra.config.loader import get_config_path as _loader_get_config_path
    return _loader_get_config_path()


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_webui_dir() -> Path:
    """Return the directory for WebUI-only persisted display threads (JSON)."""
    return get_runtime_subdir("webui")


def default_workspaces_root() -> Path:
    """Where workspaces live when config does not say otherwise.

    Mirrors ``tools.workspacesRoot``'s own default, and a test pins the two together:
    a default workspace that did not sit under the default root would leave a fresh
    install with a workspace its own picker calls "outside the root".
    """
    return Path.home() / ".nanoinfra" / "workspaces"


def default_workspace_path() -> Path:
    """The workspace a fresh install gets: ``default``, inside the workspaces root.

    An install that predates the root keeps whatever its ``config.json`` says --
    ``~/.nanoinfra/workspace``, typically -- and goes on working, because that
    directory is not only project files: ``secrets/``, ``diagrams/``, ``servers/``,
    ``skills/``, ``triggers/`` and ``memory/`` live in it. Moving it is an operator's
    decision (one ``mv`` and one config edit), not something a version bump does
    underneath them.
    """
    return default_workspaces_root() / "default"


def get_workspace_path(workspace: str | Path | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else default_workspace_path()
    return ensure_dir(path)


def is_default_workspace(workspace: str | Path | None) -> bool:
    """Return whether a workspace resolves to nanoinfra's default workspace path.

    Both spellings count: the current default under the workspaces root, and the
    pre-root ``~/.nanoinfra/workspace`` that existing installs still name. Callers
    use this to decide whether an operator has chosen a workspace deliberately, and
    an install that never chose one has not become deliberate by being older.
    """
    current = (
        Path(workspace).expanduser() if workspace is not None else default_workspace_path()
    ).resolve(strict=False)
    return current in {
        default_workspace_path().resolve(strict=False),
        (Path.home() / ".nanoinfra" / "workspace").resolve(strict=False),
    }


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".nanoinfra" / "history" / "cli_history"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".nanoinfra" / "sessions"
