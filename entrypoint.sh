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
# One limit, stated rather than hidden: both roots sit inside directories the agent can write,
# so the agent can still rename or remove those entries. It can never read the contents. A
# layout that also protects availability needs both roots outside the agent's home.
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
    chown -R "$exec_user:$exec_user" "$workspace/secrets" "$dir/gates" || return 1
    chmod 700 "$workspace/secrets" "$dir/gates" || return 1
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
