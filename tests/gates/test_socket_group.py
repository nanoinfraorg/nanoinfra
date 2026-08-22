# tests/gates/test_socket_group.py
"""The group on a helper's socket, set by the process that binds it.

The supervisor used to do this, and it could not hold: on a container restart the previous run's
socket file is still in place, so the supervisor's wait returns on that stale file and its chown
lands on something the executor unlinks a moment later. The fresh socket then carried the
executor's own group, the agent was refused, and nothing ran again to correct it.

The symptom was quiet in the worst way: every persisted transcript read "[nanoinfra withheld this
text]" because the scrub socket was unreachable, and remote actions failed on the execute socket.
"""

from __future__ import annotations

import grp
import os
import socket
import stat
from pathlib import Path

import pytest

from nanoinfra.gates.executor.socket_group import (
    SOCKET_GROUP_ENV,
    SOCKET_MODE,
    apply_socket_group,
)


def _bound(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    return listener


def _a_group_this_process_belongs_to() -> str | None:
    for gid in os.getgroups():
        try:
            name = grp.getgrgid(gid).gr_name
        except KeyError:
            continue
        if gid != os.getgid():
            return name
    return None


def test_an_unset_variable_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every single-uid host takes this path, and its socket must keep its own group."""
    monkeypatch.delenv(SOCKET_GROUP_ENV, raising=False)
    path = tmp_path / "s.sock"
    with _bound(path):
        before = path.stat()
        apply_socket_group(path)
        assert path.stat().st_gid == before.st_gid
        assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(before.st_mode)


def test_a_group_that_does_not_exist_is_survivable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket that works for one account beats refusing to serve at all."""
    monkeypatch.setenv(SOCKET_GROUP_ENV, "nanoinfra-group-that-is-not-here")
    path = tmp_path / "s.sock"
    with _bound(path):
        apply_socket_group(path)  # must not raise
        assert path.exists()


@pytest.mark.skipif(
    _a_group_this_process_belongs_to() is None,
    reason="this process belongs to no supplementary group to test with",
)
def test_the_socket_takes_the_named_group_and_the_mode_a_connect_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _a_group_this_process_belongs_to()
    assert name is not None
    monkeypatch.setenv(SOCKET_GROUP_ENV, name)
    path = tmp_path / "s.sock"
    with _bound(path):
        apply_socket_group(path)

        assert grp.getgrgid(path.stat().st_gid).gr_name == name
        # The write bit is the point: connect() on a Unix socket needs it.
        assert stat.S_IMODE(path.stat().st_mode) == SOCKET_MODE
        assert stat.S_IMODE(path.stat().st_mode) & stat.S_IWGRP


def test_every_executor_socket_applies_it() -> None:
    """Three sockets the agent connects to, and one of them being missed is the whole bug."""
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2] / "nanoinfra" / "gates" / "executor"
    for module in ("server.py", "scrub.py", "operator_socket.py"):
        text = (root / module).read_text(encoding="utf-8")
        assert "apply_socket_group(" in text, f"{module} binds a socket and never sets its group"


def test_the_entrypoint_hands_the_group_names_to_the_child() -> None:
    text = (Path(__file__).resolve().parents[2] / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'export NANOINFRA_SOCKET_GROUP="$ipc_group"' in text
    assert 'export NANOINFRA_OPERATOR_SOCKET_GROUP="$op_group"' in text
    # And it clears the previous run's sockets, so its own wait cannot return on a stale file.
    assert 'rm -f "$socket_path" "$scrub_socket_path" "$op_socket_path"' in text
