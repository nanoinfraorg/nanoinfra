"""Agent tools for managing stored secrets.

Pattern mirrors nanoinfra/agent/tools/diagrams.py: dry_run-by-default for
mutations, create() builds from ctx.workspace. Every result funnels through
Secret.to_public_dict() -- there is no code path in this file that touches
Secret.ciphertext or reaches into the crypto module's decrypt function. The
store's plaintext-resolving method exists for a *future* caller (the
Servers execution engine); it is intentionally never imported here.
"""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanoinfra.secrets.crypto import SecretsNotConfiguredError
from nanoinfra.secrets.normalize import SecretValidationError
from nanoinfra.secrets.postgres_backend import PostgresSecretsNotConfiguredError
from nanoinfra.secrets.store import SecretStore

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext

_KIND_ENUM = ["password", "api_key", "ssh_key", "token"]
_PROVIDER_ENUM = ["local", "postgres"]


def _resolve_secret(store: SecretStore, id_or_name: str):
    secret = store.get(id_or_name)
    if secret is not None:
        return secret
    needle = id_or_name.lower()
    for candidate in store.list_secrets():
        if candidate.name.lower() == needle:
            return candidate
    return None


class ListSecretsTool(Tool):
    """List stored secrets (metadata only -- never a value)."""

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(SecretStore(Path(ctx.workspace)))

    def __init__(self, store: SecretStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "list_secrets"

    @property
    def description(self) -> str:
        return (
            "List stored secrets in this workspace: id, name, kind, provider, "
            "timestamps. Never returns a secret's value -- use this to find a "
            "secret's id/name to reference elsewhere (e.g. a Server's credential), "
            "not to read what a secret contains."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return tool_parameters_schema()

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        return json.dumps([secret.to_public_dict() for secret in self.store.list_secrets()], ensure_ascii=False)


@tool_parameters(
    tool_parameters_schema(
        secret_id_or_name=StringSchema(
            "Exact secret id, or its name (case-insensitive exact match).",
            min_length=1,
        ),
        required=["secret_id_or_name"],
    )
)
class GetSecretTool(Tool):
    """Fetch one secret's metadata (never its value)."""

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(SecretStore(Path(ctx.workspace)))

    def __init__(self, store: SecretStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "get_secret"

    @property
    def description(self) -> str:
        return (
            "Fetch a stored secret's metadata by id or name: id, name, kind, "
            "provider, timestamps. Never returns the value -- there is no tool "
            "that does; secrets are write-only to the agent by design."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, secret_id_or_name: str, **kwargs: Any) -> Any:
        secret = _resolve_secret(self.store, secret_id_or_name)
        if secret is None:
            return ToolResult.error(f"No secret matches {secret_id_or_name!r}.")
        return json.dumps(secret.to_public_dict(), ensure_ascii=False)


def _create_update_error(exc: Exception) -> ToolResult:
    if isinstance(exc, SecretValidationError):
        return ToolResult.error(f"Invalid secret payload: {exc}")
    if isinstance(exc, (SecretsNotConfiguredError, PostgresSecretsNotConfiguredError)):
        return ToolResult.error(str(exc))
    raise exc


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema("Name for the secret (unique).", min_length=1),
        kind=StringSchema("What kind of credential this is (affects nothing functionally, UI hint only).", enum=_KIND_ENUM),
        providerId=StringSchema("Where to store it.", enum=_PROVIDER_ENUM),
        value=StringSchema("The plaintext secret value. Accepted here, never returned by any tool afterward.", min_length=1),
        dry_run=BooleanSchema(
            description=(
                "Defaults to true: validate and return a preview (without the value) "
                "without creating anything. Only pass dry_run=false after the user has "
                "explicitly confirmed."
            ),
            default=True,
        ),
        required=["name", "kind", "providerId", "value"],
    )
)
class CreateSecretTool(Tool):
    """Preview (default) or create a new stored secret."""

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(SecretStore(Path(ctx.workspace)))

    def __init__(self, store: SecretStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "create_secret"

    @property
    def description(self) -> str:
        return (
            "Create a new stored secret. Defaults to dry_run=true, which validates "
            "and returns a preview (name/kind/provider -- never the value) without "
            "creating anything. Only call again with dry_run=false, same arguments, "
            "after the user explicitly confirms. Never set dry_run=false on the first call."
        )

    async def execute(
        self,
        name: str,
        kind: str,
        providerId: str,  # noqa: N803 -- matches the JSON schema property name
        value: str,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        raw = {"name": name, "kind": kind, "providerId": providerId, "value": value}
        if dry_run:
            return (
                f"Preview (not created): name={name!r} kind={kind!r} providerId={providerId!r}\n"
                "Not saved. Call create_secret again with the same arguments and "
                "dry_run=false only after the user confirms."
            )
        try:
            secret = self.store.create(raw)
        except (SecretValidationError, SecretsNotConfiguredError, PostgresSecretsNotConfiguredError) as exc:
            return _create_update_error(exc)
        return f"Created secret {secret.id!r} ({secret.name!r})."


@tool_parameters(
    tool_parameters_schema(
        secret_id=StringSchema("Id of the secret to update.", min_length=1),
        name=StringSchema("New name.", min_length=1),
        kind=StringSchema("New kind.", enum=_KIND_ENUM),
        providerId=StringSchema("Provider -- ignored on update; a secret's storage location doesn't move.", enum=_PROVIDER_ENUM),
        value=StringSchema("New plaintext value (replaces the old one entirely).", min_length=1),
        dry_run=BooleanSchema(description="Defaults to true, same convention as create_secret.", default=True),
        required=["secret_id", "name", "kind", "providerId", "value"],
    )
)
class UpdateSecretTool(Tool):
    """Preview (default) or persist a full update to an existing secret."""

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(SecretStore(Path(ctx.workspace)))

    def __init__(self, store: SecretStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "update_secret"

    @property
    def description(self) -> str:
        return (
            "Replace a stored secret's name/kind/value. Defaults to dry_run=true -- "
            "preview without saving, confirm with dry_run=false and the same arguments "
            "only after the user explicitly approves."
        )

    async def execute(
        self,
        secret_id: str,
        name: str,
        kind: str,
        providerId: str,  # noqa: N803
        value: str,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Any:
        current = self.store.get(secret_id)
        if current is None:
            return ToolResult.error(f"No secret with id {secret_id!r}.")
        raw = {"name": name, "kind": kind, "providerId": providerId, "value": value}
        if dry_run:
            return (
                f"Preview (not saved): {current.name!r} -> name={name!r} kind={kind!r}\n"
                "Not saved. Call update_secret again with the same arguments and "
                "dry_run=false only after the user confirms."
            )
        try:
            secret = self.store.update(secret_id, raw)
        except (SecretValidationError, SecretsNotConfiguredError, PostgresSecretsNotConfiguredError) as exc:
            return _create_update_error(exc)
        if secret is None:
            return ToolResult.error(f"No secret with id {secret_id!r}.")
        return f"Saved secret {secret.id!r} ({secret.name!r})."


@tool_parameters(
    tool_parameters_schema(
        secret_id=StringSchema("Id of the secret to delete.", min_length=1),
        dry_run=BooleanSchema(
            description="Defaults to true: preview which secret would be deleted without deleting it.",
            default=True,
        ),
        required=["secret_id"],
    )
)
class DeleteSecretTool(Tool):
    """Preview (default) or delete a stored secret."""

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(SecretStore(Path(ctx.workspace)))

    def __init__(self, store: SecretStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "delete_secret"

    @property
    def description(self) -> str:
        return (
            "Delete a stored secret. Defaults to dry_run=true -- preview which "
            "secret would be deleted (by name, never its value) without deleting "
            "it. Confirm with dry_run=false only after the user explicitly approves. "
            "Deleting a secret a Server still references will break that Server's "
            "ability to connect -- check with list_servers/get_server first if unsure."
        )

    async def execute(self, secret_id: str, dry_run: bool = True, **kwargs: Any) -> Any:
        secret = self.store.get(secret_id)
        if secret is None:
            return ToolResult.error(f"No secret with id {secret_id!r}.")
        if dry_run:
            return (
                f"Preview (not deleted): {secret.name!r} (provider={secret.provider_id!r})\n"
                "Not deleted. Call delete_secret again with the same secret_id and "
                "dry_run=false only after the user confirms."
            )
        self.store.delete(secret_id)
        return f"Deleted secret {secret.id!r} ({secret.name!r})."


__all__ = [
    "CreateSecretTool",
    "DeleteSecretTool",
    "GetSecretTool",
    "ListSecretsTool",
    "UpdateSecretTool",
]
