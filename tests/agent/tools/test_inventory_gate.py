# tests/agent/tools/test_inventory_gate.py
"""Item 21 (#23): gate inventory mutation.

An inventory write changes the meaning of every later remote action against that record.
UpdateServerTool replaces `config` and `secretRef` in full, so an automation can keep a name
and repoint it at another address, or attach a different credential. Classing that as a plain
local write would let one edit redirect a standing grant.

An operator editing inventory in a chat session does normal work, so interactive stays
allowed. An automation that rewrites the inventory is the setup step for a redirected grant,
so unattended refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_INTERACTIVE,
    EXECUTION_CONTEXT_SUBAGENT,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.servers import (
    CreateServerTool,
    DeleteServerTool,
    UpdateServerTool,
)
from nanoinfra.config.gates import GatesConfig
from nanoinfra.servers.store import ServerStore

_POLICY_TARGET = "nanoinfra.agent.tools.servers.load_policy"


def _ctx(execution_context: str) -> RequestContext:
    return RequestContext(
        channel="telegram", chat_id="c1", session_key="s1", execution_context=execution_context
    )


def _store(tmp_path: Path) -> ServerStore:
    return ServerStore(tmp_path)


def _existing(tmp_path: Path) -> Any:
    return _store(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )


@pytest.mark.asyncio
async def test_an_unattended_update_is_refused(tmp_path: Path) -> None:
    server = _existing(tmp_path)
    store = _store(tmp_path)

    with (
        patch(_POLICY_TARGET, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await UpdateServerTool(store).execute(
            server_id=server.id,
            name="prod-web-01",
            providerId="ssh",
            config={"host": "10.0.9.9"},
            dry_run=False,
        )

    assert result.is_error
    assert store.get(server.id).config["host"] == "10.0.1.5"


@pytest.mark.asyncio
async def test_the_refusal_names_the_class_and_the_context(tmp_path: Path) -> None:
    server = _existing(tmp_path)

    with (
        patch(_POLICY_TARGET, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await UpdateServerTool(_store(tmp_path)).execute(
            server_id=server.id, name="x", providerId="ssh", dry_run=False
        )

    assert "mutate.inventory" in str(result)


@pytest.mark.asyncio
async def test_an_interactive_update_still_writes(tmp_path: Path) -> None:
    server = _existing(tmp_path)
    store = _store(tmp_path)

    with (
        patch(_POLICY_TARGET, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_INTERACTIVE)),
    ):
        await UpdateServerTool(store).execute(
            server_id=server.id,
            name="prod-web-01",
            providerId="ssh",
            config={"host": "10.0.9.9"},
            dry_run=False,
        )

    assert store.get(server.id).config["host"] == "10.0.9.9"


@pytest.mark.asyncio
async def test_a_subagent_update_is_refused(tmp_path: Path) -> None:
    """A subagent is unattended even when a person started its parent session."""
    server = _existing(tmp_path)
    store = _store(tmp_path)

    with (
        patch(_POLICY_TARGET, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_SUBAGENT)),
    ):
        result = await UpdateServerTool(store).execute(
            server_id=server.id,
            name="prod-web-01",
            providerId="ssh",
            config={"host": "10.0.9.9"},
            dry_run=False,
        )

    assert result.is_error
    assert store.get(server.id).config["host"] == "10.0.1.5"


@pytest.mark.asyncio
async def test_an_unattended_create_is_refused(tmp_path: Path) -> None:
    """A create can mint a record whose name matches a standing grant."""
    store = _store(tmp_path)

    with (
        patch(_POLICY_TARGET, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await CreateServerTool(store).execute(
            name="staging-web-01", providerId="ssh", config={"host": "10.0.2.7"}, dry_run=False
        )

    assert result.is_error
    assert store.list_servers() == []


@pytest.mark.asyncio
async def test_an_unattended_delete_is_refused(tmp_path: Path) -> None:
    server = _existing(tmp_path)
    store = _store(tmp_path)

    with (
        patch(_POLICY_TARGET, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await DeleteServerTool(store).execute(server_id=server.id, dry_run=False)

    assert result.is_error
    assert store.get(server.id) is not None


@pytest.mark.asyncio
async def test_a_preview_is_never_refused(tmp_path: Path) -> None:
    """A preview writes nothing, so refusing it would block reading and teach nothing."""
    server = _existing(tmp_path)

    with (
        patch(_POLICY_TARGET, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await UpdateServerTool(_store(tmp_path)).execute(
            server_id=server.id, name="prod-web-01", providerId="ssh"
        )

    assert "Preview (not saved)" in str(result)


@pytest.mark.asyncio
async def test_a_standing_grant_cannot_permit_an_inventory_write(tmp_path: Path) -> None:
    """A grant carries no class. One that permitted inventory writes could widen itself."""
    server = _existing(tmp_path)
    store = _store(tmp_path)
    granted = GatesConfig.model_validate(
        {
            "standingGrants": [
                {
                    "id": "anything",
                    "contexts": ["unattended"],
                    "hosts": ["10.0.1.5"],
                    "commands": ["update_server"],
                }
            ]
        }
    )

    with (
        patch(_POLICY_TARGET, return_value=granted),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await UpdateServerTool(store).execute(
            server_id=server.id,
            name="prod-web-01",
            providerId="ssh",
            config={"host": "10.0.9.9"},
            dry_run=False,
        )

    assert result.is_error
    assert store.get(server.id).config["host"] == "10.0.1.5"


@pytest.mark.asyncio
async def test_an_unreadable_policy_refuses_an_unattended_write(tmp_path: Path) -> None:
    """Unparseable policy fails closed. A broken config is not a reason to skip the gate."""
    server = _existing(tmp_path)
    store = _store(tmp_path)

    with (
        patch("nanoinfra.config.loader.load_config", side_effect=RuntimeError("bad config")),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await UpdateServerTool(store).execute(
            server_id=server.id,
            name="prod-web-01",
            providerId="ssh",
            config={"host": "10.0.9.9"},
            dry_run=False,
        )

    assert result.is_error
    assert store.get(server.id).config["host"] == "10.0.1.5"
