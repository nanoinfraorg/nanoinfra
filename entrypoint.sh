#!/bin/sh
dir="$HOME/.nanoinfra"

# Item 15 (nanoinfraorg/nanoinfra#18) splits the agent from the executor. This script is the
# one entry point for this image, so it is also the supervisor: it starts the executor on its
# own account, then it execs the agent. Only a root start can place two processes on two
# accounts, so the split happens here and not in Python.
exec_user="nanoinfra-exec"
ipc_group="nanoinfra-ipc"
# The socket directory lives outside the agent's home on purpose. Write rights on a parent
# directory allow a rename of any entry inside it, whatever the entry's own owner is. So a
# socket directory under $HOME/.nanoinfra could be moved aside by the agent account, and the
# agent could then present its own socket at the expected path. /run is root-owned, and the
# agent account cannot write it.
socket_dir="/run/nanoinfra-exec"
socket_path="$socket_dir/executor.sock"
# The executor's entry point is fixed by #18. Nothing else about the executor is assumed here.
executor_module="nanoinfra.gates.executor"

# Render deploy path (see render.yaml + render-config.json). Gated on Render's
# automatic RENDER=true env var so local Docker/podman usage is unaffected.
# Initializes the on-disk config from the committed template (wiring secrets via
# ${VAR} env vars, keeping runtime data on the persistent disk) and appends the
# --config flag. Logs each decision so a failed start is diagnosable in Render's
# logs. Privilege dropping is handled below, for every root start (not just here).
if [ "$RENDER" = "true" ]; then
    echo "[entrypoint] Render deploy — starting as $(id)"
    mkdir -p "$dir" || echo "[entrypoint] warning: mkdir $dir failed"
    config="$dir/config.json"
    # Initialize config only when it does not already exist, so WebUI/provider
    # settings edited at runtime survive restarts. The disk persists config.json
    # across deploys; overwriting it every boot would discard those changes.
    if [ ! -f "$config" ]; then
        echo "[entrypoint] initializing $config from render-config.json"
        cp /app/render-config.json "$config" || echo "[entrypoint] warning: cp config failed"
    else
        echo "[entrypoint] existing $config found — leaving it in place"
    fi
    set -- "$@" --config "$config"
fi

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
    printf '%s' "$dir/workspace"
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

    mkdir -p "$workspace" || return 1
    chown nanoinfra:nanoinfra "$workspace" 2>/dev/null || \
        echo "[entrypoint] warning: chown $workspace failed"
    mkdir -p "$workspace/secrets" "$dir/gates" || return 1
    chown -R "$exec_user:$exec_user" "$workspace/secrets" || return 1
    chmod 700 "$workspace/secrets" || return 1
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
                python -m "$executor_module" --socket "$socket_path" --workspace "$workspace"
            status=$?
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

# Say plainly what the deployment does not have. An operator must never read silence as a
# guarantee, so a single-uid start states the property it lacks.
warn_split_not_enforced() {
    echo "[entrypoint] warning: $1" >&2
    echo "[entrypoint] warning: the agent and the executor share one uid, so either one can" >&2
    echo "[entrypoint] warning: ptrace the other and read its memory. The #18 privilege" >&2
    echo "[entrypoint] warning: split is organisational here, not kernel-enforced." >&2
}

# Drop privileges whenever the container starts as root. Render mounts the
# persistent disk root-owned, and a plain `docker run` also defaults to root now,
# so this covers both. Chown the data dir so the non-root user can write it, then
# re-exec as nanoinfra. Fail closed: if the privilege drop cannot be performed,
# exit rather than run the agent as root.
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
        workspace=$(resolve_workspace "$@")
        echo "[entrypoint] executor workspace: $workspace"
        if ! prepare_executor_paths "$workspace"; then
            warn_split_not_enforced "the executor paths could not be prepared"
        elif [ "$run_user" = "nanoinfra" ]; then
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

    if setpriv --reuid="$run_user" --regid="$run_user" --init-groups true 2>/dev/null; then
        echo "[entrypoint] dropping privileges to $run_user via setpriv"
        exec setpriv --reuid="$run_user" --regid="$run_user" --init-groups nanoinfra "$@"
    fi
    echo "[entrypoint] error: started as root but setpriv privilege drop failed — refusing to run as root" >&2
    exit 1
fi

# Already non-root. A start that is not root cannot place the two processes on two accounts,
# so the split is not enforced here. Say it, then continue.
warn_split_not_enforced "this container did not start as root"

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
