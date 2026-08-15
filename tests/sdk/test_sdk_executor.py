"""Item 19 (#21): an embedded agent owns an executor child, or it has no remote execution.

The gateway has a supervisor above it. The SDK has nothing above it, so this is the one
configuration where a transport could still sit beside the model. Two properties carry the
milestone here, and the first one is structural: the facade must not be able to import a
backend or a credential store at any depth, so a lazy import inside a function fails the
check too.

The second property needs a real child. A mocked socket proves that the code calls a mock.
One end-to-end test proves that an SDK-embedded agent reaches a separate process, and that
the process is gone when the caller is done.
"""

from __future__ import annotations

import ast
import gc
import json
import os
import socket
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import nanoinfra.nanoinfra as facade
import nanoinfra.sdk.clients as sdk_clients
import nanoinfra.sdk.runtime as sdk_runtime
import nanoinfra.sdk.streaming as sdk_streaming
import nanoinfra.sdk.types as sdk_types
from nanoinfra.agent.tools.server_execution import PREVIEW_ON_REQUEST_NOTE, ExecuteOnServerTool
from nanoinfra.nanoinfra import Nanoinfra
from nanoinfra.sdk.types import (
    REMOTE_EXECUTION_DISABLED,
    REMOTE_EXECUTION_DISABLED_MESSAGE,
    REMOTE_EXECUTION_EXECUTOR_PROCESS,
    RemoteExecutionUnavailableError,
)

TOOL_NAME = "execute_on_server"

# What the SDK must not be able to reach. A module that imports any of these holds the means to
# dial a host or to read a credential, and then the split is false for every SDK user.
FORBIDDEN_IMPORTS = (
    "nanoinfra.secrets.store",
    "nanoinfra.gates.executor.server",
)
FORBIDDEN_IMPORT_PREFIXES = ("nanoinfra.servers.execution",)

FORBIDDEN_ATTRIBUTES = (
    "SSHBackend",
    "AnsibleRunnerBackend",
    "SSMBackend",
    "ApiBackend",
    "SecretStore",
    "Executor",
)

SDK_MODULES: tuple[ModuleType, ...] = (
    facade,
    sdk_clients,
    sdk_runtime,
    sdk_streaming,
    sdk_types,
)


def _sdk_source_files() -> list[Path]:
    """Every file the SDK surface is made of."""
    files = [Path(str(module.__file__)) for module in SDK_MODULES]
    package = Path(str(sdk_types.__file__)).parent
    files.extend(sorted(package.glob("*.py")))
    return sorted(set(files))


def _imported_modules(path: Path) -> set[str]:
    """Every module name the file imports, at any depth, including inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "providers": {"openrouter": {"apiKey": "sk-test-key"}},
            "agents": {"defaults": {"model": "openai/gpt-4.1"}},
        }),
        encoding="utf-8",
    )
    return config_path


def _write_server(workspace: Path, *, name: str) -> str:
    """Put one server in the workspace inventory and return its id.

    The provider is ssm, which names no dialed address. So a preview needs no DNS and the
    network guard has nothing to resolve.
    """
    server_id = uuid.uuid4().hex
    root = workspace / "servers"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{server_id}.json").write_text(
        json.dumps({
            "id": server_id,
            "name": name,
            "providerId": "ssm",
            "config": {"instanceId": "i-0123456789abcdef0", "region": "us-east-1"},
            "secretRef": None,
            "tags": [],
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )
    return server_id


def _tool_of(bot: Nanoinfra) -> ExecuteOnServerTool:
    tool = bot._loop.tools.get(TOOL_NAME)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(tool, ExecuteOnServerTool)
    return tool


class _FakeExecutor:
    """Stands in for the supervisor's handle, so lifetime tests need no real child."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.stops = 0

    def stop(self, *, timeout_s: int = 20) -> bool:
        self.stops += 1
        return True

    def is_running(self) -> bool:
        return self.stops == 0


# ------------------------------------------------------------------ structural


def test_the_sdk_imports_no_backend_and_no_secret_store() -> None:
    """The acceptance criterion of #21, as a check rather than a promise."""
    offences: list[str] = []
    for path in _sdk_source_files():
        for name in _imported_modules(path):
            if name in FORBIDDEN_IMPORTS or name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                offences.append(f"{path.name} imports {name}")

    assert offences == []


def test_the_sdk_cannot_construct_a_backend() -> None:
    """The runtime half of the same property."""
    offences: list[str] = []
    for module in SDK_MODULES:
        for attribute in FORBIDDEN_ATTRIBUTES:
            if hasattr(module, attribute):
                offences.append(f"{module.__name__} holds {attribute}")

    assert offences == []


# -------------------------------------------------------- spawning declined


def test_the_sdk_declines_remote_execution_unless_a_caller_asks(tmp_path: Path) -> None:
    """The default must not fork a child in every caller's process."""
    bot = Nanoinfra.from_config(_write_config(tmp_path), workspace=tmp_path)

    assert bot.remote_execution == REMOTE_EXECUTION_DISABLED
    assert bot.executor is None


async def test_a_declined_call_fails_with_the_specific_error(tmp_path: Path) -> None:
    """The words must name the choice that removed remote execution, and name the fix."""
    bot = Nanoinfra.from_config(_write_config(tmp_path), workspace=tmp_path)
    _write_server(tmp_path, name="web-1")

    result = await _tool_of(bot).execute(
        server_id_or_name="web-1", command="uptime", dry_run=False
    )

    assert getattr(result, "is_error", False) is True
    assert REMOTE_EXECUTION_DISABLED_MESSAGE in str(result)
    assert "remote_execution='executor_process'" in str(result)
    # A missing executor reads differently from a refusal. An operator who reads a deployment
    # choice as a policy decision looks for a grant that would not have helped.
    assert "rather than a policy decision" in str(result)


async def test_a_declined_call_opens_no_transport_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal that still dials would make the split false while the notes say otherwise."""
    bot = Nanoinfra.from_config(_write_config(tmp_path), workspace=tmp_path)
    _write_server(tmp_path, name="web-1")

    def no_socket(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the declined SDK path opened a socket")

    monkeypatch.setattr(socket, "socket", no_socket)

    result = await _tool_of(bot).execute(
        server_id_or_name="web-1", command="uptime", dry_run=False
    )

    assert REMOTE_EXECUTION_DISABLED_MESSAGE in str(result)


def test_the_declined_client_raises_the_specific_error_type(tmp_path: Path) -> None:
    """A caller that drives the client itself gets a type it can catch."""
    bot = Nanoinfra.from_config(_write_config(tmp_path), workspace=tmp_path)

    with pytest.raises(RemoteExecutionUnavailableError, match="executor_process"):
        _tool_of(bot).client.execute(
            server_id_or_name="web-1",
            command="uptime",
            session_id="sdk:default",
            execution_context="interactive",
            preview_requested=True,
            timeout_s=None,
        )


def test_a_failed_spawn_raises_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that asked for an executor must not get a working agent without one.

    A fallback here would be the whole hole: the model would keep the tool, and the SDK would
    answer from the caller's own process.
    """
    from nanoinfra.gates.executor.supervisor import ExecutorStartError

    def failed_spawn(**_kwargs: Any) -> Any:
        raise ExecutorStartError("the executor exited before it opened the socket")

    monkeypatch.setattr(facade, "_spawn_executor", failed_spawn)

    with pytest.raises(ExecutorStartError):
        Nanoinfra.from_config(
            _write_config(tmp_path),
            workspace=tmp_path,
            remote_execution=REMOTE_EXECUTION_EXECUTOR_PROCESS,
        )


def test_an_unknown_mode_raises_rather_than_disables_quietly(tmp_path: Path) -> None:
    """A typo must not read as 'disabled', and it must not read as 'executor_process'."""
    with pytest.raises(ValueError, match="remote_execution"):
        Nanoinfra.from_config(
            _write_config(tmp_path), workspace=tmp_path, remote_execution="executor"
        )


# ------------------------------------------------------------ the child's lifetime


async def test_the_sdk_stops_its_executor_once_when_the_caller_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aclose() ends the child, and a second close must not stop a stranger's process."""
    fake = _FakeExecutor(tmp_path / "x.sock")
    monkeypatch.setattr(facade, "_spawn_executor", lambda **_kwargs: fake)
    bot = Nanoinfra.from_config(
        _write_config(tmp_path),
        workspace=tmp_path,
        remote_execution=REMOTE_EXECUTION_EXECUTOR_PROCESS,
    )

    assert bot.executor is fake
    await bot.aclose()
    await bot.aclose()

    assert fake.stops == 1


def test_a_caller_that_never_closes_leaves_no_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child dies with the instance, and again at interpreter exit."""
    fake = _FakeExecutor(tmp_path / "x.sock")
    monkeypatch.setattr(facade, "_spawn_executor", lambda **_kwargs: fake)
    bot = Nanoinfra.from_config(
        _write_config(tmp_path),
        workspace=tmp_path,
        remote_execution=REMOTE_EXECUTION_EXECUTOR_PROCESS,
    )
    finalizer = bot._executor_stop  # pyright: ignore[reportPrivateUsage]
    assert finalizer is not None
    # atexit is what covers a caller that holds the instance until the process ends.
    assert finalizer.atexit is True

    del bot
    gc.collect()

    assert fake.stops == 1


# ------------------------------------------------------------------- end to end


async def test_an_embedded_agent_routes_a_call_through_a_real_executor_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supported path, with a real process on the other end of the socket.

    HOME moves to a temporary directory, because the child reads its policy and writes its
    audit log under the data directory. A test must not touch the operator's own.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_server(workspace, name="web-1")
    socket_path = tmp_path / "x.sock"

    bot = Nanoinfra.from_config(
        _write_config(tmp_path),
        workspace=workspace,
        remote_execution=REMOTE_EXECUTION_EXECUTOR_PROCESS,
        executor_socket=socket_path,
    )
    try:
        executor = bot.executor
        assert executor is not None
        assert executor.is_running() is True
        assert isinstance(executor.pid, int)
        assert executor.pid != os.getpid()
        tool = _tool_of(bot)
        assert tool.client.socket_path == socket_path

        result = await tool.execute(
            server_id_or_name="web-1", command="uptime", dry_run=True
        )
    finally:
        await bot.aclose()

    assert "Preview (not executed)" in str(result)
    assert "provider='ssm'" in str(result)
    assert "'web-1'" in str(result)
    assert PREVIEW_ON_REQUEST_NOTE in str(result)
    assert executor.is_running() is False
    assert not socket_path.exists()
