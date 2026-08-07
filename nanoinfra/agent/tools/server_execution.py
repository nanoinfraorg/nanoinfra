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

from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionBackend
from nanoinfra.servers.execution.timeout import IdleTimeoutTracker, run_with_idle_timeout
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.lookup import resolve_server
from nanoinfra.servers.network_guard import validate_server_target
from nanoinfra.servers.store import ServerStore

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext

_ABSOLUTE_CEILING_S = 1800


def _backend_and_default_timeout(provider_id: str) -> tuple[ExecutionBackend, int]:
    # Every backend class is imported lazily, inside its own branch --
    # nanoinfra/agent/tools/loader.py's pkgutil scan imports every tool
    # module unconditionally to discover Tool subclasses, so a top-level
    # `from ...ssh_backend import SSHBackend`-style import here would make
    # the *entire agent* fail to start if even one of asyncssh/
    # ansible-runner/boto3 (all optional, the new `servers` extra) isn't
    # installed. base.py and timeout.py have no such dependency, so
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
    network address the local process dials; ansible-runner's
    inventoryHost is validated the same way ssh's host is)."""
    if provider_id in ("ssh", "ansible-runner"):
        return config.get("host") or config.get("inventoryHost")
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

        if dry_run:
            return (
                f"Preview (not executed): server={server.name!r} (id={server.id!r}) "
                f"provider={server.provider_id!r} command={command!r}\n"
                "Nothing was run. Call execute_on_server again with the same arguments "
                "and dry_run=false only after the user explicitly confirms."
            )

        target_host = _target_host(server.provider_id, server.config)
        if target_host:
            ok, error = validate_server_target(target_host)
            if not ok:
                return ToolResult.error(f"Refusing to execute: {error}")

        secret_value: str | None = None
        if server.secret_ref:
            secret_value = self.secrets.resolve_plaintext(server.secret_ref)
            if secret_value is None:
                return ToolResult.error(
                    f"Server {server.name!r} references secret {server.secret_ref!r}, which no longer exists."
                )

        backend, default_idle_timeout = _backend_and_default_timeout(server.provider_id)
        idle_timeout = int(timeout_s) if timeout_s else default_idle_timeout

        job = self.jobs.create(
            server_id=server.id, provider_id=server.provider_id, command=command, timeout_s=idle_timeout
        )
        self.jobs.mark_running(job.id)

        tracker = IdleTimeoutTracker(idle_timeout_s=idle_timeout, absolute_ceiling_s=_ABSOLUTE_CEILING_S)
        result = await run_with_idle_timeout(
            backend.run(server, command, secret_value, on_activity=tracker.touch),
            tracker,
        )

        if result.timed_out:
            self.jobs.complete(job.id, exit_code=None, output=result.output, error="Timed out", status="timed_out")
            return ToolResult.error(f"Timed out running {command!r} on {server.name!r}.")
        if result.error:
            self.jobs.complete(job.id, exit_code=result.exit_code, output=result.output, error=result.error, status="failed")
            return ToolResult.error(f"Failed running {command!r} on {server.name!r}: {result.error}")

        self.jobs.complete(job.id, exit_code=result.exit_code, output=result.output, error=None, status="completed")
        return f"Ran {command!r} on {server.name!r} (exit code {result.exit_code}):\n{result.output}"


__all__ = ["ExecuteOnServerTool"]
