# tests/gates/test_fetcher_startup.py
"""Item 16 (#19): who starts the fetcher, in every install.

web_fetch and web_search are thin clients after the cutover. A tool that talks to a process nobody
starts is a dead tool, so the start has two homes and both need a test:

- The Python gateway starts the fetcher for a ``pip install nanoinfra`` user. Nothing else runs in
  that install, so the gateway is the supervisor there.
- ``entrypoint.sh`` starts the fetcher for the container. Only a root start can place two processes
  on two accounts, so the container gets its own socket and its own account.

Two rules shape both paths. A failed start is loud, and the fetcher's account is never the
executor's account. Two processes under one uid get no separation from the kernel, because one can
ptrace the other and read its memory.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from nanoinfra.agent.tools.web import FETCHER_SOCKET_ENV, WebFetchTool
from nanoinfra.cli import gateway_runtime
from nanoinfra.gates.fetcher import supervisor
from nanoinfra.gates.fetcher.client import FetcherClient

_ENTRYPOINT = Path("entrypoint.sh")


class _FakeProcess:
    """Stands in for the fetcher handle the supervisor returns."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.pid = 4321
        self.stopped = 0

    def stop(self) -> bool:
        self.stopped += 1
        return True


def _socket_from_environment() -> str:
    """What the gateway exported for the tool to read."""
    import os

    return os.environ.get(FETCHER_SOCKET_ENV, "")


def _config(*, enable: bool = True, workspace: Path | None = None) -> Any:
    """The two fields the fetcher start reads. A real Config needs a provider and a bus."""
    return SimpleNamespace(
        tools=SimpleNamespace(web=SimpleNamespace(enable=enable)),
        workspace_path=workspace or Path("/tmp/nanoinfra-workspace"),
    )


@pytest.fixture(autouse=True)
def _clear_fetcher_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an install that names nothing."""
    monkeypatch.delenv(FETCHER_SOCKET_ENV, raising=False)
    monkeypatch.delenv(gateway_runtime.FETCHER_EXTERNAL_ENV, raising=False)
    monkeypatch.delenv(gateway_runtime.FETCHER_USER_ENV, raising=False)


def _capture_start(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    def _start(**kwargs: Any) -> _FakeProcess:
        calls.append(kwargs)
        return _FakeProcess(Path(kwargs["socket_path"]))

    monkeypatch.setattr(supervisor, "start_fetcher", _start)


# ------------------------------------------------------------------ the pip install path


def test_the_gateway_starts_the_fetcher_and_names_its_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway starts the child, then it tells the tool where the child listens.

    The tool reads the environment. Without the export it would guess a path, and a guess reads to
    an operator as a fetcher that does not run.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    handle = gateway_runtime._start_fetcher_for_gateway(_config(workspace=tmp_path))

    assert handle is not None
    assert len(calls) == 1
    assert calls[0]["workspace"] == tmp_path
    assert Path(calls[0]["socket_path"]).name == "fetcher.sock"
    assert _socket_from_environment() == str(calls[0]["socket_path"])


def test_the_fetcher_socket_is_not_the_executor_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One socket per process. A shared path would let either process answer for the other."""
    from nanoinfra.agent.tools.server_execution import default_socket_path as executor_socket

    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    gateway_runtime._start_fetcher_for_gateway(_config(workspace=tmp_path))

    assert Path(calls[0]["socket_path"]) != executor_socket()


def test_an_external_fetcher_stops_a_second_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container already started the fetcher on its own account.

    A second fetcher would hold the agent's uid, and that undoes the split the first one claims.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(gateway_runtime.FETCHER_EXTERNAL_ENV, "1")
    monkeypatch.setenv(FETCHER_SOCKET_ENV, "/run/nanoinfra-fetch/fetcher.sock")

    handle = gateway_runtime._start_fetcher_for_gateway(_config(workspace=tmp_path))

    assert handle is None
    assert calls == []
    assert _socket_from_environment() == "/run/nanoinfra-fetch/fetcher.sock"


def test_the_named_socket_wins_over_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deployment that names a path gets that path, because only it knows its own layout."""
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(FETCHER_SOCKET_ENV, str(tmp_path / "run" / "chosen.sock"))

    gateway_runtime._start_fetcher_for_gateway(_config(workspace=tmp_path))

    assert Path(calls[0]["socket_path"]) == tmp_path / "run" / "chosen.sock"


def test_the_configured_account_reaches_the_fetcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A separate uid is the only split the kernel enforces, so the account travels through."""
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(gateway_runtime.FETCHER_USER_ENV, "nanoinfra-fetch")

    gateway_runtime._start_fetcher_for_gateway(_config(workspace=tmp_path))

    assert calls[0]["user"] == "nanoinfra-fetch"


def test_no_account_still_starts_a_separate_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No separate uid is not a reason to skip the split.

    A separate process alone removes the credential store and the transports from the address
    space that reads a page. That is worth having without the kernel's help.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    handle = gateway_runtime._start_fetcher_for_gateway(_config(workspace=tmp_path))

    assert handle is not None
    assert calls[0]["user"] is None


def test_a_failed_fetcher_start_is_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silent failure teaches an operator to read a broken deployment as a broken page.

    The log carries the child's own reason, and the console carries the consequence. An operator
    reads one of the two, so both say it.
    """

    def _fail(**_kwargs: Any) -> Any:
        raise supervisor.FetcherStartError("the fetcher exited before it opened the socket")

    monkeypatch.setattr(supervisor, "start_fetcher", _fail)
    logged: list[str] = []
    sink = logger.add(logged.append, level="ERROR")
    try:
        handle = gateway_runtime._start_fetcher_for_gateway(_config(workspace=tmp_path))
    finally:
        logger.remove(sink)

    assert handle is None
    assert any("the fetcher exited before it opened the socket" in line for line in logged)
    assert "the fetcher did not start" in capsys.readouterr().out


def test_no_fetcher_starts_when_the_web_tools_are_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An operator who disabled the web tools asked for no egress process."""
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    handle = gateway_runtime._start_fetcher_for_gateway(
        _config(enable=False, workspace=tmp_path)
    )

    assert handle is None
    assert calls == []


def test_the_gateway_stops_the_fetcher(tmp_path: Path) -> None:
    """A fetcher that outlives its gateway holds the socket the next gateway needs."""
    handle = _FakeProcess(tmp_path / "fetcher.sock")

    gateway_runtime._stop_fetcher(handle)

    assert handle.stopped == 1


def test_a_stop_without_a_fetcher_does_nothing(tmp_path: Path) -> None:
    """A start can fail, so the stop has to accept the absence."""
    gateway_runtime._stop_fetcher(None)


def test_the_gateway_wires_the_start_and_the_stop() -> None:
    """The unit tests above pass while the gateway calls neither. So the wiring gets a check."""
    source = inspect.getsource(gateway_runtime._run_gateway)

    assert "_start_fetcher_for_gateway(config)" in source
    assert "_stop_fetcher(" in source


# ------------------------------------------------------------------------- the whole chain


async def test_the_tool_reaches_a_real_fetcher(tmp_path: Path) -> None:
    """One end-to-end pass: the supervisor starts the child, and the tool reads its answer.

    Every other test here stubs one side. This one starts a real interpreter, so it proves the
    supervisor, the socket, the wire, and the tool agree.

    The URL names a loopback address. The fetcher refuses it, so nothing leaves this host.
    """
    socket_path = tmp_path / "f.sock"
    handle = supervisor.start_fetcher(socket_path=socket_path, workspace=tmp_path)
    try:
        tool = WebFetchTool(client=FetcherClient(socket_path))
        result = await tool.execute(url="http://127.0.0.1/admin")
    finally:
        handle.stop()

    assert "URL validation failed" in result


# ------------------------------------------------------------------- the container path


def test_the_container_starts_the_fetcher_module() -> None:
    """The container runs the fixed entry point, and it restarts the child after a crash."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "start_fetcher()" in source
    assert "nanoinfra.gates.fetcher" in source
    assert "start_fetcher " in source


def test_the_container_gives_the_fetcher_its_own_socket() -> None:
    """Two sockets, because either process could otherwise answer for the other."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'socket_dir="/run/nanoinfra-exec"' in source
    assert 'socket_path="$socket_dir/executor.sock"' in source
    assert 'fetch_socket_dir="/run/nanoinfra-fetch"' in source
    assert 'fetch_socket_path="$fetch_socket_dir/fetcher.sock"' in source


def test_the_container_never_runs_the_fetcher_as_the_executor() -> None:
    """The one account the fetcher must never hold is the one that reads credentials."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'fetch_user="nanoinfra-fetch"' in source
    assert '"$fetch_run_user" = "$exec_user"' in source


def test_the_container_hands_the_socket_path_to_the_agent() -> None:
    """The agent's client reads the path rather than guesses it."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'export NANOINFRA_FETCHER_SOCKET="$fetch_socket_path"' in source
    assert "export NANOINFRA_FETCHER_EXTERNAL=1" in source


def test_the_container_says_when_the_fetcher_split_is_organisational() -> None:
    """An operator must never read silence as a guarantee."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "warn_fetcher_split_not_enforced" in source


def test_the_container_reports_a_missing_fetcher_socket() -> None:
    """A failed start is loud. Web tools then fail, and the log says why."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "wait_for_fetcher_socket" in source
    assert "no fetcher socket at" in source
