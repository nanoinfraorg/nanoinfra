# tests/gates/test_executor_observations.py
"""Item 1 (#3) observations, now emitted by the executor (#18).

The log-only recorder followed the work it observes. #18 moved the credential resolution and the
transports into the executor, so the records for `execute_on_server` are written there. The
inventory tools stay agent-side, and their observations stay in
`tests/agent/tools/test_capability_observations.py`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from loguru import logger

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.store import ServerStore

_SECRET_VALUE = "s3cr3t-key-material"
_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"


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


def _server(tmp_path: Path, *, secret_ref: str | None = None) -> None:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
    }
    if secret_ref:
        raw["secretRef"] = secret_ref
    ServerStore(tmp_path).create(raw)


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _interactive_allow() -> GatesConfig:
    """Interactive policy that permits the action, so these tests ask a recorder question.

    The shipped interactive default is ``approve``, and #38 suspends an ``approve`` outcome
    until an operator answers on a second path. The log-only observation is what these tests
    check, so they declare the permission. An explicit policy also keeps the developer's own
    config out of the answer.
    """
    return GatesConfig.model_validate(
        {"interactive": {"mutate.remote": {"host": "allow", "group": "allow"}}}
    )


def _executor(tmp_path: Path) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=_interactive_allow)


@pytest.mark.asyncio
async def test_a_preview_records_one_observation_marked_preview(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    """The class does not drop for a preview. Only the decision says it was one."""
    _server(tmp_path)

    with patch(_BACKEND, new=AsyncMock()):
        await _executor(tmp_path).handle(_request(preview_requested=True))

    assert len(observations) == 1
    assert observations[0]["capability_class"] == MUTATE_REMOTE
    assert observations[0]["decision"] == "preview"


@pytest.mark.asyncio
async def test_execution_records_the_decision_the_gate_would_make(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    _server(tmp_path)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)):
        await _executor(tmp_path).handle(_request())

    assert [o["decision"] for o in observations] == ["would_gate"]


@pytest.mark.asyncio
async def test_credential_access_is_recorded_at_the_resolution_site(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    """A secretRef resolving to plaintext is its own class, emitted where it happens.

    That site now lives in the executor, which is the only process holding a plaintext.
    """
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "ssh_key", "providerId": "local", "value": _SECRET_VALUE}
    )
    _server(tmp_path, secret_ref=secret.id)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)):
        await _executor(tmp_path).handle(_request())

    assert [o["capability_class"] for o in observations] == [MUTATE_REMOTE, CREDENTIAL_ACCESS]


@pytest.mark.asyncio
async def test_no_observation_carries_a_secret_value(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "ssh_key", "providerId": "local", "value": _SECRET_VALUE}
    )
    _server(tmp_path, secret_ref=secret.id)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)):
        await _executor(tmp_path).handle(_request())

    assert observations
    assert all(_SECRET_VALUE not in str(o) for o in observations)


@pytest.mark.asyncio
async def test_an_observation_carries_a_command_digest_not_the_command_text(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    """#16 stores digests by default, because commands embed secrets."""
    _server(tmp_path)
    command = "mysql -u root -p'hunter2' -e 'select 1'"

    with patch(_BACKEND, new=AsyncMock()):
        await _executor(tmp_path).handle(_request(command=command, preview_requested=True))

    expected = "sha256:" + hashlib.sha256(command.encode()).hexdigest()
    assert observations[0]["command_digest"] == expected
    assert all("hunter2" not in str(o) for o in observations)


@pytest.mark.asyncio
async def test_a_call_refused_before_the_gate_records_nothing(
    tmp_path: Path, observations: list[dict[str, Any]]
) -> None:
    """The recorder sits after the existing refusals, so it logs reachable calls only.

    A refused call never reaches a gate, so recording it would inflate the count an operator
    uses to size the policy change.
    """
    ServerStore(tmp_path).create(
        {"name": "metadata-server", "providerId": "ssh", "config": {"host": "169.254.169.254"}}
    )

    with patch(_BACKEND, new=AsyncMock()):
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="metadata-server")
        )

    assert not response.ok
    assert observations == []
