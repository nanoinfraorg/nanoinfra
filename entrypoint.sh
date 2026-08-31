#!/bin/sh
dir="$HOME/.nanoinfra"

# Item 15 (nanoinfraorg/nanoinfra#18) splits the agent from the executor. This script is the
# one entry point for this image, so it is also the supervisor: it starts the executor on its
# own account, then it execs the agent. Only a root start can place two processes on two
# accounts, so the split happens here and not in Python.
agent_user="nanoinfra"
exec_user="nanoinfra-exec"
ipc_group="nanoinfra-ipc"
# The socket directory lives outside the agent's home on purpose. Write rights on a parent
# directory allow a rename of any entry inside it, whatever the entry's own owner is. So a
# socket directory under $HOME/.nanoinfra could be moved aside by the agent account, and the
# agent could then present its own socket at the expected path. /run is root-owned, and the
# agent account cannot write it.
socket_dir="/run/nanoinfra-exec"
socket_path="$socket_dir/executor.sock"
# nanoinfra/gates/executor/scrub_protocol.py derives this name from the execute socket's stem.
# It is the socket the agent uses to scrub a transcript before persisting it (#41).
scrub_socket_path="$socket_dir/executor.scrub.sock"

# The operator socket (#38). The executor suspends an action that needs an approval, and the
# operator answers here. It is a second socket on purpose: the agent holds the execute socket, so
# an answer accepted there would let a compromised agent approve its own action.
#
# The group gives something up, and that is deliberate. #27 answers from the WebUI, which runs
# inside the gateway process under the agent's account, so that account must reach this socket. The
# filesystem half of the split protects nothing on this one path. The answer still crosses a
# process boundary into the executor, the executor still matches the actor against
# gates.approvers, and an import closure keeps every tool module away from the client.
#
# Its own group, and never nanoinfra-ipc: that one would hand the same reach to the fetcher and to
# the MCP host, and either one could then approve an action it asked for.
op_group="nanoinfra-op"
op_socket_dir="$socket_dir/operator"
op_socket_path="$op_socket_dir/executor.op.sock"
# The executor's entry point is fixed by #18. Nothing else about the executor is assumed here.
executor_module="nanoinfra.gates.executor"

# Item 16 (nanoinfraorg/nanoinfra#19) splits the fetcher off as well. web_fetch and web_search run
# in that process, and untrusted content enters there. So the fetcher gets its own account and its
# own socket, and its account is never the executor's. Two processes under one uid give the kernel
# nothing to enforce, because either one can ptrace the other and read its memory. The one account
# that must never read a page is the account that decrypts credentials.
#
# The socket directory sits under /run for the reason the executor's does. Write rights on a parent
# directory allow a rename of any entry inside it, so a socket directory under the agent's home
# could be moved aside and replaced with the agent's own socket. /run is root-owned.
#
# An image built before the fetcher account exists still runs the fetcher, as a separate process
# under the agent's account. A separate process alone takes the credential store and the four
# execution transports out of the address space that reads a page. The log says which of the two
# this start produced, because an operator must never read silence as a guarantee.
fetch_user="nanoinfra-fetch"
# The fetcher's own IPC group. NOT nanoinfra-ipc: a member of the executor's group traverses the
# executor's socket directory and connects to its socket. The fetcher is the process that
# untrusted web content enters, so a fetcher inside that group could run a command on every
# inventory host. The agent belongs to both groups. Neither helper belongs to the other's.
fetch_ipc_group="nanoinfra-fetch-ipc"
fetch_socket_dir="/run/nanoinfra-fetch"
fetch_socket_path="$fetch_socket_dir/fetcher.sock"
# The fetcher's entry point is fixed by #19, the same shape as the executor's.
fetcher_module="nanoinfra.gates.fetcher"

# Item 20 (nanoinfraorg/nanoinfra#22) puts stdio MCP servers in a third helper process. A stdio MCP
# server is a subprocess, and the fetcher starts no program, so the exec right lives here instead.
# The host runs a command that the agent's own config names, so this account must never be the
# executor's account: that one decrypts the credentials and reaches the inventory hosts.
mcp_host_user="nanoinfra-mcp"
# The host's own IPC group, for the same reason the fetcher has one. A member of nanoinfra-ipc
# traverses the executor's socket directory and connects to its socket.
mcp_host_ipc_group="nanoinfra-mcp-ipc"
mcp_host_socket_dir="/run/nanoinfra-mcp"
# Per-server state, sockets and logs for stdio MCP servers, under the data dir rather than /run
# because an operator reads a server's log after it dies.
mcp_dir="$dir/mcp"
mcp_ipc_group="nanoinfra-mcp-ipc"
mcp_host_socket_path="$mcp_host_socket_dir/mcp_host.sock"
mcp_host_module="nanoinfra.gates.mcp_host"

# nanoinfraorg/nanoinfra#195 puts a *marketplace* connector's HTTPS request in a fourth helper. The
# package format runs no code, so this is not a sandbox for somebody else's Python: it is the
# process that makes a stranger's request, so that the process holding the credential store does
# not. Its group holds the executor and not the agent, because a connector call starts in the
# executor after the gate answered.
connector_host_user="nanoinfra-connector"
connector_host_ipc_group="nanoinfra-connector-ipc"
connector_host_socket_dir="/run/nanoinfra-connector"
connector_host_socket_path="$connector_host_socket_dir/connector_host.sock"
connector_host_module="nanoinfra.gates.connector_host"

# Item 17 (nanoinfraorg/nanoinfra#20) puts one confinement layer on each helper process. This
# script starts every helper with setpriv, so no Python supervisor runs on this path. The launcher
# below applies the same Landlock rules the supervisors apply, then it execs the helper. It reads a
# role and never an argv, so nothing here hands a caller the choice of a program. The role to module
# map lives in nanoinfra/gates/confinement.py beside the rules.
#
# A kernel with no Landlock support starts the helper anyway and says so, because such a host is
# legitimate. A kernel that supports Landlock and then rejects the ruleset makes the launcher exit
# 78, and the retry loops below stop at once. A helper never serves unconfined in silence.
confinement_module="nanoinfra.gates.confinement"
confinement_refused_status=78
executor_role="executor"
fetcher_role="fetcher"
mcp_host_role="mcp-host"
connector_host_role="connector-host"

# The executor resolves credentials out of the workspace, so it needs the same workspace the
# agent uses. Three sources, in order: NANOINFRA_WORKSPACE, then a --workspace/-w flag on the
# command, then nanoinfra's default. A shell cannot see a workspace that only config.json sets,
# so an operator must set NANOINFRA_WORKSPACE in that case. This script logs the value it
# resolved, because a mismatch here shows up later as a secret the executor cannot find.
resolve_workspace() {
    if [ -n "$NANOINFRA_WORKSPACE" ]; then
        printf '%s' "$NANOINFRA_WORKSPACE"
        return
    fi
    take_next=0
    for arg in "$@"; do
        if [ "$take_next" = "1" ]; then
            printf '%s' "$arg"
            return
        fi
        case "$arg" in
            --workspace|-w) take_next=1 ;;
            --workspace=*) printf '%s' "${arg#--workspace=}"; return ;;
        esac
    done
    printf '%s' "$dir/workspaces/default"
}

# Move a pre-root workspace before anything is prepared under it.
#
# The gateway performs this migration itself, at startup. In a container that is too late: this
# script prepares the credential store and the job store under the workspace and starts three
# confined helpers against it *first*, so a move that happens afterwards leaves the agent on
# `workspaces/default` and the executor on a path that no longer exists -- with its Landlock rules,
# its `secrets/` and its `servers/jobs` all named after the old one.
#
# So it runs here, before `resolve_workspace` is consulted, by calling the same function with the
# same guards rather than reimplementing them in sh: only the pre-root default moves, never a
# symlink, never onto a destination that holds something, and a failed config rewrite moves it back.
# As the agent account, because config.json is that account's file. The gateway then finds nothing
# left to do, which is what its own guards report.
migrate_workspace_layout() {
    if [ -n "${NANOINFRA_WORKSPACE:-}" ]; then
        echo "[entrypoint] workspace layout: NANOINFRA_WORKSPACE is set, so nothing is moved"
        return 0
    fi
    if [ ! -d "$dir/workspace" ]; then
        return 0
    fi
    setpriv --reuid="$agent_user" --regid="$agent_user" --init-groups \
        python -c 'import os, sys
from pathlib import Path
from nanoinfra.config.workspace_migration import migrate_default_workspace
result = migrate_default_workspace(Path(os.environ["HOME"]) / ".nanoinfra" / "config.json")
if result.moved:
    print(f"[entrypoint] workspace moved: {result.source} -> {result.target}")
else:
    print(f"[entrypoint] workspace layout unchanged: {result.reason}")
' 2>&1 || echo "[entrypoint] warning: the workspace migration could not run; the layout is unchanged"
}

# Hand the two executor-only paths to the executor account, and keep the agent account out.
# The paths are:
#   <workspace>/secrets   the credential store (nanoinfra/secrets/store.py)
#   $HOME/.nanoinfra/gates  the gate audit log (nanoinfra/gates/audit.py)
# The executor holds the only plaintext and writes the only record, so it owns both. Mode 700
# means the agent account cannot read either one.
# harden_audit_parents below closes the rename of the audit directory (#36). One limit stays,
# stated rather than hidden: the workspace is the agent's own directory, so the agent can still
# rename or remove <workspace>/secrets. That costs the executor its credentials, and it never
# reveals one. Confidentiality holds, and availability does not.
prepare_executor_paths() {
    workspace="$1"

    # The socket directory comes first. A later step hands the credential store to the executor
    # account, and a half-done handover would leave a store that neither account can use. So a
    # failure here returns before any ownership changes.
    #
    # The executor owns the socket directory, and that direction matters. Only a directory's
    # writer can create or replace a socket node inside it. If the agent owned this directory,
    # a compromised agent could unlink the executor's socket, bind its own in its place, and
    # then answer as the authority that polices it. So the executor owns the directory, and
    # the agent only reaches through it.
    mkdir -p "$socket_dir" || return 1
    chown "$exec_user:$ipc_group" "$socket_dir" || return 1
    # Mode 2710 on the socket directory:
    #   owner rwx  the executor binds, unlinks, and rebinds its socket.
    #   group --x  the agent traverses to a known socket name. It cannot list the directory,
    #              and it cannot create anything in it.
    #   other ---  every other account is refused before it reaches the socket.
    #   setgid     each new socket inherits group nanoinfra-ipc, so the agent keeps access
    #              after the executor rebinds.
    chmod 2710 "$socket_dir" || return 1

    # The operator socket directory (#38). bind_operator_socket() creates it at 0700 under the
    # executor account, and the agent could then not traverse it, so the #27 inbox reported
    # degraded in every container. Root prepares it here instead.
    mkdir -p "$op_socket_dir" || return 1
    if getent group "$op_group" >/dev/null 2>&1; then
        chown "$exec_user:$op_group" "$op_socket_dir" || return 1
        chmod 2710 "$op_socket_dir" || return 1
    else
        # An image built before the group exists keeps the directory closed. A silent 0700 reads
        # to an operator as an inbox that broke for no reason, so it says what it costs.
        chown "$exec_user:$exec_user" "$op_socket_dir" || return 1
        chmod 700 "$op_socket_dir" || return 1
        echo "[entrypoint] warning: no $op_group group, so no approval can be answered" >&2
        echo "[entrypoint] warning: an approve decision then waits and refuses" >&2
    fi

    mkdir -p "$workspace" || return 1
    chown nanoinfra:nanoinfra "$workspace" 2>/dev/null || \
        echo "[entrypoint] warning: chown $workspace failed"
    # The server job store is the one directory BOTH accounts write. The executor creates a job
    # and updates its output (nanoinfra/gates/executor/server.py), and the agent reconciles jobs
    # that a restart interrupted (nanoinfra/webui/ws_http.py). It was prepared for neither, so it
    # stayed the agent's alone and the executor was refused with EACCES on its temp file -- which
    # meant every remote action in a container failed *after* the gate permitted it.
    #
    # Group write on both sides is not a widening: the job record holds the command and its
    # output, and the agent renders both. The credential stays in the store below, which the
    # agent cannot read.
    mkdir -p "$workspace/servers/jobs" || return 1
    chown -R "$agent_user:$ipc_group" "$workspace/servers/jobs" || return 1
    # setgid, so a file either account creates here inherits the shared group rather than its own.
    chmod 2770 "$workspace/servers/jobs" || return 1
    find "$workspace/servers/jobs" -type f -exec chmod 660 {} + 2>/dev/null || true

    mkdir -p "$workspace/secrets" "$dir/gates" || return 1
    # The credential store is WRITTEN by the executor and its metadata is READ by the agent, so
    # it takes the same shape as the audit log below: executor owns it, the shared group reads,
    # and nobody else sees it.
    #
    # At mode 700 the agent could not open a record, `Path.glob` swallowed the PermissionError,
    # and the WebUI reported "no secrets yet" about a store holding an SSH key -- the same fault
    # this file already records for the audit log, one directory over. The Servers page then
    # showed a server whose credential read as absent.
    #
    # A group read yields the ciphertext and the metadata, and that is only safe because the
    # agent holds no key: the export below removes NANOINFRA_SECRETS_KEY from the agent's
    # environment. Encryption is what separates the two accounts here; the file mode alone never
    # did, because setpriv passed the key through to both. So the order matters and both halves
    # are required. The executor keeps the key, keeps the write, and stays the only side that can
    # turn a record into a credential.
    chown -R "$exec_user:$ipc_group" "$workspace/secrets" || return 1
    chmod 2750 "$workspace/secrets" || return 1
    find "$workspace/secrets" -type f -exec chmod 640 {} + 2>/dev/null || true
    # The audit log is written by the executor and READ by the agent. #32 rebuilds denial
    # latches from it and #29 serves it in the WebUI, and both run in the agent process. At
    # mode 700 the agent could not open a segment, Path.glob swallowed the PermissionError,
    # and every latch cleared on every boot -- #32 was void here through permissions alone.
    #
    # So the shared group reads and never writes. The log holds command digests by default,
    # and full command text needs gates.audit.recordCommandText, so a group read leaks little.
    # Write stays with the executor, and the sticky parents below stop a rename.
    chown -R "$exec_user:$ipc_group" "$dir/gates" || return 1
    chmod 2750 "$dir/gates" || return 1
    find "$dir/gates" -type f -exec chmod 640 {} + 2>/dev/null || true

    harden_audit_parents || return 1
}

# Take the rename of the audit directory away from the agent account (item 34,
# nanoinfraorg/nanoinfra#36).
#
# WHY THE PARENT MATTERS. Mode 700 on $dir/gates looks sufficient and is not. It settles who
# reads the contents. It does not settle who moves the entry. A rename needs write rights on the
# PARENT directory, and the kernel ignores the owner and the mode of the entry itself. The agent
# account owned $HOME/.nanoinfra, so it could move the audit log aside without one byte of read
# access. #32 rebuilds the denial latches from that log, an absent log reads exactly like an
# empty one, and the agent got deny, rename, restart, retry.
#
# The agent must keep the data dir writable, because it creates config.json, sessions, media,
# logs, and more in it. So this does not take the write right away. It adds the sticky bit, the
# /tmp rule: in a sticky directory, only the owner of an entry, the owner of the directory, or
# root may rename or delete that entry. root takes the directory, so the agent is not the
# directory owner either, and $exec_user owns the gates entry. The agent therefore keeps every
# entry it creates, and it cannot touch the executor's one.
#
# $HOME gets the same treatment. A rename of $HOME/.nanoinfra hides the audit root just as well
# as a rename of $HOME/.nanoinfra/gates does. /home is root-owned already, so the chain ends
# one level up.
#
# Group $ipc_group is the one group both accounts hold, so both keep write access here. The
# executor also needs to traverse both directories to reach its own audit root, and this group
# gives it that.
#
# The mode stays as open as it was. Both directories were 755, so 1775 adds the group write bit
# and the sticky bit and takes nothing away. A host that bind-mounts ~/.nanoinfra keeps its read
# and its list rights, and only the write right moves to the shared group.
#
# The Python half backs this up. The executor pins the device and inode of its audit root, so a
# move it cannot prevent stops the executor instead of the latches (nanoinfra/gates/audit.py).
#
# Residual risk, and it is deliberate: root and an operator with a shell can still remove the
# audit log. That act is visible, because the segments are then gone. The item is about the
# agent account, and the agent account can no longer do it.
harden_audit_parents() {
    for parent in "$dir" "$HOME"; do
        # A chmod on "" or on / would break the whole container, so it never runs. An unset HOME
        # is the case that produces both, and it means this layout was never prepared correctly.
        case "$parent" in
            ""|"/")
                echo "[entrypoint] warning: refusing to harden '$parent' — check HOME" >&2
                return 1
                ;;
        esac
        chown "root:$ipc_group" "$parent" || return 1
        # 1775: owner root rwx, group $ipc_group rwx, other r-x, plus the sticky bit. The sticky
        # bit is the whole point, and the group write bit keeps the agent working.
        chmod 1775 "$parent" || return 1
    done
    echo "[entrypoint] audit log: $dir/gates belongs to $exec_user, and its parents are sticky"
    echo "[entrypoint] audit log: root can still remove it, and a removal is visible"
}

# Start the executor and keep it up. A dead executor means every gated action fails, so a
# restart beats a silent outage. Five failed starts in a row stop the retries, because a module
# that cannot start at all must not fill the log for the life of the container. A run that
# lasted a minute counts as a crash rather than a broken start, so it returns the full budget.
#
# The executor keeps the environment of this script, and HOME matters there. Its audit log sits
# in the shared data dir under that HOME, so both accounts name the same directory.
#
# umask 0007 is load-bearing. A connect() on a Unix socket needs write permission on the socket
# file, so a socket created 0755 would refuse the agent. The setgid directory supplies the
# group, and this umask supplies the group write bit.
start_executor() {
    workspace="$1"
    (
        umask 0007
        failures=0
        while [ "$failures" -lt 5 ]; do
            started=$(date +%s)
            setpriv --reuid="$exec_user" --regid="$exec_user" --init-groups \
                python -m "$confinement_module" --role "$executor_role" \
                --socket "$socket_path" --workspace "$workspace"
            status=$?
            if [ "$status" = "$confinement_refused_status" ]; then
                echo "[entrypoint] error: the executor refuses to start unconfined" >&2
                echo "[entrypoint] error: every gated action stays refused" >&2
                break
            fi
            if [ "$(($(date +%s) - started))" -ge 60 ]; then
                failures=0
            else
                failures=$((failures + 1))
            fi
            echo "[entrypoint] warning: executor exited with status $status — restart in 5s" >&2
            sleep 5
        done
        echo "[entrypoint] error: the executor failed 5 starts in a row — no more restarts" >&2
        echo "[entrypoint] error: gated actions stay refused until this container restarts" >&2
    ) &
    echo "[entrypoint] executor starting as $exec_user on $socket_path"
    echo "[entrypoint] executor entry point: $executor_module, confined"
}

# Report whether the socket came up. This never blocks the agent. The executor module lands
# with #18, and an image built before it must still start its gateway and say what is missing.
wait_for_socket() {
    attempt=0
    while [ "$attempt" -lt 5 ]; do
        if [ -S "$socket_path" ]; then
            echo "[entrypoint] executor socket ready at $socket_path"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    echo "[entrypoint] warning: no executor socket at $socket_path after 5s" >&2
    echo "[entrypoint] warning: gated actions will fail until the executor answers" >&2
    return 1
}

# Pick the account that runs the fetcher (#19).
#
# The named account wins when the image has it. Otherwise the agent's account runs the fetcher, and
# the fetcher is still a separate process. The one account this never returns is the executor's,
# because that account holds the plaintext credentials.
resolve_fetcher_user() {
    if [ "$fetch_user" != "$exec_user" ] && id "$fetch_user" >/dev/null 2>&1; then
        printf '%s' "$fetch_user"
        return
    fi
    printf '%s' "nanoinfra"
}

# Pick the group that owns the fetcher's socket directory (#19).
#
# The fetcher's own group wins when the image has it. An image built before that group exists runs
# the fetcher as the agent, so the agent's own group is the right owner there. This never returns
# the executor's group, because that group is a path from the fetcher to a host command.
resolve_fetcher_group() {
    if id -g "$fetch_ipc_group" >/dev/null 2>&1 || getent group "$fetch_ipc_group" >/dev/null 2>&1
    then
        printf '%s' "$fetch_ipc_group"
        return
    fi
    printf '%s' "nanoinfra"
}

# Hand the fetcher's socket directory to the fetcher's account, and keep every other account out.
#
# The ownership direction matters for the same reason it does for the executor. Only a directory's
# writer can create or replace a socket node inside it. So the fetcher owns the directory, and the
# agent reaches through it to a known socket name.
prepare_fetcher_paths() {
    mkdir -p "$fetch_socket_dir" || return 1
    chown "$fetch_run_user:$fetch_run_group" "$fetch_socket_dir" || return 1
    # Mode 2710, the same four reasons as the executor's socket directory:
    #   owner rwx  the fetcher binds, unlinks, and rebinds its socket.
    #   group --x  the agent traverses to a known socket name. It cannot list or create.
    #   other ---  every other account is refused before it reaches the socket.
    #   setgid     each new socket inherits the fetcher's group, so a rebind keeps the agent in.
    chmod 2710 "$fetch_socket_dir" || return 1
}

# Start the fetcher and keep it up. A dead fetcher means web_fetch and web_search fail, so a restart
# beats a silent outage. Five failed starts in a row stop the retries, because a module that cannot
# start at all must not fill the log for the life of the container. A run that lasted a minute counts
# as a crash rather than a broken start, so it returns the full budget.
#
# umask 0007 is load-bearing here too. A connect() on a Unix socket needs write permission on the
# socket file, so a socket created 0755 would refuse the agent. The setgid directory supplies the
# group, and this umask supplies the group write bit.
start_fetcher() {
    fetch_workspace="$1"
    (
        umask 0007
        failures=0
        while [ "$failures" -lt 5 ]; do
            started=$(date +%s)
            setpriv --reuid="$fetch_run_user" --regid="$fetch_run_user" --init-groups \
                python -m "$confinement_module" --role "$fetcher_role" \
                --socket "$fetch_socket_path" --workspace "$fetch_workspace"
            status=$?
            if [ "$status" = "$confinement_refused_status" ]; then
                echo "[entrypoint] error: the fetcher refuses to start unconfined" >&2
                echo "[entrypoint] error: web_fetch and web_search stay unreachable" >&2
                break
            fi
            if [ "$(($(date +%s) - started))" -ge 60 ]; then
                failures=0
            else
                failures=$((failures + 1))
            fi
            echo "[entrypoint] warning: fetcher exited with status $status, restart in 5s" >&2
            sleep 5
        done
        echo "[entrypoint] error: the fetcher failed 5 starts in a row, no more restarts" >&2
        echo "[entrypoint] error: web_fetch and web_search stay unreachable until a restart" >&2
    ) &
    echo "[entrypoint] fetcher starting as $fetch_run_user on $fetch_socket_path"
    echo "[entrypoint] fetcher entry point: $fetcher_module, confined"
}

# Report whether the fetcher socket came up. This never blocks the agent, and it never goes quiet.
wait_for_fetcher_socket() {
    attempt=0
    while [ "$attempt" -lt 5 ]; do
        if [ -S "$fetch_socket_path" ]; then
            echo "[entrypoint] fetcher socket ready at $fetch_socket_path"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    echo "[entrypoint] warning: no fetcher socket at $fetch_socket_path after 5s" >&2
    echo "[entrypoint] warning: web_fetch and web_search will fail until the fetcher answers" >&2
    return 1
}

# Say plainly what the fetcher split does not have on this host (#19).
warn_fetcher_split_not_enforced() {
    echo "[entrypoint] warning: $1" >&2
    echo "[entrypoint] warning: the fetcher shares the agent's uid, so either one can ptrace" >&2
    echo "[entrypoint] warning: the other and read its memory. The #19 split is a process" >&2
    echo "[entrypoint] warning: boundary here, and the kernel does not enforce it." >&2
    echo "[entrypoint] warning: the fetcher still holds no credential store and no transport." >&2
}

# Pick the account that runs the stdio MCP host (#22).
#
# The named account wins when the image has it. Otherwise the agent's account runs the host, and the
# host is still a separate process. The one account this never returns is the executor's, because
# the host starts a program that a config in the agent's reach names.
resolve_mcp_host_user() {
    if [ "$mcp_host_user" != "$exec_user" ] && id "$mcp_host_user" >/dev/null 2>&1; then
        printf '%s' "$mcp_host_user"
        return
    fi
    printf '%s' "nanoinfra"
}

# Pick the group that owns the host's socket directory (#22).
#
# The host's own group wins when the image has it. An older image runs the host as the agent, so the
# agent's own group is the right owner there. This never returns the executor's group or the
# fetcher's group, because either one would be a path between two helpers.
resolve_mcp_host_group() {
    if id -g "$mcp_host_ipc_group" >/dev/null 2>&1 ||
        getent group "$mcp_host_ipc_group" >/dev/null 2>&1
    then
        printf '%s' "$mcp_host_ipc_group"
        return
    fi
    printf '%s' "nanoinfra"
}

# Hand the host's socket directory to the host's account, and keep every other account out.
prepare_mcp_host_paths() {
    # Where each MCP server keeps its socket, its state file and its log
    # (`gates/mcp_host/supervisor.py::_prepare_run_dir`). The host spawns those children, so the
    # host is the account that has to be able to create a directory per server here.
    #
    # It was prepared for nobody, so it stayed whatever created it first. On this deployment that
    # was the agent, from an image that predates the MCP split, and the volume kept the ownership:
    # starting a server then failed with `PermissionError: /home/nanoinfra/.nanoinfra/mcp/github`
    # -- the host refused write on a directory the agent owned at 0755. The recursive chown at the
    # top of the gateway branch hands the whole data dir to the agent on every boot, so a manual
    # fix inside the container survives exactly until the next restart. It belongs here.
    #
    # Group `nanoinfra-mcp-ipc` with mode 2770, matching the job store's reasoning: the agent is a
    # member, so the WebUI can read a server's state and log without a second transport, and setgid
    # keeps that true for directories the host creates later. It grants the agent nothing over the
    # host's own socket directory, which stays 2710 below.
    mkdir -p "$mcp_dir" || return 1
    if getent group "$mcp_ipc_group" >/dev/null 2>&1; then
        chown -R "$mcp_host_run_user:$mcp_ipc_group" "$mcp_dir" || return 1
        chmod 2770 "$mcp_dir" || return 1
        find "$mcp_dir" -mindepth 1 -type d -exec chmod 2770 {} + 2>/dev/null || true
    else
        chown -R "$mcp_host_run_user:$mcp_host_run_group" "$mcp_dir" || return 1
        chmod 2700 "$mcp_dir" || return 1
        echo "[entrypoint] warning: no $mcp_ipc_group group, so the WebUI cannot read MCP logs" >&2
    fi

    mkdir -p "$mcp_host_socket_dir" || return 1
    chown "$mcp_host_run_user:$mcp_host_run_group" "$mcp_host_socket_dir" || return 1
    # Mode 2710, the same four reasons as the other two socket directories:
    #   owner rwx  the host binds, unlinks, and rebinds its socket.
    #   group --x  the agent traverses to a known socket name. It cannot list or create.
    #   other ---  every other account is refused before it reaches the socket.
    #   setgid     each new socket inherits the host's group, so a rebind keeps the agent in.
    chmod 2710 "$mcp_host_socket_dir" || return 1
}

# Start the MCP host and keep it up. A dead host means every stdio MCP tool fails, so a restart
# beats a silent outage. The budget matches the fetcher's for the same reasons.
#
# umask 0007 is load-bearing here too. A connect() on a Unix socket needs write permission on the
# socket file, so a socket created 0755 would refuse the agent.
start_mcp_host() {
    mcp_host_workspace="$1"
    (
        umask 0007
        failures=0
        while [ "$failures" -lt 5 ]; do
            started=$(date +%s)
            setpriv --reuid="$mcp_host_run_user" --regid="$mcp_host_run_user" --init-groups \
                python -m "$confinement_module" --role "$mcp_host_role" \
                --socket "$mcp_host_socket_path" --workspace "$mcp_host_workspace"
            status=$?
            if [ "$status" = "$confinement_refused_status" ]; then
                echo "[entrypoint] error: the MCP host refuses to start unconfined" >&2
                echo "[entrypoint] error: stdio MCP tools stay unreachable until a restart" >&2
                break
            fi
            if [ "$(($(date +%s) - started))" -ge 60 ]; then
                failures=0
            else
                failures=$((failures + 1))
            fi
            echo "[entrypoint] warning: MCP host exited with status $status, restart in 5s" >&2
            sleep 5
        done
        echo "[entrypoint] error: the MCP host failed 5 starts in a row, no more restarts" >&2
        echo "[entrypoint] error: stdio MCP tools stay unreachable until a restart" >&2
    ) &
    echo "[entrypoint] MCP host starting as $mcp_host_run_user on $mcp_host_socket_path"
    echo "[entrypoint] MCP host entry point: $mcp_host_module, confined"
}

# Pick the account that runs the connector host (#195).
#
# Never the executor's account: that one decrypts the credentials, and moving the request out of it
# is the whole point. Never the agent's either, unless the image has no separate account -- the agent
# is the process the model steers.
resolve_connector_host_user() {
    if [ "$connector_host_user" != "$exec_user" ] && id "$connector_host_user" >/dev/null 2>&1; then
        printf '%s' "$connector_host_user"
        return
    fi
    printf '%s' "$exec_user"
}

# Pick the group that owns the connector host's socket directory (#195).
#
# The executor is the only client, so this is the executor's shared group and never the agent's.
resolve_connector_host_group() {
    if getent group "$connector_host_ipc_group" >/dev/null 2>&1; then
        printf '%s' "$connector_host_ipc_group"
        return
    fi
    printf '%s' "$exec_user"
}

# Hand the connector host's socket directory to its account, and keep every other account out.
prepare_connector_host_paths() {
    mkdir -p "$connector_host_socket_dir" || return 1
    chown "$connector_host_run_user:$connector_host_run_group" "$connector_host_socket_dir" || return 1
    # Mode 2710, the same reasoning as the other socket directories -- except that the group here
    # is the executor's, so the "group --x" line means the executor traverses to the socket and the
    # agent does not.
    chmod 2710 "$connector_host_socket_dir" || return 1
}

# Start the connector host and keep it up. A dead host means a marketplace connector's calls fail
# while a first-party one keeps working, which is a confusing outage: hence the restart loop and
# the plain message.
start_connector_host() {
    connector_host_workspace="$1"
    (
        umask 0007
        failures=0
        while [ "$failures" -lt 5 ]; do
            started=$(date +%s)
            setpriv --reuid="$connector_host_run_user" --regid="$connector_host_run_user" \
                --init-groups \
                python -m "$confinement_module" --role "$connector_host_role" \
                --socket "$connector_host_socket_path" --workspace "$connector_host_workspace"
            status=$?
            if [ "$status" = "$confinement_refused_status" ]; then
                echo "[entrypoint] error: the connector host refuses to start unconfined" >&2
                echo "[entrypoint] error: marketplace connectors stay unreachable" >&2
                break
            fi
            if [ "$(($(date +%s) - started))" -ge 60 ]; then
                failures=0
            else
                failures=$((failures + 1))
            fi
            echo "[entrypoint] warning: connector host exited with status $status, restart in 5s" >&2
            sleep 5
        done
        echo "[entrypoint] error: the connector host failed 5 starts in a row, no more restarts" >&2
    ) &
    echo "[entrypoint] connector host starting as $connector_host_run_user on $connector_host_socket_path"
    echo "[entrypoint] connector host entry point: $connector_host_module, confined"
}

wait_for_connector_host_socket() {
    attempt=0
    while [ "$attempt" -lt 5 ]; do
        if [ -S "$connector_host_socket_path" ]; then
            echo "[entrypoint] connector host socket ready at $connector_host_socket_path"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    echo "[entrypoint] warning: no connector host socket at $connector_host_socket_path after 5s" >&2
    echo "[entrypoint] warning: marketplace connectors will fail until the host answers" >&2
    return 1
}

# Report whether the host socket came up. This never blocks the agent, and it never goes quiet.
wait_for_mcp_host_socket() {
    attempt=0
    while [ "$attempt" -lt 5 ]; do
        if [ -S "$mcp_host_socket_path" ]; then
            echo "[entrypoint] MCP host socket ready at $mcp_host_socket_path"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    echo "[entrypoint] warning: no MCP host socket at $mcp_host_socket_path after 5s" >&2
    echo "[entrypoint] warning: stdio MCP tools will fail until the host answers" >&2
    return 1
}

# Say plainly what the MCP host split does not have on this host (#22).
warn_mcp_host_split_not_enforced() {
    echo "[entrypoint] warning: $1" >&2
    echo "[entrypoint] warning: the MCP host shares the agent's uid, so either one can ptrace" >&2
    echo "[entrypoint] warning: the other and read its memory. The #22 split is a process" >&2
    echo "[entrypoint] warning: boundary here, and the kernel does not enforce it." >&2
    echo "[entrypoint] warning: the host still holds no credential store and no transport." >&2
}

# Say plainly what the deployment does not have. An operator must never read silence as a
# guarantee, so a single-uid start states the property it lacks.
warn_split_not_enforced() {
    echo "[entrypoint] warning: $1" >&2
    echo "[entrypoint] warning: the agent and the executor share one uid, so either one can" >&2
    echo "[entrypoint] warning: ptrace the other and read its memory. The #18 privilege" >&2
    echo "[entrypoint] warning: split is organisational here, not kernel-enforced." >&2
}

# Drop privileges whenever the container starts as root. A plain `docker run`
# defaults to root, and a root-owned bind mount or volume lands the data dir there
# too, so this covers both. Chown the data dir so the non-root user can write it,
# then re-exec as nanoinfra. Fail closed: if the privilege drop cannot be
# performed, exit rather than run the agent as root.
if [ "$(id -u)" = "0" ]; then
    # This recursive chown runs before prepare_executor_paths, because it would otherwise
    # hand the executor's two directories back to the agent account.
    chown -R nanoinfra:nanoinfra "$dir" 2>/dev/null || echo "[entrypoint] warning: chown $dir failed"

    # Credential administration cannot run as the agent, because the credential store is not
    # readable by that account. NANOINFRA_ENTRYPOINT_ROLE=executor runs one command, such as
    # `secret add`, on the executor account instead. That role needs no socket server.
    run_user="nanoinfra"
    if [ "$NANOINFRA_ENTRYPOINT_ROLE" = "executor" ]; then
        run_user="$exec_user"
        echo "[entrypoint] role=executor — running this command as $exec_user"
    fi

    if ! id "$exec_user" >/dev/null 2>&1; then
        # An image built before #18, or a host account set that was never created. The command
        # still starts, and the log states the property this deployment does not have.
        warn_split_not_enforced "no $exec_user account on this image"
    else
        migrate_workspace_layout
        workspace=$(resolve_workspace "$@")
        echo "[entrypoint] executor workspace: $workspace"
        if ! prepare_executor_paths "$workspace"; then
            warn_split_not_enforced "the executor paths could not be prepared"
        elif [ "$run_user" = "nanoinfra" ]; then
            # The child binds this socket at start, and the agent reads the same variable for
            # the #27 inbox. So it goes out before the start and not after it.
            export NANOINFRA_OPERATOR_SOCKET="$op_socket_path"
            # The executor sets the group on each socket it binds, because this script cannot do
            # it reliably: on a restart the previous run's socket file is still there, so the wait
            # below returns on the stale file and the chown lands on something the executor is
            # about to unlink. See nanoinfra/gates/executor/socket_group.py.
            export NANOINFRA_SOCKET_GROUP="$ipc_group"
            export NANOINFRA_OPERATOR_SOCKET_GROUP="$op_group"
            # And remove the stale files anyway, so the wait below can only see this run's socket.
            rm -f "$socket_path" "$scrub_socket_path" "$op_socket_path" 2>/dev/null || true
            start_executor "$workspace"
            if wait_for_socket; then
                # The executor prepares its own socket directory, and a single-uid host wants
                # that directory closed to every other account. A two-uid host needs the group
                # bit back, or the agent cannot reach the socket at all. So this start re-applies
                # the owner, the group, and the two modes while it still holds root.
                chown "$exec_user:$ipc_group" "$socket_dir" "$socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chown $socket_dir failed"
                chmod 2710 "$socket_dir" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $socket_dir failed"
                chmod 660 "$socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $socket_path failed"
                # And the scrub socket, which the agent connects to as well (#41). It was left
                # out of this block, so it kept the executor's own group and the agent was
                # refused: every persisted transcript came back as "nanoinfra withheld this
                # text", in every container, since that socket existed. `bind_scrub_socket` says
                # the two sockets "get their mode from the same umask" -- true of the mode, and
                # not of the group, because this block is what supplies the group and it named
                # only one of them.
                chown "$exec_user:$ipc_group" "$scrub_socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chown $scrub_socket_path failed"
                chmod 660 "$scrub_socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $scrub_socket_path failed"
                # The same treatment for the operator socket. The executor creates it, and a
                # rebind can narrow the mode. connect() needs the group write bit.
                chown "$exec_user:$op_group" "$op_socket_dir" "$op_socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chown $op_socket_dir failed"
                chmod 2710 "$op_socket_dir" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $op_socket_dir failed"
                chmod 660 "$op_socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $op_socket_path failed"
            fi
            # The socket path travels in the environment so the agent's client does not guess
            # it. NANOINFRA_EXECUTOR_EXTERNAL tells the Python side that an executor already
            # runs on its own account, so it must not start a second one.
            # Both variables go out even when the socket is late or absent, and that is the
            # fail-closed choice. A retry may still succeed. An executor that the agent starts
            # for itself would hold the agent's uid, and that undoes the split it claims.
            export NANOINFRA_EXECUTOR_SOCKET="$socket_path"
            export NANOINFRA_EXECUTOR_EXTERNAL=1
        fi
    fi

    # The fetcher (#19). It starts on every root start, and it does not depend on the executor: an
    # image with no executor account still needs web_fetch and web_search. The role=executor path
    # runs one administrative command, and that command needs no fetcher.
    if [ "$run_user" = "nanoinfra" ]; then
        fetch_run_user=$(resolve_fetcher_user)
        if [ "$fetch_run_user" = "$exec_user" ]; then
            # A guard on the resolved value, and not only on the configured name. The account that
            # decrypts credentials must never be the account that reads a page.
            warn_fetcher_split_not_enforced "the resolved fetcher account is the executor account"
            fetch_run_user="nanoinfra"
        fi
        # The group the agent shares with the fetcher, and never the executor's group.
        fetch_run_group=$(resolve_fetcher_group)
        if [ "$fetch_run_user" = "nanoinfra" ]; then
            warn_fetcher_split_not_enforced "no separate $fetch_user account runs the fetcher"
        else
            echo "[entrypoint] fetcher account: $fetch_run_user (separate uid from the agent)"
        fi
        fetch_workspace=$(resolve_workspace "$@")
        echo "[entrypoint] fetcher workspace: $fetch_workspace"
        if ! prepare_fetcher_paths; then
            # No export in this case. The Python gateway then starts a fetcher of its own under the
            # agent's account, which is a working web tool rather than a broken one.
            echo "[entrypoint] warning: the fetcher paths could not be prepared" >&2
            echo "[entrypoint] warning: the gateway starts its own fetcher instead" >&2
        else
            # The fetcher sets the group on the socket it binds, for the reason
            # nanoinfra/gates/executor/socket_group.py states: on a restart the wait below returns
            # on the previous run's socket file, so a chown here lands on a file that is about to
            # be unlinked.
            export NANOINFRA_FETCHER_SOCKET_GROUP="$fetch_run_group"
            rm -f "$fetch_socket_path" 2>/dev/null || true
            start_fetcher "$fetch_workspace"
            if wait_for_fetcher_socket; then
                # The fetcher creates its own socket, and a rebind can widen or narrow the mode.
                # So this start re-applies the owner, the group, and the two modes while it still
                # holds root. Without the group write bit the agent cannot connect at all.
                chown "$fetch_run_user:$fetch_run_group" "$fetch_socket_dir" \
                    "$fetch_socket_path" \
                    2>/dev/null || \
                    echo "[entrypoint] warning: chown $fetch_socket_dir failed"
                chmod 2710 "$fetch_socket_dir" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $fetch_socket_dir failed"
                chmod 660 "$fetch_socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $fetch_socket_path failed"
            fi
            # The socket path travels in the environment so the tool's client does not guess it.
            # NANOINFRA_FETCHER_EXTERNAL tells the gateway that a fetcher already runs, so it must
            # not start a second one. Both variables go out even when the socket is late, because a
            # retry may still succeed and a second fetcher would hold the agent's uid.
            export NANOINFRA_FETCHER_SOCKET="$fetch_socket_path"
            export NANOINFRA_FETCHER_EXTERNAL=1
        fi
    fi

    # The stdio MCP host (#22). It starts on every root start, the same as the fetcher. The host
    # reads the agent's config for its server list, so it needs the same HOME.
    if [ "$run_user" = "nanoinfra" ]; then
        mcp_host_run_user=$(resolve_mcp_host_user)
        if [ "$mcp_host_run_user" = "$exec_user" ]; then
            # A guard on the resolved value, and not only on the configured name. The account that
            # decrypts credentials must never be the account that runs a configured command.
            warn_mcp_host_split_not_enforced "the resolved MCP host account is the executor account"
            mcp_host_run_user="nanoinfra"
        fi
        if [ "$mcp_host_run_user" = "nanoinfra" ]; then
            warn_mcp_host_split_not_enforced "no separate $mcp_host_user account runs the MCP host"
        else
            echo "[entrypoint] MCP host account: $mcp_host_run_user (separate uid from the agent)"
        fi
        # The group the agent shares with the host, and never another helper's group.
        mcp_host_run_group=$(resolve_mcp_host_group)
        mcp_host_workspace=$(resolve_workspace "$@")
        if ! prepare_mcp_host_paths; then
            # No export in this case. The Python gateway then starts a host of its own under the
            # agent's account, which is a working MCP tool rather than a broken one.
            echo "[entrypoint] warning: the MCP host paths could not be prepared" >&2
            echo "[entrypoint] warning: the gateway starts its own MCP host instead" >&2
        else
            export NANOINFRA_MCP_HOST_SOCKET_GROUP="$mcp_host_run_group"
            rm -f "$mcp_host_socket_path" 2>/dev/null || true
            start_mcp_host "$mcp_host_workspace"
            if wait_for_mcp_host_socket; then
                # The host creates its own socket, and a rebind can widen or narrow the mode. So
                # this start re-applies the owner, the group, and the two modes while it still holds
                # root. Without the group write bit the agent cannot connect at all.
                chown "$mcp_host_run_user:$mcp_host_run_group" "$mcp_host_socket_dir" \
                    "$mcp_host_socket_path" \
                    2>/dev/null || \
                    echo "[entrypoint] warning: chown $mcp_host_socket_dir failed"
                chmod 2710 "$mcp_host_socket_dir" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $mcp_host_socket_dir failed"
                chmod 660 "$mcp_host_socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chmod $mcp_host_socket_path failed"
            fi
            export NANOINFRA_MCP_HOST_SOCKET="$mcp_host_socket_path"
            export NANOINFRA_MCP_HOST_EXTERNAL=1
        fi

        # The connector host (#195). Started even when no marketplace connector is installed: a
        # host that is only started on demand is a host whose first call pays for the start, and
        # the process costs one idle python.
        connector_host_run_user=$(resolve_connector_host_user)
        connector_host_run_group=$(resolve_connector_host_group)
        if [ "$connector_host_run_user" = "$exec_user" ]; then
            echo "[entrypoint] warning: no separate $connector_host_user account, so the" >&2
            echo "[entrypoint] warning: connector host shares the executor's uid. A marketplace" >&2
            echo "[entrypoint] warning: package's request is then made by the process that holds" >&2
            echo "[entrypoint] warning: the credential store, which is what #195 moves away." >&2
        else
            echo "[entrypoint] connector host account: $connector_host_run_user (separate uid)"
        fi
        if ! prepare_connector_host_paths; then
            echo "[entrypoint] warning: the connector host paths could not be prepared" >&2
            echo "[entrypoint] warning: marketplace connectors will refuse rather than run" >&2
        else
            export NANOINFRA_CONNECTOR_HOST_SOCKET_GROUP="$connector_host_run_group"
            rm -f "$connector_host_socket_path" 2>/dev/null || true
            start_connector_host "$mcp_host_workspace"
            if wait_for_connector_host_socket; then
                chown "$connector_host_run_user:$connector_host_run_group" \
                    "$connector_host_socket_dir" "$connector_host_socket_path" 2>/dev/null || \
                    echo "[entrypoint] warning: chown $connector_host_socket_dir failed"
                chmod 2710 "$connector_host_socket_dir" 2>/dev/null || true
                chmod 660 "$connector_host_socket_path" 2>/dev/null || true
            fi
            export NANOINFRA_CONNECTOR_HOST_SOCKET="$connector_host_socket_path"
        fi
    fi

    if setpriv --reuid="$run_user" --regid="$run_user" --init-groups true 2>/dev/null; then
        echo "[entrypoint] dropping privileges to $run_user via setpriv"
        # The agent runs without the secrets key. The executor already started with it, and
        # nothing on the agent side decrypts: #41 moved the transcript scrub to the executor, and
        # the executor resolves the credential for a remote action. What the agent loses is the
        # ability to mint a secret, which now refuses with the message the routes already carry
        # for a missing key rather than writing one the executor cannot own.
        #
        # This is what lets the credential store carry a group read above. Without it, a group
        # read would hand the agent account the key and the ciphertext together, which is every
        # plaintext credential -- and setpriv passes the environment through, so the key reached
        # the agent whether anything used it or not.
        echo "[entrypoint] agent runs without NANOINFRA_SECRETS_KEY: the executor holds it"
        # umask 0007 for the same reason the executor sets it: a file the agent creates in the
        # setgid job directory must stay writable by the other account, or the next update from
        # the executor fails on a file the agent made two seconds earlier. The default 022 would
        # produce 644 there and the failure would depend on which side wrote first.
        umask 0007
        exec env -u NANOINFRA_SECRETS_KEY \
            setpriv --reuid="$run_user" --regid="$run_user" --init-groups nanoinfra "$@"
    fi
    echo "[entrypoint] error: started as root but setpriv privilege drop failed — refusing to run as root" >&2
    exit 1
fi

# Already non-root. A start that is not root cannot place the two processes on two accounts,
# so the split is not enforced here. Say it, then continue.
warn_split_not_enforced "this container did not start as root"

# The fetcher starts anyway, and the gateway starts it (#19). This script exports neither fetcher
# variable here, so the Python side finds no external fetcher and supervises one of its own. That
# child holds the agent's uid, and it is still a separate process with no credential store in it.
warn_fetcher_split_not_enforced "this container did not start as root"

# Make sure the data dir is writable before starting.
if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    owner_uid=$(stat -c %u "$dir" 2>/dev/null || stat -f %u "$dir" 2>/dev/null)
    cat >&2 <<EOF
Error: $dir is not writable (owned by UID $owner_uid, running as UID $(id -u)).

Fix (pick one):
  Host:   sudo chown -R 1000:1000 ~/.nanoinfra
  Docker: docker run --user \$(id -u):\$(id -g) ...
  Podman: podman run --userns=keep-id ...
EOF
    exit 1
fi

exec nanoinfra "$@"
