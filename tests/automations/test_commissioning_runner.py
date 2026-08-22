# tests/automations/test_commissioning_runner.py
"""One rehearsal, and the verdict it leaves -- #183, #184, #189, #190."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.automations.commissioning import (
    COMMISSIONING_TURN_META,
    CommissioningCollector,
    PreviewedAction,
    bind_commissioning,
    current_commissioning,
)
from nanoinfra.automations.commissioning_runner import (
    commission_cron_job,
    compose_finding,
)
from nanoinfra.automations.commissioning_state import (
    ERROR,
    OK,
    REFUSED,
    UNCHECKED,
    CommissioningState,
    commissioning_fingerprint,
)
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.cron.service import CronService
from nanoinfra.cron.types import CronSchedule


def _job(
    tmp_path: Path,
    message: str = "Report the uptime",
    references: list[dict[str, str]] | None = None,
) -> Any:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service._running = True
    return service, service.add_job(
        name="uptime watch",
        schedule=CronSchedule(kind="every", every_ms=300_000),
        message=message,
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        references=references or [],
    )


class _Agent:
    """An agent that runs one previewed action, the way a real turn would."""

    def __init__(self, action: PreviewedAction | None, *, fail: bool = False) -> None:
        self.action = action
        self.fail = fail
        self.messages: list[InboundMessage] = []

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        self.messages.append(msg)
        if self.fail:
            raise RuntimeError("the provider was unreachable")
        # The turn runs in another task, which is exactly what the metadata id is for.
        async def _turn() -> None:
            with bind_commissioning(msg.metadata):
                collector = current_commissioning()
                assert collector is not None, "the turn must see the collector"
                if self.action is not None:
                    collector.record(self.action)

        await asyncio.create_task(_turn())
        return OutboundMessage(channel="websocket", chat_id="chat-1", content="reported")


def _refused_action() -> PreviewedAction:
    return PreviewedAction(
        tool="execute_on_server",
        capability_class="mutate.remote",
        outcome="deny",
        reason="no standing grant covers it.",
        scope="host",
        hosts=("10.0.0.9",),
        command="uptime",
        credential_outcome="deny",
        credential_reason="gates.unattended.credential.access is 'deny'.",
    )


def _permitted_action() -> PreviewedAction:
    return PreviewedAction(
        tool="execute_on_server",
        capability_class="mutate.remote",
        outcome="allow",
        reason="Standing grant uptime-watch covers this action.",
        grant_id="uptime-watch",
        scope="host",
        hosts=("10.0.0.9",),
        command="uptime",
        credential_outcome="allow",
        credential_reason="the grant authorizes the credential.",
    )


@pytest.mark.asyncio
async def test_a_refused_rehearsal_proposes_the_grant_and_names_the_credential(
    tmp_path: Path,
) -> None:
    _, job = _job(tmp_path)
    agent = _Agent(_refused_action())

    report = await commission_cron_job(job, agent=agent, workspace_path=tmp_path)

    assert report.refused
    assert report.state.status == REFUSED
    assert "would be refused" in report.state.finding
    assert "the credential it needs would be deny" in report.state.finding
    [grant] = report.state.proposed_grants
    assert grant == {
        "id": "uptime-watch",
        "contexts": ["unattended"],
        "hosts": ["10.0.0.9"],
        "commands": ["uptime"],
    }
    # The rehearsal ran the automation's own turn, carrying its collector id.
    assert agent.messages[0].metadata[COMMISSIONING_TURN_META]


@pytest.mark.asyncio
async def test_a_permitted_rehearsal_leaves_the_automation_alone(tmp_path: Path) -> None:
    _, job = _job(tmp_path)

    report = await commission_cron_job(job, agent=_Agent(_permitted_action()), workspace_path=tmp_path)

    assert not report.refused
    assert report.state.status == OK
    assert report.state.proposed_grants == ()
    assert "permitted (standing grant uptime-watch)" in report.state.finding


@pytest.mark.asyncio
async def test_a_rehearsal_that_touches_nothing_gated_needs_no_grant(tmp_path: Path) -> None:
    _, job = _job(tmp_path)

    report = await commission_cron_job(job, agent=_Agent(None), workspace_path=tmp_path)

    assert report.state.status == OK
    assert "needs no standing grant" in report.state.finding


@pytest.mark.asyncio
async def test_a_rehearsal_that_could_not_run_is_an_error_and_not_a_refusal(
    tmp_path: Path,
) -> None:
    """Reporting a provider outage as refused would disable an automation over an unrelated fault."""
    _, job = _job(tmp_path)

    report = await commission_cron_job(job, agent=_Agent(None, fail=True), workspace_path=tmp_path)

    assert report.state.status == ERROR
    assert not report.refused
    assert "did not complete" in report.state.finding


@pytest.mark.asyncio
async def test_a_latched_session_outranks_every_grant(tmp_path: Path) -> None:
    _, job = _job(tmp_path)

    class _Latches:
        def latched_classes(self, session_id: str) -> frozenset[str]:
            return frozenset({"mutate.remote"})

    report = await commission_cron_job(
        job, agent=_Agent(_permitted_action()), workspace_path=tmp_path, latches=_Latches()
    )

    assert report.refused, "a permitted action still cannot run while the session is blocked"
    assert "blocked for mutate.remote" in report.state.finding


@pytest.mark.asyncio
async def test_the_verdict_disables_the_job_and_says_why(tmp_path: Path) -> None:
    """#189: the authoring work survives, and no refused automation stays enabled."""
    service, job = _job(tmp_path)

    report = await commission_cron_job(job, agent=_Agent(_refused_action()), workspace_path=tmp_path)
    service.set_commissioning(job.id, report.state, disable=report.refused)

    stored = service.get_job(job.id)
    assert stored is not None
    assert stored.enabled is False
    assert stored.state.next_run_at_ms is None
    assert stored.commissioning.status == REFUSED
    assert stored.commissioning.proposed_grants

    # Durable, and readable by a fresh process: the finding is what the editor renders.
    stored_json = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    [written] = [entry for entry in stored_json["jobs"] if entry["id"] == job.id]
    assert written["enabled"] is False
    assert written["commissioning"]["status"] == REFUSED
    assert written["commissioning"]["proposedGrants"][0]["commands"] == ["uptime"]


def test_an_inventory_write_is_reported_as_ungrantable() -> None:
    collector = CommissioningCollector()
    collector.record(
        PreviewedAction(
            tool="update_server",
            capability_class="mutate.inventory",
            outcome="deny",
            reason="mutate.inventory in a unattended context is deny.",
            command="update_server",
        )
    )

    finding, grants = compose_finding(name="repoint", collector=collector)

    assert grants == []
    assert "a standing grant cannot permit this" in finding


def test_the_fingerprint_ignores_what_does_not_change_the_commands() -> None:
    """#190: renaming or rescheduling must not cost a model turn."""
    base = commissioning_fingerprint(
        message="Report the uptime",
        references=[{"kind": "server", "id": "srv-1"}],
        skills=["docker"],
    )

    assert base == commissioning_fingerprint(
        message="  Report the uptime  ",
        references=[{"kind": "server", "id": "srv-1"}],
        skills=["docker"],
    )
    assert base != commissioning_fingerprint(
        message="Report the disk usage",
        references=[{"kind": "server", "id": "srv-1"}],
        skills=["docker"],
    )
    assert base != commissioning_fingerprint(
        message="Report the uptime",
        references=[{"kind": "server", "id": "srv-2"}],
        skills=["docker"],
    )
    assert base != commissioning_fingerprint(
        message="Report the uptime",
        references=[{"kind": "server", "id": "srv-1"}],
        skills=[],
    )


def test_a_stored_verdict_that_cannot_be_trusted_reads_as_unchecked() -> None:
    assert CommissioningState.from_dict(None).status == UNCHECKED
    assert CommissioningState.from_dict({"status": "ok!"}).status == UNCHECKED
    assert CommissioningState.from_dict({"status": "ok"}).status == OK
    # A verdict only applies to the automation it was reached about.
    state = CommissioningState(status=OK, fingerprint="abc")
    assert state.applies_to("abc")
    assert not state.applies_to("def")
    assert not CommissioningState(status=OK).applies_to("")


@pytest.mark.asyncio
async def test_a_reference_that_no_longer_resolves_is_a_refusal_about_the_automation(
    tmp_path: Path,
) -> None:
    """No grant fixes a deleted server, so the verdict must not read as a missing permission."""
    _, job = _job(tmp_path, references=[{"kind": "server", "id": "gone"}])

    report = await commission_cron_job(job, agent=_Agent(None), workspace_path=tmp_path)

    assert report.refused
    assert report.state.status == REFUSED
    assert "cannot run as written" in report.state.finding
    assert "no longer exists" in report.state.finding
