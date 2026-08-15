"""The agent tool that actually connects to and runs something on a
Server. Highest-consequence tool in this codebase -- a call previews by
default, and the gate rather than the caller decides whether a call
executes (#10); this is also the one place in the whole system where a
secretRef gets resolved to a real credential value; that value is
passed to the backend and never placed anywhere else (not the returned
ToolResult, not the ServerJob's command/output/error fields, not a log
line).
"""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import time
from contextlib import suppress
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_REMOTE,
    command_digest,
    record_observation,
)
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    current_request_execution_context,
)
from nanoinfra.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanoinfra.gates.policy import Outcome, evaluate, load_policy
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
from nanoinfra.servers.scope import (
    ALL,
    ScopeResolutionError,
    resolve_scope,
    resolve_scope_label,
)
from nanoinfra.servers.store import ServerStore

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext

# Providers with a real network-address concept at this layer -- if
# neither _target_host() nor _group_pattern() finds one for these, that's
# not "nothing to check" (unlike ssm, which is validated via
# IAM/instance-profile instead of a dialed address); it means execute()
# must refuse rather than proceed unguarded. Keys are the config field
# name(s) shown to the user in the refusal message.
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
#
# A field is checkable in one of two ways (#9). "host"/"inventoryHost"/
# "baseUrl" name an address, so the guard checks that one value.
# "group" names an inventory pattern, so the guard expands it with #4's
# resolver first and then checks EVERY host it names. A pattern field
# therefore belongs here too: the backend targets it, so the guard owes
# it a check. What it must never get is the single-address check, because
# a validated label is not a validated host set.
_HOST_FIELDS_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "ssh": ("host",),
    "ansible-runner": ("inventoryHost", "group"),
    "api": ("baseUrl",),
}
_KNOWN_PROVIDER_IDS = frozenset({"ssh", "ansible-runner", "ssm", "api"})

# Providers whose on_activity chunks are the command's own output arriving
# incrementally, rather than a single status token at completion.
_STREAMING_PROVIDERS = frozenset({"ssh"})

# How often partial output is checkpointed to the job file while a command runs.
_PARTIAL_OUTPUT_PERSIST_INTERVAL_S = 5.0

class _Disposition(Enum):
    """What the gate decided one call is (#10). Never what the caller asked for.

    execute() branches on this value and never on ``dry_run``, so the argument cannot label
    a call safe. A caller asks. The gate answers, and the tool reports the answer.
    """

    EXECUTE = "execute"
    PREVIEW_ON_REQUEST = "preview_on_request"
    PREVIEW_WITHHELD = "preview_withheld"


# The two sentences that keep the two previews apart (#10). One says a caller asked to
# look. The other says the gate stopped an action. They are constants, and a test pins
# them, because an operator who cannot tell the cases apart learns that a preview means
# nothing.
PREVIEW_ON_REQUEST_NOTE = (
    "Nothing was run, because this call asked for a preview. A preview needs no permission: "
    "it reaches no host and resolves no credential."
)
PREVIEW_WITHHELD_NOTE = (
    "Nothing was run, and nobody asked for a preview. This call asked to execute, and the "
    "capability gate did not permit execution, so the action is shown instead. The same "
    "call gets the same answer, and no argument on the call changes it. Only operator "
    "policy does."
)

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


def _preview_line(server: Any, command: str, validated_target: str | None) -> str:
    """The resolved action in one line. Both preview cases show it (#10).

    A withheld preview needs the action and not only the verdict: this line is what tells an
    operator which grant to write, and it tells the caller that the command was understood.

    ``validated_target`` is what the guard really checked, and never what the config says. For
    a group that is the resolved host set (#9), because a grant lists hosts and a pattern
    names none of them.
    """
    validated = f" validated target={validated_target!r}" if validated_target else ""
    return (
        f"Preview (not executed): server={server.name!r} (id={server.id!r}) "
        f"provider={server.provider_id!r} command={command!r}{validated}"
    )


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
    just means the server's config supplied no single address. That case
    splits in two (#9). An ansible-runner config with only `group` names
    an inventory pattern, so _group_pattern() takes it and the guard
    checks every host the pattern resolves to. Anything else is "cannot
    validate, refuse" rather than "validated, proceed" -- see
    _HOST_FIELDS_BY_PROVIDER and execute()'s use of it.
    """
    if provider_id == "ssh":
        return config.get("host")
    if provider_id == "ansible-runner":
        return config.get("inventoryHost")
    if provider_id == "api":
        from urllib.parse import urlparse

        return urlparse(config.get("baseUrl", "")).hostname
    return None


def _group_pattern(provider_id: str, config: dict[str, str]) -> str | None:
    """The inventory pattern the backend targets when it dials no single address.

    Only ansible-runner has one. AnsibleRunnerBackend reads
    ``inventoryHost or group`` (ansible_backend.py:58), so ``group`` is
    the real target exactly when ``inventoryHost`` is absent or empty.
    This function mirrors that precedence, because a guard that expands
    the other field describes hosts nothing connects to.

    The pattern itself is never a valid argument to
    validate_server_target(). It is a label, and a label carries no
    address. execute() expands it first.
    """
    if provider_id != "ansible-runner":
        return None
    if config.get("inventoryHost"):
        return None
    return config.get("group") or None


# How many resolved hosts a preview names before it counts the rest. #4's resolver permits a
# pattern to reach hundreds of hosts, and a result that lists every one of them buries the
# action an operator is trying to read.
_MAX_LABELLED_HOSTS = 8


def _targets_label(pattern: str, hosts: tuple[str, ...]) -> str:
    """The pattern beside the hosts the guard really checked.

    Both halves matter. The pattern alone hides the blast radius, and the hosts alone hide
    what the backend dials. An operator who reads a withheld group action needs the hosts to
    write a grant, because a grant lists hosts (#24).
    """
    shown = ", ".join(hosts[:_MAX_LABELLED_HOSTS])
    remaining = len(hosts) - _MAX_LABELLED_HOSTS
    if remaining > 0:
        shown = f"{shown}, +{remaining} more"
    noun = "host" if len(hosts) == 1 else "hosts"
    return f"{pattern} -> {len(hosts)} {noun}: {shown}"


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
        # Kept in the schema on purpose (#10). Old sessions and transcripts carry the
        # argument, so removing it would break a replay for no gain. It asks, and it
        # authorizes nothing: execute() reads it only as a request for a preview.
        dry_run=BooleanSchema(
            description=(
                "Defaults to true: ask for a preview of exactly what would run (server, "
                "provider, command) without connecting to anything. This argument only "
                "asks. dry_run=false is a request for execution and not a grant of it: the "
                "capability gate decides whether a call executes, and it may answer a "
                "request to execute with a preview instead. This is the only tool in the "
                "system that actually connects to remote infrastructure."
            ),
            default=True,
        ),
        required=["server_id_or_name", "command"],
    )
)
class ExecuteOnServerTool(Tool):
    """Preview (default) or actually run a command/action on a Server.

    The gate owns the choice between the two (#10). A caller requests, and this tool
    reports what the gate decided.
    """

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
            "Defaults to a preview -- the resolved server/provider/command, with nothing "
            "connected to. Ask for a preview freely. You do not decide execution: the "
            "capability gate decides it from operator policy, and it may answer a request "
            "to execute with a preview. This is the highest-consequence tool in the "
            "system: never infer approval, never call a command safe, and never retry the "
            "same purpose with a different command after a refusal."
        )

    def _decide(
        self, server: Any, command: str, *, preview_requested: bool
    ) -> tuple[_Disposition, str]:
        """Decide preview or execution for one call, and say why (#10).

        ``preview_requested`` is what the caller asked for. It is a request and never an
        authorization, so it can only reduce a call to a preview. It can never turn a
        preview into an execution, which is why ``dry_run=false`` arrives at the policy
        below rather than past it.

        A requested preview asks no policy question at all. Such a call reaches no host and
        resolves no credential, so a refusal there would block reading and teach nobody
        anything.

        The host set comes from #4's resolver rather than from the single dialed address,
        because a grant must cover every host the action reaches. A pattern that will not
        expand withholds execution: an unexpandable pattern is not an empty one, and an
        unknown host set cannot be checked against a grant.

        Note for operators until #24 lands: a grant's ``hosts`` must list the *resolved*
        targets, which for ssh is the address in ``config.host``. #24 resolves grant hosts
        through the same resolver so an inventory name matches too.
        """
        if preview_requested:
            return _Disposition.PREVIEW_ON_REQUEST, ""

        execution_context = current_request_execution_context()
        interactive = execution_context == EXECUTION_CONTEXT_INTERACTIVE

        try:
            resolution = resolve_scope(server)
        except ScopeResolutionError as exc:
            if interactive:
                # An interactive call whose scope will not resolve keeps today's behaviour.
                # The guard above already refuses a pattern it cannot expand, and refusing
                # here as well would withhold interactive calls that work now, over a fact
                # the resolver cannot see (ansible's own ansible.cfg fallback).
                return _Disposition.EXECUTE, ""
            return (
                _Disposition.PREVIEW_WITHHELD,
                "The target did not resolve, so the host set cannot be checked against a "
                f"standing grant ({exc}).",
            )

        # `all` scope has no path to execution, in any context. #7 types the field as
        # deny-only and #8 states the rule as absolute, so this check runs before the
        # interactive short-circuit below. That short-circuit exists because an interactive
        # `approve` has no approval surface before #13 and #27, and `all` has no approval
        # path by design, so the reason for the short-circuit does not reach this case.
        # #9 made group execution reachable, which is what turned this into a live hole.
        if resolution.scope == ALL:
            return (
                _Disposition.PREVIEW_WITHHELD,
                f"The pattern names an unbounded host set, so its scope is `all`. No policy "
                f"permits `all` scope, and no approval path exists for it. It resolved to "
                f"{len(resolution.hosts)} host(s) now, and it would cover a host added later.",
            )

        # Only unattended contexts are enforced past this point (#8). The interactive default
        # is `approve`, and no approval path exists before #13 and #27. Enforcing interactive
        # here would withhold every interactive remote command with no way to answer.
        if interactive:
            return _Disposition.EXECUTE, ""

        decision = evaluate(
            load_policy(),
            capability_class=MUTATE_REMOTE,
            scope=resolution.scope,
            execution_context=execution_context,
            hosts=resolution.hosts,
            command=command,
            # #24: the gate resolves each grant host through this same store, so a grant
            # matches on the address the backend will dial and not on a mutable label.
            servers=self.servers,
        )
        if decision.outcome is Outcome.ALLOW:
            return _Disposition.EXECUTE, decision.reason
        # An unattended context has no interactive fallback. A prompt with nobody present
        # becomes a hang or a rubber stamp, and both are worse than a narrow grant. So an
        # `approve` decision withholds execution here too, exactly like a `deny`.
        return _Disposition.PREVIEW_WITHHELD, decision.reason

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
        pattern = _group_pattern(server.provider_id, server.config)
        # What the guard actually checked, for the preview and the record below. It is the
        # dialed address for one host, and the resolved host set for a group.
        validated_target = target_host
        if target_host:
            ok, error = validate_server_target(target_host)
            if not ok:
                return ToolResult.error(f"Refusing to execute: {error}")
        elif pattern:
            # A group is an inventory label, so there is no address to check yet (#9).
            # #4's resolver names the hosts behind the label, and it reads the same
            # inventory file the backend passes to ansible. It opens no connection.
            try:
                hosts = resolve_scope(server).hosts
            except ScopeResolutionError as exc:
                # An unexpandable pattern is not an empty one. An unreadable inventory
                # leaves the host set unknown, and an unknown host set cannot be guarded,
                # so the old refusal still stands here.
                return ToolResult.error(
                    f"Cannot validate network target: cannot expand {pattern!r} to the hosts "
                    f"it names ({exc})"
                )
            for host in hosts:
                # Every host, and not the first one. A guard that checks one address while
                # the backend dials fourteen is a bypass, exactly as this module's
                # _HOST_FIELDS_BY_PROVIDER comment warns for the single-host case.
                ok, error = validate_server_target(host)
                if not ok:
                    # The whole group stops. ansible takes one pattern, so there is no way
                    # to run on the rest only, and a partial run on hosts nobody cleared is
                    # worse than no run. The message says so, because an operator who reads
                    # "blocked" must not assume the other hosts went ahead.
                    return ToolResult.error(
                        f"Refusing to execute: {error}. The pattern {pattern!r} names "
                        f"{len(hosts)} hosts, and one blocked host refuses all of them. "
                        "No host ran."
                    )
            # The whole set passed, so the group may proceed to the gate below.
            validated_target = _targets_label(pattern, hosts)
        elif server.provider_id in _HOST_FIELDS_BY_PROVIDER:
            # This provider DOES have a network-address concept (unlike
            # ssm, validated via IAM instead) but the config supplied
            # neither an address nor a pattern -- e.g. an ansible-runner
            # server carrying only `host`, a key the backend never reads.
            # Refuse rather than proceed unguarded into secret decryption
            # and the backend.
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
        # #8's breakage -- and before the disposition below, because a preview is still
        # a mutate.remote call. Only the recorded decision distinguishes the two.
        #
        # `decision` names what the CALL asked for, and not what the gate answered. #16
        # owns the record of the answer. So the count stays comparable across M1 and M2.
        record_observation(
            capability_class=MUTATE_REMOTE,
            decision="preview" if dry_run else "would_gate",
            tool=self.name,
            server_id=server.id,
            server_name=server.name,
            provider_id=server.provider_id,
            target=validated_target,
            # #4's blast radius, beside what the guard checked. `target` names what this
            # process reaches, which for a group is the resolved host set rather than the
            # pattern (#9). `scope` names how many hosts the action reaches, which for
            # ansible-runner is a resolved inventory fact rather than a config field.
            # resolve_scope_label() reads local files only and never raises, so a log-only
            # record cannot fail an execution.
            scope=resolve_scope_label(server),
            command_digest=command_digest(command),
        )

        # The gate (#8, #10). Ordering carries the security property, not just tidiness:
        # this runs after the refusals and the network guard, so a withheld action names
        # the real resolved target -- and before the lazy backend import, before
        # resolve_plaintext(), and before jobs.create(). A withheld action must not
        # decrypt a credential on its way to a preview, and must not leave a job record
        # implying that it ran.
        #
        # The branch reads the gate's disposition and never `dry_run`. That is the whole
        # of #10: the argument asks, and this decides.
        disposition, reason = self._decide(server, command, preview_requested=dry_run)

        if disposition is _Disposition.PREVIEW_ON_REQUEST:
            return (
                f"{_preview_line(server, command, validated_target)}\n{PREVIEW_ON_REQUEST_NOTE}"
            )

        if disposition is _Disposition.PREVIEW_WITHHELD:
            # An error result, because the caller asked for something it did not get. The
            # two preview messages stay separate sentences: a caller and an operator must
            # be able to tell a look from a stopped action.
            return ToolResult.error(
                f"Did not execute on {server.name!r}. {reason}\n"
                f"{_preview_line(server, command, validated_target)}\n"
                f"{PREVIEW_WITHHELD_NOTE}"
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


__all__ = ["PREVIEW_ON_REQUEST_NOTE", "PREVIEW_WITHHELD_NOTE", "ExecuteOnServerTool"]
