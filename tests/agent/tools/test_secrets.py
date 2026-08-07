"""Tests for the agent tools that manage Secrets. Metadata only, ever --
these tests exist specifically to catch a future regression where someone
adds a field that leaks a value or ciphertext into a tool result."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent.tools.loader import ToolLoader
from nanoinfra.agent.tools.secrets import (
    CreateSecretTool,
    DeleteSecretTool,
    GetSecretTool,
    ListSecretsTool,
    UpdateSecretTool,
)
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _decode(value: object) -> object:
    return json.loads(str(value))


def test_secret_tools_are_discovered() -> None:
    names = {tool.__name__ for tool in ToolLoader().discover()}
    assert {
        "ListSecretsTool",
        "GetSecretTool",
        "CreateSecretTool",
        "UpdateSecretTool",
        "DeleteSecretTool",
    } <= names


@pytest.mark.asyncio
async def test_create_secret_dry_run_does_not_persist(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    tool = CreateSecretTool(store)

    result = await tool.execute(name="n", kind="password", providerId="local", value="s3cr3t")

    assert not getattr(result, "is_error", False)
    assert "Preview (not created)" in result
    assert "s3cr3t" not in result  # the plaintext must never appear even in a dry-run preview
    assert store.list_secrets() == []


@pytest.mark.asyncio
async def test_create_secret_persists_when_dry_run_false_and_never_echoes_value(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    tool = CreateSecretTool(store)

    result = await tool.execute(name="n", kind="password", providerId="local", value="s3cr3t", dry_run=False)

    assert not getattr(result, "is_error", False)
    assert "s3cr3t" not in result
    secrets = store.list_secrets()
    assert len(secrets) == 1
    assert secrets[0].name == "n"


@pytest.mark.asyncio
async def test_list_secrets_never_includes_value_or_ciphertext(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.create({"name": "n", "kind": "password", "providerId": "local", "value": "s3cr3t"})

    result = _decode(await ListSecretsTool(store).execute())

    assert isinstance(result, list)
    assert result[0]["name"] == "n"
    assert "value" not in result[0]
    assert "ciphertext" not in result[0]
    assert "s3cr3t" not in json.dumps(result)


@pytest.mark.asyncio
async def test_get_secret_never_includes_value_or_ciphertext(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    secret = store.create({"name": "n", "kind": "password", "providerId": "local", "value": "s3cr3t"})

    result = _decode(await GetSecretTool(store).execute(secret_id_or_name=secret.id))

    assert result["name"] == "n"
    assert "value" not in result
    assert "ciphertext" not in result


@pytest.mark.asyncio
async def test_get_secret_by_name(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.create({"name": "prod-db-password", "kind": "password", "providerId": "local", "value": "1"})

    result = _decode(await GetSecretTool(store).execute(secret_id_or_name="prod-db-password"))
    assert result["name"] == "prod-db-password"


@pytest.mark.asyncio
async def test_get_secret_unknown_returns_error(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    result = await GetSecretTool(store).execute(secret_id_or_name="ghost")
    assert result.is_error


@pytest.mark.asyncio
async def test_update_secret_dry_run_then_confirm(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    secret = store.create({"name": "old", "kind": "password", "providerId": "local", "value": "1"})
    tool = UpdateSecretTool(store)

    preview = await tool.execute(secret_id=secret.id, name="new", kind="password", providerId="local", value="2")
    assert "Preview (not saved)" in preview
    assert store.get(secret.id).name == "old"

    confirmed = await tool.execute(
        secret_id=secret.id, name="new", kind="password", providerId="local", value="2", dry_run=False
    )
    assert "new" in confirmed or "Saved" in confirmed
    assert store.get(secret.id).name == "new"


@pytest.mark.asyncio
async def test_delete_secret_dry_run_then_confirm(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    secret = store.create({"name": "n", "kind": "password", "providerId": "local", "value": "1"})
    tool = DeleteSecretTool(store)

    preview = await tool.execute(secret_id=secret.id)
    assert "Preview (not deleted)" in preview
    assert store.get(secret.id) is not None

    confirmed = await tool.execute(secret_id=secret.id, dry_run=False)
    assert "Deleted" in confirmed
    assert store.get(secret.id) is None
