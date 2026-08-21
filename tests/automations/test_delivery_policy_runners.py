"""The delivery policy at the two seams that actually publish.

Both automation runners get an ``OutboundMessage | None`` back from the turn and, before this,
the turn machinery had already published it. Withholding is opt-in per run, so a job on the
default policy still travels through exactly the code it travelled through before
(nanoinfraorg/nanoinfra#159).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.turn_delivery import AUTOMATION_WITHHOLD_DELIVERY_META
from nanoinfra.automations.state import AutomationDeliveryLog, response_fingerprint
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.cron.bound_runner import run_bound_cron_job
from nanoinfra.cron.types import CronJob, CronPayload, CronSchedule
from nanoinfra.triggers.local_runner import run_local_trigger_queue
from nanoinfra.triggers.local_store import LocalTriggerStore
from nanoinfra.utils.backoff import BackoffPolicy


class _Recorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        self.records.append(record)


class _Agent:
    """Stands in for the agent loop: captures the inbound turn and answers with fixed content."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.seen: list[InboundMessage] = []
        self.tools = _NoTools()

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        self.seen.append(msg)
        content = self.answers.pop(0) if self.answers else ""
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)


class _NoTools:
    def get(self, _name: str) -> None:
        return None


def _job(delivery: str) -> CronJob:
    return CronJob(
        id="job-a",
        name="Blockers",
        schedule=CronSchedule(kind="cron", expr="0 9 * * 1", tz="UTC"),
        payload=CronPayload(
            kind="agent_turn",
            message="Report new blockers",
            session_key="websocket:chat-1",
            origin_channel="websocket",
            origin_chat_id="chat-1",
        ),
        delivery=delivery,
    )


# --- cron ---


async def test_a_default_job_is_not_withheld(tmp_path: Path) -> None:
    """The existing path stays untouched: no flag, so the turn publishes as it always did."""
    agent = _Agent(["2 blockers"])
    published: list[OutboundMessage] = []

    await run_bound_cron_job(
        _job("always"),
        agent=agent,  # type: ignore[arg-type]
        cron=_Recorder(),  # type: ignore[arg-type]
        delivery_log=AutomationDeliveryLog(tmp_path),
        publish=lambda msg: _append(published, msg),
    )

    assert AUTOMATION_WITHHOLD_DELIVERY_META not in agent.seen[0].metadata
    assert published == []


async def test_never_withholds_and_publishes_nothing(tmp_path: Path) -> None:
    agent = _Agent(["2 blockers"])
    published: list[OutboundMessage] = []

    await run_bound_cron_job(
        _job("never"),
        agent=agent,  # type: ignore[arg-type]
        cron=_Recorder(),  # type: ignore[arg-type]
        delivery_log=AutomationDeliveryLog(tmp_path),
        publish=lambda msg: _append(published, msg),
    )

    assert agent.seen[0].metadata[AUTOMATION_WITHHOLD_DELIVERY_META] is True
    assert published == []


async def test_on_change_publishes_once_then_stays_quiet(tmp_path: Path) -> None:
    agent = _Agent(["2 blockers", "2 blockers", "3 blockers"])
    published: list[OutboundMessage] = []
    log = AutomationDeliveryLog(tmp_path)
    job = _job("on-change")

    for _ in range(3):
        await run_bound_cron_job(
            job,
            agent=agent,  # type: ignore[arg-type]
            cron=_Recorder(),  # type: ignore[arg-type]
            delivery_log=log,
            publish=lambda msg: _append(published, msg),
        )

    assert [msg.content for msg in published] == ["2 blockers", "3 blockers"]
    assert log.last_fingerprint("job-a") == response_fingerprint("3 blockers")


async def test_a_failed_publish_does_not_teach_on_change(tmp_path: Path) -> None:
    """Recording before the send would mean the operator was never told and the policy thinks
    they were."""
    agent = _Agent(["2 blockers"])
    log = AutomationDeliveryLog(tmp_path)

    async def _explode(_msg: OutboundMessage) -> None:
        raise RuntimeError("channel down")

    with pytest.raises(RuntimeError):
        await run_bound_cron_job(
            _job("on-change"),
            agent=agent,  # type: ignore[arg-type]
            cron=_Recorder(),  # type: ignore[arg-type]
            delivery_log=log,
            publish=_explode,
        )

    assert log.last_fingerprint("job-a") is None


async def test_withholding_needs_somewhere_to_publish(tmp_path: Path) -> None:
    """Without a publisher the runner cannot take over, so it must not withhold and drop the
    answer on the floor."""
    agent = _Agent(["2 blockers"])

    await run_bound_cron_job(
        _job("never"),
        agent=agent,  # type: ignore[arg-type]
        cron=_Recorder(),  # type: ignore[arg-type]
        delivery_log=AutomationDeliveryLog(tmp_path),
        publish=None,
    )

    assert AUTOMATION_WITHHOLD_DELIVERY_META not in agent.seen[0].metadata


# --- triggers ---


async def test_a_trigger_honours_its_delivery_policy(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path, backoff=BackoffPolicy(base_delay_ms=0, max_delay_ms=0))
    trigger = store.create(
        name="CI review",
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )
    assert store.update(trigger.id, delivery="never") is not None
    store.enqueue(trigger.id, "CI failed")

    seen: list[InboundMessage] = []
    published: list[OutboundMessage] = []

    async def _submit(msg: InboundMessage) -> OutboundMessage | None:
        seen.append(msg)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="looked at it")

    await _drain_once(
        store,
        submit_turn=_submit,
        publish=lambda msg: _append(published, msg),
        delivery_log=AutomationDeliveryLog(tmp_path),
    )

    assert seen and seen[0].metadata[AUTOMATION_WITHHOLD_DELIVERY_META] is True
    assert published == []


async def test_a_default_trigger_is_not_withheld(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path, backoff=BackoffPolicy(base_delay_ms=0, max_delay_ms=0))
    trigger = store.create(
        name="CI review",
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )
    store.enqueue(trigger.id, "CI failed")

    seen: list[InboundMessage] = []

    async def _submit(msg: InboundMessage) -> OutboundMessage | None:
        seen.append(msg)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="looked at it")

    await _drain_once(
        store,
        submit_turn=_submit,
        publish=lambda msg: _append([], msg),
        delivery_log=AutomationDeliveryLog(tmp_path),
    )

    assert seen and AUTOMATION_WITHHOLD_DELIVERY_META not in seen[0].metadata


# --- helpers ---


async def _append(sink: list[OutboundMessage], msg: OutboundMessage) -> None:
    sink.append(msg)


async def _drain_once(
    store: LocalTriggerStore,
    *,
    submit_turn: Any,
    publish: Any,
    delivery_log: AutomationDeliveryLog,
) -> None:
    """Run the queue until it has handled the pending delivery, then stop."""
    import asyncio

    task = asyncio.create_task(
        run_local_trigger_queue(
            store=store,
            submit_turn=submit_turn,
            is_channel_enabled=lambda _name: True,
            poll_interval_s=0.01,
            delivery_log=delivery_log,
            publish=publish,
        )
    )
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not list(store.inbox_dir.glob("*.json")) and not list(
                store.processing_dir.glob("*.json")
            ):
                return
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
