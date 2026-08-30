"""One tool per operation, each carrying the class its manifest declared.

This is the whole point of the kind. `capability_class_of()` reads the class off the tool, so
a single `calendar(operation=...)` tool would have one class for a listing and a write both --
`mutate.remote`, fail-closed, refused unattended. That is what an MCP server gives you, and it
is why "what is on my calendar" and "put this on my calendar" cannot have different postures
through one.

Here they do: `google_calendar_list_events` is `read` and runs, `google_calendar_create_event`
is `mutate.remote` and asks a person.

**This module holds no credential and opens no transport.** It writes one frame to the
executor and renders one frame back, exactly as `agent/tools/server_execution.py` does for a
command, and for the same three reasons: the refresh token lives in the executor, the approval
socket belongs to the executor, and the audit record is written there. So a compromised agent
can ask for a calendar write and cannot make one.

The latch is the one decision that stays on this side. It answers before the request leaves,
because asking the executor could produce a prompt, and a fresh prompt is the brute-force
oracle (#15).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from nanoinfra.agent.tools.base import Tool, ToolResult
from nanoinfra.agent.tools.capabilities import READ
from nanoinfra.agent.tools.context import (
    ToolContext,
    current_request_execution_context,
    current_request_session_key,
)
from nanoinfra.connectors.contracts import ConnectorOperation, ConnectorPlugin
from nanoinfra.gates.executor.client import ExecutorClient, ExecutorUnavailableError

# What the model is told about a write, in the description, because a tool schema is the only
# place some models read. The skill says it again in words; both are cheap.
_WRITE_NOTE = (
    " This writes to a real account other people can see, so it is gated: the executor renders "
    "what will be sent and a person approves it, and an unattended turn needs a standing grant "
    "naming this connector and operation."
)

# Said on a preview, so a model does not read "nothing happened" as "it worked".
_PREVIEW_NOTE = "Nothing was sent. This is what the executor would send if this call were run."


def _session_id() -> str | None:
    session_id = current_request_session_key()
    return session_id if session_id else None


class ConnectorOperationTool(Tool):
    """One declared operation, exposed as a tool.

    Constructed rather than subclassed per operation: the operation is data, and a class per
    row would put the manifest in two places. `_plugin_discoverable` is false because the
    discovery path is the connector registry, not the tool scan.
    """

    _plugin_discoverable = False

    def __init__(
        self,
        plugin: ConnectorPlugin,
        operation: ConnectorOperation,
        *,
        client: ExecutorClient,
        gate: Any = None,
    ) -> None:
        self._plugin = plugin
        self._operation = operation
        self._client = client
        self._gate = gate
        self._name = plugin.tool_name(operation)
        # Per instance, from the manifest. `capability_class_of()` reads exactly this, so the
        # declaration in the package is the thing policy answers about.
        self.capability_class = operation.capability_class

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        text = self._operation.description or f"{self._plugin.display_name}: {self._operation.name}"
        if self._operation.capability_class != READ:
            text += _WRITE_NOTE
        return text

    @property
    def parameters(self) -> dict[str, Any]:
        schema = dict(self._operation.parameters) or {"type": "object", "properties": {}}
        # An argument the operation never declared would become a query parameter or a body
        # field nobody reviewed. The executor refuses one too; refusing it here saves a round
        # trip and tells the model which keys exist.
        schema.setdefault("additionalProperties", False)
        if self._operation.capability_class != READ:
            properties = dict(cast(dict[str, Any], schema.get("properties", {})))
            properties["dry_run"] = {
                "type": "boolean",
                "description": (
                    "Defaults to true, which asks for a preview: the executor renders the "
                    "request it would send and sends nothing. Passing false asks to perform it. "
                    "It does not authorize anything -- the gate decides that."
                ),
                "default": True,
            }
            schema["properties"] = properties
        return schema

    @property
    def read_only(self) -> bool:
        return self._operation.capability_class == READ

    def _latched_refusal(self) -> Any:
        session_id = _session_id()
        if self._gate is None or not session_id:
            return None
        return self._gate.latched_refusal(
            session_id=session_id,
            capability_class=self._operation.capability_class,
            tool=self._name,
        )

    def _deny(self, text: str, *, reason: str) -> Any:
        """Make a refusal terminal, so the runner drops its retry hint (#15)."""
        session_id = _session_id()
        if self._gate is None or not session_id:
            return ToolResult.error(text)
        try:
            return self._gate.refuse_action(
                session_id=session_id,
                capability_class=self._operation.capability_class,
                tool=self._name,
                reason=reason or "the gate did not permit this call",
                execution_context=current_request_execution_context(),
            )
        except OSError as exc:
            return ToolResult.error(f"{text}\nThe audit record also failed to write: {exc}")

    async def execute(self, **kwargs: Any) -> Any:
        # A read is not gated anywhere in this codebase, so it never previews: a preview of a
        # listing would cost a round trip and teach nobody anything. A write defaults to a
        # preview, the same default `execute_on_server` uses.
        wants_preview = bool(kwargs.pop("dry_run", True)) and not self.read_only

        if not wants_preview:
            latched = self._latched_refusal()
            if latched is not None:
                return latched

        try:
            # The client blocks on a socket, so it runs off the event loop. An approval can hold
            # the call for as long as the operator's timeout allows.
            response = await asyncio.to_thread(
                self._client.connector_call,
                connector=self._plugin.name,
                operation=self._operation.name,
                arguments_json=json.dumps(kwargs, ensure_ascii=False),
                session_id=_session_id(),
                execution_context=current_request_execution_context(),
                preview_requested=wants_preview,
            )
        except ExecutorUnavailableError as exc:
            # A deployment fault, and not a policy decision. The words must differ, or an
            # operator reads a broken deployment as a refusal.
            return ToolResult.error(
                f"The executor is not reachable, so nothing was sent to "
                f"{self._plugin.display_name}: {exc} This is a deployment fault rather than a "
                "policy decision. Check that the executor process is running."
            )

        if response.error:
            return ToolResult.error(response.error)

        if wants_preview:
            verdict = ""
            if response.preview_outcome:
                verdict = (
                    f"\nThe gate would answer {response.preview_outcome!r}: "
                    f"{response.preview_reason}"
                )
            return f"{response.output}\n{_PREVIEW_NOTE}{verdict}"

        if not response.ok:
            text = f"{self._name} did not run. {response.reason}"
            if not response.terminal:
                return ToolResult.error(
                    f"{text}\nThis refusal describes the deployment rather than this call, so "
                    "the session is not blocked. The same call can run once the deployment can "
                    "answer it."
                )
            return self._deny(text, reason=response.reason)

        return response.output


def build_tools(
    plugin: ConnectorPlugin,
    operations: tuple[ConnectorOperation, ...],
    *,
    client: ExecutorClient,
    ctx: ToolContext | None = None,
) -> list[ConnectorOperationTool]:
    """One tool per enabled operation, in manifest order."""
    gate = getattr(ctx, "gate", None) if ctx is not None else None
    return [ConnectorOperationTool(plugin, op, client=client, gate=gate) for op in operations]


__all__ = ["ConnectorOperationTool", "build_tools"]
