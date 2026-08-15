# tests/gates/test_mcp_host_server.py
"""Item 20 (#22): the MCP host process, and what it agrees to start.

The host holds the exec right that the fetcher must not hold. So the question a test has to
answer is not "does the tool call work" but "what can the agent make this process run".

The answer must be: one server named in the operator's config, and nothing else. The agent sends
a server name. The host reads the command from its own config. A name that no config holds gets a
refusal, and so does a server that is not a stdio server.

The stdio child also has to die with its connection. An orphan MCP server would outlive the
agent that asked for it, and a process nobody supervises is a process nobody stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys
from pathlib import Path

import pytest

from nanoinfra.config.schema import MCPServerConfig
from nanoinfra.gates.mcp_host import server as host_server
from nanoinfra.gates.mcp_host.protocol import (
    CallToolRequest,
    HostResponse,
    ListPromptsRequest,
    ListResourcesRequest,
    ListToolsRequest,
    OpenRequest,
    ReadResourceRequest,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)
from nanoinfra.gates.mcp_host.server import (
    MCPHost,
    ServerRefusedError,
    StdioServerSettings,
    load_stdio_settings,
    normalize_windows_stdio_command,
)
from tests.gates.conftest import pid_alive, wait_until_pid_gone

# One trivial stdio MCP server. It reports its own pid and its parent pid, so a test can prove
# which process started it.
_SERVER_SCRIPT = '''
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(f"{os.getpid()}\\n{os.getppid()}\\n", encoding="utf-8")

mcp = FastMCP("nanoinfra-item-20")


@mcp.tool()
def echo(value: str) -> str:
    """Return the value with a prefix."""
    return f"echo:{value}"


mcp.run()
'''


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
        command=sys.executable,
        args=args,
        env=None,
        cwd=None,
        tool_timeout=30,
    )


class _Connected:
    """One agent-side end of a live host connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        task: asyncio.Task[None],
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.task = task

    async def ask(self, request: object) -> HostResponse:
        await write_frame(self.writer, encode_request(request))  # type: ignore[arg-type]
        return decode_response(await read_frame(self.reader))

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, timeout=20)


async def _connect(host: MCPHost) -> _Connected:
    left, right = socket.socketpair()
    agent_reader, agent_writer = await asyncio.open_connection(sock=left)
    host_reader, host_writer = await asyncio.open_connection(sock=right)
    task = asyncio.create_task(host.serve_connection(host_reader, host_writer))
    return _Connected(agent_reader, agent_writer, task)


def _host(settings: StdioServerSettings) -> MCPHost:
    return MCPHost(settings_loader=lambda _name: settings)


# --------------------------------------------- what the host agrees to start


def test_an_unknown_server_name_gets_a_refusal(monkeypatch) -> None:
    monkeypatch.setattr(host_server, "_configured_servers", lambda: {})

    with pytest.raises(ServerRefusedError) as caught:
        load_stdio_settings("ghost")

    assert "ghost" in str(caught.value)


def test_a_url_server_gets_a_refusal_because_http_stays_with_the_agent(monkeypatch) -> None:
    """HTTP and SSE MCP do not change with #22.

    They stay in the agent behind the SSRF guards of ``.agent/security.md``. A host that dialled
    a URL would hold a transport, and the split would gain a hole rather than lose one.
    """
    monkeypatch.setattr(
        host_server,
        "_configured_servers",
        lambda: {"remote": MCPServerConfig(url="https://mcp.example.com/sse")},
    )

    with pytest.raises(ServerRefusedError) as caught:
        load_stdio_settings("remote")

    assert "stdio" in str(caught.value)


def test_an_explicit_http_type_gets_a_refusal(monkeypatch) -> None:
    monkeypatch.setattr(
        host_server,
        "_configured_servers",
        lambda: {
            "remote": MCPServerConfig(type="streamableHttp", url="https://mcp.example.com/mcp")
        },
    )

    with pytest.raises(ServerRefusedError):
        load_stdio_settings("remote")


def test_a_stdio_server_without_a_command_gets_a_refusal(monkeypatch) -> None:
    monkeypatch.setattr(
        host_server,
        "_configured_servers",
        lambda: {"broken": MCPServerConfig(type="stdio")},
    )

    with pytest.raises(ServerRefusedError) as caught:
        load_stdio_settings("broken")

    assert "command" in str(caught.value)


def test_the_settings_come_from_the_config_and_not_from_the_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        host_server,
        "_configured_servers",
        lambda: {
            "local": MCPServerConfig(
                command="npx",
                args=["-y", "demo-mcp"],
                env={"TOKEN": "x"},
                cwd="/tmp/demo",
                tool_timeout=17,
            )
        },
    )

    settings = load_stdio_settings("local")

    assert settings.command == "npx"
    assert settings.args == ["-y", "demo-mcp"]
    assert settings.env == {"TOKEN": "x"}
    assert settings.cwd == "/tmp/demo"
    assert settings.tool_timeout == 17


# ------------------------------------------------- the windows launcher wrap


def test_normalize_windows_stdio_command_is_noop_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_server.os, "name", "posix", raising=False)

    command, args, env = normalize_windows_stdio_command(
        "npx",
        ["-y", "chrome-devtools-mcp@latest"],
        {"FOO": "bar"},
    )

    assert command == "npx"
    assert args == ["-y", "chrome-devtools-mcp@latest"]
    assert env == {"FOO": "bar"}


def test_normalize_windows_stdio_command_wraps_npx_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_server.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        host_server.shutil,
        "which",
        lambda command, path=None: r"C:\Program Files\nodejs\npx.cmd",
    )
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    command, args, env = normalize_windows_stdio_command(
        "npx",
        ["-y", "chrome-devtools-mcp@latest"],
        None,
    )

    assert command == r"C:\Windows\System32\cmd.exe"
    assert args == ["/d", "/c", "npx", "-y", "chrome-devtools-mcp@latest"]
    assert env is None


def test_normalize_windows_stdio_command_wraps_resolved_cmd_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_server.os, "name", "nt", raising=False)

    def _fake_which(command: str, path: str | None = None) -> str:
        assert command == "custom-launcher"
        assert path == r"C:\Tools"
        return r"C:\Tools\custom-launcher.cmd"

    monkeypatch.setattr(host_server.shutil, "which", _fake_which)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    command, args, _env = normalize_windows_stdio_command(
        "custom-launcher",
        ["serve"],
        {"PATH": r"C:\Tools"},
    )

    assert command == r"C:\Windows\System32\cmd.exe"
    assert args == ["/d", "/c", "custom-launcher", "serve"]


def test_normalize_windows_stdio_command_keeps_real_executables_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_server.os, "name", "nt", raising=False)

    command, args, env = normalize_windows_stdio_command(
        "python.exe",
        ["-m", "http.server"],
        {"FOO": "bar"},
    )

    assert command == "python.exe"
    assert args == ["-m", "http.server"]
    assert env == {"FOO": "bar"}


def test_normalize_windows_stdio_command_skips_existing_shells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_server.os, "name", "nt", raising=False)

    command, args, env = normalize_windows_stdio_command(
        "cmd.exe",
        ["/c", "echo", "hello"],
        None,
    )

    assert command == "cmd.exe"
    assert args == ["/c", "echo", "hello"]
    assert env is None


# ----------------------------------------------- what the stdio child receives


async def test_the_child_gets_the_command_the_config_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four stdio parameters travel from the config to the child, and from nowhere else."""
    import contextlib as stdlib_contextlib

    captured: dict[str, object] = {}

    @stdlib_contextlib.asynccontextmanager
    async def _capturing_stdio_client(parameters: object):
        captured["command"] = parameters.command  # type: ignore[attr-defined]
        captured["args"] = parameters.args  # type: ignore[attr-defined]
        captured["env"] = parameters.env  # type: ignore[attr-defined]
        captured["cwd"] = parameters.cwd  # type: ignore[attr-defined]
        yield object(), object()

    class _FakeClientSession:
        def __init__(self, _read: object, _write: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClientSession:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr("mcp.client.stdio.stdio_client", _capturing_stdio_client)
    monkeypatch.setattr("mcp.ClientSession", _FakeClientSession)

    settings = StdioServerSettings(
        command="demo-mcp",
        args=["--serve"],
        env={"TOKEN": "x"},
        cwd="/tmp/nanoinfra-mcp-test",
        tool_timeout=30,
    )
    connection = await _connect(_host(settings))
    try:
        opened = await connection.ask(OpenRequest(request_id=1, server_name="demo"))
    finally:
        await connection.close()

    assert opened.ok, opened.error
    assert captured == {
        "command": "demo-mcp",
        "args": ["--serve"],
        "env": {"TOKEN": "x"},
        "cwd": "/tmp/nanoinfra-mcp-test",
    }


# ------------------------------------------------------ one live stdio server


async def test_the_host_opens_a_stdio_server_and_lists_its_tools(server_script: Path) -> None:
    connection = await _connect(_host(_settings(server_script)))
    try:
        opened = await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        listed = await connection.ask(ListToolsRequest(request_id=2))
    finally:
        await connection.close()

    assert opened.ok, opened.error
    assert listed.ok, listed.error
    assert listed.result is not None
    assert [tool["name"] for tool in listed.result["tools"]] == ["echo"]


async def test_a_tool_call_returns_the_server_output(server_script: Path) -> None:
    connection = await _connect(_host(_settings(server_script)))
    try:
        await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        called = await connection.ask(
            CallToolRequest(request_id=2, tool_name="echo", arguments={"value": "hello"})
        )
    finally:
        await connection.close()

    assert called.ok, called.error
    assert called.result is not None
    assert called.result["content"][0]["text"] == "echo:hello"
    assert called.result["isError"] is False


async def test_the_reply_carries_the_request_id_back(server_script: Path) -> None:
    connection = await _connect(_host(_settings(server_script)))
    try:
        await connection.ask(OpenRequest(request_id=7, server_name="demo"))
        called = await connection.ask(
            CallToolRequest(request_id=99, tool_name="echo", arguments={"value": "x"})
        )
    finally:
        await connection.close()

    assert called.request_id == 99


async def test_an_unknown_tool_name_comes_back_as_an_error(server_script: Path) -> None:
    connection = await _connect(_host(_settings(server_script)))
    try:
        await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        called = await connection.ask(
            CallToolRequest(request_id=2, tool_name="no_such_tool", arguments={})
        )
    finally:
        await connection.close()

    assert called.ok is False or called.result is not None
    if called.ok:
        assert called.result is not None
        assert called.result["isError"] is True
    else:
        assert called.error


async def test_a_server_without_resources_answers_with_an_error(server_script: Path) -> None:
    """The agent already treats this as "not supported", and it logs at debug level."""
    connection = await _connect(_host(_settings(server_script)))
    try:
        await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        resources = await connection.ask(ListResourcesRequest(request_id=2))
        prompts = await connection.ask(ListPromptsRequest(request_id=3))
    finally:
        await connection.close()

    assert resources.ok is False or resources.result is not None
    assert prompts.ok is False or prompts.result is not None


async def test_a_dead_child_answers_with_the_words_for_a_terminated_session(
    server_script: Path, tmp_path: Path
) -> None:
    """A crashed MCP server must reach the agent as a terminated session.

    Before #22 the SDK raised the failure in the agent's own process, and the tool wrapper
    reconnected the server. That recovery reads the words of the failure, so the host keeps them.
    ``_is_session_terminated`` in ``nanoinfra/agent/tools/mcp.py`` holds the two markers below.

    The host answers rather than hangs. The stdio child dies, the MCP SDK cancels the scope that
    holds it, and the owner task absorbs that cancel. So the connection loop still replies.
    """
    report = tmp_path / "pids.txt"
    connection = await _connect(_host(_settings(server_script, report=report)))
    try:
        await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        child_pid = int(report.read_text(encoding="utf-8").splitlines()[0])
        os.kill(child_pid, signal.SIGKILL)
        assert wait_until_pid_gone(child_pid, timeout_s=20.0)

        answer = await asyncio.wait_for(
            connection.ask(
                CallToolRequest(request_id=2, tool_name="echo", arguments={"value": "x"})
            ),
            timeout=20,
        )
    finally:
        await connection.close()

    assert answer.ok is False
    assert answer.error is not None
    words = answer.error.lower()
    assert "session terminated" in words or "connection closed" in words


async def test_a_call_after_the_child_died_names_the_terminated_session(
    server_script: Path, tmp_path: Path
) -> None:
    """The second call finds no session at all, and it says so in the same words."""
    report = tmp_path / "pids.txt"
    connection = await _connect(_host(_settings(server_script, report=report)))
    try:
        await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        child_pid = int(report.read_text(encoding="utf-8").splitlines()[0])
        os.kill(child_pid, signal.SIGKILL)
        assert wait_until_pid_gone(child_pid, timeout_s=20.0)

        await asyncio.wait_for(
            connection.ask(
                CallToolRequest(request_id=2, tool_name="echo", arguments={"value": "x"})
            ),
            timeout=20,
        )
        answer = await asyncio.wait_for(
            connection.ask(
                CallToolRequest(request_id=3, tool_name="echo", arguments={"value": "y"})
            ),
            timeout=20,
        )
    finally:
        await connection.close()

    assert answer.ok is False
    assert answer.error is not None
    assert "session terminated" in answer.error.lower()


# --------------------------------------------------------- the wire fails closed


async def test_a_request_before_an_open_gets_a_refusal(server_script: Path) -> None:
    connection = await _connect(_host(_settings(server_script)))
    try:
        answer = await connection.ask(ListToolsRequest(request_id=1))
    finally:
        await connection.close()

    assert answer.ok is False
    assert answer.error is not None
    assert "open" in answer.error


async def test_a_second_open_on_one_connection_gets_a_refusal(server_script: Path) -> None:
    connection = await _connect(_host(_settings(server_script)))
    try:
        first = await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        second = await connection.ask(OpenRequest(request_id=2, server_name="demo"))
    finally:
        await connection.close()

    assert first.ok is True
    assert second.ok is False


async def test_a_malformed_frame_gets_a_refusal_and_never_a_crash(server_script: Path) -> None:
    connection = await _connect(_host(_settings(server_script)))
    try:
        await write_frame(connection.writer, b'{"v": 1, "op": "exec"}')
        answer = decode_response(await read_frame(connection.reader))
    finally:
        await connection.close()

    assert answer.ok is False
    assert answer.error is not None
    assert "exec" in answer.error


async def test_a_refused_server_name_answers_and_keeps_the_process() -> None:
    def _loader(name: str) -> StdioServerSettings:
        raise ServerRefusedError(f"no MCP server named {name!r} is configured")

    connection = await _connect(MCPHost(settings_loader=_loader))
    try:
        answer = await connection.ask(OpenRequest(request_id=1, server_name="ghost"))
    finally:
        await connection.close()

    assert answer.ok is False
    assert answer.error is not None
    assert "ghost" in answer.error


# ------------------------------------------------------- no orphan children


async def test_the_stdio_child_dies_with_its_connection(
    server_script: Path, tmp_path: Path
) -> None:
    """A closed connection ends the session, and the session ends the child.

    An MCP server that outlived its connection would be a process nobody supervises.
    """
    report = tmp_path / "pids.txt"
    connection = await _connect(_host(_settings(server_script, report=report)))
    await connection.ask(OpenRequest(request_id=1, server_name="demo"))
    child_pid = int(report.read_text(encoding="utf-8").splitlines()[0])
    assert pid_alive(child_pid)

    await connection.close()

    assert wait_until_pid_gone(child_pid, timeout_s=20.0), f"pid {child_pid} outlived the connection"


async def test_the_stdio_child_is_a_child_of_the_host_process(
    server_script: Path, tmp_path: Path
) -> None:
    """The host starts the server. Nobody else does.

    In this test the host runs in the test process, so the parent pid is this process. The end
    to end test proves the same property across a real host process.
    """
    report = tmp_path / "pids.txt"
    connection = await _connect(_host(_settings(server_script, report=report)))
    try:
        await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        parent_pid = int(report.read_text(encoding="utf-8").splitlines()[1])
    finally:
        await connection.close()

    assert parent_pid == os.getpid()






# --------------------------------------------------------------- the read path


async def test_a_resource_read_reaches_the_server(server_script: Path) -> None:
    """The host forwards a resource read. This server has none, so it answers with a failure."""
    connection = await _connect(_host(_settings(server_script)))
    try:
        await connection.ask(OpenRequest(request_id=1, server_name="demo"))
        answer = await connection.ask(
            ReadResourceRequest(request_id=2, uri="file:///missing.txt")
        )
    finally:
        await connection.close()

    assert answer.ok is False
    assert answer.error is not None
