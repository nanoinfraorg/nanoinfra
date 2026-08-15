# tests/sdk/test_snapshot_reader_redaction.py
"""Item 32 (#34): every snapshot reader scrubs, not just `export()`.

#31 redacted `export()`. Its siblings returned `snapshot_from_session(...)` unchanged, and
`public_history_messages` hides runtime context without scrubbing a secret. `get()` is arguably
the wider path, because it reads an existing session and it is the snapshot the persisted-turn
callback hands to host code.

The last test here is the one that matters most. It walks the client surface, so a reader a later
change adds fails a test rather than shipping an unredacted snapshot.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.sdk.types import SessionSnapshot
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore

_SECRET = "s3cr3t-key-material"

# A reader returns a snapshot to a caller, so it must scrub. The unredacted accessor is the one
# documented exception, and #31 states in its name and its docstring who may call it.
_UNREDACTED_ACCESSOR = "export_unredacted_with_secrets"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


@pytest.fixture(autouse=True)
def _scrub_service_for_the_workspace(scrub_service: Any, tmp_path: Path) -> None:
    """The executor performs the scrub (#41), so every test here starts one."""
    scrub_service(tmp_path / "workspace")


@pytest.fixture
def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An embedded agent with one stored Secret and one session holding its value."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from nanoinfra.agent.loop import AgentLoop
    from nanoinfra.bus.queue import MessageBus
    from nanoinfra.nanoinfra import Nanoinfra

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    SecretStore(workspace).create(
        # kind="password", because the value is a password-shaped string. An ssh_key
        # secret has to hold a PEM private key.
        {"name": "web-key", "kind": "password", "providerId": "local", "value": _SECRET}
    )
    provider = MagicMock(name="provider")
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=8192, temperature=0.1, reasoning_effort=None
    )
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=workspace, model="test-model"
    )
    instance = Nanoinfra(loop)
    session = loop.sessions.get_or_create("s1")
    session.messages.append(
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "execute_on_server",
            "content": f"connected with {_SECRET}",
        }
    )
    loop.sessions.save(session)
    return instance


def _snapshot_text(snapshot: Any) -> str:
    return f"{snapshot.messages}{snapshot.metadata}"


def test_get_redacts_a_stored_secret(bot: Any) -> None:
    """The widest path: `get()` is the snapshot the persisted-turn callback hands to host code."""
    snapshot = bot.sessions.get("s1")

    assert snapshot is not None
    assert _SECRET not in _snapshot_text(snapshot)


def test_get_keeps_the_secret_name(bot: Any) -> None:
    """The value goes and the reference stays, so an operator can still tell which one it was."""
    snapshot = bot.sessions.get("s1")

    assert "web-key" in _snapshot_text(snapshot)


def test_clear_redacts_the_snapshot_it_returns(bot: Any) -> None:
    snapshot = bot.sessions.clear("s1")

    assert _SECRET not in _snapshot_text(snapshot)


@pytest.mark.asyncio
async def test_ingest_redacts_the_snapshot_it_returns(bot: Any) -> None:
    messages = [{"role": "user", "content": f"look at {_SECRET}"}]

    snapshot = await bot.sessions.ingest("s1", messages)

    assert _SECRET not in _snapshot_text(snapshot)


@pytest.mark.asyncio
async def test_restore_redacts_the_snapshot_it_returns(bot: Any) -> None:
    source = SessionSnapshot(
        key="s2",
        messages=[{"role": "user", "content": f"restore with {_SECRET}"}],
        metadata={},
    )

    snapshot = await bot.sessions.restore(source)

    assert _SECRET not in _snapshot_text(snapshot)


@pytest.mark.asyncio
async def test_compact_session_redacts_the_snapshot_it_returns(bot: Any) -> None:
    snapshot = await bot.runtime.compact_session("s1")

    assert _SECRET not in _snapshot_text(snapshot)


def test_the_unredacted_accessor_still_returns_the_value(bot: Any) -> None:
    """The escape hatch stays real. #31 documents who may call it and what must never get it."""
    snapshot = getattr(bot.sessions, _UNREDACTED_ACCESSOR)("s1")

    assert snapshot is not None
    assert _SECRET in _snapshot_text(snapshot)


def test_every_snapshot_reader_scrubs(bot: Any) -> None:
    """A surface walk, so a reader a later change adds fails here rather than leaking.

    A method that returns a snapshot and never reaches the redaction helpers is the defect this
    test exists to catch. The check reads each method's source for the scrub, which is coarse and
    catches the case that matters: somebody adds a reader and returns
    `snapshot_from_session(...)` straight to a caller.
    """
    from nanoinfra.sdk import clients

    scrubbed: list[str] = []
    unscrubbed: list[str] = []
    for holder in (clients.SessionClient, clients.RuntimeClient):
        for name, member in inspect.getmembers(holder, inspect.isfunction):
            if name.startswith("_") or name == _UNREDACTED_ACCESSOR:
                continue
            source = inspect.getsource(member)
            if "snapshot_from_session" not in source and "export_unredacted" not in source:
                continue
            target = scrubbed if "_redacted_snapshot" in source else unscrubbed
            target.append(f"{holder.__name__}.{name}")

    assert unscrubbed == []
    assert scrubbed
