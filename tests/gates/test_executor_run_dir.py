# tests/gates/test_executor_run_dir.py
"""The executor's socket directory must serve two accounts (#18, found while #19 landed).

`executor_user` promised a kernel-enforced split on the Python startup path. It could not work.
`_prepare_run_dir` set the directory to 0700 and gave it to the child, so three things broke at
once. The agent could not traverse the directory to reach the socket. The supervisor could not
write its own state file or log inside it. The socket carried no group write bit, and `connect()`
needs one.

The fetcher hit the same defect and fixed it first. The executor keeps one difference: the
sticky bit. The agent owns this directory, so the sticky bit is what stops the agent from a
rename of the executor's socket. #36 is the same argument one directory higher.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.gates.executor import supervisor as supervisor_module
from nanoinfra.gates.executor.supervisor import (
    CHILD_UMASK,
    RUN_DIR_MODE,
    SHARED_RUN_DIR_MODE,
    ExecutorRuntime,
    _prepare_run_dir,
    _UserPlan,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_single_account_run_dir_stays_private(tmp_path: Path) -> None:
    """One account needs no group at all, so the directory shuts every other uid out."""
    socket_path = tmp_path / "run" / "executor.sock"

    run_dir = _prepare_run_dir(socket_path, plan=_UserPlan())

    assert _mode(run_dir) == RUN_DIR_MODE


def test_a_two_account_run_dir_lets_both_accounts_in(tmp_path: Path) -> None:
    """The child binds the socket here, and the supervisor writes its state file here."""
    socket_path = tmp_path / "run" / "executor.sock"

    run_dir = _prepare_run_dir(socket_path, plan=_UserPlan(uid=4242, gid=4242, enforced=True))

    assert _mode(run_dir) == SHARED_RUN_DIR_MODE
    assert _mode(run_dir) & stat.S_ISVTX, "the sticky bit stops the agent renaming the socket"
    assert _mode(run_dir) & stat.S_IRWXG, "the agent reaches the socket through its group"


def test_the_supervisor_keeps_the_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory the child owned would let the executor rewrite the supervisor's own state."""
    socket_path = tmp_path / "run" / "executor.sock"
    calls: list[Any] = []
    monkeypatch.setattr(os, "chown", lambda *args, **_kwargs: calls.append(args))

    _prepare_run_dir(socket_path, plan=_UserPlan(uid=4242, gid=4242, enforced=True))

    assert calls == []


def test_a_two_account_child_shares_the_agent_group_and_a_group_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket needs a group write bit, because the agent connects to it.

    The child creates the socket, so the child's umask decides its mode. A default umask would
    write 0755 and refuse every connect from the agent.
    """
    monkeypatch.setattr(
        supervisor_module,
        "_resolve_user",
        lambda _user: _UserPlan(name="nanoinfra-exec", uid=4242, gid=4242, enforced=True),
    )
    runtime = ExecutorRuntime(
        socket_path=tmp_path / "run" / "executor.sock", user="nanoinfra-exec"
    )

    kwargs = runtime._popen_platform_kwargs()

    assert kwargs["user"] == 4242
    assert kwargs["group"] == 4242
    assert kwargs["extra_groups"] == [os.getgid()]
    assert kwargs["umask"] == CHILD_UMASK


def test_a_single_account_child_changes_no_account(tmp_path: Path) -> None:
    """With one account there is nothing to share, and a umask here would help nobody."""
    runtime = ExecutorRuntime(socket_path=tmp_path / "run" / "executor.sock", user=None)

    kwargs = runtime._popen_platform_kwargs()

    for key in ("user", "group", "extra_groups", "umask"):
        assert key not in kwargs


def test_a_socket_the_agent_may_not_unlink_does_not_stop_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sticky bit refuses the agent's unlink, and the child clears its own socket at bind.

    A PermissionError here used to end the start. The stale entry is the child's to remove, so
    the supervisor must record the refusal and continue.
    """
    socket_path = tmp_path / "run" / "executor.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()

    def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(Path, "unlink", _refuse)

    supervisor_module._clear_stale_socket(socket_path)
