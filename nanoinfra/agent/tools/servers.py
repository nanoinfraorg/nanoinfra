"""Agent tools for managing the server inventory.

Pattern mirrors nanoinfra/agent/tools/diagrams.py exactly: dry_run-by-
default for mutations, create() builds from ctx.workspace. This file
only covers inventory CRUD -- a separate, later module adds
execute_on_server, which is the only tool allowed to resolve a
secretRef to a real credential.
"""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.capabilities import MUTATE_INVENTORY, record_observation
from nanoinfra.agent.tools.context import current_request_execution_context
from nanoinfra.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanoinfra.gates.policy import Outcome, evaluate, load_policy
from nanoinfra.servers.lookup import resolve_server
from nanoinfra.servers.normalize import ServerValidationError, normalize_server_input
from nanoinfra.servers.scope import HOST
from nanoinfra.servers.store import ServerStore

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext

_PROVIDER_ENUM = ["ssh", "ansible-runner", "ssm", "api"]

_CONFIG_SCHEMA = ObjectSchema(
    description=(
        "Provider-specific connection fields. ssh: host, port, username. "
        "ansible-runner: inventoryHost, group, projectPath. ssm: instanceId, region. "
        "api: baseUrl."
    ),
    additional_properties={"type": "string"},
    nullable=True,
)


def _inventory_gate_refusal(tool: str, dry_run: bool) -> ToolResult | None:
    """Ask the gate before an inventory write (#23). Return a refusal, or None to proceed.

    An inventory write changes the meaning of every later remote action against that record,
    so it carries its own class. update_server replaces ``config`` and ``secretRef`` in full,
    which lets one write keep a name and repoint it at another address.

    A preview writes nothing, so it never refuses. Refusing a preview would block reading and
    teach an operator nothing.

    Scope is not meaningful for an inventory record, so the call passes ``host``. Blast radius
    describes how many hosts an action reaches, and this action reaches none: it edits a local
    file. The consequence lands later, on whichever remote action uses the record.
    """
    if dry_run:
        return None

    decision = evaluate(
        load_policy(),
        capability_class=MUTATE_INVENTORY,
        scope=HOST,
        execution_context=current_request_execution_context(),
        hosts=(),
        command=tool,
    )
    if decision.outcome is Outcome.ALLOW:
        return None
    return ToolResult.error(f"Refusing {tool}. {decision.reason}")


def _record_inventory_observation(tool: str, dry_run: bool, **fields: Any) -> None:
    """Log-only in M1 (#3): count an inventory write, enforce nothing.

    Inventory writes are ``mutate.inventory`` rather than ``mutate.local`` because
    update_server replaces ``config`` and ``secretRef`` in full, so one write changes
    which address a later execute_on_server call reaches. #23 gates this class and #24
    stops a standing grant from matching a repointed name. Placed after payload
    validation and before the dry_run branch: an invalid payload never reaches a gate,
    and a preview is still an inventory call, so only the decision differs.

    No config values are recorded. A config carries hostnames and could carry more
    later, and #16 keeps this record digest-only by default.
    """
    record_observation(
        capability_class=MUTATE_INVENTORY,
        decision="preview" if dry_run else "would_gate",
        tool=tool,
        **fields,
    )


class ListServersTool(Tool):
    """List inventoried servers."""

    capability_class = "read"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ServerStore(Path(ctx.workspace)))

    def __init__(self, store: ServerStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "list_servers"

    @property
    def description(self) -> str:
        return "List inventoried servers: id, name, connection provider, tags, last updated."

    @property
    def parameters(self) -> dict[str, Any]:
        return tool_parameters_schema()

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        return json.dumps(
            [summary.to_dict() for summary in self.store.list_servers()], ensure_ascii=False
        )


@tool_parameters(
    tool_parameters_schema(
        server_id_or_name=StringSchema(
            "Exact server id, or its name (case-insensitive exact match).",
            min_length=1,
        ),
        required=["server_id_or_name"],
    )
)
class GetServerTool(Tool):
    """Fetch one server's full record, including its secretRef (an id, never a value)."""

    capability_class = "read"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ServerStore(Path(ctx.workspace)))

    def __init__(self, store: ServerStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "get_server"

    @property
    def description(self) -> str:
        return (
            "Fetch a server's full record by id or name: connection provider, config, "
            "the secret id it authenticates with (secretRef -- an opaque id, never a "
            "credential value; Secrets has no agent-facing tool, so this id can't be "
            "resolved to a name or metadata from chat), tags, timestamps."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, server_id_or_name: str, **kwargs: Any) -> Any:
        server = resolve_server(self.store, server_id_or_name)
        if server is None:
            return ToolResult.error(f"No server matches {server_id_or_name!r}.")
        return json.dumps(server.to_dict(), ensure_ascii=False)


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema("Name for the server (unique).", min_length=1),
        providerId=StringSchema("Connection method.", enum=_PROVIDER_ENUM),
        config=_CONFIG_SCHEMA,
        secretRef=StringSchema(
            "Id of a Secret to authenticate with. Secrets has no agent-facing tool -- "
            "get this id from the user (they wire it up in the WebUI's Secrets view), "
            "never invent one.",
            nullable=True,
        ),
        tags=ArraySchema(StringSchema(""), description="Grouping tags, e.g. ['prod', 'web'].", nullable=True),
        dry_run=BooleanSchema(
            description=(
                "Defaults to true: validate and return a preview without creating "
                "anything. Only pass dry_run=false after the user explicitly confirms."
            ),
            default=True,
        ),
        required=["name", "providerId"],
    )
)
class CreateServerTool(Tool):
    """Preview (default) or create a new inventoried server."""

    capability_class = "mutate.inventory"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ServerStore(Path(ctx.workspace)))

    def __init__(self, store: ServerStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "create_server"

    @property
    def description(self) -> str:
        return (
            "Add a new server to the inventory. Defaults to dry_run=true -- preview "
            "without creating anything, confirm with dry_run=false and the same "
            "arguments only after the user explicitly approves. This only records "
            "how to reach the server; it does not verify the server is actually "
            "reachable, and there is no tool yet that connects to it."
        )

    async def execute(
        self,
        name: str,
        providerId: str,  # noqa: N803
        config: dict[str, str] | None = None,
        secretRef: str | None = None,  # noqa: N803
        tags: list[str] | None = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        raw = {"name": name, "providerId": providerId, "config": config or {}, "secretRef": secretRef, "tags": tags or []}
        try:
            # "0" * 32 is a throwaway id for validation only -- store.create()
            # below mints the real one and re-normalizes independently.
            normalize_server_input(raw, server_id="0" * 32)
        except ServerValidationError as exc:
            return ToolResult.error(f"Invalid server payload: {exc}")
        _record_inventory_observation(self.name, dry_run, server_name=name, provider_id=providerId)
        refusal = _inventory_gate_refusal(self.name, dry_run)
        if refusal is not None:
            return refusal
        if dry_run:
            return (
                f"Preview (not created): name={name!r} providerId={providerId!r} config={config or {}}\n"
                "Not saved. Call create_server again with the same arguments and "
                "dry_run=false only after the user confirms."
            )
        try:
            server = self.store.create(raw)
        except ServerValidationError as exc:
            return ToolResult.error(f"Invalid server payload: {exc}")
        return f"Created server {server.id!r} ({server.name!r})."


@tool_parameters(
    tool_parameters_schema(
        server_id=StringSchema("Id of the server to update.", min_length=1),
        name=StringSchema("New name.", min_length=1),
        providerId=StringSchema("New connection method.", enum=_PROVIDER_ENUM),
        config=_CONFIG_SCHEMA,
        secretRef=StringSchema("New secret id, or omit/null to clear it.", nullable=True),
        tags=ArraySchema(StringSchema(""), description="Replacement tag list.", nullable=True),
        dry_run=BooleanSchema(description="Defaults to true, same convention as create_server.", default=True),
        required=["server_id", "name", "providerId"],
    )
)
class UpdateServerTool(Tool):
    """Preview (default) or persist a full update to an existing server."""

    capability_class = "mutate.inventory"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ServerStore(Path(ctx.workspace)))

    def __init__(self, store: ServerStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "update_server"

    @property
    def description(self) -> str:
        return (
            "Replace a server's name/provider/config/secretRef/tags. This is a full "
            "replacement, not a delta -- fields you omit are cleared, not left "
            "unchanged. Defaults to dry_run=true; confirm with dry_run=false and the "
            "same arguments only after the user explicitly approves."
        )

    async def execute(
        self,
        server_id: str,
        name: str,
        providerId: str,  # noqa: N803
        config: dict[str, str] | None = None,
        secretRef: str | None = None,  # noqa: N803
        tags: list[str] | None = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        current = self.store.get(server_id)
        if current is None:
            return ToolResult.error(f"No server with id {server_id!r}.")
        raw = {"name": name, "providerId": providerId, "config": config or {}, "secretRef": secretRef, "tags": tags or []}
        try:
            normalize_server_input(raw, server_id=server_id)
        except ServerValidationError as exc:
            return ToolResult.error(f"Invalid server payload: {exc}")
        _record_inventory_observation(
            self.name, dry_run, server_id=server_id, server_name=name, provider_id=providerId
        )
        refusal = _inventory_gate_refusal(self.name, dry_run)
        if refusal is not None:
            return refusal
        if dry_run:
            return (
                f"Preview (not saved): {current.name!r} -> name={name!r} providerId={providerId!r}\n"
                "Not saved. Call update_server again with the same arguments and "
                "dry_run=false only after the user confirms."
            )
        try:
            server = self.store.update(server_id, raw)
        except ServerValidationError as exc:
            return ToolResult.error(f"Invalid server payload: {exc}")
        if server is None:
            return ToolResult.error(f"No server with id {server_id!r}.")
        return f"Saved server {server.id!r} ({server.name!r})."


@tool_parameters(
    tool_parameters_schema(
        server_id=StringSchema("Id of the server to delete.", min_length=1),
        dry_run=BooleanSchema(
            description="Defaults to true: preview which server would be deleted without deleting it.",
            default=True,
        ),
        required=["server_id"],
    )
)
class DeleteServerTool(Tool):
    """Preview (default) or delete an inventoried server."""

    capability_class = "mutate.inventory"

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ServerStore(Path(ctx.workspace)))

    def __init__(self, store: ServerStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "delete_server"

    @property
    def description(self) -> str:
        return (
            "Remove a server from the inventory. Defaults to dry_run=true -- preview "
            "which server would be deleted without deleting it. Confirm with "
            "dry_run=false only after the user explicitly approves."
        )

    async def execute(self, server_id: str, dry_run: bool = True, **kwargs: Any) -> Any:
        server = self.store.get(server_id)
        if server is None:
            return ToolResult.error(f"No server with id {server_id!r}.")
        _record_inventory_observation(
            self.name,
            dry_run,
            server_id=server.id,
            server_name=server.name,
            provider_id=server.provider_id,
        )
        refusal = _inventory_gate_refusal(self.name, dry_run)
        if refusal is not None:
            return refusal
        if dry_run:
            return (
                f"Preview (not deleted): {server.name!r} (provider={server.provider_id!r})\n"
                "Not deleted. Call delete_server again with the same server_id and "
                "dry_run=false only after the user confirms."
            )
        self.store.delete(server_id)
        return f"Deleted server {server.id!r} ({server.name!r})."


__all__ = [
    "CreateServerTool",
    "DeleteServerTool",
    "GetServerTool",
    "ListServersTool",
    "UpdateServerTool",
]
