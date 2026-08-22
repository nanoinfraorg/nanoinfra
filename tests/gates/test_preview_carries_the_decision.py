# tests/gates/test_preview_carries_the_decision.py
"""A preview answers what the gate would say -- #179, #180.

The preview used to return before the gate ran, so it said what would run and stayed silent on
whether it would be permitted. An operator then learned the policy from a refusal at the
automation's first scheduled slot, and had to reverse-engineer the grant from it.

The second half is the trap: the hypothetical must never be written as a `denied` gate record,
because `restore_latches` rebuilds latch state from exactly those records. Asking what the gate
would say would otherwise latch the session and block the automation being commissioned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.gates.latch_restore import restore_latches
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_SECRET = "s3cr3t-key-material"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".nanoinfra" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"gates": {}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", config)


def _workspace(tmp_path: Path) -> Path:
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "password", "providerId": "local", "value": _SECRET}
    )
    ServerStore(tmp_path).create({
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
        "secretRef": secret.id,
    })
    return tmp_path


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "automation",
        "preview_requested": True,
        "timeout_s": None,
        "token_nonce": None,
        "origin_path": "websocket",
        "origin_actor": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)  # pyright: ignore[reportArgumentType]


def _gates(raw: dict[str, Any]) -> GatesConfig:
    return GatesConfig.model_validate(raw)


def _executor(tmp_path: Path, gates: GatesConfig) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=lambda: gates)


def _grant_policy() -> GatesConfig:
    return _gates({
        "unattended": {"mutate.remote": {"host": "grant"}, "credential.access": "grant"},
    })


@pytest.mark.asyncio
async def test_a_preview_names_the_grant_that_would_permit_it(tmp_path: Path) -> None:
    _workspace(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, _grant_policy()).handle(_request())

    run.assert_not_called()
    assert response.ok
    assert response.preview_outcome == "deny"
    # The two halves a standing grant takes, resolved rather than as the caller named them.
    assert response.preview_hosts == ["10.0.1.5"]
    assert response.preview_command == "uptime"
    assert response.preview_scope == "host"
    assert "grant" in response.preview_reason


@pytest.mark.asyncio
async def test_a_preview_reports_the_grant_it_matched(tmp_path: Path) -> None:
    _workspace(tmp_path)
    gates = _grant_policy()
    gates.standing_grants = _gates({
        "standingGrants": [
            {"id": "uptime-check", "contexts": ["unattended"], "hosts": ["10.0.1.5"],
             "commands": ["uptime"]},
        ]
    }).standing_grants

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, gates).handle(_request())

    run.assert_not_called()
    assert response.preview_outcome == "allow"
    assert response.preview_grant_id == "uptime-check"
    # The credential the action would need is answered too: a permitted command still dies at
    # the secret when that class refuses, which is how the first real automation failed.
    assert response.preview_credential_outcome == "allow"
    assert "uptime-check" in response.preview_credential_reason


@pytest.mark.asyncio
async def test_a_preview_names_the_cell_that_shadows_the_grant(tmp_path: Path) -> None:
    """A grant plus a deny is a silent no-op, and the gate already has the words for it."""
    _workspace(tmp_path)
    gates = _gates({
        "unattended": {"mutate.remote": {"host": "deny"}},
        "standingGrants": [
            {"id": "uptime-check", "contexts": ["unattended"], "hosts": ["10.0.1.5"],
             "commands": ["uptime"]},
        ],
    })

    response = await _executor(tmp_path, gates).handle(_request())

    assert response.preview_outcome == "deny"
    assert "gates.unattended.mutate.remote.host is 'deny'" in response.preview_reason


@pytest.mark.asyncio
async def test_a_preview_reports_a_refused_credential_behind_a_permitted_command(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    gates = _gates({
        "unattended": {"mutate.remote": {"host": "grant"}, "credential.access": "deny"},
        "standingGrants": [
            {"id": "uptime-check", "contexts": ["unattended"], "hosts": ["10.0.1.5"],
             "commands": ["uptime"]},
        ],
    })

    response = await _executor(tmp_path, gates).handle(_request())

    assert response.preview_outcome == "allow"
    assert response.preview_credential_outcome == "deny"


@pytest.mark.asyncio
async def test_a_previewed_refusal_latches_nothing(tmp_path: Path) -> None:
    """#180. Asking must not block the automation the question is about.

    The control matters as much as the assertion: the same refusal, taken for real, does latch.
    Without that half this test would pass on an executor that records nothing at all.
    """
    _workspace(tmp_path)
    audit = AuditStore(tmp_path / "audit")
    gates = _grant_policy()

    def executor() -> Executor:
        return Executor(workspace=tmp_path, gates_loader=lambda: gates, audit=audit)

    for _ in range(3):
        response = await executor().handle(_request())
        assert response.preview_outcome == "deny"

    assert dict(restore_latches(audit).latched) == {}

    real = await executor().handle(_request(preview_requested=False))

    assert not real.ok
    assert list(restore_latches(audit).latched) == [("s1", "mutate.remote")]


@pytest.mark.asyncio
async def test_the_dry_run_tells_the_operator_which_grant_to_write() -> None:
    """#181. The answer arrives in the tool result, without a refusal having to happen."""
    from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
    from nanoinfra.gates.executor.protocol import ExecuteResponse

    answer = ExecuteResponse(
        ok=True,
        output="Preview (not executed): server='prod-web-01' command='uptime'",
        exit_code=None,
        error=None,
        reason="the caller asked for a preview",
        preview_outcome="deny",
        preview_reason=(
            "mutate.remote at host scope is 'grant' for an unattended context, and no standing "
            "grant covers it."
        ),
        preview_scope="host",
        preview_hosts=["10.0.1.5"],
        preview_command="uptime",
        preview_credential_outcome="deny",
        preview_credential_reason="gates.unattended.credential.access is 'deny'.",
    )

    class _Client:
        def execute(self, **_: Any) -> ExecuteResponse:
            return answer

    tool = ExecuteOnServerTool(client=_Client())  # pyright: ignore[reportArgumentType]
    result = await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=True)

    assert "A real run of this action would be: deny" in result
    # The credential is the layer an operator fixes second, having fixed the command cell first.
    assert "The credential it needs would be: deny" in result
    assert '"hosts": ["10.0.1.5"]' in result
    assert '"commands": ["uptime"]' in result
