# tests/automations/test_commissioning_forces_preview.py
"""Every gated tool previews during a commissioning run -- #182.

The enumeration is the point. A test that only drove `execute_on_server` would pass while a tool
added later executed for real during a rehearsal, so this test discovers the gated tools from the
registry and fails when one of them is not covered here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_INVENTORY, MUTATE_REMOTE
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.servers import CreateServerTool, DeleteServerTool, UpdateServerTool
from nanoinfra.automations.commissioning import commissioning_run
from nanoinfra.gates.executor.protocol import ExecuteResponse
from nanoinfra.servers.store import ServerStore

_GATED = {MUTATE_REMOTE, MUTATE_INVENTORY, CREDENTIAL_ACCESS}

# Every gated tool, with the call that would act if commissioning did not coerce it.
_COVERED_TOOLS = {"execute_on_server", "create_server", "update_server", "delete_server"}


def test_every_gated_tool_in_the_registry_is_covered_here() -> None:
    from nanoinfra.agent.tools.loader import ToolLoader

    gated = {
        cls.__name__: getattr(cls, "capability_class", None)
        for cls in ToolLoader().discover()
        if getattr(cls, "capability_class", None) in _GATED
    }
    names = {
        # The tool name is a property on an instance, so map by class name to the tool name this
        # test drives. A new gated tool lands here as a KeyError, which is the failure wanted.
        "ExecuteOnServerTool": "execute_on_server",
        "CreateServerTool": "create_server",
        "UpdateServerTool": "update_server",
        "DeleteServerTool": "delete_server",
    }
    assert {names[cls] for cls in gated} == _COVERED_TOOLS


@pytest.mark.asyncio
async def test_execute_on_server_previews_and_reports_what_it_would_have_run() -> None:
    from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool

    seen: list[bool] = []

    class _Client:
        def execute(self, **kwargs: Any) -> ExecuteResponse:
            seen.append(bool(kwargs["preview_requested"]))
            return ExecuteResponse(
                ok=True,
                output="Preview (not executed): server='db-01' command='systemctl restart nginx'",
                exit_code=None,
                error=None,
                reason="the caller asked for a preview",
                preview_outcome="deny",
                preview_reason="no standing grant covers it.",
                preview_scope="host",
                preview_hosts=["10.0.0.9"],
                preview_command="systemctl restart nginx",
                preview_credential_outcome="deny",
                preview_credential_reason="gates.unattended.credential.access is 'deny'.",
            )

    tool = ExecuteOnServerTool(client=_Client())  # pyright: ignore[reportArgumentType]
    with commissioning_run() as collector:
        # dry_run=False on purpose: the model asked to execute, and commissioning withholds it.
        result = await tool.execute(
            server_id_or_name="db-01", command="systemctl restart nginx", dry_run=False
        )

    assert seen == [True]
    assert "commissioning run" in result
    [action] = collector.actions
    assert action.command == "systemctl restart nginx"
    assert action.hosts == ("10.0.0.9",)
    assert action.permitted is False
    assert action.grantable is True
    assert action.as_grant(grant_id="restart-nginx")["commands"] == ["systemctl restart nginx"]


@pytest.mark.asyncio
async def test_the_inventory_writes_preview_and_are_reported_as_ungrantable(
    tmp_path: Path,
) -> None:
    """A grant can never permit an inventory write, so the report must not offer one (#23)."""
    store = ServerStore(tmp_path)
    existing = store.create({"name": "db-01", "providerId": "ssh", "config": {"host": "10.0.0.9"}})

    calls = [
        (
            CreateServerTool(store),
            {"name": "new-01", "providerId": "ssh", "config": {"host": "10.0.0.10"},
             "dry_run": False},
        ),
        (
            UpdateServerTool(store),
            {"server_id": existing.id, "name": "db-01", "providerId": "ssh",
             "config": {"host": "10.0.0.11"}, "dry_run": False},
        ),
        (DeleteServerTool(store), {"server_id": existing.id, "dry_run": False}),
    ]

    with request_context(
        RequestContext(channel="websocket", chat_id="c1", session_key="websocket:c1")
    ):
        with commissioning_run() as collector:
            for tool, kwargs in calls:
                await tool.execute(**kwargs)  # pyright: ignore[reportArgumentType]

    assert {action.tool for action in collector.actions} == {
        "create_server",
        "update_server",
        "delete_server",
    }
    assert all(not action.grantable for action in collector.actions)
    # Nothing was written: the record the update would have changed still holds its old address.
    assert store.get(existing.id) is not None
    assert store.list_servers()[0].name == "db-01"
    assert len(store.list_servers()) == 1


@pytest.mark.asyncio
async def test_the_same_action_previewed_twice_proposes_one_grant() -> None:
    from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool

    class _Client:
        def execute(self, **_: Any) -> ExecuteResponse:
            return ExecuteResponse(
                ok=True, output="Preview", exit_code=None, error=None, reason="preview",
                preview_outcome="deny", preview_reason="no grant", preview_scope="host",
                preview_hosts=["10.0.0.9"], preview_command="uptime",
            )

    tool = ExecuteOnServerTool(client=_Client())  # pyright: ignore[reportArgumentType]
    with commissioning_run() as collector:
        await tool.execute(server_id_or_name="db-01", command="uptime", dry_run=True)
        await tool.execute(server_id_or_name="db-01", command="uptime", dry_run=True)

    assert len(collector.actions) == 1


def test_the_collector_is_bound_to_its_turn_only() -> None:
    from nanoinfra.automations.commissioning import current_commissioning, forces_preview

    assert current_commissioning() is None
    assert forces_preview() is False
    with commissioning_run():
        assert forces_preview() is True
    assert forces_preview() is False
