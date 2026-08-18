"""The Agent Plugins payload reports state and never changes it.

Activation is declared in tools.agentPlugins and reconciled by the executor (#141), so this API is
read-only by necessity: a mutating endpoint would be a second authority. See
nanoinfraorg/nanoinfra#142.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent import plugins as agent_plugins
from nanoinfra.agent.plugins import (
    AGENT_PLUGIN_MCP_SCHEMA,
    AGENT_PLUGIN_SCHEMA,
    NANOINFRA_EXTENSION,
    reconcile_agent_plugins,
)
from nanoinfra.webui.agent_plugins_api import agent_plugins_payload


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    config_path = state / "config.json"
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: state)

    workspace = tmp_path / "workspace"
    root = workspace / "plugins" / "acme.toolkit"
    (root / "skills" / "deploy-check").mkdir(parents=True)
    (root / "skills" / "deploy-check" / "SKILL.md").write_text(
        "---\nname: deploy-check\ndescription: Check a deploy.\n---\n", encoding="utf-8"
    )
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": AGENT_PLUGIN_SCHEMA,
                "name": "acme.toolkit",
                "description": "Deploy helpers.",
                "repository": "https://example.com/acme",
                "extensions": {
                    NANOINFRA_EXTENSION: {"displayName": "Deploy Toolkit", "category": "Ops"}
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": AGENT_PLUGIN_MCP_SCHEMA,
                "mcpServers": {"api": {"type": "stdio", "command": "echo"}},
            }
        ),
        encoding="utf-8",
    )

    other = workspace / "plugins" / "corp.advisor"
    other.mkdir(parents=True)
    (other / "plugin.json").write_text(
        json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "corp.advisor"}), encoding="utf-8"
    )

    config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(workspace)}},
                "tools": {"agentPlugins": ["acme.toolkit"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", config_path)
    reconcile_agent_plugins(workspace, ["acme.toolkit"])
    return config_path


def _by_name(payload: dict) -> dict[str, dict]:
    return {item["name"]: item for item in payload["plugins"]}


def test_the_payload_is_editable_nowhere(instance: Path) -> None:
    """The panel must be able to say plainly that it cannot change anything."""
    payload = agent_plugins_payload()

    assert payload["editable"] is False
    assert payload["authority"] == "tools.agentPlugins"


def test_an_active_plugin_reports_its_components(instance: Path) -> None:
    plugin = _by_name(agent_plugins_payload())["acme.toolkit"]

    assert plugin["state"] == "active"
    assert plugin["display_name"] == "Deploy Toolkit"
    assert plugin["category"] == "Ops"
    assert plugin["description"] == "Deploy helpers."
    assert plugin["repository"] == "https://example.com/acme"
    assert plugin["skills"] == ["deploy-check"]
    assert plugin["mcp_servers"] == ["api"]


def test_an_unlisted_plugin_reads_as_inactive(instance: Path) -> None:
    plugin = _by_name(agent_plugins_payload())["corp.advisor"]

    assert plugin["state"] == "inactive"
    assert plugin["declared"] is False


def test_a_listed_plugin_that_fails_its_fingerprint_reads_as_modified(instance: Path) -> None:
    """Listed but inactive is a distinct, diagnosable state, not just 'off'."""
    workspace = Path(json.loads(instance.read_text())["agents"]["defaults"]["workspace"])
    (workspace / "plugins" / "acme.toolkit" / "tamper.txt").write_text("x", encoding="utf-8")

    plugin = _by_name(agent_plugins_payload())["acme.toolkit"]

    assert plugin["state"] == "modified"
    assert plugin["declared"] is True


def test_a_config_name_with_no_package_is_surfaced(instance: Path) -> None:
    payload = json.loads(instance.read_text(encoding="utf-8"))
    payload["tools"]["agentPlugins"] = ["acme.toolkit", "ghost"]
    instance.write_text(json.dumps(payload), encoding="utf-8")

    assert agent_plugins_payload()["unknown"] == ["ghost"]


def test_a_workspace_with_no_packages_returns_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config_path = state / "config.json"
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: state)
    config_path.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(tmp_path / "empty")}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", config_path)

    payload = agent_plugins_payload()

    assert payload["plugins"] == []
    assert payload["unknown"] == []


def test_the_payload_carries_no_filesystem_paths(instance: Path) -> None:
    """A package root is operator-side detail; the browser has no use for it."""
    rendered = json.dumps(agent_plugins_payload())

    assert "/plugins/" not in rendered
    assert "workspace" not in rendered
