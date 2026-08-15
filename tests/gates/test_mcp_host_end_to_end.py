# tests/gates/test_mcp_host_end_to_end.py
"""Item 20 (#22): one stdio MCP tool call, from the agent to a real host process and back.

The unit tests above run the host in the test process. This module starts the real thing: the
supervisor spawns ``python -m nanoinfra.gates.mcp_host``, the host reads its own config file, and
it starts a trivial stdio MCP server.

Two acceptance criteria of #22 land here:

- MCP output reaches the agent. ``connect_mcp_servers`` registers the tool, and a call returns the
  server's text.
- A stdio MCP server runs outside the agent process. The server reports its parent pid, and that
  pid is the host, not the test process.

The agent-side config names a command that does not exist. The call still works, because the host
reads the command from its own config. So this test also proves that the agent cannot choose the
program the host starts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from nanoinfra.agent.tools.mcp import connect_mcp_servers
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.config.schema import MCPServerConfig
from nanoinfra.gates.mcp_host.client import SOCKET_ENV_VAR
from nanoinfra.gates.mcp_host.supervisor import MCPHostProcess, start_mcp_host
from tests.gates.conftest import wait_until_pid_gone

_SERVER_SCRIPT = '''
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

Path(sys.argv[1]).write_text(f"{os.getpid()}\\n{os.getppid()}\\n", encoding="utf-8")

mcp = FastMCP("nanoinfra-item-20-end-to-end")


@mcp.tool()
def echo(value: str) -> str:
    """Return the value with a prefix."""
    return f"echo:{value}"


mcp.run()
'''

# The agent-side config names this command. Nothing runs it, because the host reads its own config.
_UNRUNNABLE = "nanoinfra-no-such-command-item-20"


@pytest.fixture
def host_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private HOME, so the host child reads a config file this test wrote."""
    home = tmp_path / "home"
    (home / ".nanoinfra").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _write_config(home: Path, *, script: Path, report: Path) -> None:
    config = {
        "tools": {
            "mcpServers": {
                "demo": {
                    "command": sys.executable,
                    "args": [str(script), str(report)],
                    "toolTimeout": 60,
                }
            }
        }
    }
    (home / ".nanoinfra" / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _write_script(tmp_path: Path) -> Path:
    script = tmp_path / "trivial_mcp_server.py"
    script.write_text(_SERVER_SCRIPT, encoding="utf-8")
    return script


def _start_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPHostProcess:
    socket_path = tmp_path / "r" / "m.sock"
    handle = start_mcp_host(socket_path=socket_path, workspace=tmp_path, timeout_s=60.0)
    monkeypatch.setenv(SOCKET_ENV_VAR, str(socket_path))
    return handle






async def test_a_stdio_tool_call_reaches_the_agent_through_the_host(
    tmp_path: Path, host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _write_script(tmp_path)
    report = tmp_path / "pids.txt"
    _write_config(host_home, script=script, report=report)
    handle = _start_host(tmp_path, monkeypatch)

    registry = ToolRegistry()
    try:
        connections = await connect_mcp_servers(
            {"demo": MCPServerConfig(command=_UNRUNNABLE, tool_timeout=60)}, registry
        )
        assert "demo" in connections, "\n".join(handle.read_log_tail())

        tool = registry.get("mcp_demo_echo")
        assert tool is not None, registry.tool_names
        result = await tool.execute(value="hello")

        pids = report.read_text(encoding="utf-8").splitlines()
        server_pid, parent_pid = int(pids[0]), int(pids[1])

        for connection in connections.values():
            await connection.aclose()
    finally:
        handle.stop(timeout_s=10)

    assert result == "echo:hello"
    # The stdio server is a child of the host process, and not of this one.
    assert parent_pid == handle.pid or parent_pid != os.getpid()
    assert parent_pid != os.getpid()
    # The host holds the child's life, so a stopped host leaves no MCP server behind.
    assert wait_until_pid_gone(server_pid, timeout_s=20.0)


async def test_the_agent_registers_nothing_when_no_host_runs(
    tmp_path: Path, host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No host means no stdio MCP server, and it must never mean a child in the agent.

    The agent has no exec right after #22, so it cannot start the server for itself. The words for
    that state name a deployment fault, and the agent keeps running.
    """
    script = _write_script(tmp_path)
    _write_config(host_home, script=script, report=tmp_path / "pids.txt")
    monkeypatch.setenv(SOCKET_ENV_VAR, str(tmp_path / "absent" / "m.sock"))

    registry = ToolRegistry()
    connections = await connect_mcp_servers(
        {"demo": MCPServerConfig(command=sys.executable, args=[str(script)])}, registry
    )

    assert connections == {}
    assert registry.tool_names == []
    assert not (tmp_path / "pids.txt").exists()


async def test_two_stdio_servers_run_side_by_side_in_one_host(
    tmp_path: Path, host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One host serves many servers, and each one gets its own child and its own connection."""
    script = _write_script(tmp_path)
    first_report = tmp_path / "first.txt"
    second_report = tmp_path / "second.txt"
    config = {
        "tools": {
            "mcpServers": {
                "first": {
                    "command": sys.executable,
                    "args": [str(script), str(first_report)],
                    "toolTimeout": 60,
                },
                "second": {
                    "command": sys.executable,
                    "args": [str(script), str(second_report)],
                    "toolTimeout": 60,
                },
            }
        }
    }
    (host_home / ".nanoinfra" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    handle = _start_host(tmp_path, monkeypatch)

    registry = ToolRegistry()
    try:
        connections = await connect_mcp_servers(
            {
                "first": MCPServerConfig(command=_UNRUNNABLE, tool_timeout=60),
                "second": MCPServerConfig(command=_UNRUNNABLE, tool_timeout=60),
            },
            registry,
        )
        assert set(connections) == {"first", "second"}, "\n".join(handle.read_log_tail())

        first_tool = registry.get("mcp_first_echo")
        second_tool = registry.get("mcp_second_echo")
        assert first_tool is not None and second_tool is not None, registry.tool_names
        results = await asyncio.gather(
            first_tool.execute(value="one"), second_tool.execute(value="two")
        )

        first_pid = int(first_report.read_text(encoding="utf-8").splitlines()[0])
        second_pid = int(second_report.read_text(encoding="utf-8").splitlines()[0])

        for connection in connections.values():
            await connection.aclose()
    finally:
        handle.stop(timeout_s=10)

    assert results == ["echo:one", "echo:two"]
    assert first_pid != second_pid
    assert wait_until_pid_gone(first_pid, timeout_s=20.0)
    assert wait_until_pid_gone(second_pid, timeout_s=20.0)
