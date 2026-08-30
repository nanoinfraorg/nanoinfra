# tests/gates/test_mcp_host_supervisor.py
"""Item 20 (#22): the MCP host supervisor and the host module entry point.

The agent must not start this child. An agent that can spawn the host needs exec rights, and then
the mechanism that makes the split also undoes it. So the supervisor's public API takes no command,
no argv, and no callable, and the argv it builds is a constant plus two paths.

The signature scan is the load-bearing test here. A parameter named ``command``, or one annotated
``Callable``, would let a caller choose the program. The scan refuses both by name.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.config.loader import get_config_path
from nanoinfra.gates.mcp_host import __main__ as entry_point
from nanoinfra.gates.mcp_host import supervisor

# A caller must never hand the supervisor something that becomes a program to run.
FORBIDDEN_PARAMETER_NAMES = {
    "arg",
    "args",
    "argv",
    "cmd",
    "cmdline",
    "command",
    "entry_point",
    "entrypoint",
    "exe",
    "executable",
    "popen",
    "program",
    "script",
    "shell",
    "spawn",
}

FORBIDDEN_ANNOTATION_FRAGMENTS = ("Callable", "Popen", "Awaitable", "Coroutine")

POISON = (
    "import os, sys\n"
    "sys.stderr.write('MCP-HOST-TEST-POISON\\n')\n"
    "sys.stderr.flush()\n"
    "os._exit(9)\n"
)


def _poison_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make any new interpreter die at startup, so a child never reaches the socket."""
    poison_dir = tmp_path / "poison"
    poison_dir.mkdir()
    (poison_dir / "sitecustomize.py").write_text(POISON, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH", "")
    joined = f"{poison_dir}{os.pathsep}{existing}" if existing else str(poison_dir)
    monkeypatch.setenv("PYTHONPATH", joined)


def _public_callables() -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for name, value in vars(supervisor).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(value):
            found.append((f"{name}()", value))
        elif inspect.isclass(value) and value.__module__ == supervisor.__name__:
            for attr in dir(value):
                # __init__ is where an injected spawn hook would arrive, so it stays in the scan.
                # Other private members are not the API a caller holds.
                if attr.startswith("_") and attr != "__init__":
                    continue
                member = inspect.getattr_static(value, attr)
                if isinstance(member, (classmethod, staticmethod)):
                    member = member.__func__
                if inspect.isfunction(member):
                    found.append((f"{name}.{attr}()", member))
    return found


# ---------------------------------------------------------------- entry point


def test_entry_point_starts_the_server_with_parsed_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Any, Any]] = []

    module = type(sys)("nanoinfra.gates.mcp_host.server")

    def serve_forever(socket_path: Path, *, workspace: Path) -> None:
        calls.append((socket_path, workspace))

    module.serve_forever = serve_forever  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nanoinfra.gates.mcp_host.server", module)

    code = entry_point.main(
        ["--socket", str(tmp_path / "m.sock"), "--workspace", str(tmp_path / "ws")]
    )

    assert code == 0
    assert calls == [(tmp_path / "m.sock", tmp_path / "ws")]


def test_entry_point_requires_both_arguments() -> None:
    with pytest.raises(SystemExit):
        entry_point.main([])
    with pytest.raises(SystemExit):
        entry_point.main(["--socket", "/tmp/m.sock"])


# ------------------------------------------------- the agent cannot pick a command


def test_public_api_accepts_no_command_or_callable() -> None:
    offences: list[str] = []
    for label, function in _public_callables():
        signature = inspect.signature(function)
        for parameter in signature.parameters.values():
            if parameter.name.lower() in FORBIDDEN_PARAMETER_NAMES:
                offences.append(f"{label} takes {parameter.name}")
            annotation = str(parameter.annotation)
            for fragment in FORBIDDEN_ANNOTATION_FRAGMENTS:
                if fragment in annotation:
                    offences.append(f"{label} takes {parameter.name}: {annotation}")
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                offences.append(f"{label} takes *{parameter.name}")

    assert offences == []
    start = inspect.signature(supervisor.start_mcp_host).parameters
    assert {"socket_path", "workspace", "user"} <= set(start)
    assert start["user"].default is None


def test_child_command_is_the_fixed_module_entry_point(tmp_path: Path) -> None:
    socket_path = tmp_path / "m.sock"
    runtime = supervisor.MCPHostRuntime(socket_path=socket_path)
    options = supervisor.MCPHostStartOptions(
        socket_path=str(socket_path), workspace=str(tmp_path)
    )

    assert runtime._build_child_command(options) == [
        sys.executable,
        "-m",
        "nanoinfra.gates.mcp_host",
        "--socket",
        str(socket_path),
        "--workspace",
        str(tmp_path),
        "--config",
        str(get_config_path()),
    ]
    assert supervisor.MCP_HOST_MODULE == "nanoinfra.gates.mcp_host"


def test_the_child_needs_a_workspace_path(tmp_path: Path) -> None:
    """The entry point takes two paths, so a missing one is a bad start rather than a guess."""
    runtime = supervisor.MCPHostRuntime(socket_path=tmp_path / "m.sock")
    options = supervisor.MCPHostStartOptions(socket_path=str(tmp_path / "m.sock"), workspace="")

    with pytest.raises(supervisor.MCPHostStartError):
        runtime._build_child_command(options)


def test_the_host_has_no_tcp_port() -> None:
    """A TCP listener would let anything on the network ask the host to start a program."""
    options = supervisor.MCPHostStartOptions(socket_path="/tmp/m.sock", workspace="/tmp")

    assert options.port == 0


# ------------------------------------------------------------------- socket privacy


def test_run_dir_excludes_other_local_users(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o755)
    socket_path = run_dir / "m.sock"

    supervisor._prepare_run_dir(socket_path)

    mode = stat.S_IMODE(run_dir.stat().st_mode)
    assert mode == 0o700
    assert mode & 0o077 == 0


# --------------------------------------------------------------- readiness wait


def test_wait_for_socket_returns_when_the_socket_accepts_a_connection(tmp_path: Path) -> None:
    socket_path = tmp_path / "m.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        listener.listen(1)
        supervisor._wait_for_socket(socket_path=socket_path, is_alive=lambda: True, timeout_s=2.0)
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


def test_wait_for_socket_raises_when_the_child_dies_first(tmp_path: Path) -> None:
    socket_path = tmp_path / "m.sock"
    with pytest.raises(supervisor.MCPHostStartError) as caught:
        supervisor._wait_for_socket(socket_path=socket_path, is_alive=lambda: False, timeout_s=5.0)
    assert "exited" in str(caught.value)


def test_wait_for_socket_raises_when_the_socket_never_appears(tmp_path: Path) -> None:
    socket_path = tmp_path / "m.sock"
    with pytest.raises(supervisor.MCPHostStartError) as caught:
        supervisor._wait_for_socket(socket_path=socket_path, is_alive=lambda: True, timeout_s=0.2)
    assert "did not open" in str(caught.value)


# ------------------------------------------------------------- start and stop


def test_start_mcp_host_raises_when_the_child_dies_before_the_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _poison_child(monkeypatch, tmp_path)
    socket_path = tmp_path / "r" / "m.sock"

    with pytest.raises(supervisor.MCPHostStartError) as caught:
        supervisor.start_mcp_host(socket_path=socket_path, workspace=tmp_path, timeout_s=3.0)

    message = str(caught.value)
    assert "MCP-HOST-TEST-POISON" in message
    assert not socket_path.exists()
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700


def test_start_mcp_host_refuses_an_over_long_socket_path(tmp_path: Path) -> None:
    socket_path = tmp_path / ("s" * 120 + ".sock")
    with pytest.raises(supervisor.MCPHostStartError) as caught:
        supervisor.start_mcp_host(socket_path=socket_path, workspace=tmp_path)
    assert "bytes" in str(caught.value)


def test_stop_reports_true_when_no_child_runs(tmp_path: Path) -> None:
    socket_path = tmp_path / "m.sock"
    handle = supervisor.MCPHostProcess(
        runtime=supervisor.MCPHostRuntime(socket_path=socket_path),
        socket_path=socket_path,
    )

    assert handle.pid is None
    assert handle.is_running() is False
    assert handle.stop(timeout_s=1) is True
    assert handle.stop(timeout_s=1) is True


@pytest.mark.skipif(
    importlib.util.find_spec("nanoinfra.gates.mcp_host.server") is None,
    reason="server.py has not landed yet",
)
def test_start_mcp_host_serves_and_then_stops(tmp_path: Path) -> None:
    socket_path = tmp_path / "r" / "m.sock"
    handle = supervisor.start_mcp_host(
        socket_path=socket_path, workspace=tmp_path, timeout_s=30.0
    )
    try:
        assert handle.is_running() is True
        assert isinstance(handle.pid, int)
        assert stat.S_ISSOCK(socket_path.stat().st_mode)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(2.0)
        probe.connect(str(socket_path))
        probe.close()
    finally:
        assert handle.stop(timeout_s=5) is True
    assert handle.is_running() is False
    assert not socket_path.exists()


# ---------------------------------------------------------- the user parameter


def test_user_none_logs_that_the_split_is_organisational(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=supervisor.__name__):
        plan = supervisor._resolve_user(None)

    assert plan.enforced is False
    assert "organisational" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX accounts only")
@pytest.mark.skipif(os.geteuid() == 0, reason="root can honour the request")
def test_user_without_privilege_warns_that_the_split_is_not_enforced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=supervisor.__name__):
        plan = supervisor._resolve_user("root")

    assert plan.enforced is False
    assert plan.name == "root"
    assert "organisational" in caplog.text
    assert "not enforced" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX accounts only")
def test_unknown_user_raises_rather_than_degrades() -> None:
    with pytest.raises(supervisor.MCPHostStartError) as caught:
        supervisor._resolve_user("nanoinfra-no-such-account")
    assert "nanoinfra-no-such-account" in str(caught.value)
