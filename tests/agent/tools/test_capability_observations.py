# tests/agent/tools/test_capability_observations.py
"""Item 1 (#3): the log-only recorder.

M1 enforces nothing. It records the decision the gate *would* make, so an operator can
size M2's breakage before M2 causes it. The record shape is item 13's (#16), minus the
fields that item 2 (#4) and item 3 (#5) have not built yet.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from nanoinfra.agent.tools.capabilities import (
    MUTATE_INVENTORY,
)
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
from nanoinfra.agent.tools.servers import (
    CreateServerTool,
    DeleteServerTool,
    UpdateServerTool,
)
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.lookup import resolve_server
from nanoinfra.servers.store import ServerStore

_SECRET_VALUE = "s3cr3t-key-material"



@pytest.fixture(autouse=True)
def _interactive_turn():
    """Bind an interactive turn so the #8 gate does not refuse these tests.

    With no request context bound, execution_context falls back to unattended (#5), and #8
    refuses an unattended remote action without a standing grant. That refusal is correct.
    These tests exercise execution mechanics rather than policy, so they declare a present
    operator. Policy itself is covered by tests/agent/tools/test_unattended_enforcement.py.
    """
    ctx = RequestContext(
        channel="telegram",
        chat_id="c1",
        session_key="s1",
        execution_context=EXECUTION_CONTEXT_INTERACTIVE,
    )
    with request_context(ctx):
        yield

@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


@pytest.fixture
def observations() -> Iterator[list[dict[str, Any]]]:
    """Every log-only observation emitted while the test runs, in order."""
    captured: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        record = message.record["extra"].get("gate_observation")
        if record is not None:
            captured.append(record)

    sink_id = logger.add(sink, level=0)
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _tool(tmp_path: Path) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(
        servers=ServerStore(tmp_path), secrets=SecretStore(tmp_path), jobs=JobStore(tmp_path)
    )


def _ssh_server(tmp_path: Path, *, secret_ref: str | None = None) -> None:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
    }
    if secret_ref:
        raw["secretRef"] = secret_ref
    ServerStore(tmp_path).create(raw)


@pytest.mark.asyncio
async def test_inventory_preview_records_a_mutate_inventory_observation(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    """#23 gates these writes. M1 only counts them, so an operator can size that change."""
    store = ServerStore(tmp_path)
    server = store.create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )

    await UpdateServerTool(store).execute(
        server_id=server.id, name="prod-web-01", providerId="ssh", config={"host": "10.0.9.9"}
    )

    assert len(observations) == 1
    assert observations[0]["capability_class"] == MUTATE_INVENTORY
    assert observations[0]["decision"] == "preview"
    assert store.get(server.id).config["host"] == "10.0.1.5"


@pytest.mark.asyncio
async def test_inventory_write_records_would_gate_and_still_writes(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    """M1 records and enforces nothing. The write must still happen."""
    store = ServerStore(tmp_path)
    server = store.create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )

    await UpdateServerTool(store).execute(
        server_id=server.id,
        name="prod-web-01",
        providerId="ssh",
        config={"host": "10.0.9.9"},
        dry_run=False,
    )

    assert [o["decision"] for o in observations] == ["would_gate"]
    assert store.get(server.id).config["host"] == "10.0.9.9"


@pytest.mark.asyncio
async def test_create_and_delete_are_recorded_too(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    store = ServerStore(tmp_path)

    await CreateServerTool(store).execute(
        name="new-host", providerId="ssh", config={"host": "10.0.2.7"}, dry_run=False
    )
    created = resolve_server(store, "new-host")
    assert created is not None
    await DeleteServerTool(store).execute(server_id=created.id, dry_run=False)

    assert [o["tool"] for o in observations] == ["create_server", "delete_server"]
    assert {o["capability_class"] for o in observations} == {MUTATE_INVENTORY}
