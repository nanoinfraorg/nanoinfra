# tests/gates/test_mcp_host_startup.py
"""Something has to start the MCP host (#22, the wiring half).

#22 built the host, its wire, and its supervisor. A helper process nothing starts is a package,
so this file covers the two start paths the executor and the fetcher already have: the Python
gateway for every install, and `entrypoint.sh` for the container.

Two rules the container start must keep. The host runs stdio children from a config the agent can
edit, so it never runs as the executor account, which holds the plaintext credentials. And it gets
its own IPC group, because a member of the executor's group traverses the executor's socket
directory and connects to its socket.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanoinfra.cli import gateway_runtime
from nanoinfra.gates.mcp_host import supervisor
from nanoinfra.gates.mcp_host.client import SOCKET_ENV_VAR

_ENTRYPOINT = Path("entrypoint.sh")
_DOCKERFILE = Path("Dockerfile")


class _FakeProcess:
    """Stands in for the host handle the supervisor returns."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.pid = 5432
        self.stopped = 0

    def stop(self) -> bool:
        self.stopped += 1
        return True


def _server(**fields: Any) -> Any:
    base: dict[str, Any] = {"type": None, "command": None, "url": None}
    base.update(fields)
    return SimpleNamespace(**base)


def _config(servers: dict[str, Any] | None = None, workspace: Path | None = None) -> Any:
    """The two fields the host start reads. A real Config needs a provider and a bus."""
    return SimpleNamespace(
        tools=SimpleNamespace(mcp_servers=servers if servers is not None else {}),
        workspace_path=workspace or Path("/tmp/nanoinfra-workspace"),
    )


@pytest.fixture(autouse=True)
def _clear_host_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOCKET_ENV_VAR, raising=False)
    monkeypatch.delenv(gateway_runtime.MCP_HOST_EXTERNAL_ENV, raising=False)
    monkeypatch.delenv(gateway_runtime.MCP_HOST_USER_ENV, raising=False)


def _capture_start(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    def _start(**kwargs: Any) -> _FakeProcess:
        calls.append(kwargs)
        return _FakeProcess(Path(kwargs["socket_path"]))

    monkeypatch.setattr(supervisor, "start_mcp_host", _start)


# ------------------------------------------------------------------ the pip install path


def test_the_gateway_starts_the_host_and_names_its_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The client reads the environment. A guessed path reads as a host that does not run."""
    import os

    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    config = _config({"demo": _server(command="echo")}, workspace=tmp_path)

    handle = gateway_runtime._start_mcp_host_for_gateway(config)

    assert handle is not None
    assert len(calls) == 1
    assert calls[0]["workspace"] == tmp_path
    assert Path(calls[0]["socket_path"]).name == "mcp_host.sock"
    assert os.environ.get(SOCKET_ENV_VAR) == str(calls[0]["socket_path"])


def test_no_host_starts_without_a_stdio_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An HTTP-only install needs no host, and a process nothing uses is one more thing to break."""
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    config = _config({"remote": _server(type="http", url="https://example.test/mcp")}, tmp_path)

    handle = gateway_runtime._start_mcp_host_for_gateway(config)

    assert handle is None
    assert calls == []


def test_a_server_with_a_command_and_no_type_counts_as_stdio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The config allows an omitted type, and the runtime already reads a command as stdio."""
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)

    handle = gateway_runtime._start_mcp_host_for_gateway(
        _config({"demo": _server(command="uvx")}, tmp_path)
    )

    assert handle is not None
    assert len(calls) == 1


def test_an_external_host_stops_a_second_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A container already started the host on its own account.

    A second host would hold the agent's uid, and that undoes the split the first one claims.
    """
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(gateway_runtime.MCP_HOST_EXTERNAL_ENV, "1")
    monkeypatch.setenv(SOCKET_ENV_VAR, "/run/nanoinfra-mcp/mcp_host.sock")

    handle = gateway_runtime._start_mcp_host_for_gateway(
        _config({"demo": _server(command="echo")}, tmp_path)
    )

    assert handle is None
    assert calls == []


def test_the_configured_account_reaches_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    _capture_start(monkeypatch, calls)
    monkeypatch.setenv(gateway_runtime.MCP_HOST_USER_ENV, "nanoinfra-mcp")

    gateway_runtime._start_mcp_host_for_gateway(_config({"demo": _server(command="echo")}, tmp_path))

    assert calls[0]["user"] == "nanoinfra-mcp"


def test_a_failed_host_start_is_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A silent failure reads as an MCP server that answers nothing for no reason."""
    from nanoinfra.gates.mcp_host.supervisor import MCPHostStartError

    def _fail(**_kwargs: Any) -> None:
        raise MCPHostStartError("the socket path is too long")

    monkeypatch.setattr(supervisor, "start_mcp_host", _fail)
    printed: list[str] = []
    monkeypatch.setattr(
        gateway_runtime.console, "print", lambda text, *_a, **_k: printed.append(str(text))
    )

    handle = gateway_runtime._start_mcp_host_for_gateway(
        _config({"demo": _server(command="echo")}, tmp_path)
    )

    assert handle is None
    assert any("too long" in line for line in printed)


def test_the_gateway_stops_the_host(tmp_path: Path) -> None:
    process = _FakeProcess(tmp_path / "mcp_host.sock")

    gateway_runtime._stop_mcp_host(process)

    assert process.stopped == 1


def test_a_stop_without_a_host_does_nothing() -> None:
    gateway_runtime._stop_mcp_host(None)


def test_the_gateway_wires_the_start_and_the_stop() -> None:
    """A helper nothing calls is dead code, so the runtime source must name both halves."""
    import inspect

    source = inspect.getsource(gateway_runtime)

    assert "_start_mcp_host_for_gateway(config)" in source
    assert "_stop_mcp_host(mcp_host)" in source


# ------------------------------------------------------------------ the container path


def test_the_container_starts_the_host_module() -> None:
    text = _ENTRYPOINT.read_text(encoding="utf-8")

    assert "nanoinfra.gates.mcp_host" in text
    assert "start_mcp_host()" in text


def test_the_container_never_runs_the_host_as_the_executor() -> None:
    """The host runs a command from a config the agent edits. That account reads no credential."""
    text = _ENTRYPOINT.read_text(encoding="utf-8")
    block = text[text.index("resolve_mcp_host_user()") : text.index("start_mcp_host()")]

    assert '"$exec_user"' in block, "the resolver must check the executor account by name"


def test_the_container_gives_the_host_its_own_group() -> None:
    """The executor's group is a path from a stdio MCP child to a command on every host."""
    text = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'mcp_host_ipc_group="nanoinfra-mcp-ipc"' in text
    assert 'chown "$mcp_host_run_user:$mcp_host_run_group" "$mcp_host_socket_dir"' in text


def test_the_container_hands_the_socket_path_to_the_agent() -> None:
    text = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'export NANOINFRA_MCP_HOST_SOCKET="$mcp_host_socket_path"' in text
    assert "export NANOINFRA_MCP_HOST_EXTERNAL=1" in text


def test_the_image_creates_the_host_account() -> None:
    import re

    text = re.sub(r"\\\n\s*", " ", _DOCKERFILE.read_text(encoding="utf-8"))

    assert re.search(r"useradd\s+--system[^\n]*nanoinfra-mcp\b", text)
    assert "groupadd --system nanoinfra-mcp-ipc" in text
