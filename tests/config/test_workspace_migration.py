"""Moving a pre-root default workspace under the workspaces root."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.config.workspace_migration import migrate_default_workspace


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home, so the migration's own defaults point inside tmp_path.

    ``HOME`` rather than a patched ``Path.home``: ``expanduser()`` reads the
    environment, so patching the method alone leaves the config value it compares
    against resolving to the real home — which is how this fixture was wrong first.
    """
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    (fake / ".nanoinfra").mkdir()
    return fake


def _config(home: Path, workspace: str) -> Path:
    path = home / ".nanoinfra" / "config.json"
    path.write_text(
        json.dumps({"agents": {"defaults": {"workspace": workspace, "model": "x/y"}}, "tools": {}}),
        encoding="utf-8",
    )
    return path


def _legacy_workspace(home: Path) -> Path:
    workspace = home / ".nanoinfra" / "workspace"
    (workspace / "secrets").mkdir(parents=True)
    (workspace / "secrets" / "a.json").write_text("{}", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("mine", encoding="utf-8")
    return workspace


def test_it_moves_the_workspace_and_rewrites_the_one_key(home: Path) -> None:
    config_path = _config(home, "~/.nanoinfra/workspace")
    _legacy_workspace(home)

    result = migrate_default_workspace(config_path)

    assert result.moved is True
    target = home / ".nanoinfra" / "workspaces" / "default"
    # Everything came with it, including the state that is not project files.
    assert (target / "secrets" / "a.json").read_text(encoding="utf-8") == "{}"
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "mine"
    assert not (home / ".nanoinfra" / "workspace").exists()

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["agents"]["defaults"]["workspace"] == "~/.nanoinfra/workspaces/default"
    # Nothing else in the file was touched.
    assert raw["agents"]["defaults"]["model"] == "x/y"


def test_it_runs_once(home: Path) -> None:
    config_path = _config(home, "~/.nanoinfra/workspace")
    _legacy_workspace(home)
    assert migrate_default_workspace(config_path).moved is True

    again = migrate_default_workspace(config_path)

    assert again.moved is False
    assert again.reason == "workspace is not the pre-root default"


def test_a_deliberate_workspace_is_left_alone(home: Path) -> None:
    """Any path other than the old default is a choice, and not ours to relocate."""
    project = home / "Projects" / "thing"
    project.mkdir(parents=True)
    config_path = _config(home, str(project))
    _legacy_workspace(home)

    result = migrate_default_workspace(config_path)

    assert result.moved is False
    assert (home / ".nanoinfra" / "workspace" / "AGENTS.md").exists()
    assert json.loads(config_path.read_text(encoding="utf-8"))["agents"]["defaults"]["workspace"] == str(project)


def test_a_destination_that_holds_something_stops_it(home: Path) -> None:
    """Merging two workspaces is not a migration, and picking between two
    `secrets/` directories is not a decision code gets to make."""
    config_path = _config(home, "~/.nanoinfra/workspace")
    _legacy_workspace(home)
    occupied = home / ".nanoinfra" / "workspaces" / "default"
    occupied.mkdir(parents=True)
    (occupied / "theirs.md").write_text("someone else", encoding="utf-8")

    result = migrate_default_workspace(config_path)

    assert result.moved is False
    assert result.reason == "the destination already holds something"
    assert (home / ".nanoinfra" / "workspace" / "AGENTS.md").exists()
    assert (occupied / "theirs.md").exists()


def test_an_empty_destination_is_not_an_obstacle(home: Path) -> None:
    config_path = _config(home, "~/.nanoinfra/workspace")
    _legacy_workspace(home)
    (home / ".nanoinfra" / "workspaces" / "default").mkdir(parents=True)

    assert migrate_default_workspace(config_path).moved is True
    assert (home / ".nanoinfra" / "workspaces" / "default" / "AGENTS.md").exists()


def test_a_symlinked_workspace_is_not_followed(home: Path) -> None:
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "AGENTS.md").write_text("not ours to move", encoding="utf-8")
    (home / ".nanoinfra" / "workspace").symlink_to(elsewhere, target_is_directory=True)
    config_path = _config(home, "~/.nanoinfra/workspace")

    result = migrate_default_workspace(config_path)

    assert result.moved is False
    assert result.reason == "the workspace is a symlink"
    assert (elsewhere / "AGENTS.md").exists()


def test_nothing_to_move_is_not_an_error(home: Path) -> None:
    config_path = _config(home, "~/.nanoinfra/workspace")

    result = migrate_default_workspace(config_path)

    assert result.moved is False
    assert result.reason == "the workspace does not exist"


def test_an_unreadable_config_moves_nothing(home: Path) -> None:
    config_path = home / ".nanoinfra" / "config.json"
    config_path.write_text("{not json", encoding="utf-8")
    _legacy_workspace(home)

    result = migrate_default_workspace(config_path)

    assert result.moved is False
    assert (home / ".nanoinfra" / "workspace" / "AGENTS.md").exists()


def test_a_missing_config_moves_nothing(home: Path) -> None:
    _legacy_workspace(home)

    result = migrate_default_workspace(home / ".nanoinfra" / "config.json")

    assert result.moved is False
    assert (home / ".nanoinfra" / "workspace" / "AGENTS.md").exists()


def test_a_failed_config_rewrite_puts_the_workspace_back(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the data moved and config still names the old path: the next start
    would come up pointing at nothing."""
    config_path = _config(home, "~/.nanoinfra/workspace")
    _legacy_workspace(home)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("nanoinfra.config.workspace_migration._write_json_atomic", explode)

    result = migrate_default_workspace(config_path)

    assert result.moved is False
    assert result.reason == "the config rewrite failed"
    assert (home / ".nanoinfra" / "workspace" / "AGENTS.md").exists()
    assert not (home / ".nanoinfra" / "workspaces" / "default").exists()
