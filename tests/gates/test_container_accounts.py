# tests/gates/test_container_accounts.py
"""The container layout must not hand the fetcher a path to the executor (#19, #18).

#18 gives the executor its own account, and the socket directory carries `nanoinfra-ipc` so the
agent can connect. #19 gives the fetcher its own account. The fetcher then joined the same group,
which is the defect this file exists to stop: a member of the executor's IPC group can traverse
the executor's socket directory and connect to its socket.

The fetcher is the process that untrusted web content enters. A fetcher that reaches the executor
socket can run a command on every inventory host, which is the whole thing the split prevents.

So the two sockets need two groups. The agent belongs to both. Neither helper belongs to the
other's group.

The tests read the two files that build the layout. A container test needs a container, and these
properties live in text that a reader can check without one.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

AGENT_USER = "nanoinfra"
EXECUTOR_USER = "nanoinfra-exec"
FETCHER_USER = "nanoinfra-fetch"
MCP_HOST_USER = "nanoinfra-mcp"
EXECUTOR_GROUP = "nanoinfra-ipc"
FETCHER_GROUP = "nanoinfra-fetch-ipc"
MCP_HOST_GROUP = "nanoinfra-mcp-ipc"


def _dockerfile() -> str:
    """The Dockerfile with its line continuations joined, so one command reads as one line."""
    text = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    return re.sub(r"\\\n\s*", " ", text)


def _entrypoint() -> str:
    return (_ROOT / "entrypoint.sh").read_text(encoding="utf-8")


def _group_members(text: str, group: str) -> set[str]:
    """The accounts a usermod line adds to *group*."""
    pattern = re.compile(rf"usermod\s+--append\s+--groups\s+{re.escape(group)}\s+(\S+)")
    return set(pattern.findall(text))


def test_the_image_creates_a_fetcher_account() -> None:
    """Without the account the fetcher runs as the agent, so the split is organisational."""
    text = _dockerfile()

    assert FETCHER_USER in text
    assert re.search(rf"useradd\s+--system[^\n]*{re.escape(FETCHER_USER)}", text)


def test_the_fetcher_account_differs_from_the_executor_account() -> None:
    assert FETCHER_USER != EXECUTOR_USER
    uids = set(re.findall(r"--uid\s+(\d+)", _dockerfile()))
    assert len(uids) >= 3, "each helper needs its own uid, or the kernel enforces nothing"


def test_the_fetcher_never_joins_the_executor_group() -> None:
    """The acceptance property: no path from untrusted web content to a host command."""
    members = _group_members(_dockerfile(), EXECUTOR_GROUP)

    assert FETCHER_USER not in members
    assert members == {AGENT_USER, EXECUTOR_USER}


def test_the_executor_never_joins_the_fetcher_group() -> None:
    """The same argument in reverse. The executor has no business on the egress socket."""
    members = _group_members(_dockerfile(), FETCHER_GROUP)

    assert EXECUTOR_USER not in members
    assert members == {AGENT_USER, FETCHER_USER}


def test_the_entrypoint_gives_the_fetcher_socket_its_own_group() -> None:
    text = _entrypoint()

    assert f'fetch_ipc_group="{FETCHER_GROUP}"' in text
    assert 'chown "$fetch_run_user:$fetch_run_group" "$fetch_socket_dir"' in text


def test_the_entrypoint_never_falls_back_to_the_executor_group() -> None:
    """A missing group must degrade to the agent's own group, never to the executor's.

    An old image has no fetcher group. The fetcher then runs as the agent, and the agent's own
    group is the right owner. The executor's group would reopen the path this file closes.
    """
    text = _entrypoint()
    fetcher_block = text[text.index("resolve_fetcher_user()") : text.index("start_fetcher()")]

    assert "$ipc_group" not in fetcher_block


def test_each_helper_group_holds_the_agent_and_one_helper() -> None:
    """The agent reaches every socket. No helper reaches another helper's socket.

    The MCP host runs a program that a config in the agent's reach names, so a path from there to
    the executor would hand a configured command the credentials and the inventory hosts.
    """
    text = _dockerfile()

    assert _group_members(text, EXECUTOR_GROUP) == {AGENT_USER, EXECUTOR_USER}
    assert _group_members(text, FETCHER_GROUP) == {AGENT_USER, FETCHER_USER}
    assert _group_members(text, MCP_HOST_GROUP) == {AGENT_USER, MCP_HOST_USER}


def test_the_entrypoint_never_runs_two_helpers_on_one_account() -> None:
    """Two helpers on one uid can ptrace each other, so the split would be a comment."""
    text = _entrypoint()

    assert 'mcp_host_user="nanoinfra-mcp"' in text
    assert 'fetch_user="nanoinfra-fetch"' in text
    assert '[ "$mcp_host_run_user" = "$exec_user" ]' in text
    assert '[ "$fetch_run_user" = "$exec_user" ]' in text
