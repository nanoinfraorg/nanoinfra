"""Secret redaction in SDK session exports (nanoinfraorg/nanoinfra#31).

``SessionClient.export`` reads the live session object and the session file.
Both sit around the persistence boundary that #17 scrubs, so the scrub must
also run on the snapshot the SDK hands out.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.tools.base import Tool
from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS
from nanoinfra.bus.queue import MessageBus
from nanoinfra.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RuntimeContextBlock,
    append_runtime_context,
)
from nanoinfra.sdk.clients import SessionClient
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore

SECRET_NAME = "db-password"

# An obvious placeholder value, long enough to pass MIN_REDACTABLE_SECRET_CHARS.
SECRET_VALUE = "example-not-a-real-credential-0000"

SECRET_PLACEHOLDER = f"[redacted secret: {SECRET_NAME}]"


@pytest.fixture(autouse=True)
def _local_secrets_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the store a usable key, and keep it on the local backend.

    Without a key the executor resolves no sentinel, so a test would pass for
    the wrong reason.
    """
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)


@pytest.fixture(autouse=True)
def _scrub_service_for_the_workspace(scrub_service: Any, tmp_path: Path) -> None:
    """The executor performs the scrub (#41), so every test here starts one.

    Each test stores its secret in tmp_path, so one service covers the module.
    """
    scrub_service(tmp_path)


def _store_secret(workspace: Path) -> None:
    SecretStore(workspace).create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )


def _loop(workspace: Path) -> AgentLoop:
    provider = MagicMock(name="provider")
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=8192,
        temperature=0.1,
        reasoning_effort=None,
    )
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
    )


class _CredentialTool(Tool):
    """A tool in the credential.access class, for the drop-whole-result path."""

    capability_class = CREDENTIAL_ACCESS

    @property
    def name(self) -> str:
        return "read_credential"

    @property
    def description(self) -> str:
        return "Test double that resolves a credential."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return SECRET_VALUE


@pytest.mark.asyncio
async def test_export_redacts_a_stored_secret_from_a_cached_session(tmp_path: Path) -> None:
    _store_secret(tmp_path)
    client = SessionClient(_loop(tmp_path))
    await client.ingest(
        "sdk:cached",
        [{"role": "assistant", "content": f"I used {SECRET_VALUE} to connect."}],
        save=False,
    )

    snapshot = client.export("sdk:cached")

    assert snapshot is not None
    assert SECRET_VALUE not in snapshot.to_dict()["messages"][0]["content"]
    assert SECRET_PLACEHOLDER in snapshot.messages[0]["content"]


@pytest.mark.asyncio
async def test_export_redacts_a_stored_secret_read_from_the_session_file(tmp_path: Path) -> None:
    _store_secret(tmp_path)
    writer = SessionClient(_loop(tmp_path))
    await writer.ingest(
        "sdk:persisted",
        [{"role": "assistant", "content": f"I used {SECRET_VALUE} to connect."}],
        save=True,
    )

    # A second loop starts with an empty cache, so this reads the session file.
    snapshot = SessionClient(_loop(tmp_path)).export("sdk:persisted")

    assert snapshot is not None
    assert SECRET_VALUE not in snapshot.messages[0]["content"]
    assert SECRET_PLACEHOLDER in snapshot.messages[0]["content"]


@pytest.mark.asyncio
async def test_export_redacts_a_stored_secret_in_session_metadata(tmp_path: Path) -> None:
    _store_secret(tmp_path)
    client = SessionClient(_loop(tmp_path))
    await client.ingest(
        "sdk:metadata",
        [{"role": "user", "content": "hi"}],
        metadata={"last_error": f"login with {SECRET_VALUE} failed"},
        save=False,
    )

    snapshot = client.export("sdk:metadata")

    assert snapshot is not None
    assert snapshot.metadata["last_error"] == f"login with {SECRET_PLACEHOLDER} failed"


@pytest.mark.asyncio
async def test_export_drops_a_credential_access_tool_result(tmp_path: Path) -> None:
    _store_secret(tmp_path)
    loop = _loop(tmp_path)
    loop.tools.register(_CredentialTool())
    client = SessionClient(loop)
    await client.ingest(
        "sdk:credential",
        [
            {
                "role": "tool",
                "name": "read_credential",
                "content": SECRET_VALUE,
            }
        ],
        save=False,
    )

    snapshot = client.export("sdk:credential")

    assert snapshot is not None
    assert snapshot.messages[0]["content"] == (
        f"[redacted credential.access result: secret={SECRET_NAME}]"
    )


@pytest.mark.asyncio
async def test_export_keeps_model_only_runtime_context_and_redacts_it(tmp_path: Path) -> None:
    """Export stays the trusted read, so the runtime context must survive the scrub."""
    _store_secret(tmp_path)
    content, marker = append_runtime_context(
        "visible user text",
        [RuntimeContextBlock(source="goal", content=f"goal uses {SECRET_VALUE}")],
    )
    client = SessionClient(_loop(tmp_path))
    await client.ingest(
        "sdk:runtime-context",
        [
            {
                "role": "user",
                "content": content,
                RUNTIME_CONTEXT_HISTORY_META: marker,
            }
        ],
        save=False,
    )

    snapshot = client.export("sdk:runtime-context")

    assert snapshot is not None
    exported = snapshot.messages[0]["content"]
    assert "visible user text" in exported
    assert "goal uses" in exported
    assert SECRET_VALUE not in exported
    assert SECRET_PLACEHOLDER in exported


@pytest.mark.asyncio
async def test_unredacted_export_still_returns_the_secret_from_a_cached_session(
    tmp_path: Path,
) -> None:
    _store_secret(tmp_path)
    client = SessionClient(_loop(tmp_path))
    await client.ingest(
        "sdk:escape-hatch",
        [{"role": "assistant", "content": f"I used {SECRET_VALUE} to connect."}],
        save=False,
    )

    snapshot = client.export_unredacted_with_secrets("sdk:escape-hatch")

    assert snapshot is not None
    assert snapshot.messages[0]["content"] == f"I used {SECRET_VALUE} to connect."


@pytest.mark.asyncio
async def test_unredacted_export_still_returns_the_secret_from_the_session_file(
    tmp_path: Path,
) -> None:
    _store_secret(tmp_path)
    writer = SessionClient(_loop(tmp_path))
    await writer.ingest(
        "sdk:escape-hatch-file",
        [{"role": "assistant", "content": f"I used {SECRET_VALUE} to connect."}],
        save=True,
    )

    snapshot = SessionClient(_loop(tmp_path)).export_unredacted_with_secrets(
        "sdk:escape-hatch-file"
    )

    assert snapshot is not None
    assert snapshot.messages[0]["content"] == f"I used {SECRET_VALUE} to connect."


def test_export_returns_none_for_an_unknown_session(tmp_path: Path) -> None:
    client = SessionClient(_loop(tmp_path))

    assert client.export("sdk:missing") is None
    assert client.export_unredacted_with_secrets("sdk:missing") is None
