# tests/gates/test_executor_startup.py
"""Item 38 (#40): who starts the executor, in every install.

`execute_on_server` is a thin client after #18. The #27 approvals inbox reads a second socket of
the same process. So a deployment with no executor holds a dead tool and a degraded inbox, and the
start needs two homes:

- The Python gateway starts the executor for a ``pip install nanoinfra`` user. Nothing else runs in
  that install, so the gateway is the supervisor there.
- ``entrypoint.sh`` starts the executor for the container. Only a root start places two processes on
  two accounts, so the container owns its own socket and its own account.

Two rules shape both paths. A failed start is loud, and it names one consequence: every gated
action refuses until an executor answers. And one deployment starts one executor. A second
executor holds the agent's uid, and that undoes the split the first one claims.

The start decision is the third rule, and it differs from the other two children. The fetcher waits
for the web tools, and the MCP host waits for a stdio server. The executor waits for nothing. The
tool is registered in every install, an operator adds a server record at any time after boot, and
the approvals inbox needs the second socket even before the first server exists.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from nanoinfra.agent.tools.server_execution import (
    EXECUTOR_SOCKET_ENV,
    ExecuteOnServerTool,
    default_socket_path,
)
from nanoinfra.cli import gateway_runtime
from nanoinfra.gates.executor import supervisor
from nanoinfra.gates.executor.client import ExecutorClient

_ENTRYPOINT = Path("entrypoint.sh")
_DOCKERFILE = Path("Dockerfile")


class _FakeProcess:
    """Stands in for the executor handle the supervisor returns."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.pid = 6543
        self.stopped = 0

    def stop(self) -> bool:
        self.stopped += 1
        return True


def _socket_from_environment() -> str:
    """What the gateway exported for the tool to read."""
    return os.environ.get(EXECUTOR_SOCKET_ENV, "")


def _config(workspace: Path | None = None) -> Any:
    """The one field the executor start reads.

    The object carries no tool config and no inventory on purpose. A start that read either one
    would raise here, and that keeps the unconditional decision honest.
    """
    return SimpleNamespace(workspace_path=workspace or Path("/tmp/nanoinfra-workspace"))


@pytest.fixture(autouse=True)
def _clear_executor_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an install that names nothing."""
    monkeypatch.delenv(EXECUTOR_SOCKET_ENV, raising=False)
    monkeypatch.delenv(gateway_runtime.EXECUTOR_EXTERNAL_ENV, raising=False)
    monkeypatch.delenv(gateway_runtime.EXECUTOR_USER_ENV, raising=False)


def _capture_start(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    def _start(**kwargs: Any) -> _FakeProcess:
        calls.append(kwargs)
        return _FakeProcess(Path(kwargs["socket_path"]))

    monkeypatch.setattr(supervisor, "start_executor", _start)


# ------------------------------------------------------------------ the pip install path


def test_the_gateway_starts_the_executor_and_names_its_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway starts the child, then it tells the tool where the child listens.

    The tool reads the environment (#38). Without the export it guesses a path, and a guess reads
    to an operator as an executor that does not run.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    handle = gateway_runtime._start_executor_for_gateway(_config(tmp_path))

    assert handle is not None
    assert len(calls) == 1
    assert calls[0]["workspace"] == tmp_path
    assert Path(calls[0]["socket_path"]).name == "executor.sock"
    assert _socket_from_environment() == str(calls[0]["socket_path"])


def test_the_start_needs_no_server_and_no_tool_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The start is unconditional, and this test is the reason it must stay so.

    An operator adds a server record after boot, and a gateway does not restart for it. A start
    that read the server store would answer for one moment and then be wrong. The config here
    carries only a workspace, so any other read fails this test at once.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    handle = gateway_runtime._start_executor_for_gateway(_config(tmp_path))

    assert handle is not None
    assert len(calls) == 1


def test_the_executor_socket_is_not_the_fetcher_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One socket per process. A shared path lets either process answer for the other."""
    from nanoinfra.agent.tools.web import default_socket_path as fetcher_socket

    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    gateway_runtime._start_executor_for_gateway(_config(tmp_path))

    assert Path(calls[0]["socket_path"]) != fetcher_socket()


def test_an_external_executor_stops_a_second_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container already started the executor on its own account.

    A second executor holds the agent's uid, and that undoes the split the first one claims. The
    named socket also survives, because the tool still has to reach the first executor.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(gateway_runtime.EXECUTOR_EXTERNAL_ENV, "1")
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, "/run/nanoinfra-exec/executor.sock")

    handle = gateway_runtime._start_executor_for_gateway(_config(tmp_path))

    assert handle is None
    assert calls == []
    assert _socket_from_environment() == "/run/nanoinfra-exec/executor.sock"


def test_the_named_socket_wins_over_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deployment that names a path gets that path, because only it knows its own layout."""
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(tmp_path / "run" / "chosen.sock"))

    gateway_runtime._start_executor_for_gateway(_config(tmp_path))

    assert Path(calls[0]["socket_path"]) == tmp_path / "run" / "chosen.sock"


def test_the_configured_account_reaches_the_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A separate uid is the only split the kernel enforces, so the account travels through."""
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(gateway_runtime.EXECUTOR_USER_ENV, "nanoinfra-exec")

    gateway_runtime._start_executor_for_gateway(_config(tmp_path))

    assert calls[0]["user"] == "nanoinfra-exec"


def test_no_account_still_starts_a_separate_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No separate uid is not a reason to skip the split.

    A separate process alone takes the credential store and the four transports out of the address
    space that runs the model. That is worth having without the kernel's help.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    handle = gateway_runtime._start_executor_for_gateway(_config(tmp_path))

    assert handle is not None
    assert calls[0]["user"] is None


def test_a_failed_executor_start_is_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silent failure teaches an operator to read a broken deployment as a policy refusal.

    The log carries the child's own reason, and both surfaces carry the consequence. An operator
    reads one of the two, so both name it.
    """

    def _fail(**_kwargs: Any) -> Any:
        raise supervisor.ExecutorStartError("the executor exited before it opened the socket")

    monkeypatch.setattr(supervisor, "start_executor", _fail)
    logged: list[str] = []
    sink = logger.add(logged.append, level="ERROR")
    try:
        handle = gateway_runtime._start_executor_for_gateway(_config(tmp_path))
    finally:
        logger.remove(sink)

    printed = capsys.readouterr().out
    assert handle is None
    assert any("the executor exited before it opened the socket" in line for line in logged)
    assert any("every gated action refuses" in line for line in logged)
    assert "the executor did not start" in printed
    assert "every gated action refuses" in printed


def test_the_gateway_stops_the_executor(tmp_path: Path) -> None:
    """An executor that outlives its gateway holds the socket the next gateway needs."""
    handle = _FakeProcess(tmp_path / "executor.sock")

    gateway_runtime._stop_executor(handle)

    assert handle.stopped == 1


def test_a_stop_without_an_executor_does_nothing() -> None:
    """A start can fail, so the stop has to accept the absence."""
    gateway_runtime._stop_executor(None)


def test_the_gateway_wires_the_start_and_the_stop() -> None:
    """The unit tests above pass while the gateway calls neither. So the wiring gets a check."""
    source = inspect.getsource(gateway_runtime)

    assert "_start_executor_for_gateway(config)" in source
    assert "_stop_executor(executor)" in source


def test_the_start_comes_before_the_operator_inbox() -> None:
    """The #27 inbox derives its socket from the executor socket this start exports.

    An inbox built first reads the guessed path, and it then reports a degraded inbox for a
    deployment that names its socket.
    """
    source = inspect.getsource(gateway_runtime._run_gateway)
    started_at = source.index("_start_executor_for_gateway(config)")
    inbox_at = source.index("_operator_client_for_gateway()")

    assert started_at < inbox_at


# ------------------------------------------------------------------------- the whole chain


async def test_the_tool_reaches_a_real_executor_from_a_gateway_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One end-to-end pass: the gateway start answers a real request from the real tool.

    Every other test here stubs one side. This one starts a real interpreter through the gateway
    helper, so it proves the supervisor, the socket, the wire, and the tool agree.

    The request asks for a preview, so the executor resolves the server and connects to nothing.
    HOME points at *tmp_path*, so the child writes its own state under the test directory.
    """
    from nanoinfra.servers.store import ServerStore

    home = tmp_path / "home"
    (home / ".nanoinfra").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # The child resolves a scope with its own parser, and a user-site ansible-inventory reads HOME.
    # This file asks about the start, so the child gets a PATH with no ansible on it. The supervisor
    # runs an absolute interpreter path, so the launch is unaffected.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ServerStore(workspace).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(tmp_path / "r" / "e.sock"))

    handle = gateway_runtime._start_executor_for_gateway(_config(workspace))
    assert handle is not None
    try:
        tool = ExecuteOnServerTool(client=ExecutorClient(handle.socket_path))
        result = await tool.execute(server_id_or_name="prod-web-01", command="id", dry_run=True)
    finally:
        gateway_runtime._stop_executor(handle)

    text = str(result)
    assert "Preview (not executed)" in text
    assert "prod-web-01" in text
    assert "not reachable" not in text
    assert not handle.is_running()


def test_the_default_socket_path_is_the_one_the_tool_reads() -> None:
    """The supervisor and the client must agree without an export, or the export hides a bug."""
    assert default_socket_path().name == "executor.sock"


# ------------------------------------------------------------------- the container path


def test_the_container_starts_the_executor_module() -> None:
    """The container runs the fixed entry point (#18), and no caller chooses a program."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "nanoinfra.gates.executor" in source
    assert "start_executor " in source


def test_the_container_hands_the_socket_path_to_the_agent() -> None:
    """The agent's client reads the path rather than guesses it, and it starts no second child."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'export NANOINFRA_EXECUTOR_SOCKET="$socket_path"' in source
    assert "export NANOINFRA_EXECUTOR_EXTERNAL=1" in source


def test_the_gateway_reads_the_two_variables_the_container_exports() -> None:
    """One shape for three children. An operator learns one idiom and applies it three times."""
    assert gateway_runtime.EXECUTOR_EXTERNAL_ENV == "NANOINFRA_EXECUTOR_EXTERNAL"
    assert gateway_runtime.EXECUTOR_USER_ENV == "NANOINFRA_EXECUTOR_USER"
    assert EXECUTOR_SOCKET_ENV == "NANOINFRA_EXECUTOR_SOCKET"


def test_the_container_runs_the_executor_on_its_own_account() -> None:
    """The account that reads a credential is never the account that runs the model."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'exec_user="nanoinfra-exec"' in source
    assert '--reuid="$exec_user"' in source


def test_the_container_reports_a_missing_executor_socket() -> None:
    """A failed start is loud on that path too. Every gated action then refuses."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "wait_for_socket" in source
    assert "no executor socket at" in source


def test_the_image_creates_the_executor_account() -> None:
    text = re.sub(r"\\\n\s*", " ", _DOCKERFILE.read_text(encoding="utf-8"))

    assert re.search(r"useradd\s+--system[^\n]*nanoinfra-exec\b", text)
