# tests/automations/test_commissioning_on_creation.py
"""Creating an automation rehearses it -- #183.

The shape under test is the awkward part: the rehearsal is itself a turn, and it is asked for
from inside the turn that created the automation. So it cannot be awaited there, and the
message carries defer-until-idle exactly as a scheduled run does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.cron import CronTool
from nanoinfra.automations.commissioning import PreviewedAction, bind_commissioning
from nanoinfra.automations.commissioning_state import OK, REFUSED
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.cron.service import CronService
from nanoinfra.cron.session_turns import CRON_DEFER_UNTIL_IDLE_META


def _service(tmp_path: Path) -> CronService:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service._running = True
    return service


async def _add_job(tool: CronTool) -> str:
    with request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            session_key="websocket:chat-1",
            metadata={"webui": True},
        )
    ):
        return tool._add_job("uptime watch", "Report the uptime", 300, None, None, None)


@pytest.mark.asyncio
async def test_creating_a_job_asks_for_a_rehearsal_without_waiting_on_it(tmp_path: Path) -> None:
    asked: list[str] = []
    tool = CronTool(_service(tmp_path), commission=asked.append)

    result = await _add_job(tool)

    assert "Created job" in result
    assert "commissioning run will rehearse it" in result
    assert len(asked) == 1


@pytest.mark.asyncio
async def test_a_job_created_from_inside_a_cron_turn_is_not_rehearsed(tmp_path: Path) -> None:
    """Otherwise an automation that writes an automation starts a chain nobody asked for."""
    asked: list[str] = []
    tool = CronTool(_service(tmp_path), commission=asked.append)
    token = tool.set_cron_context(True)
    try:
        result = await _add_job(tool)
    finally:
        tool.reset_cron_context(token)

    assert "Created job" in result
    assert "commissioning" not in result
    assert asked == []


class _Loop:
    """The two halves of AgentLoop the commissioning path uses."""

    def __init__(self, service: CronService, action: PreviewedAction | None) -> None:
        from nanoinfra.agent.loop import AgentLoop

        self.cron_service = service
        self.gate = None
        self.workspace = "/tmp"
        self.action = action
        self.published: list[OutboundMessage] = []
        self.turns: list[InboundMessage] = []
        self.background: list[Any] = []
        # Bind the real methods to this stand-in: the logic under test is theirs.
        self._commission_automation = AgentLoop._commission_automation.__get__(self)
        self._deliver_commissioning_finding = (
            AgentLoop._deliver_commissioning_finding.__get__(self)
        )
        self.commission_automation_later = AgentLoop.commission_automation_later.__get__(self)

    def schedule_background(self, coro: Any) -> None:
        self.background.append(asyncio.ensure_future(coro))

    @property
    def bus(self) -> Any:
        loop = self

        class _Bus:
            async def publish_outbound(self, msg: OutboundMessage) -> None:
                loop.published.append(msg)

        return _Bus()

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        self.turns.append(msg)

        async def _turn() -> None:
            with bind_commissioning(msg.metadata):
                from nanoinfra.automations.commissioning import current_commissioning

                collector = current_commissioning()
                assert collector is not None
                if self.action is not None:
                    collector.record(self.action)

        await asyncio.create_task(_turn())
        return OutboundMessage(channel="websocket", chat_id="chat-1", content="done")


def _refused() -> PreviewedAction:
    return PreviewedAction(
        tool="execute_on_server",
        capability_class="mutate.remote",
        outcome="deny",
        reason="no standing grant covers it.",
        scope="host",
        hosts=("10.0.0.9",),
        command="uptime",
    )


@pytest.mark.asyncio
async def test_a_refused_rehearsal_disables_the_job_and_reports_the_grant(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _Loop(service, _refused())
    tool = CronTool(service, commission=loop.commission_automation_later)

    await _add_job(tool)
    await asyncio.gather(*loop.background)

    job = service.get_job(service.list_jobs(include_disabled=True)[0].id)
    assert job is not None
    assert job.enabled is False
    assert job.commissioning.status == REFUSED

    [message] = loop.published
    assert "saved but disabled" in message.content
    assert '"commands": [\n      "uptime"\n    ]' in message.content
    # The sentence that keeps an operator from believing a grant is scoped to one automation.
    assert "in any unattended turn" in message.content
    # The rehearsal waited for the creating turn to go idle, like a scheduled run does.
    assert loop.turns[0].metadata[CRON_DEFER_UNTIL_IDLE_META] is True


@pytest.mark.asyncio
async def test_a_clean_rehearsal_leaves_the_job_running(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _Loop(service, None)
    tool = CronTool(service, commission=loop.commission_automation_later)

    await _add_job(tool)
    await asyncio.gather(*loop.background)

    job = service.list_jobs(include_disabled=True)[0]
    assert job.enabled is True
    assert job.commissioning.status == OK
    [message] = loop.published
    assert "rehearsed clean" in message.content
