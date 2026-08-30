"""One tool per operation, each carrying the class its manifest declared.

This is the whole point of the kind. `capability_class_of()` reads the class off the tool, so
a single `calendar(operation=...)` tool would have one class for a listing and a write both —
`mutate.remote`, fail-closed, refused unattended. That is what an MCP server gives you, and it
is why "what is on my calendar" and "put this on my calendar" cannot have different postures
through one.

Here they do: `google_calendar_list_events` is `read` and runs, `google_calendar_create_event`
is `mutate.remote` and asks. The gate answers about each one separately because each one is a
separate tool with a separate declaration.

The gate call in this module is the agent-side half. It refuses, and it records the refusal in
the audit log, exactly as an inventory write does in `agent/tools/servers.py`. The half that
cannot be talked around is the token: the executor holds the refresh token and mints a
short-lived access token per action, so an agent that skipped this check would still have
nothing to call the API with.
"""

from __future__ import annotations

from typing import Any, cast

from nanoinfra.agent.tools.base import Tool, ToolResult
from nanoinfra.agent.tools.capabilities import READ, record_observation
from nanoinfra.agent.tools.context import (
    ToolContext,
    current_request_execution_context,
    current_request_session_key,
)
from nanoinfra.connectors.contracts import ConnectorOperation, ConnectorPlugin
from nanoinfra.connectors.engine import ConnectorCallError, TokenSource, call, prepare
from nanoinfra.gates.policy import Outcome, evaluate_connector, load_policy

# What the model is told about a write, in the description, because a tool schema is the only
# place some models read. The skill says it again in words; both are cheap.
_WRITE_NOTE = (
    " This writes to a real account other people can see, so it is gated: it asks a person "
    "in an interactive turn and needs a standing grant to run unattended."
)


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
        tokens: TokenSource,
        defaults: dict[str, Any] | None = None,
        gate: Any = None,
    ) -> None:
        self._plugin = plugin
        self._operation = operation
        self._tokens = tokens
        self._defaults = dict(defaults or {})
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
        # An argument the operation never declared becomes a query parameter or a body field
        # nobody reviewed, so it is refused by the same validator every tool uses.
        schema.setdefault("additionalProperties", False)
        return schema

    @property
    def read_only(self) -> bool:
        return self._operation.capability_class == READ

    def _gate_refusal(self) -> ToolResult | None:
        """Ask the gate. Return a refusal, or None to proceed."""
        if self._operation.capability_class == READ:
            # `read` is not a gated class anywhere in this codebase, so there is no decision to
            # take and no observation to record. A line per calendar listing would train an
            # operator to skim the one log that has to stay readable.
            return None

        execution_context = current_request_execution_context()
        decision = evaluate_connector(
            load_policy(),
            capability_class=self._operation.capability_class,
            execution_context=execution_context,
            connector=self._plugin.name,
            operation=self._operation.name,
        )
        record_observation(
            capability_class=self._operation.capability_class,
            decision=decision.outcome.value,
            tool=self._name,
            connector=self._plugin.name,
            operation=self._operation.name,
            grant_id=decision.grant_id,
        )
        if decision.outcome is Outcome.ALLOW:
            return None

        # `approve` has no delivery path from this side yet: the approval socket belongs to the
        # executor, and a connector call does not travel over it. So an interactive write that
        # would ask is refused with the words that say who could permit it, rather than
        # performed on the assumption somebody would have said yes.
        text = f"Refusing {self._name}. {decision.reason}"
        if decision.outcome is Outcome.APPROVE:
            text = (
                f"Refusing {self._name}: {self._plugin.display_name} writes are not yet routed "
                "through the approval path, so nobody can be asked. A standing grant naming "
                f'{{"connectors": ["{self._plugin.name}"], "operations": '
                f'["{self._operation.name}"]}} permits it without a prompt.'
            )
        session_id = _session_id()
        if self._gate is None or not session_id:
            return ToolResult.error(text)
        try:
            return cast(
                ToolResult,
                self._gate.refuse_action(
                    session_id=session_id,
                    capability_class=self._operation.capability_class,
                    tool=self._name,
                    reason=decision.reason,
                    execution_context=execution_context,
                ),
            )
        except OSError as exc:
            return ToolResult.error(f"{text}\nThe audit record also failed to write: {exc}")

    async def execute(self, **kwargs: Any) -> Any:
        refusal = self._gate_refusal()
        if refusal is not None:
            return refusal
        try:
            payload = await call(
                self._plugin,
                self._operation,
                kwargs,
                tokens=self._tokens,
                defaults=self._defaults,
            )
        except ConnectorCallError as exc:
            return ToolResult.error(str(exc))
        return payload

    def preview(self, **kwargs: Any) -> str:
        """The request this call would make, with no token minted and nothing sent."""
        try:
            return prepare(
                self._plugin, self._operation, kwargs, defaults=self._defaults
            ).describe()
        except ConnectorCallError as exc:
            return str(exc)


def build_tools(
    plugin: ConnectorPlugin,
    operations: tuple[ConnectorOperation, ...],
    *,
    tokens: TokenSource,
    defaults: dict[str, Any] | None = None,
    ctx: ToolContext | None = None,
) -> list[ConnectorOperationTool]:
    """One tool per enabled operation, in manifest order."""
    gate = getattr(ctx, "gate", None) if ctx is not None else None
    return [
        ConnectorOperationTool(plugin, op, tokens=tokens, defaults=defaults, gate=gate)
        for op in operations
    ]


__all__ = ["ConnectorOperationTool", "build_tools"]
