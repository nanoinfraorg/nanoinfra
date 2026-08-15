"""Start, stop, and check the executor child process -- nanoinfraorg/nanoinfra#18.

The agent must not start this child. An agent that can spawn the executor needs exec rights, and
then the mechanism that makes the split also undoes it. So this module accepts no command, no
argv, and no callable from a caller. It spawns one fixed entry point,
``python -m nanoinfra.gates.executor``, and nothing else.

Process control comes from :mod:`nanoinfra.process_runtime`. That module already keeps a state
file under a filelock, terminates a process group on POSIX, and handles Windows. A second process
model would drift from it, so this module only adds what the executor needs: a Unix socket
readiness wait, a private run directory, and the account of the child.

``user`` runs the child under another account. Two processes under one uid get no separation from
the kernel, because one can ptrace the other and read its memory. So the parameter stays even
where the platform or the caller's privilege cannot honour it. In that case the log says plainly
that the split is organisational.

The child also starts under a confinement layer (#20). ``nanoinfra/gates/confinement.py`` builds it.
A TCP port allowlist fits the executor badly, because contact with inventory hosts is its purpose,
and such an allowlist would equal the inventory. So the executor's layer confines its filesystem
and its exec surface instead, and it refuses a TCP listener.
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

from nanoinfra.gates.confinement import EXECUTOR_ROLE, LAYER_NONE, plan_child
from nanoinfra.process_runtime import (
    ManagedProcessRuntime,
    ProcessRuntimePaths,
    ProcessStartOptions,
)

logger = logging.getLogger(__name__)

EXECUTOR_MODULE = "nanoinfra.gates.executor"

DEFAULT_START_TIMEOUT_S = 15.0

# The kernel copies a socket path into sun_path, which holds 108 bytes on Linux and 104 on macOS.
# A longer path fails at bind with a message that hides the cause, so refuse it here instead.
MAX_SOCKET_PATH_BYTES = 100

# 0o700 keeps every other local uid out of the directory that holds the socket. A socket's own
# mode is not honoured on every platform, so the directory is the control that works everywhere.
RUN_DIR_MODE = 0o700

# The mode for a kernel-enforced split, where two accounts must both reach one directory.
#
#   owner rwx  the supervisor writes the state file and the log.
#   group rwx  the child creates the socket, and the agent connects to it.
#   other ---  every other local account stays out, the same as RUN_DIR_MODE.
#   setgid     each new entry inherits the group, so the socket carries a group the agent holds.
#   sticky     neither account removes the other's entries. This is the control that matters here:
#              the executor is the authority (#18), so the agent must not replace its socket.
#
# The group is the supervisor's own group, and the spawn adds that group to the child. A host that
# runs the agent under a shared primary group therefore opens this directory to that group. That is
# wider than 0o700, and it applies only where a deployment asked for two accounts.
SHARED_RUN_DIR_MODE = 0o3770

# connect() on a Unix socket needs write access to the socket file. The child creates that file, so
# the child's umask decides the mode. 0o007 keeps the group write bit and refuses every other
# account.
CHILD_UMASK = 0o007

_POLL_INTERVAL_S = 0.02
_PROBE_TIMEOUT_S = 0.5
_STOP_TIMEOUT_S = 20


class ExecutorStartError(RuntimeError):
    """The executor did not reach a state where a caller can connect."""


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
class ExecutorStartOptions(ProcessStartOptions):
    """Options for one executor child.

    ``port`` stays 0. The shared options carry a TCP port, and the executor has none: a TCP
    listener would widen an egress policy to include our own executor.
    """

    port: int = 0
    socket_path: str


class ExecutorRuntime(ManagedProcessRuntime[ExecutorStartOptions]):
    """Control one executor child through the shared process runtime.

    The command is a constant in :meth:`_build_child_command`. No caller reaches it.
    """

    service_name = "executor"

    def __init__(
        self, *, socket_path: Path, user: str | None = None, workspace: Path | None = None
    ) -> None:
        # The runtime keeps its state file, its lock, and its log beside the socket. One private
        # directory per executor means two instances cannot read each other's state.
        super().__init__(paths=_runtime_paths(socket_path), popen=self._spawn)
        self.socket_path = socket_path
        self.user_plan = _resolve_user(user)
        # The confinement plan needs the workspace, and the argv builder reads it from the options.
        # A constructor parameter keeps the two independent of call order.
        self.workspace = workspace
        self._child: subprocess.Popen[Any] | None = None

    def _build_child_command(self, options: ExecutorStartOptions) -> list[str]:
        """Return the fixed argv for the executor.

        The argv holds a constant module name plus two paths. So a caller of this class still has
        no way to run a program of its choice.
        """
        workspace = options.workspace
        if not workspace:
            raise ExecutorStartError("the executor needs a workspace path")
        return [
            self.python_executable,
            "-m",
            EXECUTOR_MODULE,
            "--socket",
            options.socket_path,
            "--workspace",
            workspace,
        ]

    def _spawn(self, command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        """Start the child and keep the handle.

        The handle matters because the executor is a direct child. An exit leaves a zombie until
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
            # The agent connects to the socket the child creates, so both accounts need one
            # shared group and a umask that keeps the group write bit. Without these two the
            # executor binds a socket that refuses every connect from the agent.
            kwargs["extra_groups"] = [os.getgid()]
            kwargs["umask"] = CHILD_UMASK
        _add_confinement(kwargs, socket_path=self.socket_path, workspace=self.workspace)
        return kwargs

    def _is_pid_running(self, pid: int) -> bool:
        child = self._child
        if child is not None and child.pid == pid and child.poll() is not None:
            # poll() reaps the child. Without the reap, signal 0 reports a dead executor as live.
            return False
        return super()._is_pid_running(pid)


class ExecutorProcess:
    """A handle on one executor child."""

    def __init__(self, *, runtime: ExecutorRuntime, socket_path: Path) -> None:
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

        The result is True when no child runs at the end, so a second call is safe. A caller that
        stops twice wants the same state both times.
        """
        self._runtime.stop(timeout_s=timeout_s)
        stopped = not self._runtime.status().running
        if stopped:
            # A killed executor cannot unlink its own socket, and a stale socket file makes the
            # next bind fail. So clear the path once the child is gone.
            _clear_stale_socket(self._socket_path)
        return stopped

    def read_log_tail(self, *, tail: int = 40) -> list[str]:
        """Return the last lines the child wrote."""
        return self._runtime.read_log_tail(tail=tail)


def start_executor(
    *,
    socket_path: Path,
    workspace: Path,
    user: str | None = None,
    timeout_s: float = DEFAULT_START_TIMEOUT_S,
) -> ExecutorProcess:
    """Start the executor and return a handle a caller can connect through.

    The call returns only after one connect to *socket_path* succeeds. So a caller that holds a
    handle can reach the executor. A child that exits first raises, and the message carries the
    child's last output.
    """
    socket_path = Path(socket_path)
    workspace = Path(workspace)
    _check_socket_path(socket_path)

    runtime = ExecutorRuntime(socket_path=socket_path, user=user, workspace=workspace)
    _prepare_run_dir(socket_path, plan=runtime.user_plan)
    if not runtime.status().running:
        # No executor holds this path, so any socket file here is a leftover from a killed child.
        _clear_stale_socket(socket_path)

    options = ExecutorStartOptions(
        socket_path=str(socket_path),
        workspace=str(workspace),
    )
    try:
        result = runtime.start_background(options)
    except subprocess.SubprocessError as exc:
        # A refused confinement lands here. CPython reports one fixed sentence for any failure of a
        # preexec callable, so the reason sits in the child's log and _hint quotes it.
        raise ExecutorStartError(
            f"the executor did not start under its confinement ({exc}). {_hint(runtime)}"
        ) from exc
    if not result.ok:
        raise ExecutorStartError(f"the executor did not start ({result.message}). {_hint(runtime)}")

    try:
        _wait_for_socket(
            socket_path=socket_path,
            is_alive=lambda: runtime.status().running,
            timeout_s=timeout_s,
        )
    except ExecutorStartError as exc:
        # A half started child must not outlive this call. An orphan would hold the socket and
        # block the next start.
        runtime.stop(timeout_s=5)
        _clear_stale_socket(socket_path)
        raise ExecutorStartError(f"{exc}. {_hint(runtime)}") from exc

    return ExecutorProcess(runtime=runtime, socket_path=socket_path)


def _add_confinement(
    kwargs: dict[str, Any], *, socket_path: Path, workspace: Path | None
) -> None:
    """Put the confinement of the child into the spawn arguments (#20).

    The rules apply in the child after the fork and before the exec. So the argv stays the fixed
    module entry point, and the layer still governs the process that serves.

    A host with no Landlock support gets a warning and a start. A host that has Landlock and then
    rejects the ruleset gets a refusal, and :func:`start_executor` reports it.
    """
    decision = plan_child(EXECUTOR_ROLE, run_dir=socket_path.parent, workspace=workspace)
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
        raise ExecutorStartError(
            f"socket path is {length} bytes, above the {MAX_SOCKET_PATH_BYTES} bytes a Unix "
            f"socket accepts: {socket_path}"
        )


def _prepare_run_dir(socket_path: Path, *, plan: _UserPlan | None = None) -> Path:
    """Make the directory that holds the socket private to the accounts that need it.

    One account needs one mode. Two accounts need a shared directory, because the child creates
    the socket in it and the supervisor writes the state file and the log in it.

    The supervisor keeps the directory in both cases, and the sticky bit carries the property that
    matters: the agent cannot rename or remove the executor's socket. A directory the child owned
    would take the state file and the log away from the supervisor instead.
    """
    plan = plan or _UserPlan()
    run_dir = socket_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    # mkdir applies the umask, and a directory that already exists may be wider still. chmod makes
    # the mode exact rather than a hope.
    run_dir.chmod(SHARED_RUN_DIR_MODE if plan.enforced else RUN_DIR_MODE)
    return run_dir


def _clear_stale_socket(socket_path: Path) -> None:
    """Remove a socket no child holds, and tolerate a refusal.

    The sticky bit on a two-account run directory refuses this unlink, because the socket belongs
    to the executor account. That refusal is the property working as intended, and the child
    removes its own socket before it binds. So a refusal must not end the start.
    """
    try:
        socket_path.unlink(missing_ok=True)
    except PermissionError:
        logger.debug("the executor socket at %s belongs to the child, so it stays", socket_path)


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
            raise ExecutorStartError(
                f"the executor exited before it opened {socket_path}"
            )
        if time.monotonic() >= deadline:
            raise ExecutorStartError(
                f"the executor did not open {socket_path} within {timeout_s:g}s"
            )
        time.sleep(_POLL_INTERVAL_S)


def _socket_accepts_connection(socket_path: Path) -> bool:
    """Report whether one connect to *socket_path* succeeds.

    bind() creates the path before listen() accepts a peer. So existence alone would hand back a
    handle that a caller cannot use. The probe connects and closes at once, and the protocol
    reader already treats a peer that hangs up as a refusal.
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
            "The executor runs under the supervisor's own account. The split is organisational, "
            "and the kernel does not enforce it. Pass user= to get a separate uid."
        )
        return _UserPlan()

    if os.name == "nt":
        logger.warning(
            "This platform cannot start the executor as %r. The executor shares the supervisor's "
            "uid, so the split is organisational and not enforced.",
            user,
        )
        return _UserPlan(name=user)

    import pwd

    try:
        entry = pwd.getpwnam(user)
    except KeyError as exc:
        # A named account that does not exist is a bad argument, not a limit of the platform. A
        # fallback here would start the executor under the wrong uid without a word.
        raise ExecutorStartError(f"account {user!r} does not exist on this host") from exc

    euid = os.geteuid()
    if entry.pw_uid == euid:
        logger.warning(
            "The executor account %r is the supervisor's own uid (%d). One process can ptrace the "
            "other, so the split is organisational and not enforced.",
            user,
            euid,
        )
        return _UserPlan(name=user, uid=entry.pw_uid, gid=entry.pw_gid)
    if euid != 0:
        logger.warning(
            "No privilege to start the executor as %r: euid %d is not root. The executor shares "
            "the supervisor's uid, so the split is organisational and not enforced.",
            user,
            euid,
        )
        return _UserPlan(name=user, uid=entry.pw_uid, gid=entry.pw_gid)

    logger.info("The executor starts as %r (uid %d, gid %d).", user, entry.pw_uid, entry.pw_gid)
    return _UserPlan(name=user, uid=entry.pw_uid, gid=entry.pw_gid, enforced=True)


def _hint(runtime: ExecutorRuntime, *, tail: int = 20) -> str:
    """Quote the child's last output, so a failure names its own cause."""
    lines = runtime.read_log_tail(tail=tail)
    if not lines:
        return f"The log {runtime.paths.log_path} holds no output."
    body = "\n".join(lines)
    return f"Last output in {runtime.paths.log_path}:\n{body}"


__all__ = [
    "DEFAULT_START_TIMEOUT_S",
    "EXECUTOR_MODULE",
    "CHILD_UMASK",
    "MAX_SOCKET_PATH_BYTES",
    "RUN_DIR_MODE",
    "SHARED_RUN_DIR_MODE",
    "ExecutorProcess",
    "ExecutorRuntime",
    "ExecutorStartError",
    "ExecutorStartOptions",
    "start_executor",
]
