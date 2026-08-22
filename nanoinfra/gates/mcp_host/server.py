"""The MCP host: the one process that starts a stdio MCP server -- nanoinfraorg/nanoinfra#22.

A stdio MCP server is a subprocess. #19 states that the fetcher cannot exec, and a test walks the
fetcher's syntax tree to hold that. #22 resolves the contradiction by moving the exec right here,
where confinement can address it on its own terms.

What this process holds:

- The exec right for stdio MCP servers, and nothing more than that.
- One child per open connection, and the child dies with the connection. A host that somebody
  kills also leaves no child, because each child asks the kernel for a parent death signal (#50).

What this process does not hold, and ``tests/gates/test_mcp_host_isolation.py`` asserts each one:

- No credential store. A compromise here yields no host credential.
- No HTTP transport. ``load_stdio_settings`` refuses every server that is not stdio, so HTTP and
  SSE MCP stay in the agent behind the SSRF guards of ``.agent/security.md``.
- No command from the agent. The request carries a server name. This process reads the command,
  the arguments, the environment, and the working directory from its own config.

The config read happens on every open. An operator changes a command in the WebUI, and a
long-lived host must not start the command it read hours ago.

One connection holds one session. That binding is the lifecycle: the agent closes the connection,
and the child goes with it. A session handle in a table would outlive a dead agent, and an orphan
MCP server is a process nobody supervises.

The progress notification filter and the Windows launcher wrap are copies of the two helpers in
``nanoinfra/agent/tools/mcp.py``. The agent keeps its copies for HTTP and SSE streams, and this
process needs its own for the stdio stream. An import across the split would put the agent's tool
layer inside this process, and the copies are 60 lines that both sides can read on their own.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import signal
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from nanoinfra.gates.mcp_host.protocol import (
    MAX_FRAME_BYTES,
    CallToolRequest,
    GetPromptRequest,
    HostRequest,
    HostResponse,
    ListPromptsRequest,
    ListResourcesRequest,
    ListToolsRequest,
    OpenRequest,
    ProtocolError,
    ReadResourceRequest,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)
from nanoinfra.gates.socket_group import (
    MCP_HOST_SOCKET_GROUP_ENV,
    apply_socket_group,
)

if TYPE_CHECKING:
    from nanoinfra.config.schema import MCPServerConfig

# The socket's own mode is not honoured on every platform, so the directory carries the control.
# 0o700 keeps another local account out of the host's door.
_SOCKET_DIR_MODE = 0o700

# The agent owns the timeout a user sees, and it cancels its own wait. This grace keeps a hung
# server from pinning a host task for ever after the agent gave up.
HOST_TIMEOUT_GRACE_S = 5.0

# How long a close waits for the session owner before it cancels the task. A server that ignores
# a shutdown must not hold the host, and the process group kill still ends the child.
_CLOSE_TIMEOUT_S = 10.0

# How long a stdio child may take to end after TERM, before the host sends KILL. The wait blocks
# the event loop, because it must survive a cancellation, so it stays short. A server may flush on
# TERM, and a server that ignores it must not outlive its connection.
_CHILD_END_GRACE_S = 1.0

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# The failures that mean the stdio child is gone. The list matches ``_TRANSIENT_EXC_NAMES`` in
# ``nanoinfra/agent/tools/mcp.py``, because the agent recognised these type names in one process
# and it cannot read a type name across a socket.
_DEAD_SESSION_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionAbortedError",
))

# A malformed frame carries no id this side can trust, so a refusal answers on id 0.
_NO_REQUEST_ID = 0


class ServerRefusedError(Exception):
    """The host will not start this server. The agent must not guess past it."""


@dataclass(frozen=True, slots=True)
class StdioServerSettings:
    """Everything the host needs to start one stdio MCP server.

    The host takes a flat set of values rather than the config object. So a test builds one in a
    line, and the wire never carries any of these fields.
    """

    command: str
    args: list[str]
    env: dict[str, str] | None
    cwd: str | None
    tool_timeout: int


SettingsLoader = Callable[[str], StdioServerSettings]


def _configured_servers() -> dict[str, MCPServerConfig]:
    """Read the MCP servers from the host's own config, plus enabled Agent Plugins.

    The plugin merge happens *here* and not only on the agent side, because this is the process
    that starts the program. A plugin server resolved anywhere else would run wherever that caller
    lives; resolved here it inherits this account and this process's Landlock rules like every
    other stdio server (nanoinfraorg/nanoinfra#140).

    Activation is re-read on every resolution rather than cached, so a package that changed since
    it was enabled stops resolving without the host being restarted.

    The imports stay local. The config module pulls in a large part of the package, and this
    process must not pay for that at import time.
    """
    from nanoinfra.agent.plugins import merged_mcp_servers
    from nanoinfra.config.loader import load_config, resolve_config_env_vars

    return merged_mcp_servers(resolve_config_env_vars(load_config()))


def load_stdio_settings(server_name: str) -> StdioServerSettings:
    """Resolve one configured stdio MCP server, or refuse.

    Three refusals, and each one closes a hole:

    - A name no config holds. The agent must not name a program, so an unknown name is a refusal
      rather than a default.
    - A server that is not stdio. HTTP and SSE MCP stay in the agent behind the SSRF guards, and
      a host that dialled a URL would hold a transport.
    - A stdio server with no command. A guess here would start something the operator never
      wrote.
    """
    servers = _configured_servers()
    config = servers.get(server_name)
    if config is None:
        raise ServerRefusedError(
            f"no MCP server named {server_name!r} is configured on the host"
        )

    transport = config.type or ("stdio" if config.command else "")
    if transport != "stdio":
        raise ServerRefusedError(
            f"MCP server {server_name!r} is not a stdio server. The host starts stdio servers "
            "only. HTTP and SSE transports stay in the agent behind the SSRF guards."
        )
    if not config.command:
        raise ServerRefusedError(f"MCP server {server_name!r} names no command")

    command, args, env = normalize_windows_stdio_command(
        config.command, config.args, config.env or None
    )
    return StdioServerSettings(
        command=command,
        args=args,
        env=env,
        cwd=config.cwd or None,
        tool_timeout=config.tool_timeout,
    )


# ------------------------------------------------------- the windows launcher wrap


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


# ------------------------------------------- the malformed progress notification filter


def _is_malformed_progress_notification(message: Any) -> bool:
    payload = _jsonrpc_payload(message)
    if _payload_value(payload, "method") != "notifications/progress":
        return False

    params = _payload_value(payload, "params")
    return not _progress_params_have_token(params)


def _jsonrpc_payload(message: Any) -> Any:
    """Return the JSON-RPC payload across current and future MCP SDK shapes."""
    envelope = getattr(message, "message", message)
    return getattr(envelope, "root", None) or envelope


def _payload_value(payload: Any, key: str) -> Any:
    if isinstance(payload, Mapping):
        return cast("Mapping[str, Any]", payload).get(key)
    return getattr(payload, key, None)


def _progress_params_have_token(params: Any) -> bool:
    if isinstance(params, Mapping):
        return "progressToken" in params
    return hasattr(params, "progressToken") or hasattr(params, "progress_token")


class _ProgressNotificationFilter:
    """Drop a progress notification that carries no token.

    A stdio server that sends one would otherwise fail the SDK's validation and end the session.
    """

    def __init__(self, read_stream: Any, server_name: str) -> None:
        self._read_stream = read_stream
        self._server_name = server_name
        self._iterator: AsyncIterator[Any] | None = None

    async def __aenter__(self) -> _ProgressNotificationFilter:
        await self._read_stream.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return await self._read_stream.__aexit__(exc_type, exc, tb)

    def __aiter__(self) -> _ProgressNotificationFilter:
        self._iterator = self._read_stream.__aiter__()
        return self

    async def __anext__(self) -> Any:
        iterator = self._iterator
        if iterator is None:
            iterator = self._read_stream.__aiter__()
            self._iterator = iterator

        while True:
            message = await anext(iterator)
            if _is_malformed_progress_notification(message):
                logger.debug(
                    "gates: mcp host dropped a progress notification without a token from '{}'",
                    self._server_name,
                )
                continue
            return message

    async def aclose(self) -> None:
        close = getattr(self._read_stream, "aclose", None)
        if close is not None:
            await close()


def _filter_progress_notifications(read_stream: Any, server_name: str) -> Any:
    if not all(hasattr(read_stream, name) for name in ("__aenter__", "__aexit__", "__aiter__")):
        return read_stream
    return _ProgressNotificationFilter(read_stream, server_name)


# ----------------------------------------------------------------- one session


def _own_child_pids() -> set[int]:
    """Every direct child of this process, from procfs.

    A stdio child is a direct child of the host, and the host needs the set to verify that a close
    ended the one it started. procfs answers without a dependency, and a platform with no procfs
    answers an empty set, which turns the verification below into a no-op rather than an error.
    """
    children: set[int] = set()
    own = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            status = Path(f"/proc/{entry}/status").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("PPid:"):
                if line.split()[1] == str(own):
                    children.add(int(entry))
                break
    return children


def _end_child(pid: int) -> None:
    """End one stdio child that a close left behind, and reap it.

    The MCP SDK ends the child when its teardown runs to completion. A close that cancels the owner
    task leaves that teardown unfinished on Python 3.11, and the child then outlives the connection.
    CI caught that on the minimum version while the same test passed on 3.13.

    **This function performs no await, and that is the point.** The close runs inside a `finally`,
    and a `finally` that runs during a cancellation raises CancelledError at its first await. An
    async reaper therefore never reached the kill on the path that needed it most. A blocking wait of
    up to one second during a teardown costs less than a child that outlives its connection.

    TERM first, because a server may flush on it. KILL after that, because a server that ignores
    TERM must not outlive the connection it served.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _CHILD_END_GRACE_S
    while time.monotonic() < deadline:
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, os.WNOHANG)
        if not _pid_is_live(pid):
            return
        time.sleep(0.02)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def _pid_is_live(pid: int) -> bool:
    """Report whether *pid* still runs, and count a zombie as gone.

    A killed child answers signal 0 until somebody reaps it, so the state decides.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    _, _, tail = stat.rpartition(")")
    fields = tail.split()
    return bool(fields) and fields[0] != "Z"


# --------------------------------------- the kernel ends the child when the host dies

# prctl(PR_SET_PDEATHSIG, SIGKILL), from include/uapi/linux/prctl.h.
_PR_SET_PDEATHSIG = 1

# SIGKILL is signal 9 on every Linux architecture. The number stays a literal here, because the
# signal module of Windows holds no SIGKILL and this module must import on that platform too.
_SIGKILL_NUMBER = 9

# The program the host starts in place of the server. It arms the parent death signal, and then it
# becomes the server through an exec. An exec keeps the pid and the signal setting, so the host
# still holds one direct child per session.
#
# The argv reads: -c, this source, the host pid, the command, then the arguments of the command. A
# Python interpreter puts "-c" in sys.argv[0], so the host pid sits at sys.argv[1].
#
# The parent pid check closes a race. A host that dies between the fork and the prctl call leaves
# no signal for the kernel to deliver, and the child then reads a parent pid that is not the host.
#
# The exec failure message goes to stderr, which is the host log. A command that no longer exists
# must name itself there, because the host sees only a child that started and said nothing.
_PARENT_DEATH_LAUNCHER = (
    "import ctypes, os, sys\n"
    "try:\n"
    f"    ctypes.CDLL(None, use_errno=True).prctl({_PR_SET_PDEATHSIG},"
    f" {_SIGKILL_NUMBER}, 0, 0, 0)\n"
    "except Exception as exc:\n"
    "    sys.stderr.write('nanoinfra mcp host: no parent death signal: %s\\n' % exc)\n"
    "if os.getppid() != int(sys.argv[1]):\n"
    "    os._exit(0)\n"
    "try:\n"
    "    os.execvp(sys.argv[2], sys.argv[2:])\n"
    "except OSError as exc:\n"
    "    sys.stderr.write('nanoinfra mcp host: cannot run %s: %s\\n' % (sys.argv[2], exc))\n"
    "    os._exit(127)\n"
)

_parent_death_signal_warned = False


def wrap_with_parent_death_signal(
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[str, list[str]]:
    """Return the argv that arms the parent death signal, then runs *command* with *args*.

    The reaper above covers a host that closes a session. It cannot cover a host that somebody
    kills, because SIGKILL leaves this process no code to run. ``PR_SET_PDEATHSIG`` gives that case
    to the kernel: the kernel sends SIGKILL to the child on the death of the host.

    Only the child can arm the signal, because prctl acts on the process that calls it. The MCP SDK
    starts the child, and it offers no hook between the fork and the exec. So the host starts its
    own interpreter first, and that interpreter arms the signal and becomes the server.

    The wrap adds no exec right. The interpreter is the one this process already runs, and the
    command is the one the config named.

    A command that no PATH holds raises ``FileNotFoundError`` here. ``env`` and ``cwd`` carry the
    two values that decide where the child would look for it.

    Linux holds this call. Every other platform gets the command unchanged, and one log line says
    so.
    """
    if sys.platform != "linux":
        _warn_no_parent_death_signal(f"the platform {sys.platform!r} has no PR_SET_PDEATHSIG")
        return command, list(args)
    if not sys.executable:
        _warn_no_parent_death_signal("this build reports no interpreter path")
        return command, list(args)
    _check_the_command_exists(command, env=env, cwd=cwd)
    return sys.executable, ["-c", _PARENT_DEATH_LAUNCHER, str(os.getpid()), command, *args]


def _check_the_command_exists(
    command: str, *, env: dict[str, str] | None, cwd: str | None
) -> None:
    """Raise the words of the operating system for a command that no PATH holds.

    The launcher execs the command inside the child, so a bad command reaches the host as a closed
    connection. Those words name the host, and an operator then looks at a deployment rather than
    at the config they mistyped. The MCP SDK raised the words below in this process before the
    launcher existed, and the host still answers them.

    The check acts on the case it can decide, and it stays silent for every other case. A relative
    command with a working directory belongs to the child, because this process sits somewhere
    else. A file that exists also belongs to the child, because the exec right and the file type
    carry their own words from the kernel.
    """
    if cwd is not None and not os.path.isabs(command):
        return
    if shutil.which(command, path=(env or {}).get("PATH")) is not None:
        return
    if os.path.exists(command):
        return
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), command)


def _warn_no_parent_death_signal(reason: str) -> None:
    """Say once that no kernel signal ends a stdio child when the host dies."""
    global _parent_death_signal_warned
    if _parent_death_signal_warned:
        return
    _parent_death_signal_warned = True
    logger.warning(
        "gates: mcp host arms no parent death signal on a stdio child, because {}. A host that "
        "somebody kills can leave an MCP server behind.",
        reason,
    )


class SessionTerminatedError(Exception):
    """The stdio MCP server ended, so this session can serve nothing more.

    The message opens with "Session terminated" on purpose. The agent's tool wrappers read those
    words and reconnect the server, and that recovery predates the split.
    """


class _StdioSession:
    """One stdio MCP server, its child process, and its client session.

    One task owns the session for its whole life. That task enters the MCP SDK contexts, waits for
    a close, and exits them itself. Two reasons make the owner task necessary:

    - An anyio cancel scope must exit in the task that entered it.
    - A dead child cancels that scope. With the session in the connection's own task, the cancel
      would take the connection loop down and leave every caller without an answer. The owner task
      absorbs it, and the loop then answers "Session terminated".
    """

    def __init__(self, *, server_name: str, settings: StdioServerSettings) -> None:
        self.server_name = server_name
        self.settings = settings
        self._session: Any = None
        self._owner: asyncio.Task[None] | None = None
        self._close_requested = asyncio.Event()
        # The children this session started, so a close can verify that each one ended (#22).
        self._children: set[int] = set()

    async def open(self) -> None:
        """Start the child and initialize the MCP session, or raise what stopped it."""
        before = _own_child_pids()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._owner = asyncio.create_task(
            self._own_session(ready), name=f"mcp-host-session:{self.server_name}"
        )
        try:
            await ready
        except BaseException:
            self._children = _own_child_pids() - before
            await self.aclose()
            raise
        self._children = _own_child_pids() - before

    async def _own_session(self, ready: asyncio.Future[None]) -> None:
        """Hold the session open until a close is asked for."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            async with AsyncExitStack() as stack:
                # The wrap arms the kernel's parent death signal on the child, so a host that
                # somebody kills leaves no MCP server behind (#50).
                command, args = wrap_with_parent_death_signal(
                    self.settings.command,
                    self.settings.args,
                    env=self.settings.env,
                    cwd=self.settings.cwd,
                )
                parameters = StdioServerParameters(
                    command=command,
                    args=args,
                    env=self.settings.env,
                    cwd=self.settings.cwd,
                )
                read, write = await stack.enter_async_context(stdio_client(parameters))
                read = _filter_progress_notifications(read, self.server_name)
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._session = session
                if not ready.done():
                    ready.set_result(None)
                await self._close_requested.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            self._session = None

    async def aclose(self) -> None:
        """Close the session and end the child."""
        owner = self._owner
        self._owner = None
        self._session = None
        if owner is None:
            return
        self._close_requested.set()
        done, _pending = await asyncio.wait({owner}, timeout=_CLOSE_TIMEOUT_S)
        if not done:
            logger.debug("gates: mcp host cancels a slow close of '{}'", self.server_name)
            owner.cancel()
        # Read the outcome either way. An owner that ended on an error must not leave an
        # exception nobody retrieved, because that logs noise the operator cannot act on.
        with contextlib.suppress(BaseException):
            await owner
        # Verify the child, and never trust the teardown to have ended it. A cancelled teardown
        # skips the SDK cleanup on Python 3.11, and the child then outlives the connection.
        children = self._children
        self._children = set()
        for pid in children:
            if _pid_is_live(pid):
                logger.debug(
                    "gates: mcp host ends child {} of '{}' itself", pid, self.server_name
                )
                _end_child(pid)

    async def act(self, request: HostRequest) -> dict[str, Any]:
        """Run one request against the session, and return the MCP result as JSON.

        The call races the owner task. A child that dies mid-call therefore answers at once,
        rather than after the timeout of a request nobody can serve.
        """
        session = self._session
        owner = self._owner
        if session is None or owner is None or owner.done():
            raise SessionTerminatedError(
                f"Session terminated: the MCP server {self.server_name!r} is not running"
            )

        call = asyncio.ensure_future(self._invoke(session, request))
        deadline = self.settings.tool_timeout + HOST_TIMEOUT_GRACE_S
        try:
            done, _pending = await asyncio.wait(
                {call, owner}, timeout=deadline, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # The connection went away, so this request has no reader. The call must not outlive
            # it, because a pending task with no owner logs a warning and holds the session.
            call.cancel()
            raise
        if call in done:
            if call.cancelled():
                # An anyio cancel scope of the dead child reached the call. The session is gone,
                # and the words say so rather than reporting a cancellation nobody asked for.
                raise SessionTerminatedError(
                    f"Session terminated: the call to {self.server_name!r} was cancelled by the "
                    "MCP SDK"
                )
            return call.result()

        call.cancel()
        with contextlib.suppress(BaseException):
            await call
        if owner in done:
            raise SessionTerminatedError(
                f"Session terminated: the MCP server {self.server_name!r} ended during the call"
            )
        raise TimeoutError(f"the MCP server {self.server_name!r} did not answer in time")

    async def _invoke(self, session: Any, request: HostRequest) -> dict[str, Any]:
        """Send one request to the session and dump the result for the wire."""
        if isinstance(request, ListToolsRequest):
            result = await session.list_tools()
        elif isinstance(request, ListResourcesRequest):
            result = await session.list_resources()
        elif isinstance(request, ListPromptsRequest):
            result = await session.list_prompts()
        elif isinstance(request, CallToolRequest):
            result = await session.call_tool(request.tool_name, arguments=request.arguments)
        elif isinstance(request, ReadResourceRequest):
            from pydantic import AnyUrl

            result = await session.read_resource(AnyUrl(request.uri))
        elif isinstance(request, GetPromptRequest):
            result = await session.get_prompt(
                request.prompt_name, arguments=request.arguments
            )
        else:
            raise ServerRefusedError(f"the host serves no operation for {type(request).__name__}")

        # by_alias keeps ``_meta`` and the camelCase names the SDK validates on the other side.
        return cast("dict[str, Any]", result.model_dump(mode="json", by_alias=True))


# -------------------------------------------------------------------- the host


@dataclass(slots=True)
class MCPHost:
    """Serves one MCP session per connection."""

    # None means the host's own config. The lookup happens per open rather than at class
    # definition, so the loader a deployment installs is the loader this process calls.
    settings_loader: SettingsLoader | None = None

    async def serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Answer one connection until the peer hangs up.

        One open per connection, then any number of calls. A request runs in its own task, so a
        long tool call does not block the next one. A write lock keeps two replies apart on the
        wire.
        """
        session: _StdioSession | None = None
        write_lock = asyncio.Lock()
        tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                try:
                    payload = await read_frame(reader)
                except ProtocolError as exc:
                    logger.debug("gates: mcp host connection ended: {}", exc)
                    return

                try:
                    request = decode_request(payload)
                except ProtocolError as exc:
                    logger.warning("gates: mcp host refused a frame: {}", exc)
                    await self._reply(
                        writer, write_lock, _failure(_NO_REQUEST_ID, f"Malformed request: {exc}")
                    )
                    continue

                if isinstance(request, OpenRequest):
                    session, response = await self._open(session, request)
                    await self._reply(writer, write_lock, response)
                    continue

                if session is None:
                    await self._reply(
                        writer,
                        write_lock,
                        _failure(
                            request.request_id,
                            "no MCP server is open on this connection. Send an open request "
                            "first.",
                        ),
                    )
                    continue

                task = asyncio.create_task(
                    self._answer(session, request, writer, write_lock),
                    name=f"mcp-host:{request.request_id}",
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if session is not None:
                await session.aclose()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _open(
        self, session: _StdioSession | None, request: OpenRequest
    ) -> tuple[_StdioSession | None, HostResponse]:
        """Start the named server, or answer with a refusal."""
        if session is not None:
            return session, _failure(
                request.request_id,
                "this connection already holds an open MCP server. Use one connection per "
                "server.",
            )

        loader = self.settings_loader or load_stdio_settings
        try:
            settings = loader(request.server_name)
        except ServerRefusedError as exc:
            logger.warning("gates: mcp host refused server '{}': {}", request.server_name, exc)
            return None, _failure(request.request_id, str(exc))
        except Exception as exc:  # noqa: BLE001 -- a broken config must not end the process
            logger.exception("gates: mcp host could not read its config")
            return None, _failure(
                request.request_id, f"The host could not read its MCP config: {exc}"
            )

        started = _StdioSession(server_name=request.server_name, settings=settings)
        try:
            await started.open()
        except Exception as exc:  # noqa: BLE001 -- one bad server must not end the process
            logger.exception("gates: mcp host could not start server '{}'", request.server_name)
            await started.aclose()
            return None, _failure(
                request.request_id,
                f"The host could not start MCP server {request.server_name!r}: "
                f"{type(exc).__name__}: {exc}",
            )

        logger.info(
            "gates: mcp host started stdio server '{}' ({})",
            request.server_name,
            started.settings.command,
        )
        return started, HostResponse(
            request_id=request.request_id, ok=True, result={}, error=None, error_data=None
        )

    async def _answer(
        self,
        session: _StdioSession,
        request: HostRequest,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
    ) -> None:
        """Run one request and send its reply."""
        await self._reply(writer, write_lock, await self._act(session, request))

    async def _act(self, session: _StdioSession, request: HostRequest) -> HostResponse:
        """Run one request, and turn every failure into a reply."""
        try:
            result = await session.act(request)
        except asyncio.CancelledError:
            raise
        except SessionTerminatedError as exc:
            # The words matter. The agent's wrappers read them and reconnect the server.
            logger.warning("gates: mcp host session for '{}' ended: {}", session.server_name, exc)
            return _failure(request.request_id, str(exc))
        except TimeoutError:
            return _failure(
                request.request_id,
                f"The host cut this request after {session.settings.tool_timeout} seconds plus "
                f"{HOST_TIMEOUT_GRACE_S:g} seconds of grace.",
            )
        except Exception as exc:  # noqa: BLE001 -- one bad request must not end the process
            if type(exc).__name__ in _DEAD_SESSION_EXC_NAMES:
                # A closed stream means the child is gone. The type name alone would not survive
                # the wire, and an anyio ClosedResourceError carries no message at all. So the
                # host names the state in the words the agent's reconnect path reads.
                logger.warning(
                    "gates: mcp host session for '{}' lost its child ({})",
                    session.server_name,
                    type(exc).__name__,
                )
                return _failure(
                    request.request_id,
                    f"Session terminated: the MCP server {session.server_name!r} closed its "
                    f"stream ({type(exc).__name__})",
                )
            error_data = _error_data(exc)
            if error_data is None:
                logger.warning(
                    "gates: mcp host failed a request for '{}': {}: {}",
                    session.server_name,
                    type(exc).__name__,
                    exc,
                )
            return HostResponse(
                request_id=request.request_id,
                ok=False,
                result=None,
                error=f"{type(exc).__name__}: {exc}",
                error_data=error_data,
            )

        return HostResponse(
            request_id=request.request_id, ok=True, result=result, error=None, error_data=None
        )

    async def _reply(
        self, writer: asyncio.StreamWriter, write_lock: asyncio.Lock, response: HostResponse
    ) -> None:
        """Send one reply, and answer rather than hang up when the reply is too large.

        A tool result can carry an image and exceed the wire limit. A silent close would read to
        the agent as "the host is not running", and that sends an operator to a deployment
        problem they do not have.
        """
        payload = encode_response(response)
        if len(payload) > MAX_FRAME_BYTES:
            logger.warning("gates: mcp host reply of {} bytes is above the wire limit", len(payload))
            payload = encode_response(
                _failure(
                    response.request_id,
                    f"The MCP result came to {len(payload)} bytes, above the {MAX_FRAME_BYTES} "
                    "byte limit of the host wire. Ask the tool for less content.",
                )
            )
        async with write_lock:
            with contextlib.suppress(OSError, ProtocolError):
                await write_frame(writer, payload)


def _failure(request_id: int, message: str) -> HostResponse:
    """A request the host could not complete. ``ok`` False keeps this apart from a tool error."""
    return HostResponse(
        request_id=request_id, ok=False, result=None, error=message, error_data=None
    )


def _error_data(exc: BaseException) -> dict[str, Any] | None:
    """Return the MCP ``ErrorData`` of an ``McpError``, or None for anything else.

    The agent raises the same ``McpError`` again from this payload. So a prompt call that failed
    on the server reads the same way it read before the split.
    """
    error = getattr(exc, "error", None)
    dump = getattr(error, "model_dump", None)
    if dump is None:
        return None
    with contextlib.suppress(Exception):
        return cast("dict[str, Any]", dump(mode="json", by_alias=True))
    return None


def serve_forever(socket_path: Path | str, *, workspace: Path | str) -> None:
    """Bind the Unix socket and serve until terminated.

    ``workspace`` keeps one entry-point shape with the executor (#18) and the fetcher (#19), so
    one supervisor pattern starts any of the three. The host reads no file from it: a stdio MCP
    server needs a config entry, and the config lives beside the gateway's data.

    The socket file is removed on exit. A stale file blocks the next bind, and a supervisor that
    restarts the host must not need a human to delete one.
    """
    asyncio.run(_serve(Path(socket_path), workspace=Path(workspace)))


async def _serve(path: Path, *, workspace: Path) -> None:
    # A private mode only on a directory this process creates. A two-uid deployment owns that
    # decision: with separate accounts the directory carries setgid plus group traversal (2710),
    # so the agent account can reach a known socket name. A blanket chmod here would lock the
    # agent out, and a split the agent cannot talk to is worse than the mode it replaced.
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, _SOCKET_DIR_MODE)
    if path.exists():
        path.unlink()

    host = MCPHost()
    server = await asyncio.start_unix_server(host.serve_connection, path=str(path), backlog=8)
    # asyncio binds and listens in one call, so the group goes on right after. The window is this
    # process's own event loop rather than a network round trip, and the alternative -- leaving it
    # to the supervisor -- is what left this socket unreachable after a restart. See
    # nanoinfra/gates/socket_group.py.
    apply_socket_group(path, env_var=MCP_HOST_SOCKET_GROUP_ENV)
    logger.info("gates: mcp host listening on {} (workspace {})", path, workspace)
    try:
        async with server:
            await server.serve_forever()
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


__all__ = [
    "HOST_TIMEOUT_GRACE_S",
    "MCPHost",
    "ServerRefusedError",
    "SessionTerminatedError",
    "SettingsLoader",
    "StdioServerSettings",
    "load_stdio_settings",
    "normalize_windows_stdio_command",
    "serve_forever",
    "wrap_with_parent_death_signal",
]
