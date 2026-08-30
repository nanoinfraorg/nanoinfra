"""The executor half of a connector call: the gate, the approval, and the record.

A connector call is performed in the executor for the same reasons a command is -- the
credential lives there, the approval socket belongs to it, and the audit log is written by it.
So the checks here are the ones ``tests/gates/test_approval_gate.py`` makes about a command,
asked of a data call: an unattended write refuses, an interactive write suspends and runs only
after a person answers, and the record names what was used and for what.

The one property that has no command equivalent is the first test: two operations of one
connector take two different decisions in the same turn. That is what the kind exists for.

The file lives beside the other executor tests rather than under ``tests/connectors`` for one
practical reason: the suspension wait is a sibling module here, and ``tests`` holds no
``__init__.py``, so it imports by bare name only from this directory. A local copy of that wait
is what ``suspension_wait.py`` exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.executor.connector_action import ConnectorActionRunner
from nanoinfra.gates.executor.operator_socket import ApprovalService
from nanoinfra.gates.executor.protocol import ConnectorRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.prompt import digest_rendered_prompt
from nanoinfra.gates.tokens import ApprovalTokenStore

READONLY = "https://www.googleapis.com/auth/calendar.readonly"
EVENTS = "https://www.googleapis.com/auth/calendar.events"
CALENDAR = "google-calendar"


def _connectors(**over: Any) -> ConnectorRuntimeConfig:
    payload: dict[str, Any] = {
        "credentials": {
            "google-workspace": {
                "clientId": "cid.apps.googleusercontent.test",
                "secretRef": "google-refresh",
                "clientSecretRef": "google-client-secret",
                "scopes": [READONLY, EVENTS],
            }
        },
        "connectors": {
            "google-calendar": {
                "credential": "google-workspace",
                "settings": {"calendarId": "primary"},
            }
        },
        "active": [CALENDAR],
    }
    payload.update(over)
    return ConnectorRuntimeConfig.model_validate(payload)


def _gates(**over: Any) -> GatesConfig:
    """A deployment with two paths and one approver on the second one."""
    raw: dict[str, Any] = {
        "approvers": [{"channel": "webui", "sender": "operator-1"}],
        "approvalPaths": ["webui", "telegram"],
        "approvalTimeoutS": 30,
    }
    raw.update(over)
    return GatesConfig.model_validate(raw)


def _write_args(summary: str = "Standup") -> str:
    """A create_event body the operation's own schema accepts."""
    return json.dumps(
        {
            "summary": summary,
            "start": {"date": "2026-09-01"},
            "end": {"date": "2026-09-01"},
        }
    )


def _request(**over: Any) -> ConnectorRequest:
    fields: dict[str, Any] = {
        "connector": CALENDAR,
        "operation": "list_events",
        "arguments_json": "{}",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "token_nonce": None,
        "origin_path": "telegram",
        "origin_actor": None,
    }
    fields.update(over)
    return ConnectorRequest(**fields)


class _Tokens:
    """Stands in for the executor's token source. Records what it was asked for."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def access_token(
        self, connector: str, capability_class: str, *, force_refresh: bool = False
    ) -> str:
        self.asked.append((connector, capability_class))
        return f"token-for-{capability_class}"


class _Harness:
    """One runner, one audit store, and the approval path, over a fake API."""

    def __init__(self, tmp_path: Path, gates: GatesConfig | None = None) -> None:
        self.gates = gates or _gates()
        self.audit = AuditStore(tmp_path / "gates")
        self.pending = PendingApprovalStore()
        self.approvals = ApprovalTokenStore()
        self.tokens = _Tokens()
        self.requests: list[httpx.Request] = []
        self.runner = ConnectorActionRunner(
            workspace=tmp_path,
            connectors_loader=_connectors,
            gates_loader=lambda: self.gates,
            audit=self.audit,
            pending=self.pending,
            tokens=self.approvals,
        )
        # The token source is the executor's; the fake keeps a real refresh exchange out of a
        # unit test without moving where the mint happens.
        self.runner._sources[CALENDAR] = self.tokens  # pyright: ignore[reportAttributeAccessIssue]
        self.service = ApprovalService(
            pending=self.pending,
            tokens=self.approvals,
            gates_loader=lambda: self.gates,
            audit=self.audit,
        )

    def api(self, response: httpx.Response | None = None) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return response or httpx.Response(200, json={"items": []})

        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def records(self) -> list[dict[str, Any]]:
        return list(self.audit.read_all())

    def decisions(self) -> list[str]:
        return [str(record["decision"]) for record in self.records()]

    async def wait_for_one_pending(self, task: "asyncio.Task[Any] | None" = None) -> Any:
        from suspension_wait import wait_for_one_pending as _wait

        return await _wait(self.pending, None, task)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the engine's client factory at a harness. Nothing leaves the process."""

    def install(harness: _Harness, response: httpx.Response | None = None) -> None:
        monkeypatch.setattr("nanoinfra.connectors.engine._client", harness.api(response))

    return install


# --- the asymmetry the kind exists for -------------------------------------------------


async def test_a_read_and_a_write_of_one_connector_take_different_decisions(
    tmp_path: Path, api: Any
) -> None:
    """One turn, one credential, two answers. An MCP server has one class for both."""
    harness = _Harness(tmp_path)
    api(harness)

    read = await harness.runner.handle(_request(execution_context="cron"))
    write = await harness.runner.handle(
        _request(
            execution_context="cron",
            operation="create_event",
            arguments_json=json.dumps(
                {"summary": "Standup", "start": {"date": "2026-09-01"}, "end": {"date": "2026-09-01"}}
            ),
        )
    )

    assert read.ok is True
    assert json.loads(read.output) == {"items": []}
    assert write.ok is False
    assert "mutate.remote" in write.reason or "deny" in write.reason
    # One request reached the API: the read. The write never got a token.
    assert len(harness.requests) == 1
    assert harness.tokens.asked == [(CALENDAR, "read")]


async def test_a_write_the_gate_refuses_sends_nothing_and_is_recorded(
    tmp_path: Path, api: Any
) -> None:
    harness = _Harness(tmp_path)
    api(harness)

    response = await harness.runner.handle(
        _request(
            execution_context="cron", operation="create_event", arguments_json=_write_args()
        )
    )
    assert response.ok is False
    assert harness.requests == []
    assert "denied" in harness.decisions()


async def test_a_standing_grant_naming_the_connector_lets_an_unattended_write_run(
    tmp_path: Path, api: Any
) -> None:
    """The grant dimension that did not exist: a connector call has no host and no command."""
    harness = _Harness(
        tmp_path,
        _gates(
            unattended={"mutate.remote": {"host": "grant"}},
            standingGrants=[
                {
                    "id": "cal-write",
                    "contexts": ["unattended"],
                    "connectors": [CALENDAR],
                    "operations": ["create_event"],
                }
            ],
        ),
    )
    api(harness, httpx.Response(200, json={"id": "e1", "summary": "Standup"}))

    response = await harness.runner.handle(
        _request(
            execution_context="cron",
            operation="create_event",
            arguments_json=json.dumps(
                {"summary": "Standup", "start": {"date": "2026-09-01"}, "end": {"date": "2026-09-01"}}
            ),
        )
    )
    assert response.ok is True
    assert json.loads(response.output)["id"] == "e1"
    assert harness.requests[0].method == "POST"
    # The token was minted for the write class, and only for it.
    assert harness.tokens.asked == [(CALENDAR, "mutate.remote")]
    record = harness.records()[-1]
    assert record["decision"] == "allow"
    assert record["grant_id"] == "cal-write"


# --- what the record has to answer -----------------------------------------------------


async def test_the_record_names_the_connector_the_class_and_the_credential(
    tmp_path: Path, api: Any
) -> None:
    """"What used the Google credential, and for what" is answered where it already is."""
    harness = _Harness(tmp_path)
    api(harness)

    await harness.runner.handle(_request(execution_context="cron"))
    record = harness.records()[-1]
    assert record["tool"] == "google_calendar_list_events"
    assert record["capability_class"] == "read"
    assert record["secret_ref"] == "google-workspace"
    assert record["hosts"] == ["www.googleapis.com"]
    assert record["scope"] == "host"


# --- the approval path -----------------------------------------------------------------


async def test_an_interactive_write_suspends_and_runs_after_a_person_answers(
    tmp_path: Path, api: Any
) -> None:
    """The reason the call belongs in the executor: this is not expressible in the agent."""
    harness = _Harness(tmp_path)
    api(harness, httpx.Response(200, json={"id": "e1", "summary": "Standup"}))

    task = asyncio.create_task(
        harness.runner.handle(
            _request(
                operation="create_event",
                arguments_json=json.dumps(
                    {
                        "summary": "Standup",
                        "start": {"dateTime": "2026-09-01T10:00:00-06:00"},
                        "end": {"dateTime": "2026-09-01T10:15:00-06:00"},
                    }
                ),
            )
        )
    )
    approval = await harness.wait_for_one_pending(task)

    # Nothing has been sent while a person is being asked.
    assert harness.requests == []
    # The payload a person reads carries the values that make up the action.
    assert "Standup" in approval.payload
    assert "POST https://www.googleapis.com/calendar/v3/calendars/primary/events" in approval.payload

    answer = harness.service.approve(
        request_id=approval.request_id,
        actor="operator-1",
        approval_path="webui",
        target_digest=digest_rendered_prompt(approval.payload),
    )
    assert answer.ok is True

    response = await task
    assert response.ok is True
    assert harness.requests[0].method == "POST"
    assert harness.decisions() == ["approve", "allow"]


async def test_a_denial_refuses_the_call_and_carries_the_operators_words(
    tmp_path: Path, api: Any
) -> None:
    harness = _Harness(tmp_path)
    api(harness)

    task = asyncio.create_task(
        harness.runner.handle(
            _request(
                operation="create_event",
                arguments_json=json.dumps(
                    {"summary": "No", "start": {"date": "2026-09-01"}, "end": {"date": "2026-09-01"}}
                ),
            )
        )
    )
    approval = await harness.wait_for_one_pending(task)
    harness.service.deny(
        request_id=approval.request_id,
        actor="operator-1",
        approval_path="webui",
        reason="not this calendar",
    )

    response = await task
    assert response.ok is False
    assert "not this calendar" in response.reason
    assert harness.requests == []


async def test_an_executor_with_no_approval_store_refuses_without_latching(
    tmp_path: Path, api: Any
) -> None:
    """A deployment fact, so the refusal names the grant that would work and does not latch."""
    harness = _Harness(tmp_path)
    harness.runner.pending = None
    harness.runner.tokens = None
    api(harness)

    response = await harness.runner.handle(
        _request(operation="create_event", arguments_json=_write_args())
    )
    assert response.ok is False
    assert response.terminal is False
    assert '"connectors": ["google-calendar"]' in response.reason


# --- the preview -----------------------------------------------------------------------


async def test_a_preview_renders_the_request_and_sends_nothing(tmp_path: Path, api: Any) -> None:
    harness = _Harness(tmp_path)
    api(harness)

    response = await harness.runner.handle(
        _request(
            operation="create_event",
            preview_requested=True,
            arguments_json=json.dumps(
                {"summary": "Standup", "start": {"date": "2026-09-01"}, "end": {"date": "2026-09-01"}}
            ),
        )
    )
    assert response.ok is True
    assert response.output.startswith("POST https://www.googleapis.com/calendar/v3/calendars/primary/events")
    assert "Standup" in response.output
    assert response.preview_outcome == "approve"
    assert harness.requests == []
    # A preview asks nobody and records nothing: writing it as a denial would latch the session.
    assert harness.records() == []


# --- what the frame cannot do ----------------------------------------------------------


async def test_the_frame_cannot_relabel_a_write_as_a_read(tmp_path: Path, api: Any) -> None:
    """The class comes from the manifest. The agent names an operation and nothing else."""
    harness = _Harness(tmp_path)
    api(harness)

    response = await harness.runner.handle(
        _request(
            execution_context="cron", operation="create_event", arguments_json=_write_args()
        )
    )
    assert response.ok is False
    assert harness.records()[-1]["capability_class"] == "mutate.remote"


async def test_an_undeclared_argument_is_refused(tmp_path: Path, api: Any) -> None:
    harness = _Harness(tmp_path)
    api(harness)

    response = await harness.runner.handle(
        _request(arguments_json=json.dumps({"pageToken": "x", "sneaky": "y"}))
    )
    assert response.ok is False
    assert "sneaky" in response.reason
    assert harness.requests == []


async def test_arguments_that_are_not_a_json_object_are_refused(
    tmp_path: Path, api: Any
) -> None:
    harness = _Harness(tmp_path)
    api(harness)

    assert (await harness.runner.handle(_request(arguments_json="[1, 2]"))).ok is False
    assert (await harness.runner.handle(_request(arguments_json="{"))).ok is False
    assert harness.requests == []


async def test_a_nonce_on_the_frame_is_refused(tmp_path: Path, api: Any) -> None:
    """The executor issues every nonce and hands none to the agent."""
    harness = _Harness(tmp_path)
    api(harness)

    response = await harness.runner.handle(_request(token_nonce="deadbeef"))
    assert response.ok is False
    assert "issues every nonce" in response.reason
    assert harness.requests == []


async def test_a_connector_that_is_not_active_names_the_ones_that_are(
    tmp_path: Path, api: Any
) -> None:
    harness = _Harness(tmp_path)
    api(harness)

    response = await harness.runner.handle(_request(connector="gmail"))
    assert response.ok is False
    assert "not active" in response.reason
    assert response.terminal is False


async def test_an_operation_the_deployment_disabled_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """`enabledOperations` is a ceiling the executor applies, not a hint to the agent."""
    harness = _Harness(tmp_path)
    api(harness)
    harness.runner.connectors_loader = lambda: _connectors(
        connectors={
            "google-calendar": {
                "credential": "google-workspace",
                "enabledOperations": ["list_events"],
            }
        }
    )

    response = await harness.runner.handle(
        _request(operation="create_event", arguments_json=_write_args())
    )
    assert response.ok is False
    assert "does not offer" in response.reason
    assert harness.requests == []


# --- the dispatch ----------------------------------------------------------------------


async def test_the_executor_routes_a_connector_frame_to_the_connector_half(
    tmp_path: Path,
) -> None:
    """One socket, two request kinds, and neither can be read as the other."""
    executor = Executor(workspace=tmp_path, gates_loader=_gates)
    runner = ConnectorActionRunner(
        workspace=tmp_path, connectors_loader=_connectors, gates_loader=_gates
    )
    executor.connector_runner = runner

    response = await executor.dispatch(_request(token_nonce="deadbeef"))
    assert response.ok is False
    assert "issues every nonce" in response.reason
