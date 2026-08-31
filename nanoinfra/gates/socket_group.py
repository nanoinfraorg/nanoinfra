"""The group a helper's socket carries, applied by the process that creates it.

Lives beside the three helper packages rather than inside one of them. It first landed in
``gates/executor/`` and the isolation tests refused it: the fetcher and the MCP host may import
nothing from the executor's package, because that package reaches the credential store and the
inventory. A shared concern belongs one level up.

The agent connects to three sockets the executor binds, and a connect() needs write permission on
the socket file. So each one has to carry a group the agent belongs to. That was the supervisor's
job: `entrypoint.sh` waited for the socket and then chowned it while it still held root.

**Why that could not hold.** The supervisor waits for *a* socket at the path, not for *this run's*
socket. On a container restart the previous run's socket file is still there -- the container
filesystem survives a restart -- so the wait returns at once, the chown lands on the stale file,
and the executor then unlinks it and binds a fresh one carrying its own primary group. The agent
is refused, and nothing runs again to correct it. The same holds after a supervised restart of a
crashed executor, where the supervisor is no longer at that point in the script at all.

The symptom was quiet in the worst way. A scrub socket the agent cannot reach makes every
persisted transcript read `[nanoinfra withheld this text]`, and an execute socket it cannot reach
fails every remote action. Both were reported as something else.

So the group moves to the side that creates the socket. The executor belongs to both groups --
`nanoinfra-ipc` with the agent, and `nanoinfra-op` for the approval path -- so it can set them
without any privilege, and it does so between bind and listen, before a peer can connect.

The group name arrives in the environment because the layout that defines it is the container's,
not this module's. A start with nothing set changes nothing, which is every single-uid host.
"""

from __future__ import annotations

import grp
import os
from pathlib import Path

from loguru import logger

#: Names the group the agent shares with the executor. `entrypoint.sh` exports it.
SOCKET_GROUP_ENV = "NANOINFRA_SOCKET_GROUP"

#: Names the group the operator path uses (#38). A separate variable, because a helper that could
#: read the operator socket could answer the approvals it asks for.
OPERATOR_SOCKET_GROUP_ENV = "NANOINFRA_OPERATOR_SOCKET_GROUP"

#: The fetcher's and the MCP host's own groups. One variable per helper, and never one shared
#: name: a member of the executor's group can reach the executor's socket and run a command on
#: every inventory host, which is the one thing the split exists to prevent. So the fetcher and
#: the MCP host each get a group the agent belongs to and the other helpers do not.
FETCHER_SOCKET_GROUP_ENV = "NANOINFRA_FETCHER_SOCKET_GROUP"
#: The connector host's group (#195). Unlike the fetcher's and the MCP host's, the agent is
#: **not** a member: a connector call originates in the executor after the gate answered, so
#: nothing in the process the model steers has a reason to reach this socket.
CONNECTOR_HOST_SOCKET_GROUP_ENV = "NANOINFRA_CONNECTOR_HOST_SOCKET_GROUP"
MCP_HOST_SOCKET_GROUP_ENV = "NANOINFRA_MCP_HOST_SOCKET_GROUP"

#: Owner read/write plus group read/write. A connect() needs the write bit, and nothing outside
#: the two accounts gets any bit at all.
SOCKET_MODE = 0o660


def apply_socket_group(path: Path | str, *, env_var: str = SOCKET_GROUP_ENV) -> None:
    """Give *path* the group named by *env_var*, and the mode a connect() needs.

    Silent when the variable is unset, when the group does not exist, or when this process does
    not belong to it: each of those is a deployment that shares nothing, and a socket that keeps
    its own group is the right answer there. A failure is logged and never raised -- a socket that
    works for one account beats no socket at all, and the caller's own log states the layout.
    """
    name = (os.environ.get(env_var) or "").strip()
    if not name:
        return
    try:
        group = grp.getgrnam(name)
    except KeyError:
        logger.warning("gates: socket group {!r} does not exist on this host", name)
        return
    try:
        os.chown(path, -1, group.gr_gid)
        os.chmod(path, SOCKET_MODE)
    except OSError as exc:
        # Not fatal. The supervisor may still fix it, and a refusal to serve would cost every
        # gated action rather than one account's access.
        logger.warning("gates: could not give {} the group {!r}: {}", path, name, exc)
        return
    logger.info("gates: socket {} carries group {!r} (mode {:o})", path, name, SOCKET_MODE)


__all__ = [
    "FETCHER_SOCKET_GROUP_ENV",
    "MCP_HOST_SOCKET_GROUP_ENV",
    "OPERATOR_SOCKET_GROUP_ENV",
    "SOCKET_GROUP_ENV",
    "SOCKET_MODE",
    "apply_socket_group",
]
