"""The agent tool that actually connects to and runs something on a
Server. Highest-consequence tool in this codebase -- dry_run defaults
to true and is worded strongly, same convention as
nanoinfra/agent/tools/diagrams.py's UpdateDiagramTool, but this is the
one place in the whole system where a secretRef gets resolved to a
real credential value; that value is passed to the backend and never
placed anywhere else (not the returned ToolResult, not the ServerJob's
command/output/error fields, not a log line).
"""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_REMOTE,
    command_digest,
    record_observation,
)
from nanoinfra.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import (
    ABSOLUTE_CEILING_S,
    BoundedOutput,
    ExecutionBackend,
    truncate_output,
)
from nanoinfra.servers.execution.timeout import IdleTimeoutTracker, run_with_idle_timeout
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.lookup import resolve_server
from nanoinfra.servers.network_guard import validate_server_target
from nanoinfra.servers.scope import resolve_scope_label
from nanoinfra.servers.store import ServerStore

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext

# Providers with a real network-address concept at this layer -- if
# _target_host() can't find one for these, that's not "nothing to check"
# (unlike ssm, which is validated via IAM/instance-profile instead of a
# dialed address); it means execute() must refuse rather than proceed
# unguarded. Keys are the config field name(s) shown to the user in the
# refusal message.
#
# Each entry must name EXACTLY the config field(s) the corresponding
# backend actually reads to decide what to connect to -- nothing more.
# Listing an extra host-shaped field here (ansible-runner used to list
# "host", which AnsibleRunnerBackend never reads) means the guard
# validates one value while the backend dials another, which is a
# bypass, not extra safety: an agent could add {"host": "8.8.8.8"} to a
# group-only ansible config and satisfy the guard with an address
# nothing ever connects to. tests/servers/execution/
# test_guard_backend_consistency.py holds this table to that rule.
_HOST_FIELDS_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "ssh": ("host",),
    "ansible-runner": ("inventoryHost",),
    "api": ("baseUrl",),
}
_KNOWN_PROVIDER_IDS = frozenset({"ssh", "ansible-runner", "ssm", "api"})

# Providers whose on_activity chunks are the command's own output arriving
# incrementally, rather than a single status token at completion.
_STREAMING_PROVIDERS = frozenset({"ssh"})

# How often partial output is checkpointed to the job file while a command runs.
_PARTIAL_OUTPUT_PERSIST_INTERVAL_S = 5.0

# Providers whose work cannot actually be stopped when this tool gives up waiting
# (both wrap a blocking call in asyncio.to_thread; see timeout.py's module
# docstring). Their timeout message must not imply the command was stopped.
_UNSTOPPABLE_ON_TIMEOUT = frozenset({"ansible-runner", "ssm"})


def _backend_and_default_timeout(provider_id: str) -> tuple[ExecutionBackend, int]:
    # Every backend class is imported lazily, inside its own branch -- an
    # unguarded top-level `from ...ssh_backend import SSHBackend`-style
    # import here would make loading THIS tool module fail if even one of
    # asyncssh/ansible-runner/boto3 (all optional, the new `servers`
    # extra) isn't installed. nanoinfra/agent/tools/loader.py's pkgutil
    # scan wraps each tool module's import in try/except, so that failure
    # mode is "this one tool silently goes missing from the tool list",
    # not an agent-wide crash -- still worth avoiding, since a partially
    # installed `servers` extra would otherwise make ALL FOUR providers
    # unavailable instead of just the one actually missing a library.
    # base.py and timeout.py have no such dependency, so
    # ExecutionBackend/IdleTimeoutTracker/run_with_idle_timeout stay as
    # ordinary top-of-file imports above.
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
    if provider_id == "api":
        from nanoinfra.servers.execution.api_backend import DEFAULT_IDLE_TIMEOUT_S, ApiBackend

        return ApiBackend(), DEFAULT_IDLE_TIMEOUT_S
    raise ValueError(f"Unknown providerId: {provider_id!r}")


def _target_host(provider_id: str, config: dict[str, str]) -> str | None:
    """The host this providerId actually connects to, for target
    validation -- None for providers with no single "host" concept at
    this layer (ssm targets an AWS instance id via IAM, not a raw
    network address the local process dials).

    Each branch reads exactly the field its backend reads, and no
    fallbacks across fields the backend ignores: SSHBackend only ever
    dials ``host`` (never ``inventoryHost``) and AnsibleRunnerBackend
    only ever targets ``inventoryHost``/``group`` (never ``host``).
    Guard and backend reading different keys is how a validated address
    ends up being one nothing connects to.

    For ssh/ansible-runner/api, None does NOT mean "nothing to validate"
    -- those providers DO have a checkable network address; None here
    just means the server's config didn't supply one (e.g. an
    ansible-runner server configured with only `group`, which is an
    inventory-file label rather than an address there is anything to
    resolve). Callers must treat that case as "cannot validate, refuse"
    rather than "validated, proceed" -- see _HOST_FIELDS_BY_PROVIDER and
    execute()'s use of it.
    """
    if provider_id == "ssh":
        return config.get("host")
    if provider_id == "ansible-runner":
        return config.get("inventoryHost")
    if provider_id == "api":
        from urllib.parse import urlparse

        return urlparse(config.get("baseUrl", "")).hostname
    return None


@tool_parameters(
    tool_parameters_schema(
        server_id_or_name=StringSchema("Exact server id, or its name.", min_length=1),
        command=StringSchema(
            "What to run. Meaning depends on the server's provider: a shell "
            "command for ssh; a shell command run ad-hoc via ansible for "
            "ansible-runner; a shell command for ssm; '<METHOD> <path>' "
            "(method optional, defaults to GET) for api.",
            min_length=1,
        ),
        timeout_s=StringSchema(
            "Optional override for the idle/absolute timeout in seconds. Omit to use the provider's default.",
            nullable=True,
        ),
        dry_run=BooleanSchema(
            description=(
                "Defaults to true: resolve the server and preview exactly what would "
                "run (server, provider, command) without connecting to anything. "
                "Only pass dry_run=false after the user has explicitly confirmed. "
                "Never set dry_run=false on the first call -- this is the only tool "
                "in the system that actually connects to remote infrastructure."
            ),
            default=True,
        ),
        required=["server_id_or_name", "command"],
    )
)
class ExecuteOnServerTool(Tool):
    """Preview (default) or actually run a command/action on a Server."""

    capability_class = "mutate.remote"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        workspace = Path(ctx.workspace)
        return cls(servers=ServerStore(workspace), secrets=SecretStore(workspace), jobs=JobStore(workspace))

    def __init__(self, *, servers: ServerStore, secrets: SecretStore, jobs: JobStore) -> None:
        self.servers = servers
        self.secrets = secrets
        self.jobs = jobs

    @property
    def name(self) -> str:
        return "execute_on_server"

    @property
    def description(self) -> str:
        return (
            "Connect to an inventoried server and run a command/action on it, via "
            "whichever connection provider that server uses (ssh/ansible-runner/ssm/api). "
            "Defaults to dry_run=true -- preview the resolved server/provider/command "
            "without connecting to anything, then only proceed with dry_run=false and "
            "the same arguments after the user explicitly confirms. This is the highest-"
            "consequence tool in the system: never infer approval, never retry with a "
            "different command without a fresh confirmation."
        )

    async def execute(
        self,
        server_id_or_name: str,
        command: str,
        timeout_s: str | None = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        server = resolve_server(self.servers, server_id_or_name)
        if server is None:
            return ToolResult.error(f"No server matches {server_id_or_name!r}.")

        # Everything that can refuse this call runs BEFORE the dry_run branch, so a
        # preview of an unknown-provider or metadata-pointed server says so instead
        # of cheerfully inviting the user to confirm a call that can only fail.
        # None of these checks connect to anything or decrypt anything.
        if server.provider_id not in _KNOWN_PROVIDER_IDS:
            return ToolResult.error(f"Unknown providerId: {server.provider_id!r}.")

        target_host = _target_host(server.provider_id, server.config)
        if target_host:
            ok, error = validate_server_target(target_host)
            if not ok:
                return ToolResult.error(f"Refusing to execute: {error}")
        elif server.provider_id in _HOST_FIELDS_BY_PROVIDER:
            # This provider DOES have a network-address concept (unlike
            # ssm, validated via IAM instead) but the config didn't supply
            # one -- e.g. an ansible-runner server configured with only
            # `group`, which is an inventory label, not an address. Refuse
            # rather than proceed unguarded into secret decryption and the
            # backend.
            fields = "/".join(_HOST_FIELDS_BY_PROVIDER[server.provider_id])
            configured_keys = ", ".join(sorted(server.config.keys())) or "nothing"
            return ToolResult.error(
                f"Cannot validate network target: server config has no {fields} to check "
                f"(found only: {configured_keys})."
            )

        try:
            idle_timeout_override = int(timeout_s) if timeout_s else None
        except ValueError:
            return ToolResult.error(f"Invalid timeout_s: {timeout_s!r} is not an integer.")

        # Log-only in M1 (#3): record the decision the gate *would* make, and enforce
        # nothing. Positioned after the refusals above -- those calls never reach a
        # gate, so recording them would inflate the count an operator uses to size
        # #8's breakage -- and before the dry_run branch, because a preview is still a
        # mutate.remote call. Only the recorded decision distinguishes the two.
        record_observation(
            capability_class=MUTATE_REMOTE,
            decision="preview" if dry_run else "would_gate",
            tool=self.name,
            server_id=server.id,
            server_name=server.name,
            provider_id=server.provider_id,
            target=target_host,
            # #4's blast radius, beside the one address the guard checked. `target`
            # names what this process dials. `scope` names how many hosts the action
            # reaches, which for ansible-runner is a resolved inventory fact rather
            # than a config field. resolve_scope_label() reads local files only and
            # never raises, so a log-only record cannot fail an execution.
            scope=resolve_scope_label(server),
            command_digest=command_digest(command),
        )

        if dry_run:
            validated = f" validated target={target_host!r}" if target_host else ""
            return (
                f"Preview (not executed): server={server.name!r} (id={server.id!r}) "
                f"provider={server.provider_id!r} command={command!r}{validated}\n"
                "Nothing was run. Call execute_on_server again with the same arguments "
                "and dry_run=false only after the user explicitly confirms."
            )

        # Resolved before the secret: this lazily imports the provider's optional
        # library, and if it isn't installed there is no point decrypting a
        # credential first. Same ordering principle as the checks above.
        backend, default_idle_timeout = _backend_and_default_timeout(server.provider_id)
        idle_timeout = idle_timeout_override if idle_timeout_override is not None else default_idle_timeout

        secret_value: str | None = None
        if server.secret_ref:
            secret_value = self.secrets.resolve_plaintext(server.secret_ref)
            if secret_value is None:
                return ToolResult.error(
                    f"Server {server.name!r} references secret {server.secret_ref!r}, which no longer exists."
                )
            # Emitted from the resolution site rather than inferred from the tool name:
            # this is the only agent-reachable path to plaintext today (SecretStore's
            # other consumers are WebUI operator routes), and one call both resolves the
            # secret and uses it. That may not stay true. secret_ref is an opaque id.
            record_observation(
                capability_class=CREDENTIAL_ACCESS,
                decision="would_gate",
                tool=self.name,
                server_id=server.id,
                server_name=server.name,
                secret_ref=server.secret_ref,
                command_digest=command_digest(command),
            )

        job = self.jobs.create(
            server_id=server.id, provider_id=server.provider_id, command=command, timeout_s=idle_timeout
        )
        self.jobs.mark_running(job.id)

        tracker = IdleTimeoutTracker(idle_timeout_s=idle_timeout, absolute_ceiling_s=ABSOLUTE_CEILING_S)

        # Only streaming providers' on_activity chunks are the command's real
        # output (see base.py's ExecutionBackend docstring -- the others pass a
        # short status token exactly once at completion), so only those are worth
        # accumulating. Two things depend on this buffer: the timeout path
        # returning what was read before it fired instead of nothing, and the
        # periodic JobStore.update_output() below, which is what makes a job
        # record survive a crash mid-run with something better than "".
        streams_output = server.provider_id in _STREAMING_PROVIDERS
        partial = BoundedOutput()
        last_persist_at = time.monotonic()

        def on_activity(chunk: str) -> None:
            nonlocal last_persist_at
            tracker.touch(chunk)
            if not streams_output:
                return
            partial.append(chunk)
            now = time.monotonic()
            if now - last_persist_at < _PARTIAL_OUTPUT_PERSIST_INTERVAL_S:
                return
            last_persist_at = now
            # Throttled: this is a synchronous atomic write with an fsync, and
            # chunks can arrive every few kilobytes. Failing to checkpoint
            # partial output must never take down a running command.
            with suppress(OSError, KeyError, ValueError):
                self.jobs.update_output(job.id, partial.text())

        result = await run_with_idle_timeout(
            backend.run(server, command, secret_value, on_activity=on_activity),
            tracker,
            partial_output=(partial.text if streams_output else None),
        )

        # One cap for every backend, applied to the same string that gets both
        # persisted into the job file and handed to the model -- neither the
        # ServerJob JSON nor the tool result may grow without bound just because
        # a remote command was chatty.
        output = truncate_output(result.output)

        if result.timed_out:
            # Honesty, not confidence: for ansible-runner and ssm, "timed out"
            # only means this tool stopped waiting. The work itself keeps going
            # (an uncancellable pool thread, or a command already accepted by
            # SSM), so telling the user it stopped would invite a retry that puts
            # two copies of the same command in flight.
            caveat = (
                " The remote command may still be running and this tool cannot confirm it has "
                "stopped -- check on it before retrying."
                if server.provider_id in _UNSTOPPABLE_ON_TIMEOUT
                else ""
            )
            self.jobs.complete(
                job.id,
                exit_code=None,
                output=output,
                error=f"Timed out.{caveat}",
                status="timed_out",
            )
            partial_note = f"\nPartial output before the timeout:\n{output}" if output else ""
            return ToolResult.error(
                f"Timed out running {command!r} on {server.name!r}.{caveat}{partial_note}"
            )
        if result.error:
            self.jobs.complete(job.id, exit_code=result.exit_code, output=output, error=result.error, status="failed")
            return ToolResult.error(f"Failed running {command!r} on {server.name!r}: {result.error}")

        self.jobs.complete(job.id, exit_code=result.exit_code, output=output, error=None, status="completed")
        return f"Ran {command!r} on {server.name!r} (exit code {result.exit_code}):\n{output}"


__all__ = ["ExecuteOnServerTool"]
