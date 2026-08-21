"""An automation carries a resolved reference instead of a name to re-resolve.

This is the half of resource mentions that makes them worth building. In a chat turn a name search
is cheap and self-correcting -- you watch the agent pick and you correct it. In an automation it is
neither: the match is re-done on every unattended run, so a rename or a closer-matching host
silently changes what it touches (nanoinfraorg/nanoinfra#169).

The failure shape is settled: if a datum is wrong, the process does not execute. Resolution happens
before the turn is built, and an unresolvable reference is terminal -- it bypasses the retry policy,
because a deleted server fails identically on every attempt and burning ten backed-off attempts to
re-learn that only delays the one notification the operator needs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.cron.bound_runner import run_bound_cron_job
from nanoinfra.cron.service import CronJobTerminalError, CronService
from nanoinfra.cron.types import CronJob, CronPayload, CronRetryPolicy, CronSchedule
from nanoinfra.runtime_context import RUNTIME_CONTEXT_INPUT_META
from nanoinfra.servers.store import ServerStore
from nanoinfra.triggers.local_store import LocalTriggerStore


class _Recorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        self.records.append(record)


class _NoTools:
    def get(self, _name: str) -> None:
        return None


class _Agent:
    def __init__(self) -> None:
        self.seen: list[InboundMessage] = []
        self.tools = _NoTools()

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        self.seen.append(msg)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="done")


def _server(tmp_path: Path, name: str = "db-01") -> str:
    return ServerStore(tmp_path).create(
        {"name": name, "providerId": "ssh", "host": "10.0.0.5", "username": "ops"}
    ).id


def _job(references: list[dict[str, str]]) -> CronJob:
    return CronJob(
        id="job-a",
        name="Nightly check",
        schedule=CronSchedule(kind="cron", expr="0 3 * * *", tz="UTC"),
        payload=CronPayload(
            kind="agent_turn",
            message="Check the server and report",
            session_key="websocket:chat-1",
            origin_channel="websocket",
            origin_chat_id="chat-1",
        ),
        references=references,
    )


# --- persistence ---


def test_references_survive_a_reload(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    job = service.add_job(
        name="Nightly check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the server",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        references=[{"kind": "server", "id": "srv_abc"}],
    )

    reloaded = CronService(store_path)
    reloaded._load_store()
    stored = reloaded.get_job(job.id)

    assert stored is not None
    assert stored.references == [{"kind": "server", "id": "srv_abc"}]


def test_a_job_written_before_references_existed_has_none(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps({
            "version": 1,
            "jobs": [{
                "id": "legacy",
                "name": "legacy",
                "enabled": True,
                "schedule": {"kind": "every", "everyMs": 3_600_000},
                "payload": {
                    "kind": "agent_turn",
                    "message": "hello",
                    "sessionKey": "websocket:chat-1",
                    "originChannel": "websocket",
                    "originChatId": "chat-1",
                },
            }],
        }),
        encoding="utf-8",
    )

    service = CronService(store_path)
    service._load_store()
    stored = service.get_job("legacy")

    assert stored is not None
    assert stored.references == []


def test_a_malformed_stored_reference_is_dropped(tmp_path: Path) -> None:
    from nanoinfra.cron.types import CronJob as Job

    job = Job.from_store_dict({
        "id": "j",
        "name": "j",
        "schedule": {"kind": "every", "everyMs": 1000},
        "references": ["server:abc", {"kind": "server"}, {"id": "abc"}, {"kind": "s", "id": "i"}],
    })

    assert job.references == [{"kind": "s", "id": "i"}]


def test_a_trigger_reference_round_trips(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = store.create(
        name="CI review",
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )

    assert store.update(trigger.id, references=[{"kind": "server", "id": "srv_abc"}]) is not None
    reloaded = LocalTriggerStore(tmp_path).get(trigger.id)

    assert reloaded is not None
    assert reloaded.references == [{"kind": "server", "id": "srv_abc"}]


# --- resolution before the turn ---


async def test_a_resolved_reference_reaches_the_turn(tmp_path: Path) -> None:
    server_id = _server(tmp_path, "db-01")
    agent = _Agent()

    await run_bound_cron_job(
        _job([{"kind": "server", "id": server_id}]),
        agent=agent,  # type: ignore[arg-type]
        cron=_Recorder(),  # type: ignore[arg-type]
        workspace_path=tmp_path,
    )

    blocks = agent.seen[0].metadata[RUNTIME_CONTEXT_INPUT_META]
    assert len(blocks) == 1
    assert "db-01" in blocks[0].content
    assert server_id in blocks[0].content
    # The reference, not the record.
    assert "10.0.0.5" not in blocks[0].content


async def test_a_renamed_server_still_resolves(tmp_path: Path) -> None:
    """The point of storing an id."""
    store = ServerStore(tmp_path)
    server_id = _server(tmp_path, "db-01")
    store.update(
        server_id,
        {"name": "db-primary", "providerId": "ssh", "host": "10.0.0.5", "username": "ops"},
    )
    agent = _Agent()

    await run_bound_cron_job(
        _job([{"kind": "server", "id": server_id}]),
        agent=agent,  # type: ignore[arg-type]
        cron=_Recorder(),  # type: ignore[arg-type]
        workspace_path=tmp_path,
    )

    blocks = agent.seen[0].metadata[RUNTIME_CONTEXT_INPUT_META]
    assert "db-primary" in blocks[0].content


async def test_a_deleted_reference_stops_the_run_before_the_turn(tmp_path: Path) -> None:
    """The model never sees a partially resolved context and never gets to improvise."""
    agent = _Agent()

    with pytest.raises(CronJobTerminalError) as excinfo:
        await run_bound_cron_job(
            _job([{"kind": "server", "id": "srv_deleted"}]),
            agent=agent,  # type: ignore[arg-type]
            cron=_Recorder(),  # type: ignore[arg-type]
            workspace_path=tmp_path,
        )

    assert "srv_deleted" in str(excinfo.value)
    assert agent.seen == []


async def test_a_job_with_no_references_is_unchanged(tmp_path: Path) -> None:
    """Nothing about an existing automation changes."""
    agent = _Agent()

    await run_bound_cron_job(
        _job([]),
        agent=agent,  # type: ignore[arg-type]
        cron=_Recorder(),  # type: ignore[arg-type]
        workspace_path=tmp_path,
    )

    assert RUNTIME_CONTEXT_INPUT_META not in agent.seen[0].metadata


# --- terminal, not retried ---


async def test_a_stale_reference_does_not_burn_the_retry_budget(tmp_path: Path) -> None:
    """A deleted server fails identically every attempt, and retrying delays the notification."""
    service = CronService(tmp_path / "cron" / "jobs.json")
    job = service.add_job(
        name="Nightly check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the server",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        retry=CronRetryPolicy(attempts=3, base_delay_ms=1_000, max_delay_ms=60_000),
    )
    service._running = True
    service._load_store()

    async def _terminal(_job: CronJob) -> None:
        raise CronJobTerminalError("referenced resource no longer exists: server:srv_deleted")

    service.on_job = _terminal
    await service.run_job(job.id, force=True)

    stored = service.get_job(job.id)
    assert stored is not None
    assert stored.state.last_status == "error"
    assert stored.state.retry_attempts == 0
    assert stored.state.retry_pending is False


async def test_an_ordinary_failure_still_retries(tmp_path: Path) -> None:
    """The terminal path must not disable retrying for everything else."""
    service = CronService(tmp_path / "cron" / "jobs.json")
    job = service.add_job(
        name="Nightly check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the server",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        retry=CronRetryPolicy(attempts=3, base_delay_ms=1_000, max_delay_ms=60_000),
    )
    service._running = True
    service._load_store()

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    await service.run_job(job.id, force=True)

    stored = service.get_job(job.id)
    assert stored is not None
    assert stored.state.retry_attempts == 1
    assert stored.state.retry_pending is True
