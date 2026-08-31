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


def test_the_entrypoint_prepares_the_job_store_for_both_accounts() -> None:
    """The one directory both accounts write, and it was prepared for neither.

    The executor creates a server job and updates its output; the agent reconciles jobs a restart
    interrupted. It stayed the agent's alone, so the executor was refused on its own temp file and
    every remote action in a container failed *after* the gate permitted it.
    """
    text = _entrypoint()
    assert 'mkdir -p "$workspace/servers/jobs"' in text
    assert re.search(
        r'chown -R "\$agent_user:\$ipc_group" "\$workspace/servers/jobs"', text
    ), "the job store never gets a group both accounts belong to"
    # setgid plus group write: a file either side creates has to stay writable by the other.
    assert 'chmod 2770 "$workspace/servers/jobs"' in text
    assert re.search(r'find "\$workspace/servers/jobs" -type f -exec chmod 660', text)


def test_the_agent_runs_with_the_group_writable_umask() -> None:
    """The default 022 would make a 644 file in that setgid directory, and the other account's
    next write would fail depending on which side wrote first. The executor already sets this."""
    text = _entrypoint()
    agent_exec = text.index("exec env -u NANOINFRA_SECRETS_KEY")
    assert "umask 0007" in text[max(0, agent_exec - 600):agent_exec]


def test_the_entrypoint_gives_every_agent_facing_socket_the_shared_group() -> None:
    """The agent connects to two executor sockets, and both need the group that admits it.

    The scrub socket was left out of that block, so it kept the executor's own group and the
    agent was refused with EACCES. Nothing failed loudly: every persisted transcript came back as
    "nanoinfra withheld this text", in every container, for as long as that socket has existed.
    `bind_scrub_socket` says the two sockets "get their mode from the same umask", which is true
    of the mode and not of the group -- the entrypoint supplies the group, and it named one.
    """
    text = _entrypoint()
    for variable in ("$socket_path", "$scrub_socket_path"):
        assert re.search(
            rf'chown "\$exec_user:\$ipc_group"[^\n]*{re.escape(variable)}', text
        ), f"{variable} never gets the group the agent belongs to"
        assert re.search(rf'chmod 660 "{re.escape(variable)}"', text), (
            f"{variable} never gets the group write bit that connect() needs"
        )


def test_the_scrub_socket_path_matches_the_name_the_code_derives() -> None:
    """A hand-written path in the entrypoint and a derived one in the code must not drift."""
    from pathlib import Path as _Path

    from nanoinfra.gates.executor.scrub_protocol import default_scrub_socket_path

    derived = default_scrub_socket_path(_Path("/run/nanoinfra-exec/executor.sock")).name
    assert f'scrub_socket_path="$socket_dir/{derived}"' in _entrypoint()


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


# ---------------------------------------------------------- the confinement layer (#20)


def test_the_entrypoint_starts_every_helper_under_the_confinement_launcher() -> None:
    """The container starts each helper with setpriv, so no Python supervisor runs there.

    The launcher applies the same rules the supervisors apply. Without it the container path, which
    is the main deployment, would hold no confinement at all.
    """
    text = _entrypoint()

    assert 'confinement_module="nanoinfra.gates.confinement"' in text
    assert 'executor_role="executor"' in text
    assert 'fetcher_role="fetcher"' in text
    assert 'mcp_host_role="mcp-host"' in text
    for variable in ("$executor_role", "$fetcher_role", "$mcp_host_role"):
        assert f'--role "{variable}"' in text


def test_the_entrypoint_starts_no_helper_module_directly() -> None:
    """A direct start would run the helper outside its confinement.

    The launcher reads a role and never an argv, so the container hands no exec right to a caller.
    """
    text = _entrypoint()

    for variable in ("$executor_module", "$fetcher_module", "$mcp_host_module"):
        assert f'python -m "{variable}"' not in text


def test_a_refused_confinement_stops_the_container_retries() -> None:
    """A kernel that rejects the ruleset refuses the start, so a retry loop is noise.

    The launcher exits 78 for that case. A crash keeps the restart, and a refusal ends it with a
    message an operator reads.
    """
    text = _entrypoint()

    assert 'confinement_refused_status=78' in text
    # One per confined helper: the executor, the fetcher, the MCP host, and the connector host.
    assert text.count('"$status" = "$confinement_refused_status"') == 4
    assert "refuses to start unconfined" in text


def test_the_mcp_state_dir_belongs_to_the_mcp_host() -> None:
    """Each MCP server's socket, state and log live under `~/.nanoinfra/mcp`, and the host spawns
    those children -- so the host must be able to create a directory per server there.

    It was prepared for nobody, so it kept whatever created it first. On a volume that predates the
    MCP split that was the agent, and starting a server failed with `PermissionError` on
    `~/.nanoinfra/mcp/<server>`. A fix inside the container does not survive, because the gateway
    branch chowns the whole data dir to the agent on every boot.
    """
    script = (Path(__file__).resolve().parents[2] / "entrypoint.sh").read_text(encoding="utf-8")
    body = script[script.index("prepare_mcp_host_paths() {"):]
    body = body[: body.index("\n}\n")]

    assert 'mkdir -p "$mcp_dir"' in body
    # Recursive, because the directories that already exist are the ones that are wrong.
    assert 'chown -R "$mcp_host_run_user:$mcp_ipc_group" "$mcp_dir"' in body
    # setgid, so a directory the host creates later keeps the group the agent reads through.
    assert 'chmod 2770 "$mcp_dir"' in body
    assert '-type d -exec chmod 2770' in body
    # The host's own socket directory is not widened by any of this.
    assert 'chmod 2710 "$mcp_host_socket_dir"' in body
    # A group that does not exist closes the directory and says what it costs.
    assert 'chmod 2700 "$mcp_dir"' in body
