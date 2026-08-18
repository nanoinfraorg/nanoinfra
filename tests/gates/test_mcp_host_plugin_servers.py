"""A plugin-declared stdio MCP server launches through the mcp-host role, not the agent.

The mcp-host process resolves its own server list, so merging plugin servers there is what makes
them inherit the host's uid and its Landlock rules. Doing it only on the agent side would leave the
process where the agent can reach it. See nanoinfraorg/nanoinfra#140.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent import plugins as agent_plugins
from nanoinfra.agent.plugins import (
    AGENT_PLUGIN_MCP_SCHEMA,
    AGENT_PLUGIN_SCHEMA,
    set_agent_plugin_enabled,
)
from nanoinfra.config.schema import Config, MCPServerConfig
from nanoinfra.gates.mcp_host.server import ServerRefusedError, load_stdio_settings


@pytest.fixture
def plugin_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated workspace holding one enabled plugin that declares an MCP server."""
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: config_path.parent)

    workspace = tmp_path / "workspace"
    root = workspace / "plugins" / "demo"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "demo"}), encoding="utf-8"
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
    set_agent_plugin_enabled(workspace, "demo", True)
    return workspace


def _patch_host_config(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, servers: dict[str, MCPServerConfig]
) -> None:
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    config.tools.mcp_servers = servers
    monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *a, **k: config)
    monkeypatch.setattr(
        "nanoinfra.config.loader.resolve_config_env_vars", lambda value: value
    )


def test_the_host_resolves_a_plugin_declared_server(
    plugin_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_host_config(monkeypatch, plugin_workspace, {})

    settings = load_stdio_settings("demo")

    assert settings.command == "echo"
    assert settings.env is not None
    assert settings.env["PLUGIN_ROOT"] == str((plugin_workspace / "plugins" / "demo").resolve())


def test_a_disabled_plugin_server_is_refused_by_the_host(
    plugin_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_agent_plugin_enabled(plugin_workspace, "demo", False)
    _patch_host_config(monkeypatch, plugin_workspace, {})

    with pytest.raises(ServerRefusedError, match="no MCP server named"):
        load_stdio_settings("demo")


def test_a_tampered_plugin_server_is_refused_by_the_host(
    plugin_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host must honour the activation fingerprint, not just the manifest."""
    (plugin_workspace / "plugins" / "demo" / "tamper.txt").write_text("x", encoding="utf-8")
    _patch_host_config(monkeypatch, plugin_workspace, {})

    with pytest.raises(ServerRefusedError, match="no MCP server named"):
        load_stdio_settings("demo")


def test_a_configured_server_wins_a_name_collision_on_the_host(
    plugin_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = {"demo": MCPServerConfig(type="stdio", command="operator-owned")}
    _patch_host_config(monkeypatch, plugin_workspace, configured)

    assert load_stdio_settings("demo").command == "operator-owned"


def test_an_unknown_name_is_still_refused(
    plugin_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_host_config(monkeypatch, plugin_workspace, {})

    with pytest.raises(ServerRefusedError, match="no MCP server named"):
        load_stdio_settings("nope")
