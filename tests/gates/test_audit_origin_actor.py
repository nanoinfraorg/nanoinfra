# tests/gates/test_audit_origin_actor.py
"""The audit log names who asked as well as who answered -- nanoinfraorg/nanoinfra#79.

#16 records ``actor``, and that field names the person who answered. No field named the person
who asked, so a reviewer read the origin identity in a sentence of prose inside ``reason``. Prose
is not a field anybody can filter or count.

``origin_actor`` is that field. #68 turns two people on one path into two factors, so the question
a reviewer asks is "who asked, and who approved". Both halves are now columns.

Three rules hold here, and each one has a test below.

1. A blank string never stands for "nobody". #67 keeps ``None`` and empty text apart on the wire,
   because empty text reads as a name. The record holds the same line, so the store writes
   ``null``.
2. Both record kinds carry the value: the decision record and the completion record of #46.
3. The rendered approval payload carries nothing of this. Those are the bytes a human authorizes,
   and the origin identity is the agent's assertion about itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.config.gates import AuditConfig, GatesConfig
from nanoinfra.gates.audit import DECISION_COMPLETION, AuditStore
from nanoinfra.gates.executor.operator_socket import REFUSED_ANSWER_DECISION, ApprovalService
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.secrets import crypto
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_COMMAND = "systemctl reload nginx"
_HOST = "10.0.1.5"

# The two people. One raises the turn, and the other answers it. #68 exists so that these are
# never the same person.
_WHO_ASKED = "webui:alberto@example.com"
_WHO_ANSWERED = "webui:paula@example.com"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _store(root: Path) -> AuditStore:
    return AuditStore(root / "gates" / "audit", config=AuditConfig(retention_days=90))


def _server(tmp_path: Path) -> None:
    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": _HOST}}
    )


def _request(**over: Any) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "automation",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
        "origin_path": "webui",
        "origin_actor": _WHO_ASKED,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _granted() -> GatesConfig:
    """One standing grant, so an unattended action reaches the host with no human."""
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["unattended"],
                    "hosts": [_HOST],
                    "commands": [_COMMAND],
                }
            ],
        }
    )


def _two_people_on_one_path() -> GatesConfig:
    """One authenticated path, two named people, and identity independence on (#68)."""
    return GatesConfig.model_validate(
        {
            "approvers": [{"channel": "webui", "sender": _WHO_ANSWERED}],
            "approvalPaths": ["webui"],
            "approvalTimeoutS": 30,
            "identityIndependence": True,
        }
    )


# -- the store -------------------------------------------------------------------------------


def test_a_decision_record_names_the_person_who_asked_beside_the_one_who_answered(
    tmp_path: Path,
) -> None:
    """The two halves of #68 are two columns, and neither one reads as the other."""
    store = _store(tmp_path)

    written = store.record(
        decision="allow",
        capability_class=MUTATE_REMOTE,
        execution_context="interactive",
        origin_actor=_WHO_ASKED,
        actor=_WHO_ANSWERED,
    )

    assert written["origin_actor"] == _WHO_ASKED
    assert written["actor"] == _WHO_ANSWERED
    assert store.read_all() == [written]


@pytest.mark.parametrize("nobody", [None, "", "   "])
def test_a_request_that_authenticated_nobody_records_null_and_never_empty_text(
    tmp_path: Path, nobody: str | None
) -> None:
    """Empty text reads as a name, so it must never stand for "the path authenticated nobody".

    #67 keeps ``None`` and the empty string apart on the wire for this reason. The store holds
    the same line, and it holds it for every writer rather than trust each one.
    """
    store = _store(tmp_path)

    written = store.record(
        decision="deny",
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        origin_actor=nobody,
    )

    assert written["origin_actor"] is None
    assert store.read_all()[0]["origin_actor"] is None
    line = store.segments()[0].read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(line)["origin_actor"] is None


def test_a_completion_record_carries_the_person_who_asked(tmp_path: Path) -> None:
    """A filter on one person must show the outcome of their action and not the decision alone."""
    store = _store(tmp_path)
    decision = store.record(
        decision="allow",
        capability_class=MUTATE_REMOTE,
        execution_context="interactive",
        origin_actor=_WHO_ASKED,
        actor=_WHO_ANSWERED,
    )

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    assert completion["origin_actor"] == _WHO_ASKED
    # The person who answered stays on the decision record alone. That record holds the
    # authorization, so one authorization cannot read two ways.
    assert completion["actor"] is None


def test_a_completion_record_of_a_request_that_named_nobody_stays_null(tmp_path: Path) -> None:
    """The copy must not invent a name where the decision record holds none."""
    store = _store(tmp_path)
    decision = store.record(
        decision="allow", capability_class=MUTATE_REMOTE, execution_context="automation"
    )

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    assert decision["origin_actor"] is None
    assert completion["origin_actor"] is None


def test_a_completion_record_ignores_an_origin_actor_that_is_not_text(tmp_path: Path) -> None:
    """A record read back from disk is a dynamic boundary, so the copy normalizes the shape."""
    store = _store(tmp_path)

    completion = store.record_completion(
        follows={"record_id": "abc", "origin_actor": ["not", "a", "name"]},
        exit_code=0,
        duration_ms=7,
    )

    assert completion["origin_actor"] is None


# -- the executor ----------------------------------------------------------------------------


def _executor(tmp_path: Path, audit: AuditStore, gates: GatesConfig, **over: Any) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=lambda: gates, audit=audit, **over)


@pytest.mark.asyncio
async def test_the_executor_records_the_origin_identity_on_the_decision_and_the_completion(
    tmp_path: Path,
) -> None:
    """The acceptance case of #79. Both record kinds name the person the request came from."""
    _server(tmp_path)
    audit = _store(tmp_path)
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        response = await _executor(tmp_path, audit, _granted()).handle(_request())

    assert response.ok, response.error
    decision, completion = audit.read_all()
    assert decision["decision"] == "allow"
    assert completion["decision"] == DECISION_COMPLETION
    assert decision["origin_actor"] == _WHO_ASKED
    assert completion["origin_actor"] == _WHO_ASKED


@pytest.mark.asyncio
@pytest.mark.parametrize("nobody", [None, ""])
async def test_a_wire_request_that_names_nobody_records_null(
    tmp_path: Path, nobody: str | None
) -> None:
    """A channel that authenticated nobody must not reach the log as a blank name.

    The executor holds the value as text for the pending store, so the round trip ends here.
    """
    _server(tmp_path)
    audit = _store(tmp_path)
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        await _executor(tmp_path, audit, _granted()).handle(_request(origin_actor=nobody))

    assert [record["origin_actor"] for record in audit.read_all()] == [None, None]


@pytest.mark.asyncio
async def test_a_refused_action_records_the_person_who_asked(tmp_path: Path) -> None:
    """A denial is the record an incident reads first, so it names the person who raised it."""
    _server(tmp_path)
    audit = _store(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, audit, GatesConfig()).handle(_request())

    assert not response.ok
    run.assert_not_called()
    assert [record["origin_actor"] for record in audit.read_all()] == [_WHO_ASKED]


# -- the approval path ------------------------------------------------------------------------


class _Suspended:
    """One executor, its pending store, and the service an operator answers through."""

    def __init__(self, tmp_path: Path, gates: GatesConfig) -> None:
        self.audit = _store(tmp_path)
        self.pending = PendingApprovalStore()
        self.tokens = ApprovalTokenStore()
        self.gates = gates
        self.executor = _executor(
            tmp_path, self.audit, gates, pending=self.pending, tokens=self.tokens
        )
        self.service = ApprovalService(
            pending=self.pending,
            tokens=self.tokens,
            gates_loader=lambda: gates,
            audit=self.audit,
        )

    async def wait_for_one_pending(self, timeout_s: float = 5.0) -> Any:
        for _ in range(int(timeout_s / 0.01)):
            items = self.pending.pending()
            if items:
                return items[0]
            await asyncio.sleep(0.01)
        raise AssertionError("the executor never suspended an action")

    def records(self, decision: str) -> list[dict[str, Any]]:
        return [record for record in self.audit.read_all() if record["decision"] == decision]


@pytest.mark.asyncio
async def test_an_approved_action_records_who_asked_and_who_answered(tmp_path: Path) -> None:
    """The question #68 makes a reviewer ask. One record answers both halves of it."""
    _server(tmp_path)
    running = _Suspended(tmp_path, _two_people_on_one_path())
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        task = asyncio.create_task(
            running.executor.handle(_request(execution_context="interactive"))
        )
        suspended = await running.wait_for_one_pending()
        answer = running.service.approve(
            request_id=suspended.request_id,
            actor=_WHO_ANSWERED,
            approval_path="webui",
            target_digest=suspended.target_digest,
        )
        response = await task

    assert answer.ok, answer.error
    assert response.ok, response.error
    # The record that suspends the action, the record that allows it, and the outcome.
    for decision in ("approve", "allow", DECISION_COMPLETION):
        holding = running.records(decision)
        assert [record["origin_actor"] for record in holding] == [_WHO_ASKED], decision
    assert running.records("allow")[0]["actor"] == _WHO_ANSWERED


@pytest.mark.asyncio
async def test_a_refused_answer_names_the_person_who_raised_the_request(tmp_path: Path) -> None:
    """The answer did not count, and the action still waits. The record names both people."""
    _server(tmp_path)
    running = _Suspended(tmp_path, _two_people_on_one_path())

    with patch(_BACKEND, new=AsyncMock()):
        task = asyncio.create_task(
            running.executor.handle(_request(execution_context="interactive"))
        )
        suspended = await running.wait_for_one_pending()
        answer = running.service.approve(
            request_id=suspended.request_id,
            # The person who asked cannot answer their own request in any mode.
            actor=_WHO_ASKED,
            approval_path="webui",
            target_digest=suspended.target_digest,
        )
        running.service.deny(
            request_id=suspended.request_id, actor=_WHO_ANSWERED, approval_path="webui"
        )
        await task

    assert not answer.ok
    refused = running.records(REFUSED_ANSWER_DECISION)
    assert [record["origin_actor"] for record in refused] == [_WHO_ASKED]
    assert [record["actor"] for record in refused] == [_WHO_ASKED]


@pytest.mark.asyncio
async def test_the_rendered_approval_payload_holds_no_origin_identity(tmp_path: Path) -> None:
    """What #79 does not build.

    The payload is the bytes a human authorizes, and it holds what the executor resolved rather
    than what the agent claimed. An unverified name in it is an unfaithful summary.
    """
    _server(tmp_path)
    running = _Suspended(tmp_path, _two_people_on_one_path())

    with patch(_BACKEND, new=AsyncMock()):
        task = asyncio.create_task(
            running.executor.handle(_request(execution_context="interactive"))
        )
        suspended = await running.wait_for_one_pending()
        running.service.deny(
            request_id=suspended.request_id, actor=_WHO_ANSWERED, approval_path="webui"
        )
        await task

    assert _WHO_ASKED not in suspended.payload
    assert "alberto" not in suspended.payload
