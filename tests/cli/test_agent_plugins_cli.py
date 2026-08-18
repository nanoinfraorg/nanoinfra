"""`nanoinfra agent-plugins` inspects Agent Plugins; it never activates one.

Activation is declared in tools.agentPlugins and reconciled by the executor (#141), so a CLI that
could enable a package would be a second authority contradicting the first. See
nanoinfraorg/nanoinfra#143.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nanoinfra.agent import plugins as agent_plugins
from nanoinfra.agent.plugins import (
    AGENT_PLUGIN_MCP_SCHEMA,
    AGENT_PLUGIN_SCHEMA,
    reconcile_agent_plugins,
)
from nanoinfra.cli.commands import app

runner = CliRunner()


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config file plus a workspace holding two installed packages."""
    state = tmp_path / "state"
    state.mkdir()
    config_path = state / "config.json"
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: state)

    workspace = tmp_path / "workspace"
    for name, with_mcp in (("acme.toolkit", True), ("corp.advisor", False)):
        root = workspace / "plugins" / name
        (root / "skills" / "thing").mkdir(parents=True)
        (root / "skills" / "thing" / "SKILL.md").write_text(
            "---\nname: thing\ndescription: A thing.\n---\n", encoding="utf-8"
        )
        (root / "plugin.json").write_text(
            json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": name}), encoding="utf-8"
        )
        if with_mcp:
            (root / "mcp.json").write_text(
                json.dumps(
                    {
                        "$schema": AGENT_PLUGIN_MCP_SCHEMA,
                        "mcpServers": {"api": {"type": "stdio", "command": "echo"}},
                    }
                ),
                encoding="utf-8",
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
    reconcile_agent_plugins(workspace, ["acme.toolkit"])
    return config_path


def test_list_shows_state_and_bundles(instance: Path) -> None:
    result = runner.invoke(app, ["agent-plugins", "list", "--config", str(instance)])

    assert result.exit_code == 0
    assert "acme.toolkit" in result.stdout
    assert "corp.advisor" in result.stdout
    assert "active" in result.stdout
    assert "inactive" in result.stdout


def test_list_reports_a_config_name_with_no_package(
    instance: Path, tmp_path: Path
) -> None:
    payload = json.loads(instance.read_text(encoding="utf-8"))
    payload["tools"]["agentPlugins"] = ["acme.toolkit", "ghost"]
    instance.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(app, ["agent-plugins", "list", "--config", str(instance)])

    assert result.exit_code == 0
    assert "ghost" in result.stdout


def test_list_on_an_empty_workspace_says_so(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config_path = state / "config.json"
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: state)
    config_path.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(tmp_path / "empty")}}}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["agent-plugins", "list", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "No Agent Plugins installed" in result.stdout


def test_show_reports_components(instance: Path) -> None:
    result = runner.invoke(
        app, ["agent-plugins", "show", "acme.toolkit", "--config", str(instance)]
    )

    assert result.exit_code == 0
    assert "acme.toolkit" in result.stdout
    assert "api" in result.stdout
    assert "active" in result.stdout


def test_show_explains_an_inactive_package(instance: Path) -> None:
    result = runner.invoke(
        app, ["agent-plugins", "show", "corp.advisor", "--config", str(instance)]
    )

    assert result.exit_code == 0
    assert "tools.agentPlugins" in result.stdout


def test_show_explains_a_modified_package(instance: Path) -> None:
    """Listed in config but failing its fingerprint is a distinct, diagnosable state."""
    payload = json.loads(instance.read_text(encoding="utf-8"))
    workspace = Path(payload["agents"]["defaults"]["workspace"])
    (workspace / "plugins" / "acme.toolkit" / "tamper.txt").write_text("x", encoding="utf-8")

    result = runner.invoke(
        app, ["agent-plugins", "show", "acme.toolkit", "--config", str(instance)]
    )

    assert result.exit_code == 0
    assert "modified" in result.stdout


def test_show_exits_nonzero_for_an_unknown_name(instance: Path) -> None:
    result = runner.invoke(app, ["agent-plugins", "show", "nope", "--config", str(instance)])

    assert result.exit_code == 1


def test_there_is_no_enable_or_disable_subcommand(instance: Path) -> None:
    """Config is the only authority, so the CLI must not offer a second one."""
    for verb in ("enable", "disable"):
        result = runner.invoke(
            app, ["agent-plugins", verb, "acme.toolkit", "--config", str(instance)]
        )
        assert result.exit_code != 0
