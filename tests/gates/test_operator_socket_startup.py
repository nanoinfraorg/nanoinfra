# tests/gates/test_operator_socket_startup.py
"""The container must let an operator answer an approval (#38, #27).

#38 gave the executor a second socket, and #27 put the answerer in the WebUI. The container
prepared neither. `bind_operator_socket` creates its directory at mode 0700 under the executor
account, so the agent account could not traverse it, and the inbox reported degraded in every
Docker deployment.

The group here is the one place where this split gives something up, and it is deliberate. #27
answers from inside the gateway process, which runs as the agent account, so that account must
reach this socket. The filesystem half of the split therefore protects nothing on this one path.
Three things carry the rest: the answer still crosses a process boundary into the executor, the
executor still matches the actor against `gates.approvers`, and an AST import closure keeps every
tool module away from the client inside the process.

`nanoinfra-op` exists as its own group so that membership stays one line an operator can read. A
reuse of `nanoinfra-ipc` would have handed the same reach to the fetcher and the MCP host.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTRYPOINT = Path("entrypoint.sh")
_DOCKERFILE = Path("Dockerfile")

AGENT_USER = "nanoinfra"
EXECUTOR_USER = "nanoinfra-exec"
OPERATOR_GROUP = "nanoinfra-op"


def _entrypoint() -> str:
    return _ENTRYPOINT.read_text(encoding="utf-8")


def _dockerfile() -> str:
    """The Dockerfile with its line continuations joined, so one command reads as one line."""
    return re.sub(r"\\\n\s*", " ", _DOCKERFILE.read_text(encoding="utf-8"))


def test_the_entrypoint_names_the_operator_socket() -> None:
    text = _entrypoint()

    assert 'op_group="nanoinfra-op"' in text
    assert 'op_socket_dir="$socket_dir/operator"' in text
    assert 'op_socket_path="$op_socket_dir/executor.op.sock"' in text


def test_the_entrypoint_prepares_the_operator_directory_with_its_own_group() -> None:
    """The executor owns the directory, and the operator group traverses it.

    The same ownership direction as the execute socket: only a directory's writer can replace a
    socket node inside it, so the authority owns the directory and the answerer reaches through.
    """
    block = _entrypoint()
    block = block[block.index("prepare_executor_paths()") : block.index("start_executor()")]

    assert 'mkdir -p "$op_socket_dir"' in block
    assert 'chown "$exec_user:$op_group" "$op_socket_dir"' in block
    assert 'chmod 2710 "$op_socket_dir"' in block


def test_a_missing_operator_group_is_loud_and_closed() -> None:
    """An image without the group must close the directory and say what that costs.

    A silent 0700 directory reads to an operator as an inbox that is broken for no reason.
    """
    block = _entrypoint()
    block = block[block.index("prepare_executor_paths()") : block.index("start_executor()")]

    assert 'chmod 700 "$op_socket_dir"' in block
    assert "no approval can be answered" in block


def test_the_executor_child_learns_the_operator_socket_path() -> None:
    """The child binds it and the agent reads it, so the path cannot be a guess on either side."""
    text = _entrypoint()

    assert 'export NANOINFRA_OPERATOR_SOCKET="$op_socket_path"' in text
    exported_at = text.index('export NANOINFRA_OPERATOR_SOCKET="$op_socket_path"')
    started_at = text.index('start_executor "$workspace"')
    assert exported_at < started_at, "the child reads the variable at start, so it goes out first"


def test_the_start_reapplies_the_operator_socket_mode() -> None:
    """The executor creates the socket, and a rebind can narrow the mode.

    connect() needs the group write bit, so the root start puts it back while it still can.
    """
    text = _entrypoint()

    assert 'chmod 660 "$op_socket_path"' in text
    assert 'chown "$exec_user:$op_group" "$op_socket_dir" "$op_socket_path"' in text


def test_the_image_creates_the_operator_group() -> None:
    assert "groupadd --system nanoinfra-op" in _dockerfile()


def test_only_the_agent_joins_the_operator_group() -> None:
    """The executor owns the socket, so it needs no membership. No other helper may hold this.

    A fetcher or an MCP host inside this group could approve an action it asked for.
    """
    text = _dockerfile()
    members = set(
        re.findall(rf"usermod\s+--append\s+--groups\s+{re.escape(OPERATOR_GROUP)}\s+(\S+)", text)
    )

    assert members == {AGENT_USER}


def test_the_operator_group_never_reaches_the_other_sockets() -> None:
    """The group's only job is this one directory, so it must own nothing else."""
    text = _entrypoint()

    for variable in ('"$socket_dir"', '"$fetch_socket_dir"', '"$mcp_host_socket_dir"'):
        assert f'chown "$exec_user:$op_group" {variable}' not in text
