"""Start the connector host, and keep the argv a constant (#195, part 4).

The same shape as the MCP host's supervisor, deliberately: one process model means one set of
mistakes rather than two. This module accepts no command and no argv -- the child is a fixed module
entry point plus three paths -- because a supervisor that took a command would be the mechanism that
undoes the split it exists to create.

Two things differ from the MCP host's, and both are reasons rather than names:

- **The account is the executor's peer, not the agent's.** The socket group does not include the
  agent (`CONNECTOR_HOST_SOCKET_GROUP_ENV`): a connector call originates in the executor *after*
  the gate answered, so nothing in the process the model steers has a reason to reach it.
- **The confinement grants read on `connectors/` and not on the workspace.** The workspace also
  holds the credential store, and the host must not reach it -- the whole point of moving the call
  out of the executor is that a stranger's request is made by a process that holds no secrets.
"""

from __future__ import annotations

import logging
import os
import socket
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanoinfra.gates.confinement import CONNECTOR_HOST_ROLE, LAYER_NONE, plan_child
from nanoinfra.process_runtime import (
    ManagedProcessRuntime,
    ProcessRuntimePaths,
    ProcessStartOptions,
)

logger = logging.getLogger(__name__)

CONNECTOR_HOST_MODULE = "nanoinfra.gates.connector_host"

DEFAULT_START_TIMEOUT_S = 15.0

# The kernel copies a socket path into sun_path, which holds 108 bytes on Linux and 104 on macOS.
# A longer path fails at bind with a message that hides the cause, so refuse it here instead.
MAX_SOCKET_PATH_BYTES = 100

# 0o700 keeps every other local uid out of the directory that holds the socket. A socket's own mode
# is not honoured on every platform, so the directory is the control that works everywhere.
RUN_DIR_MODE = 0o700

_POLL_INTERVAL_S = 0.02
_PROBE_TIMEOUT_S = 0.5
_STOP_TIMEOUT_S = 20


class ConnectorHostStartError(RuntimeError):
    """The connector host did not reach a state where a caller can connect."""


@dataclass(frozen=True)
class _UserPlan:
    """The account for the child, and whether the kernel enforces the split.

    ``enforced`` is False when the child shares the supervisor's uid. The caller then holds an
    organisational boundary, not one the kernel keeps.
    """

    name: str | None = None
    uid: int | None = None
    gid: int | None = None
    enforced: bool = False


@dataclass(frozen=True, kw_only=True)
class ConnectorHostStartOptions(ProcessStartOptions):
    """Options for one connector host child.

    ``port`` stays 0. The shared options carry a TCP port, and the host has none: this is the
    process that starts programs, and a TCP listener on it would let anything on the network ask it
    to start one.
    """

    port: int = 0
    socket_path: str


class ConnectorHostRuntime(ManagedProcessRuntime[ConnectorHostStartOptions]):
    """Control one connector host child through the shared process runtime.

    The command is a constant in :meth:`_build_child_command`. No caller reaches it.
    """

    service_name = "connector-host"

    def __init__(
        self, *, socket_path: Path, user: str | None = None, workspace: Path | None = None
    ) -> None:
        # The runtime keeps its state file, its lock, and its log beside the socket. One private
        # directory per host means two instances cannot read each other's state.
        super().__init__(paths=_runtime_paths(socket_path), popen=self._spawn)
        self.socket_path = socket_path
        self.user_plan = _resolve_user(user)
        # The confinement plan needs the workspace, and the argv builder reads it from the options.
        # A constructor parameter keeps the two independent of call order.
        self.workspace = workspace
        self._child: subprocess.Popen[Any] | None = None

    def _build_child_command(self, options: ConnectorHostStartOptions) -> list[str]:
        """Return the fixed argv for the connector host.

        The argv holds a constant module name plus three paths. So a caller of this class still
        has no way to run a program of its choice.

        The config path travels because the child otherwise falls back to
        ``~/.nanoinfra/config.json``: an instance started with ``--config`` ran its helper
        against another instance's settings. The Landlock rule already grants read on this
        exact file (``confinement._live_config_path``), so nothing widens.
        """
        from nanoinfra.config.loader import get_config_path

        workspace = options.workspace
        if not workspace:
            raise ConnectorHostStartError("the connector host needs a workspace path")
        return [
            self.python_executable,
            "-m",
            CONNECTOR_HOST_MODULE,
            "--socket",
            options.socket_path,
            "--workspace",
            workspace,
            "--config",
            str(get_config_path()),
        ]

    def _spawn(self, command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        """Start the child and keep the handle.

        The handle matters because the host is a direct child. An exit leaves a zombie until
        someone reaps it, and a zombie still answers signal 0.
        """
        child: subprocess.Popen[Any] = subprocess.Popen(command, **kwargs)
        self._child = child
        return child

    def _popen_platform_kwargs(self) -> dict[str, Any]:
        kwargs = super()._popen_platform_kwargs()
        plan = self.user_plan
        if plan.enforced and plan.uid is not None:
            # Popen changes the uid in the child after the fork. So no helper program such as su
            # or setpriv joins the command, and the argv stays a constant.
            kwargs["user"] = plan.uid
            if plan.gid is not None:
                kwargs["group"] = plan.gid
        _add_confinement(kwargs, socket_path=self.socket_path, workspace=self.workspace)
        return kwargs

    def _is_pid_running(self, pid: int) -> bool:
        child = self._child
        if child is not None and child.pid == pid and child.poll() is not None:
            # poll() reaps the child. Without the reap, signal 0 reports a dead host as live.
            return False
        return super()._is_pid_running(pid)


class ConnectorHostProcess:
    """A handle on one connector host child."""

    def __init__(self, *, runtime: ConnectorHostRuntime, socket_path: Path) -> None:
        self._runtime = runtime
        self._socket_path = socket_path

    @property
    def pid(self) -> int | None:
        """The child's pid, or None when no child runs."""
        return self._runtime.status().pid

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def log_path(self) -> Path:
        return self._runtime.paths.log_path

    @property
    def user_plan(self) -> _UserPlan:
        return self._runtime.user_plan

    def is_running(self) -> bool:
        """Report whether the child is alive."""
        return self._runtime.status().running

    def stop(self, *, timeout_s: int = _STOP_TIMEOUT_S) -> bool:
        """Stop the child and report whether it is gone.

        Every stdio MCP server the host started goes with the host. The kill of the process group
        does not reach those children, because the MCP SDK starts each one in a session of its own.
        Two other mechanisms end them. The host asks the kernel for a parent death signal on each
        child (#50), and a child that reads its stdin sees the end of file when the host dies.

        The result is True when no child runs at the end, so a second call is safe. A caller that
        stops twice wants the same state both times.
        """
        self._runtime.stop(timeout_s=timeout_s)
        stopped = not self._runtime.status().running
        if stopped:
            # A killed host cannot unlink its own socket, and a stale socket file makes the next
            # bind fail. So clear the path once the child is gone.
            self._socket_path.unlink(missing_ok=True)
        return stopped

    def read_log_tail(self, *, tail: int = 40) -> list[str]:
        """Return the last lines the child wrote."""
        return self._runtime.read_log_tail(tail=tail)


def start_connector_host(
    *,
    socket_path: Path,
    workspace: Path,
    user: str | None = None,
    timeout_s: float = DEFAULT_START_TIMEOUT_S,
) -> ConnectorHostProcess:
    """Start the connector host and return a handle a caller can connect through.

    The call returns only after one connect to *socket_path* succeeds. So a caller that holds a
    handle can reach the host. A child that exits first raises, and the message carries the child's
    last output.
    """
    socket_path = Path(socket_path)
    workspace = Path(workspace)
    _check_socket_path(socket_path)

    runtime = ConnectorHostRuntime(socket_path=socket_path, user=user, workspace=workspace)
    _prepare_run_dir(socket_path, plan=runtime.user_plan)
    if not runtime.status().running:
        # No host holds this path, so any socket file here is a leftover from a killed child.
        socket_path.unlink(missing_ok=True)

    options = ConnectorHostStartOptions(
        socket_path=str(socket_path),
        workspace=str(workspace),
    )
    try:
        result = runtime.start_background(options)
    except subprocess.SubprocessError as exc:
        # A refused confinement lands here. CPython reports one fixed sentence for any failure of a
        # preexec callable, so the reason sits in the child's log and _hint quotes it.
        raise ConnectorHostStartError(
            f"the connector host did not start under its confinement ({exc}). {_hint(runtime)}"
        ) from exc
    if not result.ok:
        raise ConnectorHostStartError(f"the connector host did not start ({result.message}). {_hint(runtime)}")

    try:
        _wait_for_socket(
            socket_path=socket_path,
            is_alive=lambda: runtime.status().running,
            timeout_s=timeout_s,
        )
    except ConnectorHostStartError as exc:
        # A half started child must not outlive this call. An orphan would hold the socket and
        # block the next start.
        runtime.stop(timeout_s=5)
        socket_path.unlink(missing_ok=True)
        raise ConnectorHostStartError(f"{exc}. {_hint(runtime)}") from exc

    return ConnectorHostProcess(runtime=runtime, socket_path=socket_path)


def _add_confinement(
    kwargs: dict[str, Any], *, socket_path: Path, workspace: Path | None
) -> None:
    """Put the confinement of the child into the spawn arguments (#20).

    The rules apply in the child after the fork and before the exec, so the argv stays the fixed
    module entry point. The layer grants read on the connector packages, outbound 443, and nothing
    else: the package format runs no code, so a host that could execute anything would hold a
    capability the format never uses.

    A host with no Landlock support gets a warning and a start. A host that has Landlock and then
    rejects the ruleset gets a refusal, and :func:`start_connector_host` reports it.
    """
    decision = plan_child(CONNECTOR_HOST_ROLE, run_dir=socket_path.parent, workspace=workspace)
    if decision.layer == LAYER_NONE:
        logger.warning("gates: %s", decision.summary())
        return
    logger.info("gates: %s", decision.summary())
    preexec = decision.preexec()
    if preexec is not None:
        kwargs["preexec_fn"] = preexec


def _runtime_paths(socket_path: Path) -> ProcessRuntimePaths:
    run_dir = socket_path.parent
    return ProcessRuntimePaths(
        run_dir=run_dir,
        logs_dir=run_dir,
        state_path=run_dir / f"{socket_path.name}.json",
        log_path=run_dir / f"{socket_path.name}.log",
    )


def _check_socket_path(socket_path: Path) -> None:
    length = len(str(socket_path).encode("utf-8"))
    if length > MAX_SOCKET_PATH_BYTES:
        raise ConnectorHostStartError(
            f"socket path is {length} bytes, above the {MAX_SOCKET_PATH_BYTES} bytes a Unix "
            f"socket accepts: {socket_path}"
        )


def _prepare_run_dir(socket_path: Path, *, plan: _UserPlan | None = None) -> Path:
    """Make the directory that holds the socket private to one account."""
    plan = plan or _UserPlan()
    run_dir = socket_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    # mkdir applies the umask, and a directory that already exists may be wider still. chmod makes
    # the mode exact rather than a hope.
    run_dir.chmod(RUN_DIR_MODE)
    if plan.enforced and plan.uid is not None:
        # The child binds the socket, so the child owns the directory. The mode still shuts out
        # every other local uid.
        os.chown(run_dir, plan.uid, plan.gid if plan.gid is not None else -1)
    return run_dir


def _wait_for_socket(
    *, socket_path: Path, is_alive: Callable[[], bool], timeout_s: float
) -> None:
    """Wait until one connect to *socket_path* succeeds, or raise.

    The wait is bounded, and a dead child ends it at once. A caller must never get a handle to a
    socket that refuses a connection.
    """
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        if _socket_accepts_connection(socket_path):
            return
        if not is_alive():
            raise ConnectorHostStartError(f"the connector host exited before it opened {socket_path}")
        if time.monotonic() >= deadline:
            raise ConnectorHostStartError(
                f"the connector host did not open {socket_path} within {timeout_s:g}s"
            )
        time.sleep(_POLL_INTERVAL_S)


def _socket_accepts_connection(socket_path: Path) -> bool:
    """Report whether one connect to *socket_path* succeeds.

    bind() creates the path before listen() accepts a peer. So existence alone would hand back a
    handle that a caller cannot use. The probe connects and closes at once, and the host reader
    already treats a peer that hangs up as the end of a connection.
    """
    try:
        if not stat.S_ISSOCK(socket_path.stat().st_mode):
            return False
    except OSError:
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(_PROBE_TIMEOUT_S)
        probe.connect(str(socket_path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _resolve_user(user: str | None) -> _UserPlan:
    """Work out the child's account, and say plainly what the kernel will enforce."""
    if user is None:
        logger.info(
            "The connector host runs under the supervisor's own account. The split is organisational, "
            "and the kernel does not enforce it. Pass user= to get a separate uid."
        )
        return _UserPlan()

    if os.name == "nt":
        logger.warning(
            "This platform cannot start the connector host as %r. The host shares the supervisor's uid, "
            "so the split is organisational and not enforced.",
            user,
        )
        return _UserPlan(name=user)

    import pwd

    try:
        entry = pwd.getpwnam(user)
    except KeyError as exc:
        # A named account that does not exist is a bad argument, not a limit of the platform. A
        # fallback here would start the host under the wrong uid without a word.
        raise ConnectorHostStartError(f"account {user!r} does not exist on this host") from exc

    euid = os.geteuid()
    if entry.pw_uid == euid:
        logger.warning(
            "The connector host account %r is the supervisor's own uid (%d). One process can ptrace the "
            "other, so the split is organisational and not enforced.",
            user,
            euid,
        )
        return _UserPlan(name=user, uid=entry.pw_uid, gid=entry.pw_gid)
    if euid != 0:
        logger.warning(
            "No privilege to start the connector host as %r: euid %d is not root. The host shares the "
            "supervisor's uid, so the split is organisational and not enforced.",
            user,
            euid,
        )
        return _UserPlan(name=user, uid=entry.pw_uid, gid=entry.pw_gid)

    logger.info("The connector host starts as %r (uid %d, gid %d).", user, entry.pw_uid, entry.pw_gid)
    return _UserPlan(name=user, uid=entry.pw_uid, gid=entry.pw_gid, enforced=True)


def _hint(runtime: ConnectorHostRuntime, *, tail: int = 20) -> str:
    """Quote the child's last output, so a failure names its own cause."""
    lines = runtime.read_log_tail(tail=tail)
    if not lines:
        return f"The log {runtime.paths.log_path} holds no output."
    body = "\n".join(lines)
    return f"Last output in {runtime.paths.log_path}:\n{body}"


__all__ = [
    "DEFAULT_START_TIMEOUT_S",
    "MAX_SOCKET_PATH_BYTES",
    "CONNECTOR_HOST_MODULE",
    "RUN_DIR_MODE",
    "ConnectorHostProcess",
    "ConnectorHostRuntime",
    "ConnectorHostStartError",
    "ConnectorHostStartOptions",
    "start_connector_host",
]
