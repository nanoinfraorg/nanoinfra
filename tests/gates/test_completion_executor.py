# tests/gates/test_completion_executor.py
"""The executor side of the completion record (nanoinfraorg/nanoinfra#46).

The executor decides, so the executor records. It recorded the decision alone, and a reviewer who
read an ``allow`` could not tell a command that ran from one that never reached the host.

The executor now appends one completion record when the action ends. The decision record still
lands before the action runs, and the completion record names it.

A refused action writes no completion record. Nothing ran, and a record of an outcome would
invent one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import DECISION_COMPLETION, AuditStore
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_GRANTED_COMMAND = "systemctl reload nginx"
_OUTPUT_MARKER = "line-the-log-must-never-hold-9137"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _server(tmp_path: Path) -> None:
    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _GRANTED_COMMAND,
        "session_id": "s1",
        "execution_context": "automation",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _granted() -> GatesConfig:
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["unattended"],
                    "hosts": ["10.0.1.5"],
                    "commands": [_GRANTED_COMMAND],
                }
            ],
        }
    )


def _executor(tmp_path: Path, audit: AuditStore, *, gates: GatesConfig | None = None) -> Executor:
    return Executor(
        workspace=tmp_path, gates_loader=lambda: gates or _granted(), audit=audit
    )


def _completions(audit: AuditStore) -> list[dict[str, Any]]:
    return [r for r in audit.read_all() if r["decision"] == DECISION_COMPLETION]


@pytest.mark.asyncio
async def test_a_successful_action_writes_a_completion_after_the_decision(tmp_path: Path) -> None:
    """The acceptance case. The second record holds the exit code and the duration."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        response = await _executor(tmp_path, audit).handle(_request())

    assert response.ok
    records = audit.read_all()
    assert [r["decision"] for r in records] == ["allow", DECISION_COMPLETION]
    assert records[1]["exit_code"] == 0
    assert isinstance(records[1]["duration_ms"], int)
    assert records[1]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_the_completion_names_the_decision_it_follows(tmp_path: Path) -> None:
    """A reader pairs the two records by an id, and not by two timestamps."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        await _executor(tmp_path, audit).handle(_request())

    decision, completion = audit.read_all()
    assert completion["follows"] == decision["record_id"]
    assert completion["session_id"] == "s1"
    assert completion["capability_class"] == "mutate.remote"
    assert completion["scope"] == decision["scope"]
    assert completion["hosts"] == decision["hosts"]
    assert completion["command_digest"] == decision["command_digest"]


@pytest.mark.asyncio
async def test_a_failed_command_records_the_exit_code_it_returned(tmp_path: Path) -> None:
    """A non-zero exit is a known outcome. It must not read as an unknown one."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")
    result = ExecutionResult(exit_code=1, output="", error="unit not found")

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        response = await _executor(tmp_path, audit).handle(_request())

    assert not response.ok
    assert _completions(audit)[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_a_timeout_records_an_unknown_exit_code(tmp_path: Path) -> None:
    """A timeout ends the action and leaves the outcome unknown. The record says so."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")
    result = ExecutionResult(
        exit_code=None, output="", error="Idle/absolute timeout exceeded", timed_out=True
    )

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        response = await _executor(tmp_path, audit).handle(_request())

    assert not response.ok
    completion = _completions(audit)[0]
    assert completion["exit_code"] is None
    assert "unknown" in str(completion["reason"])
    assert isinstance(completion["duration_ms"], int)


@pytest.mark.asyncio
async def test_a_lost_transport_records_an_unknown_exit_code(tmp_path: Path) -> None:
    """A transport that raises leaves the decision recorded and the outcome unknown."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")

    with patch(_BACKEND, new=AsyncMock(side_effect=OSError("connection reset"))):
        with pytest.raises(OSError):
            await _executor(tmp_path, audit).handle(_request())

    completion = _completions(audit)[0]
    assert completion["exit_code"] is None
    assert "unknown" in str(completion["reason"])


@pytest.mark.asyncio
async def test_a_refused_action_writes_no_completion(tmp_path: Path) -> None:
    """Nothing ran. Never ran and unknown are opposite facts, so no record may blur them."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, audit, gates=GatesConfig()).handle(_request())

    assert not response.ok
    run.assert_not_called()
    assert [r["decision"] for r in audit.read_all()] == ["denied"]
    assert _completions(audit) == []


@pytest.mark.asyncio
async def test_a_preview_writes_no_completion(tmp_path: Path) -> None:
    """A preview reaches no host, so it ends no action."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")

    with patch(_BACKEND, new=AsyncMock()) as run:
        await _executor(tmp_path, audit).handle(_request(preview_requested=True))

    run.assert_not_called()
    assert _completions(audit) == []


@pytest.mark.asyncio
async def test_no_completion_record_holds_the_command_output(tmp_path: Path) -> None:
    """#16 keeps a digest of the command for a reason. Output carries the same risk."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")
    result = ExecutionResult(exit_code=0, output=_OUTPUT_MARKER, error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        response = await _executor(tmp_path, audit).handle(_request())

    assert _OUTPUT_MARKER in response.output
    assert _OUTPUT_MARKER not in str(audit.read_all())


@pytest.mark.asyncio
async def test_an_action_still_answers_when_the_completion_write_fails(tmp_path: Path) -> None:
    """The decision record landed before the action ran, which is the order #16 protects.

    A failed completion write costs the outcome record alone. The action already reached the
    host, so a refusal after the fact would hide a real result.
    """
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with (
        patch(_BACKEND, new=AsyncMock(return_value=result)),
        patch.object(AuditStore, "record_completion", side_effect=OSError("disk full")),
    ):
        response = await _executor(tmp_path, audit).handle(_request())

    assert response.ok
    assert [r["decision"] for r in audit.read_all()] == ["allow"]
