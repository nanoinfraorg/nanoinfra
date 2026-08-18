"""Config is the authority for plugin activation; markers are reconciled against it.

Enabling a package that ships an `mcp.json` grants a new stdio process. That decision belongs in a
git-reviewed file, not in a directory the agent can write, so `tools.agentPlugins` is the source of
truth and `reconcile_agent_plugins` makes the on-disk markers match it. See
nanoinfraorg/nanoinfra#141.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent import plugins as agent_plugins
from nanoinfra.agent.plugins import (
    AGENT_PLUGIN_SCHEMA,
    discover_agent_plugins,
    reconcile_agent_plugins,
    set_agent_plugin_enabled,
)


@pytest.fixture(autouse=True)
def _isolate_plugin_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: config_path.parent)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def _plugin(workspace: Path, name: str) -> Path:
    root = workspace / "plugins" / name
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": name}), encoding="utf-8"
    )
    return root


def _enabled(workspace: Path) -> set[str]:
    return {p.name for p in discover_agent_plugins(workspace) if p.enabled}


def test_a_listed_plugin_is_activated(workspace: Path) -> None:
    _plugin(workspace, "demo")

    result = reconcile_agent_plugins(workspace, ["demo"])

    assert _enabled(workspace) == {"demo"}
    assert result.enabled == ("demo",)


def test_an_unlisted_plugin_is_deactivated(workspace: Path) -> None:
    _plugin(workspace, "demo")
    set_agent_plugin_enabled(workspace, "demo", True)

    result = reconcile_agent_plugins(workspace, [])

    assert _enabled(workspace) == set()
    assert result.disabled == ("demo",)


def test_reconciling_twice_changes_nothing(workspace: Path) -> None:
    _plugin(workspace, "demo")
    reconcile_agent_plugins(workspace, ["demo"])

    result = reconcile_agent_plugins(workspace, ["demo"])

    assert result.enabled == ()
    assert result.disabled == ()
    assert _enabled(workspace) == {"demo"}


def test_removing_a_name_from_config_revokes_it(workspace: Path) -> None:
    _plugin(workspace, "one")
    _plugin(workspace, "two")
    reconcile_agent_plugins(workspace, ["one", "two"])

    reconcile_agent_plugins(workspace, ["one"])

    assert _enabled(workspace) == {"one"}


def test_a_name_no_package_provides_is_reported_not_raised(workspace: Path) -> None:
    """A typo in config must not stop the gateway; it must be visible."""
    _plugin(workspace, "demo")

    result = reconcile_agent_plugins(workspace, ["demo", "ghost"])

    assert _enabled(workspace) == {"demo"}
    assert result.unknown == ("ghost",)


def test_duplicate_names_in_config_are_tolerated(workspace: Path) -> None:
    _plugin(workspace, "demo")

    result = reconcile_agent_plugins(workspace, ["demo", "demo"])

    assert result.enabled == ("demo",)
    assert _enabled(workspace) == {"demo"}


def test_a_tampered_listed_package_is_reactivated_to_its_new_content(workspace: Path) -> None:
    """Config says the operator trusts this identity, so reconcile re-binds the fingerprint.

    Content-binding still does its job between reconciles: a package that changes while the
    gateway runs loses its marker on the next read. Reconcile is the reviewed moment.
    """
    root = _plugin(workspace, "demo")
    reconcile_agent_plugins(workspace, ["demo"])
    (root / "extra.txt").write_text("new\n", encoding="utf-8")
    assert _enabled(workspace) == set()

    reconcile_agent_plugins(workspace, ["demo"])

    assert _enabled(workspace) == {"demo"}


def test_an_empty_config_list_deactivates_everything(workspace: Path) -> None:
    _plugin(workspace, "one")
    _plugin(workspace, "two")
    reconcile_agent_plugins(workspace, ["one", "two"])

    reconcile_agent_plugins(workspace, [])

    assert _enabled(workspace) == set()


def test_no_packages_installed_is_not_an_error(workspace: Path) -> None:
    result = reconcile_agent_plugins(workspace, ["ghost"])

    assert result.unknown == ("ghost",)
    assert result.enabled == ()


def test_a_partial_config_object_resolves_a_default_workspace() -> None:
    """The gateway and the host are handed stand-in configs; a missing block is not a crash.

    Reaching through config.agents.defaults.workspace directly raised AttributeError on a
    SimpleNamespace that only carried `tools`, which would have taken the gateway down at startup.
    """
    from types import SimpleNamespace

    from nanoinfra.agent.plugins import workspace_from_config

    assert workspace_from_config(SimpleNamespace(tools=SimpleNamespace())).is_absolute()
    assert workspace_from_config(SimpleNamespace()).is_absolute()
    assert workspace_from_config(object()).is_absolute()


def test_an_explicit_workspace_in_config_is_honoured(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from nanoinfra.agent.plugins import workspace_from_config

    config = SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(workspace=str(tmp_path / "ws")))
    )

    assert workspace_from_config(config) == (tmp_path / "ws")
