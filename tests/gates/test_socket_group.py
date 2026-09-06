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

from nanoinfra.gates.socket_group import (
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


def test_every_module_that_binds_a_socket_applies_the_group() -> None:
    """Discovered rather than listed, because a list is what missed two of them.

    The first version of this test named the three executor modules and passed while the fetcher
    and the MCP host still lost their group on every restart -- which took web_search and
    web_fetch down with an EACCES the agent reported as "the fetcher is not reachable". So the
    subjects come from the source tree: any module under gates/ that binds a Unix socket has to
    set the group on it.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2] / "nanoinfra" / "gates"
    binders: list[_Path] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if ".bind(str(" in text or "start_unix_server(" in text:
            binders.append(path)

    assert binders, "no module binds a socket, which cannot be right"
    missing = [
        path.relative_to(root).as_posix()
        for path in binders
        if "apply_socket_group(" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"these bind a socket and never set its group: {missing}"


def test_the_entrypoint_hands_a_group_name_to_every_helper() -> None:
    """One variable per helper, and never one shared name.

    A member of the executor's group reaches the executor's socket, and from there a command on
    every inventory host. So the fetcher and the MCP host get groups of their own, which the
    agent belongs to and the other helpers do not.
    """
    text = (Path(__file__).resolve().parents[2] / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'export NANOINFRA_SOCKET_GROUP="$ipc_group"' in text
    assert 'export NANOINFRA_OPERATOR_SOCKET_GROUP="$op_group"' in text
    # The resolved group, not the raw name: an image built before a helper's group existed runs
    # that helper as the agent, and the resolver already answers that case.
    assert 'export NANOINFRA_FETCHER_SOCKET_GROUP="$fetch_run_group"' in text
    assert 'export NANOINFRA_MCP_HOST_SOCKET_GROUP="$mcp_host_run_group"' in text
    # And it clears each previous run's socket, so its own waits cannot return on a stale file.
    assert 'rm -f "$socket_path" "$scrub_socket_path" "$op_socket_path"' in text
    assert 'rm -f "$fetch_socket_path"' in text
    assert 'rm -f "$mcp_host_socket_path"' in text


# --- the mode is the half a connect() needs ---------------------------------------------


def test_the_mode_is_set_even_when_the_group_cannot_be(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed chown must not take the chmod with it.

    The two calls fail for different reasons, and only one of them is what a peer needs. A
    non-root process may set a file's group only to a group it belongs to, so the chown returns
    EPERM in any layout where the socket's creator is not a member of the target group -- which
    was the operator socket's situation on every containerized boot for as long as the group
    existed. In one try block the EPERM skipped the chmod, and the chmod is what grants the group
    its write bit.
    """
    path = tmp_path / "helper.sock"
    listener = _bound(path)
    try:
        path.chmod(0o600)
        name = _a_group_this_process_belongs_to()
        if name is None:
            pytest.skip("this process belongs to no second group")
        monkeypatch.setenv(SOCKET_GROUP_ENV, name)

        def _refuse(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("nanoinfra.gates.socket_group.os.chown", _refuse)

        apply_socket_group(path)

        assert stat.S_IMODE(path.stat().st_mode) == SOCKET_MODE
    finally:
        listener.close()


def test_a_mode_that_cannot_be_set_is_logged_and_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket that works for one account beats no socket at all."""
    path = tmp_path / "helper.sock"
    listener = _bound(path)
    try:
        name = _a_group_this_process_belongs_to()
        if name is None:
            pytest.skip("this process belongs to no second group")
        monkeypatch.setenv(SOCKET_GROUP_ENV, name)

        def _refuse(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("nanoinfra.gates.socket_group.os.chmod", _refuse)

        apply_socket_group(path)  # must not raise
    finally:
        listener.close()


# --- which groups the module can actually set ---------------------------------------------


def test_the_executor_can_set_the_ipc_group_and_deliberately_not_the_operator_one() -> None:
    """The membership decides which sockets this module can actually set, and it is asymmetric.

    `apply_socket_group` works only where the socket's creator belongs to the target group: a
    non-root process may set a file's group only to one of its own. The executor is in
    `nanoinfra-ipc`, so the execute and scrub sockets get their group from the code that binds
    them -- the whole point, since the supervisor's chown can land on the previous run's file.

    It is **not** in `nanoinfra-op`, on purpose: it owns that socket so it needs no membership,
    and no other helper may hold that group or it could answer an approval for an action it asked
    for (`test_only_the_agent_joins_the_operator_group` states the same rule from the other side).
    The consequence is the point of this test: for the operator socket the group can only come
    from the **setgid directory**, which is why its mode order is load-bearing.

    Read from the Dockerfile rather than from `/etc/group`, because the test host is a single-uid
    machine with none of these accounts.
    """
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "--groups nanoinfra-ipc nanoinfra-exec" in dockerfile
    assert "--groups nanoinfra-op nanoinfra-exec" not in dockerfile
    # The agent is the peer on both, and holds both memberships.
    assert "--groups nanoinfra-ipc nanoinfra" in dockerfile
    assert "--groups nanoinfra-op nanoinfra" in dockerfile


# --- the setgid bit the entrypoint claims, and the order that keeps it -------------------


def test_every_socket_directory_is_made_setgid_before_it_changes_hands() -> None:
    """`chmod 2710` must precede the `chown` of the same directory, and the reason is a syscall.

    `chmod(2)`: without CAP_FSETID, S_ISGID is turned off when the file's group is not the
    caller's egid or one of its supplementary groups -- **and no error is returned**. The
    published compose file drops ALL capabilities and adds six; FSETID is not among them. So
    `chown` to a helper group followed by `chmod 2710` left every socket directory at 710,
    silently, with `chmod` exiting 0 -- which is why the `|| return 1` guards never fired and no
    log ever said so. Verified in the published 2.2.0 image: chown-then-chmod yields 710,
    chmod-then-chown yields 2710, and a socket bound inside then inherits the group.

    It matters most for the operator socket, where `apply_socket_group` cannot set the group at
    all: the executor deliberately does not join `nanoinfra-op` (it owns the socket, so it needs
    no membership, and no other helper may hold that group), which leaves the inherited group as
    the only non-racy mechanism.
    """
    lines = Path("entrypoint.sh").read_text(encoding="utf-8").splitlines()

    for directory in (
        "socket_dir",
        "op_socket_dir",
        "fetch_socket_dir",
        "mcp_host_socket_dir",
        "connector_host_socket_dir",
    ):
        # A chown may be split over a line continuation, so pair each `chmod 2710` with the next
        # `chown` that mentions the same directory rather than matching whole statements.
        events: list[tuple[int, str]] = []
        for number, line in enumerate(lines, 1):
            if f'"${directory}"' not in line:
                continue
            if "chmod 2710" in line:
                events.append((number, "setgid"))
            elif "chmod 700" in line:
                # The fallback branch for an image with no operator group. 700 has no setgid bit
                # to lose, so its order does not matter.
                events.append((number, "private"))
            elif line.lstrip().startswith("chown") or line.lstrip().startswith(f'"${directory}"'):
                events.append((number, "chown"))

        assert events, f"{directory}: no chmod/chown found at all"
        pending_setgid = False
        for number, kind in events:
            if kind == "setgid":
                pending_setgid = True
            elif kind == "chown" and not pending_setgid:
                # A chown with no setgid ahead of it in the same block is the losing order, unless
                # this directory was deliberately closed to 700 first.
                if not any(k == "private" for _, k in events if _ < number):
                    raise AssertionError(
                        f"{directory}: chown at line {number} runs before any chmod 2710, "
                        "so the setgid bit is dropped without an error"
                    )
