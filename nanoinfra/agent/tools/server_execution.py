"""The agent's thin client for remote execution -- nanoinfraorg/nanoinfra#18.

This module used to hold the transports, the credential store, and the guard. It holds none of
them now. It writes one request to the executor and renders the reply.

The import list is the security property, not a style choice. This file must import no backend,
no ``SecretStore``, and not ``nanoinfra.gates.executor.server``. A module that imports any of
those holds the means to dial a host or to read a credential, and
``tests/gates/test_executor_client.py`` walks this file's whole syntax tree to assert it does
not. A lazy import inside a function would satisfy a grep and fail that test.

What stays on this side, because neither needs a credential:

- The denial latch (#15). It keys on the session, and only an operator clears it, so it answers
  before the request leaves. A latched class must never reach a policy question, because a
  question can produce a prompt and a fresh prompt is the oracle.
- The two preview messages (#10). The gate decides, and this renders the decision. One message
  says a caller asked to look. The other says the gate stopped an action.

**One call can block while a human answers (#38).** An unusual action suspends inside the
executor, and this side holds the connection until an operator answers or the deadline passes.
The call already ran for minutes under an idle timeout, so the shape does not change. Two
consequences do. The wait spends one thread of the default executor pool, and the turn cannot
finish before the operator acts. The tool proposes no approval and holds no token: the executor
issues every nonce, and a nonce the agent could read is a nonce the model could propose.

A denied approval and an expired one both arrive as a refusal, so both become terminal denials
below and both latch the class (#15).
"""


from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.agent.tools.context import (
    current_request_context,
    current_request_execution_context,
)
from nanoinfra.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanoinfra.gates.executor.client import ExecutorClient, ExecutorUnavailableError

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext

# The two sentences that keep the two previews apart (#10). One says a caller asked to look. The
# other says the gate stopped an action. A test pins them, because an operator who cannot tell
# the cases apart learns that a preview means nothing.
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


def _preview_verdict(response: Any) -> str:
    """What a real run of this previewed action would meet, as the operator reads it (#181).

    The executor answers the gate on a preview now (#179), so the answer arrives here without a
    refusal having to happen first. When a grant is missing this names the grant that would
    permit the action -- resolved hosts and the exact command, in the form config takes -- and
    when a matrix cell shadows an existing grant, the executor's own sentence already names the
    key, so it is passed through rather than reworded.
    """
    outcome = getattr(response, "preview_outcome", None)
    if not outcome:
        return ""
    lines = [f"\nA real run of this action would be: {outcome}. {response.preview_reason}"]
    credential = getattr(response, "preview_credential_outcome", None)
    if credential and credential != "allow":
        # A permitted command still dies at the secret, and an operator who fixes only the
        # command cell meets the same failure one layer down.
        lines.append(
            f"The credential it needs would be: {credential}. {response.preview_credential_reason}"
        )
    hosts = list(getattr(response, "preview_hosts", ()) or ())
    if outcome != "allow" and hosts and response.preview_command:
        grant = {
            "contexts": ["unattended"],
            "hosts": hosts,
            "commands": [response.preview_command],
        }
        lines.append(
            "The standing grant that would permit it, for gates.standingGrants: "
            f"{json.dumps(grant, ensure_ascii=False)}"
        )
    return "\n" + "\n".join(lines)

# Where a deployment names the executor's socket. entrypoint.sh exports this variable, because
# the container binds the socket outside the agent's home: write rights on a parent directory
# allow a rename of any entry inside it, so a socket directory the agent can write is a socket
# the agent can replace. Nothing read the variable before #38, and the tool then looked in the
# agent's home while the executor listened under /run. That is a broken deployment rather than a
# refusal, and it reads as one now.
EXECUTOR_SOCKET_ENV = "NANOINFRA_EXECUTOR_SOCKET"

# Where the executor listens when no deployment names a path. The supervisor binds the same place.
DEFAULT_SOCKET_NAME = "executor.sock"


def default_socket_path() -> Path:
    """The executor socket this deployment uses.

    The environment wins, then the data dir. The same order as the fetcher (#19) and the MCP host
    (#22), so one deployment describes all three the same way.
    """
    import os

    from nanoinfra.config.paths import get_data_dir

    named = os.environ.get(EXECUTOR_SOCKET_ENV, "").strip()
    if named:
        return Path(named)
    return get_data_dir() / "run" / DEFAULT_SOCKET_NAME


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
            "Optional override for the idle/absolute timeout in seconds. Omit to use the "
            "provider's default.",
            nullable=True,
        ),
        dry_run=BooleanSchema(
            description=(
                "Defaults to true, which asks for a preview: the executor resolves the "
                "server and reports what would run, and it connects to nothing. Passing "
                "false asks to execute. It does not authorize execution -- the capability "
                "gate decides that, and no value here changes the answer."
            ),
            default=True,
        ),
        required=["server_id_or_name", "command"],
    )
)


class ExecuteOnServerTool(Tool):
    """Ask the executor to run a command on an inventoried server."""

    capability_class = "mutate.remote"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        socket_path = getattr(ctx, "executor_socket", None) or default_socket_path()
        return cls(client=ExecutorClient(socket_path), gate=ctx.gate)

    def __init__(self, *, client: ExecutorClient, gate: Any = None) -> None:
        self.client = client
        # The gate runtime (#33) for the latch only. Policy and audit moved to the executor.
        self.gate = gate

    @property
    def name(self) -> str:
        return "execute_on_server"

    @property
    def description(self) -> str:
        return (
            "Connect to an inventoried server and run a command/action on it, via "
            "whichever connection provider that server uses (ssh/ansible-runner/ssm/api). "
            "Defaults to dry_run=true, which previews the resolved server, provider and "
            "command without connecting to anything. This is the highest-consequence tool "
            "in the system: a capability gate decides whether a call executes, and a "
            "refusal is final for that action."
        )

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        server_id_or_name: str,
        command: str,
        timeout_s: str | None = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        # The latch answers before the request leaves (#15). Asking the executor could produce
        # a prompt, and a fresh prompt is the brute-force oracle.
        if not dry_run:
            latched = self._latched_refusal()
            if latched is not None:
                return latched

        try:
            # The client blocks on a socket, so it runs off the event loop. The executor owns
            # the idle timeout, and a command can run for minutes.
            response = await asyncio.to_thread(
                self.client.execute,
                server_id_or_name=server_id_or_name,
                command=command,
                session_id=self._session_id(),
                execution_context=current_request_execution_context(),
                preview_requested=dry_run,
                timeout_s=timeout_s,
            )
        except ExecutorUnavailableError as exc:
            # A deployment fault, and not a policy decision. The words must differ, or an
            # operator reads a broken deployment as a refusal.
            return ToolResult.error(
                f"The executor is not reachable, so nothing ran: {exc} "
                "This is a deployment fault rather than a policy decision. Check that the "
                "executor process is running."
            )

        if response.error:
            return ToolResult.error(response.error)

        if dry_run:
            return f"{response.output}\n{PREVIEW_ON_REQUEST_NOTE}{_preview_verdict(response)}"

        if not response.ok:
            text = (
                f"Did not execute on {server_id_or_name!r}. {response.reason}\n"
                f"{response.output}\n{PREVIEW_WITHHELD_NOTE}"
            )
            if not response.terminal:
                # A refusal about the deployment, and not about this action (#42). The latch would
                # cost the operator a clear for their own config, and it would teach them to clear
                # one without reading it. The action can run again once the deployment can answer.
                return ToolResult.error(
                    f"{text}\nThis refusal describes the deployment rather than this action, so "
                    "the session is not blocked. The same action can run once the deployment can "
                    "answer it."
                )
            return self._deny(text, reason=response.reason)

        return (
            f"Ran {command!r} on {server_id_or_name!r} (exit code {response.exit_code}):\n"
            f"{response.output}"
        )

    def _session_id(self) -> str | None:
        ctx = current_request_context()
        return ctx.session_key if ctx else None

    def _latched_refusal(self) -> Any:
        """Return a refusal when this session's class is latched, or None."""
        session_id = self._session_id()
        if self.gate is None or not session_id:
            return None
        return self.gate.latched_refusal(
            session_id=session_id, capability_class=MUTATE_REMOTE, tool=self.name
        )

    def _deny(self, text: str, *, reason: str) -> Any:
        """Make a refusal terminal, so the runner drops its retry hint (#15).

        Without a gate runtime the result stays a plain error. It still refuses.
        """
        session_id = self._session_id()
        if self.gate is None or not session_id:
            return ToolResult.error(text)
        try:
            return self.gate.refuse_action(
                session_id=session_id,
                capability_class=MUTATE_REMOTE,
                tool=self.name,
                reason=reason or "the gate did not permit execution",
                execution_context=current_request_execution_context(),
            )
        except OSError as exc:
            return ToolResult.error(f"{text}\nThe audit record also failed to write: {exc}")


__all__ = [
    "DEFAULT_SOCKET_NAME",
    "EXECUTOR_SOCKET_ENV",
    "PREVIEW_ON_REQUEST_NOTE",
    "PREVIEW_WITHHELD_NOTE",
    "ExecuteOnServerTool",
    "default_socket_path",
]
