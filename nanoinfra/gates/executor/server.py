"""The executor: the only process that reaches a host -- nanoinfraorg/nanoinfra#18.

Everything the agent must lose lives here. The credential store, the four transports, the
target guard, the scope resolver, and the gate itself. The agent submits a structured request
and renders the reply, so a compromised agent can ask and cannot act.

The order inside ``handle`` is the same order the tool used before the split, and for the same
reasons. Refusals that need no credential run first, the gate runs before the credential
resolves, and the job record follows the decision. What changed is the address space: the agent
no longer holds the plaintext, the backends, or the guard.

This module imports the privileged parts on purpose. ``nanoinfra/agent/tools/server_execution.py``
must import none of them, and a test asserts that. Import direction is how the split stays true
rather than merely intended.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_REMOTE,
    command_digest,
    record_observation,
)
from nanoinfra.gates.executor.protocol import (
    ExecuteResponse,
    ProtocolError,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)
from nanoinfra.gates.policy import Outcome, evaluate, load_policy
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import (
    ABSOLUTE_CEILING_S,
    BoundedOutput,
    truncate_output,
)
from nanoinfra.servers.execution.timeout import IdleTimeoutTracker, run_with_idle_timeout
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.lookup import resolve_server
from nanoinfra.servers.network_guard import validate_server_target
from nanoinfra.servers.scope import (
    ALL,
    ScopeResolutionError,
    resolve_scope,
    resolve_scope_label,
)
from nanoinfra.servers.store import ServerStore

if TYPE_CHECKING:
    from nanoinfra.config.gates import GatesConfig
    from nanoinfra.gates.executor.protocol import ExecuteRequest

_KNOWN_PROVIDER_IDS = frozenset({"ssh", "ansible-runner", "ssm", "api"})
_STREAMING_PROVIDERS = frozenset({"ssh"})
_UNSTOPPABLE_ON_TIMEOUT = frozenset({"ansible-runner", "ssm"})
_PARTIAL_OUTPUT_PERSIST_INTERVAL_S = 5.0

# The socket's own mode is not honoured on every platform, so the directory carries the
# control. 0o700 keeps another local account out of the executor's door.
_SOCKET_DIR_MODE = 0o700


@dataclass(slots=True)
class Executor:
    """Serves one request at a time. Holds the credential store and the transports."""

    workspace: Path
    gates_loader: Callable[[], GatesConfig] = load_policy
    # The audit store (#16). The executor decides, so the executor records. #33 wired this into
    # the tool, and the split moved the decision here, so the record moved with it.
    audit: Any = None

    async def handle(self, request: ExecuteRequest) -> ExecuteResponse:
        """Answer one request. Never raises for a refusal: a refusal is a response."""
        servers = ServerStore(self.workspace)
        server = resolve_server(servers, request.server_id_or_name)
        if server is None:
            return _error(f"No server matches {request.server_id_or_name!r}.")
        if server.provider_id not in _KNOWN_PROVIDER_IDS:
            return _error(f"Unknown providerId: {server.provider_id!r}.")

        guard_error = _guard(server)
        if guard_error is not None:
            return _error(guard_error)

        try:
            idle_override = int(request.timeout_s) if request.timeout_s else None
        except ValueError:
            return _error(f"Invalid timeout_s: {request.timeout_s!r} is not an integer.")

        record_observation(
            capability_class=MUTATE_REMOTE,
            decision="preview" if request.preview_requested else "would_gate",
            tool="execute_on_server",
            server_id=server.id,
            server_name=server.name,
            provider_id=server.provider_id,
            # #4's blast radius. resolve_scope_label() never raises, so a log-only record
            # cannot fail an action. The split dropped this field briefly, and a record
            # without it cannot answer how many hosts an action would have reached.
            scope=resolve_scope_label(server),
            command_digest=command_digest(request.command),
        )

        if request.preview_requested:
            return ExecuteResponse(
                ok=True,
                output=_preview_line(server, request.command),
                exit_code=None,
                error=None,
                reason="the caller asked for a preview",
            )

        allowed, reason, resolution = self._gate(server, request, servers)
        outcome = Outcome.ALLOW if allowed else Outcome.DENY
        recorded = self._record(outcome, server, request, reason=reason, resolution=resolution)
        if recorded is not None:
            return recorded

        if not allowed:
            return ExecuteResponse(
                ok=False,
                output=_preview_line(server, request.command),
                exit_code=None,
                error=None,
                reason=reason,
            )

        return await self._run(server, request, idle_override)

    def _record(
        self,
        outcome: Outcome,
        server: Any,
        request: ExecuteRequest,
        *,
        reason: str,
        resolution: Any,
    ) -> ExecuteResponse | None:
        """Write the audit record, or refuse the action when the write fails.

        The executor decides, so the executor records. #16 raises rather than swallow a write
        failure, so an action that nothing recorded does not run: the audit log is the only
        account of what this process did.

        A refusal records too, and it still refuses when the record fails. The caller then reads
        both facts.
        """
        if self.audit is None:
            return None
        try:
            self.audit.record(
                decision="allow" if outcome is Outcome.ALLOW else "denied",
                capability_class=MUTATE_REMOTE,
                execution_context=request.execution_context,
                session_id=request.session_id,
                tool="execute_on_server",
                scope=getattr(resolution, "scope", None),
                hosts=list(getattr(resolution, "hosts", ()) or ()),
                command=request.command,
                reason=reason or None,
            )
        except OSError as exc:
            return _error(
                f"The executor did not act on {server.name!r}. The gate decided, and the audit "
                f"record could not be written ({exc}). An action that nothing records does not "
                "run."
            )
        return None

    def _gate(
        self, server: Any, request: ExecuteRequest, servers: ServerStore
    ) -> tuple[bool, str, Any]:
        """Ask the gate, and hand back the resolution so the record can name the hosts.

        The executor verifies, and it does not trust the caller's word about the target.
        """
        interactive = request.execution_context == "interactive"

        try:
            resolution = resolve_scope(server)
        except ScopeResolutionError as exc:
            if interactive:
                return True, "", None
            return (
                False,
                f"The target did not resolve, so no grant can cover it ({exc}).",
                None,
            )

        if resolution.scope == ALL:
            return False, (
                "The pattern names an unbounded host set, so its scope is `all`. No policy "
                "permits `all` scope, and no approval path exists for it."
            ), resolution
        if interactive:
            # #8 enforces the unattended half. The interactive approval path arrives with #27.
            return True, "", resolution

        decision = evaluate(
            self.gates_loader(),
            capability_class=MUTATE_REMOTE,
            scope=resolution.scope,
            execution_context=request.execution_context,
            hosts=resolution.hosts,
            command=request.command,
            servers=servers,
        )
        if decision.outcome is Outcome.ALLOW:
            return True, decision.reason, resolution
        return False, decision.reason, resolution

    async def _run(
        self, server: Any, request: ExecuteRequest, idle_override: int | None
    ) -> ExecuteResponse:
        """Resolve the credential, dial the host, and record the job."""
        backend, default_idle = _backend_for(server.provider_id)
        idle_timeout = idle_override if idle_override is not None else default_idle

        secret_value: str | None = None
        if server.secret_ref:
            secret_value = SecretStore(self.workspace).resolve_plaintext(server.secret_ref)
            if secret_value is None:
                return _error(
                    f"Server {server.name!r} references secret {server.secret_ref!r}, "
                    "which no longer exists."
                )
            # The one place a plaintext exists, and it exists only in this process (#18).
            record_observation(
                capability_class=CREDENTIAL_ACCESS,
                decision="would_gate",
                tool="execute_on_server",
                server_id=server.id,
                server_name=server.name,
                secret_ref=server.secret_ref,
                command_digest=command_digest(request.command),
            )

        jobs = JobStore(self.workspace)
        job = jobs.create(
            server_id=server.id,
            provider_id=server.provider_id,
            command=request.command,
            timeout_s=idle_timeout,
        )
        jobs.mark_running(job.id)

        tracker = IdleTimeoutTracker(
            idle_timeout_s=idle_timeout, absolute_ceiling_s=ABSOLUTE_CEILING_S
        )
        streams = server.provider_id in _STREAMING_PROVIDERS
        partial = BoundedOutput()
        last_persist = time.monotonic()

        def on_activity(chunk: str) -> None:
            nonlocal last_persist
            tracker.touch(chunk)
            if not streams:
                return
            partial.append(chunk)
            now = time.monotonic()
            if now - last_persist < _PARTIAL_OUTPUT_PERSIST_INTERVAL_S:
                return
            last_persist = now
            with contextlib.suppress(OSError, KeyError, ValueError):
                jobs.update_output(job.id, partial.text())

        result = await run_with_idle_timeout(
            backend.run(server, request.command, secret_value, on_activity=on_activity),
            tracker,
            partial_output=(partial.text if streams else None),
        )
        output = truncate_output(result.output)

        if result.timed_out:
            caveat = (
                " The remote command may still run, and this side cannot confirm it stopped."
                if server.provider_id in _UNSTOPPABLE_ON_TIMEOUT
                else ""
            )
            jobs.complete(
                job.id, exit_code=None, output=output, error=f"Timed out.{caveat}",
                status="timed_out",
            )
            return ExecuteResponse(
                ok=False, output=output, exit_code=None,
                error=f"Timed out running the command on {server.name!r}.{caveat}", reason="",
            )
        if result.error:
            jobs.complete(
                job.id, exit_code=result.exit_code, output=output, error=result.error,
                status="failed",
            )
            return ExecuteResponse(
                ok=False, output=output, exit_code=result.exit_code,
                error=f"Failed on {server.name!r}: {result.error}", reason="",
            )

        jobs.complete(job.id, exit_code=result.exit_code, output=output, error=None,
                      status="completed")
        return ExecuteResponse(
            ok=True, output=output, exit_code=result.exit_code, error=None, reason="",
        )


def _error(message: str) -> ExecuteResponse:
    return ExecuteResponse(ok=False, output="", exit_code=None, error=message, reason="")


_MAX_LABELLED_HOSTS = 8


def _preview_line(server: Any, command: str) -> str:
    """The resolved action in one line, including the blast radius.

    The host list is not decoration. An operator reads this line after a refusal to learn which
    grant to write, and a group that names three hosts must say which three. #18 dropped this
    briefly, and a group preview then named no host and not even the pattern.

    A cap keeps a thousand-host pattern from flooding a result.
    """
    return (
        f"Preview (not executed): server={server.name!r} (id={server.id!r}) "
        f"provider={server.provider_id!r} command={command!r} target={_targets_label(server)}"
    )


def _targets_label(server: Any) -> str:
    """What the guard validated, as text. Falls back to the reason it could not resolve."""
    try:
        resolution = resolve_scope(server)
    except ScopeResolutionError as exc:
        return f"unresolved ({exc})"
    hosts = list(resolution.hosts)
    shown = ", ".join(hosts[:_MAX_LABELLED_HOSTS])
    more = f" +{len(hosts) - _MAX_LABELLED_HOSTS} more" if len(hosts) > _MAX_LABELLED_HOSTS else ""
    pattern = f"{resolution.pattern!r} -> " if resolution.pattern else ""
    return f"{pattern}{len(hosts)} host(s): {shown}{more}"


def _guard(server: Any) -> str | None:
    """Validate what this process will dial, or say why it cannot.

    ssm carries no dialed address and IAM authorizes it instead, so it has nothing to check
    here. Every other provider that names an address gets that address checked.
    """
    config: dict[str, str] = server.config
    if server.provider_id == "ssh":
        target = config.get("host")
    elif server.provider_id == "api":
        from urllib.parse import urlparse

        target = urlparse(config.get("baseUrl", "")).hostname
    elif server.provider_id == "ansible-runner":
        # Both fields are patterns to ansible. AnsibleRunnerBackend passes
        # `inventoryHost or group` as host_pattern, and resolve_scope expands the same field.
        # So the guard expands whichever one the backend will use, and checks every host.
        #
        # `inventoryHost` used to take the single-address path, which validated a label. A
        # label naming three hosts was checked as one name, and it failed closed only by
        # accident of DNS. A group label that did resolve to a permitted address would have
        # passed the guard while the play ran against every host in the group.
        target = config.get("inventoryHost")
        if target or config.get("group"):
            try:
                hosts = resolve_scope(server).hosts
            except ScopeResolutionError as exc:
                if target:
                    # No local inventory to read. Ansible still reads ansible.cfg and
                    # /etc/ansible/hosts, and the resolver cannot see either, so fall back to
                    # the single-address check this field had before. That keeps interactive
                    # runs working, and a group name still fails closed because it does not
                    # resolve in DNS.
                    ok, error = validate_server_target(target)
                    return None if ok else f"Refusing to execute: {error}"
                return f"Cannot validate network target: {exc}"
            for host in hosts:
                ok, error = validate_server_target(host)
                if not ok:
                    return f"Refusing to execute: {error} One blocked host refuses all of them."
            return None
    else:
        return None

    if not target:
        return "Cannot validate network target: the server config names no address to check."
    ok, error = validate_server_target(target)
    return None if ok else f"Refusing to execute: {error}"


def _backend_for(provider_id: str) -> tuple[Any, int]:
    """Import one backend lazily. Each optional library stays optional."""
    if provider_id == "ssh":
        from nanoinfra.servers.execution.ssh_backend import DEFAULT_IDLE_TIMEOUT_S, SSHBackend

        return SSHBackend(), DEFAULT_IDLE_TIMEOUT_S
    if provider_id == "ansible-runner":
        from nanoinfra.servers.execution.ansible_backend import (
            DEFAULT_IDLE_TIMEOUT_S,
            AnsibleRunnerBackend,
        )

        return AnsibleRunnerBackend(), DEFAULT_IDLE_TIMEOUT_S
    if provider_id == "ssm":
        from nanoinfra.servers.execution.ssm_backend import DEFAULT_IDLE_TIMEOUT_S, SSMBackend

        return SSMBackend(), DEFAULT_IDLE_TIMEOUT_S
    from nanoinfra.servers.execution.api_backend import DEFAULT_IDLE_TIMEOUT_S, ApiBackend

    return ApiBackend(), DEFAULT_IDLE_TIMEOUT_S


def serve_forever(
    socket_path: Path | str, *, workspace: Path | str, max_requests: int | None = None
) -> None:
    """Bind the Unix socket and serve until terminated.

    ``max_requests`` exists for tests. Production passes nothing and the loop runs until the
    supervisor stops the process.

    The socket file is removed on exit. A stale file blocks the next bind, and a supervisor that
    restarts the executor must not need a human to delete one.
    """
    path = Path(socket_path)
    # A private mode only on a directory this process creates. A two-uid deployment owns that
    # decision: with separate accounts the directory is owned by the executor and carries setgid
    # plus group traversal (2710), so the agent account can reach a known socket name without
    # listing the directory or creating anything in it. A blanket chmod here would lock the agent
    # out, and a split the agent cannot talk to is worse than the mode it replaced.
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, _SOCKET_DIR_MODE)
    if path.exists():
        path.unlink()

    executor = Executor(workspace=Path(workspace), audit=_audit_store())
    served = 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(8)
        logger.info("gates: executor listening on {}", path)
        try:
            while max_requests is None or served < max_requests:
                conn, _ = server.accept()
                with conn:
                    _serve_one(conn, executor)
                served += 1
        finally:
            with contextlib.suppress(OSError):
                path.unlink()


def _audit_store() -> Any:
    """The audit store this process writes to (#16).

    It lives beside the gateway's data rather than in the workspace. The workspace is reachable
    by the agent's filesystem tools, and an audit log a model can edit is not an audit log.

    ``pin_root`` holds the device and inode of that directory for the life of this process
    (#36). A model cannot un-append a record, and it could still rename the directory that
    holds every record, because write rights on a parent allow a rename of any entry inside it.
    entrypoint.sh takes that write right away. This pin covers the case the layout cannot: an
    executor that keeps running while its audit root moves underneath it. The store then raises
    on every read and every write, ``_record`` refuses the action and names the cause, and the
    latch restore degrades and keeps every session latched. The cost is availability, and the
    alternative cost is every latch the log holds.
    """
    from nanoinfra.config.paths import get_data_dir
    from nanoinfra.gates.audit import AuditStore

    store = AuditStore(get_data_dir() / "gates", config=load_policy().audit, pin_root=True)
    logger.info("gates: executor audit root {} pinned as {}", store.root, store.pinned_identity)
    return store


def _serve_one(conn: socket.socket, executor: Executor) -> None:
    """Answer one connection. A bad frame gets a refusal, and never a crash.

    A peer that speaks nonsense must not take the executor down. The executor is the only way
    to reach a host, so its availability is part of the control.
    """
    try:
        payload = read_frame(conn)
        request = decode_request(payload)
    except ProtocolError as exc:
        logger.warning("gates: executor refused a frame: {}", exc)
        with contextlib.suppress(OSError, ProtocolError):
            write_frame(conn, encode_response(_error(f"Malformed request: {exc}")))
        return

    try:
        response = asyncio.run(executor.handle(request))
    except Exception as exc:  # noqa: BLE001 -- one bad request must not end the process
        logger.exception("gates: executor failed a request")
        response = _error(f"The executor failed this request: {exc}")

    with contextlib.suppress(OSError, ProtocolError):
        write_frame(conn, encode_response(response))


__all__ = ["Executor", "serve_forever"]
