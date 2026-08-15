# tests/gates/test_interactive_scope_refusal.py
"""Item 35 (#37): an interactive turn must not execute with an unknown blast radius.

`Executor._gate` allowed an interactive action whose scope would not resolve. My justification in
#9 was that the guard already refuses a pattern it cannot expand -- and it does not. An
`inventoryHost` holding a plain address takes the single-address path, passes the guard, and then
fails scope resolution when no local inventory file exists.

Two consequences followed. The `all`-scope refusal never ran on that path, because it needs a
resolution to know the scope. And ansible then targeted whatever its own configuration resolved,
which the resolver never saw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.servers.store import ServerStore

_ANSIBLE = "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())



def _interactive_allow() -> GatesConfig:
    """Interactive policy that permits a resolvable action, so the refusal must come from #37.

    The shipped interactive default is ``approve``, and #38 suspends an ``approve`` outcome
    until an operator answers. An unknown host set is what these tests check, so the two tests
    that must run declare the permission instead.
    """
    return GatesConfig.model_validate(
        {"interactive": {"mutate.remote": {"host": "allow", "group": "allow"}}}
    )


def _server_without_inventory(tmp_path: Path) -> ServerStore:
    """An ansible server whose projectPath holds no inventory file.

    The guard passes, because `inventoryHost` is a routine address and no expansion happens.
    """
    store = ServerStore(tmp_path)
    store.create(
        {
            "name": "no-inventory",
            "providerId": "ansible-runner",
            "config": {"inventoryHost": "10.0.2.11", "projectPath": str(tmp_path / "absent")},
        }
    )
    return store


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "no-inventory",
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


@pytest.mark.asyncio
async def test_an_interactive_action_with_an_unresolvable_scope_is_refused(
    tmp_path: Path,
) -> None:
    _server_without_inventory(tmp_path)

    with (
        patch(_ANSIBLE, new=AsyncMock()) as run,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
    ):
        response = await Executor(workspace=tmp_path, gates_loader=_interactive_allow).handle(_request())

    assert not response.ok
    run.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_the_refusal_names_the_fix(tmp_path: Path) -> None:
    """An operator who reads "denied" edits policy. One who reads the cause fixes the cause."""
    _server_without_inventory(tmp_path)

    with patch(_ANSIBLE, new=AsyncMock()):
        response = await Executor(workspace=tmp_path, gates_loader=_interactive_allow).handle(_request())

    reason = f"{response.reason} {response.error}"
    assert "inventory" in reason.lower()


@pytest.mark.asyncio
async def test_an_unattended_action_with_an_unresolvable_scope_is_still_refused(
    tmp_path: Path,
) -> None:
    """This half already held. The test keeps both contexts on one rule."""
    _server_without_inventory(tmp_path)

    with patch(_ANSIBLE, new=AsyncMock()) as run:
        response = await Executor(workspace=tmp_path, gates_loader=_interactive_allow).handle(
            _request(execution_context="automation")
        )

    assert not response.ok
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_preview_still_answers_without_a_resolution(tmp_path: Path) -> None:
    """A preview reaches no host, so an unknown blast radius costs it nothing."""
    _server_without_inventory(tmp_path)

    with patch(_ANSIBLE, new=AsyncMock()) as run:
        response = await Executor(workspace=tmp_path, gates_loader=_interactive_allow).handle(_request(preview_requested=True))

    assert response.ok
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_resolvable_interactive_action_still_runs(tmp_path: Path) -> None:
    """The refusal must be about an unknown host set, and not about ansible."""
    from nanoinfra.servers.execution.base import ExecutionResult

    project = tmp_path / "proj"
    project.mkdir()
    (project / "inventory").write_text("[web]\n10.0.2.11\n", encoding="utf-8")
    store = ServerStore(tmp_path)
    store.create(
        {
            "name": "has-inventory",
            "providerId": "ansible-runner",
            "config": {"inventoryHost": "10.0.2.11", "projectPath": str(project)},
        }
    )
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_ANSIBLE, new=AsyncMock(return_value=fake)) as run:
        response = await Executor(workspace=tmp_path, gates_loader=_interactive_allow).handle(
            _request(server_id_or_name="has-inventory")
        )

    assert response.ok
    run.assert_called_once()


@pytest.mark.asyncio
async def test_an_ssh_server_is_unaffected(tmp_path: Path) -> None:
    """ssh resolves from its own config, so this rule must not touch it."""
    from nanoinfra.servers.execution.base import ExecutionResult

    ServerStore(tmp_path).create(
        {"name": "web", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(
        "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
        new=AsyncMock(return_value=fake),
    ) as run:
        response = await Executor(workspace=tmp_path, gates_loader=_interactive_allow).handle(_request(server_id_or_name="web"))

    assert response.ok
    run.assert_called_once()
