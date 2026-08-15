# tests/gates/test_mcp_host_client.py
"""Item 20 (#22): the agent side of the MCP host wire.

This module runs in the agent, so it holds no exec right and no MCP server. It writes one frame
and reads one frame. The tests below cover the three jobs it has:

- It hands the agent real MCP SDK objects. The tool wrappers in ``nanoinfra/agent/tools/mcp.py``
  read ``result.content`` and ``result.isError``, and they must not learn that a socket sits in
  the middle.
- It keeps the words of a failure apart. "The host is not running" is a deployment fault. "The
  server said no" is a result. An operator who reads one as the other looks in the wrong place.
- It matches each reply to its own request. A tool call can time out on this side while the host
  still works, and a late reply must never read as the answer to the next call.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from nanoinfra.gates.mcp_host.client import (
    DEFAULT_SOCKET_NAME,
    SOCKET_ENV_VAR,
    MCPHostError,
    MCPHostUnavailableError,
    default_socket_path,
    open_stdio_session,
)
from nanoinfra.gates.mcp_host.protocol import (
    CallToolRequest,
    HostRequest,
    HostResponse,
    OpenRequest,
    ProtocolError,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)
from nanoinfra.gates.mcp_host.server import MCPHost, StdioServerSettings

_SERVER_SCRIPT = '''
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(f"{os.getpid()}\\n{os.getppid()}\\n", encoding="utf-8")

mcp = FastMCP("nanoinfra-item-20-client")


@mcp.tool()
def echo(value: str) -> str:
    """Return the value with a prefix."""
    return f"echo:{value}"


mcp.run()
'''

_Rule = Callable[[HostRequest, asyncio.StreamWriter], Awaitable[None]]


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    script = tmp_path / "trivial_mcp_server.py"
    script.write_text(_SERVER_SCRIPT, encoding="utf-8")
    return script


def _settings(script: Path, *, report: Path | None = None) -> StdioServerSettings:
    args = [str(script)]
    if report is not None:
        args.append(str(report))
    return StdioServerSettings(
        command=sys.executable, args=args, env=None, cwd=None, tool_timeout=30
    )


async def _real_host(socket_path: Path, settings: StdioServerSettings) -> asyncio.Server:
    host = MCPHost(settings_loader=lambda _name: settings)
    return await asyncio.start_unix_server(host.serve_connection, path=str(socket_path))


async def _scripted_host(socket_path: Path, rule: _Rule) -> asyncio.Server:
    """A host that answers by rule. The rule owns when and what it writes."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                payload = await read_frame(reader)
            except ProtocolError:
                return
            await rule(decode_request(payload), writer)

    return await asyncio.start_unix_server(handle, path=str(socket_path))


def _ok(request_id: int, result: dict[str, object] | None = None) -> HostResponse:
    return HostResponse(
        request_id=request_id, ok=True, result=result or {}, error=None, error_data=None
    )


async def _stop(server: asyncio.Server) -> None:
    server.close()
    with contextlib.suppress(Exception):
        await server.wait_closed()


# ------------------------------------------------------- real MCP objects


async def test_the_session_returns_real_mcp_tool_definitions(
    server_script: Path, tmp_path: Path
) -> None:
    socket_path = tmp_path / "host.sock"
    server = await _real_host(socket_path, _settings(server_script))
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            listed = await session.list_tools()
    finally:
        await _stop(server)

    assert [tool.name for tool in listed.tools] == ["echo"]
    assert listed.tools[0].inputSchema["type"] == "object"


async def test_a_tool_call_returns_a_real_call_tool_result(
    server_script: Path, tmp_path: Path
) -> None:
    from mcp import types

    socket_path = tmp_path / "host.sock"
    server = await _real_host(socket_path, _settings(server_script))
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            result = await session.call_tool("echo", arguments={"value": "hi"})
    finally:
        await _stop(server)

    assert isinstance(result.content[0], types.TextContent)
    assert result.content[0].text == "echo:hi"
    assert result.isError is False


async def test_the_child_dies_when_the_agent_leaves_the_session(
    server_script: Path, tmp_path: Path, pid_alive, wait_until_pid_gone) -> None:
    socket_path = tmp_path / "host.sock"
    report = tmp_path / "pids.txt"
    server = await _real_host(socket_path, _settings(server_script, report=report))
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            await session.list_tools()
            child_pid = int(report.read_text(encoding="utf-8").splitlines()[0])
            assert pid_alive(child_pid)
    finally:
        await _stop(server)

    assert wait_until_pid_gone(child_pid, timeout_s=20.0)


# ------------------------------------------------------ words for a failure


async def test_a_missing_socket_reads_as_a_deployment_fault(tmp_path: Path) -> None:
    with pytest.raises(MCPHostUnavailableError) as caught:
        async with open_stdio_session("demo", socket_path=tmp_path / "absent.sock"):
            pass

    assert "absent.sock" in str(caught.value)


async def test_a_refused_server_reads_as_a_refusal_and_not_a_deployment_fault(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "host.sock"

    async def rule(request: HostRequest, writer: asyncio.StreamWriter) -> None:
        await write_frame(
            writer,
            encode_response(
                HostResponse(
                    request_id=request.request_id,
                    ok=False,
                    result=None,
                    error="no MCP server named 'ghost' is configured on the host",
                    error_data=None,
                )
            ),
        )

    server = await _scripted_host(socket_path, rule)
    try:
        with pytest.raises(MCPHostError) as caught:
            async with open_stdio_session("ghost", socket_path=socket_path):
                pass
    finally:
        await _stop(server)

    assert "ghost" in str(caught.value)
    assert not isinstance(caught.value, MCPHostUnavailableError)


async def test_an_mcp_error_from_the_server_arrives_as_an_mcp_error(tmp_path: Path) -> None:
    """The prompt wrapper catches ``McpError`` by type, and it reads the code and the message."""
    from mcp.shared.exceptions import McpError

    socket_path = tmp_path / "host.sock"

    async def rule(request: HostRequest, writer: asyncio.StreamWriter) -> None:
        if isinstance(request, OpenRequest):
            await write_frame(writer, encode_response(_ok(request.request_id)))
            return
        await write_frame(
            writer,
            encode_response(
                HostResponse(
                    request_id=request.request_id,
                    ok=False,
                    result=None,
                    error="invalid argument",
                    error_data={"code": 42, "message": "invalid argument"},
                )
            ),
        )

    server = await _scripted_host(socket_path, rule)
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            with pytest.raises(McpError) as caught:
                await session.call_tool("echo", arguments={})
    finally:
        await _stop(server)

    assert caught.value.error.code == 42
    assert caught.value.error.message == "invalid argument"


async def test_a_host_that_hangs_up_mid_call_reads_as_unavailable(tmp_path: Path) -> None:
    socket_path = tmp_path / "host.sock"

    async def rule(request: HostRequest, writer: asyncio.StreamWriter) -> None:
        if isinstance(request, OpenRequest):
            await write_frame(writer, encode_response(_ok(request.request_id)))
            return
        writer.close()

    server = await _scripted_host(socket_path, rule)
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            with pytest.raises(MCPHostUnavailableError):
                await session.call_tool("echo", arguments={})
    finally:
        await _stop(server)


# ------------------------------------------- the agent still reconnects a dead server


async def test_a_dead_child_raises_what_the_agent_reads_as_a_terminated_session(
    server_script: Path, tmp_path: Path, wait_until_pid_gone) -> None:
    """The reconnect path predates #22, and it reads the words of the failure.

    ``_is_session_terminated`` decides whether ``MCPToolWrapper`` reconnects a server. A crashed
    stdio server must still satisfy it after the split, or a crash would end that server for the
    life of the process.
    """
    import signal

    from nanoinfra.agent.tools.mcp import _is_session_terminated

    socket_path = tmp_path / "host.sock"
    report = tmp_path / "pids.txt"
    server = await _real_host(socket_path, _settings(server_script, report=report))
    raised: list[BaseException] = []
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            await session.list_tools()
            child_pid = int(report.read_text(encoding="utf-8").splitlines()[0])
            os.kill(child_pid, signal.SIGKILL)
            assert wait_until_pid_gone(child_pid, timeout_s=20.0)

            for _attempt in range(2):
                try:
                    await asyncio.wait_for(
                        session.call_tool("echo", arguments={"value": "x"}), timeout=20
                    )
                except Exception as exc:  # noqa: BLE001 -- the test reads what the agent reads
                    raised.append(exc)
    finally:
        await _stop(server)

    assert len(raised) == 2
    assert all(_is_session_terminated(exc) for exc in raised), [str(exc) for exc in raised]


async def test_a_host_that_restarts_reads_as_a_terminated_session(tmp_path: Path) -> None:
    """A host that goes away mid-call must not end MCP for the life of the agent.

    The words name a closed connection, and the wrappers reconnect on those words. The reconnect
    then opens a fresh connection, which the next host start accepts.
    """
    from nanoinfra.agent.tools.mcp import _is_session_terminated

    socket_path = tmp_path / "host.sock"

    async def rule(request: HostRequest, writer: asyncio.StreamWriter) -> None:
        if isinstance(request, OpenRequest):
            await write_frame(writer, encode_response(_ok(request.request_id)))
            return
        writer.close()

    server = await _scripted_host(socket_path, rule)
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            with pytest.raises(MCPHostUnavailableError) as caught:
                await session.call_tool("echo", arguments={})
    finally:
        await _stop(server)

    assert _is_session_terminated(caught.value), str(caught.value)


# --------------------------------------------------------- one reply per request


async def test_a_late_reply_never_answers_the_next_call(tmp_path: Path) -> None:
    """The agent's own timeout abandons a call. The host may still answer it later."""
    socket_path = tmp_path / "host.sock"
    held: list[int] = []

    async def rule(request: HostRequest, writer: asyncio.StreamWriter) -> None:
        if isinstance(request, OpenRequest):
            await write_frame(writer, encode_response(_ok(request.request_id)))
            return
        assert isinstance(request, CallToolRequest)
        if request.arguments.get("value") == "slow":
            held.append(request.request_id)
            return
        await write_frame(
            writer,
            encode_response(
                _ok(
                    request.request_id,
                    {"content": [{"type": "text", "text": "fast"}], "isError": False},
                )
            ),
        )
        for late in held:
            await write_frame(
                writer,
                encode_response(
                    _ok(late, {"content": [{"type": "text", "text": "slow"}], "isError": False})
                ),
            )
        held.clear()

    server = await _scripted_host(socket_path, rule)
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    session.call_tool("echo", arguments={"value": "slow"}), timeout=0.2
                )
            result = await asyncio.wait_for(
                session.call_tool("echo", arguments={"value": "fast"}), timeout=10
            )
            await asyncio.sleep(0.1)
    finally:
        await _stop(server)

    assert result.content[0].text == "fast"  # type: ignore[union-attr]


async def test_two_calls_in_flight_each_get_their_own_reply(tmp_path: Path) -> None:
    socket_path = tmp_path / "host.sock"
    pending: list[CallToolRequest] = []

    async def rule(request: HostRequest, writer: asyncio.StreamWriter) -> None:
        if isinstance(request, OpenRequest):
            await write_frame(writer, encode_response(_ok(request.request_id)))
            return
        assert isinstance(request, CallToolRequest)
        pending.append(request)
        if len(pending) < 2:
            return
        # Reply in reverse order, so only the id can match a reply to its call.
        for held in reversed(pending):
            await write_frame(
                writer,
                encode_response(
                    _ok(
                        held.request_id,
                        {
                            "content": [
                                {"type": "text", "text": str(held.arguments.get("value"))}
                            ],
                            "isError": False,
                        },
                    )
                ),
            )
        pending.clear()

    server = await _scripted_host(socket_path, rule)
    try:
        async with open_stdio_session("demo", socket_path=socket_path) as session:
            first = asyncio.create_task(session.call_tool("echo", arguments={"value": "one"}))
            second = asyncio.create_task(session.call_tool("echo", arguments={"value": "two"}))
            results = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    finally:
        await _stop(server)

    assert [result.content[0].text for result in results] == ["one", "two"]  # type: ignore[union-attr]


# ------------------------------------------------------------- the socket path


def test_the_socket_path_comes_from_the_environment_when_it_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SOCKET_ENV_VAR, "  /run/nanoinfra-mcp/host.sock  ")

    assert default_socket_path() == Path("/run/nanoinfra-mcp/host.sock")


def test_the_default_socket_path_sits_in_the_run_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(SOCKET_ENV_VAR, raising=False)
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", tmp_path / "config.json")

    path = default_socket_path()

    assert path.name == DEFAULT_SOCKET_NAME
    assert path.parent.name == "run"




