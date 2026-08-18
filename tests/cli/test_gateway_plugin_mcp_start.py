"""A package whose only component is an MCP server still gets a host to run in.

The gateway decides whether to start the mcp-host at all. If that decision reads only
``tools.mcpServers``, a plugin-declared stdio server has nowhere to launch and fails silently.
See nanoinfraorg/nanoinfra#140.
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
from nanoinfra.cli.gateway_runtime import _stdio_mcp_server_names
from nanoinfra.config.schema import Config, MCPServerConfig


@pytest.fixture
def workspace_with_mcp_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _config(workspace: Path, **servers: MCPServerConfig) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    config.tools.mcp_servers = dict(servers)
    return config


def test_a_plugin_only_stdio_server_starts_the_host(workspace_with_mcp_plugin: Path) -> None:
    names = _stdio_mcp_server_names(_config(workspace_with_mcp_plugin))

    assert names == ["demo"]


def test_a_disabled_plugin_starts_no_host(workspace_with_mcp_plugin: Path) -> None:
    set_agent_plugin_enabled(workspace_with_mcp_plugin, "demo", False)

    assert _stdio_mcp_server_names(_config(workspace_with_mcp_plugin)) == []


def test_configured_and_plugin_servers_are_both_counted(workspace_with_mcp_plugin: Path) -> None:
    config = _config(
        workspace_with_mcp_plugin, own=MCPServerConfig(type="stdio", command="echo")
    )

    assert sorted(_stdio_mcp_server_names(config)) == ["demo", "own"]


def test_an_http_plugin_server_does_not_start_a_host(tmp_path: Path) -> None:
    """HTTP and SSE transports stay in the agent, so they need no host."""
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.tools.mcp_servers = {
        "remote": MCPServerConfig(type="streamableHttp", url="https://example.com/mcp")
    }

    assert _stdio_mcp_server_names(config) == []
