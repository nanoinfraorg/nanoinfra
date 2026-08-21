"""A failed cron run is retried when the job asks for it.

Before this, ``_execute_job`` caught the failure, recorded ``error`` and computed the next
scheduled slot, so a job that failed because a host was rebooting simply did not happen and the
operator found out from history (nanoinfraorg/nanoinfra#157).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.cron.service import CronService
from nanoinfra.cron.types import CronJob, CronRetryPolicy, CronSchedule


def _service(
    tmp_path: Path,
    *,
    retry: CronRetryPolicy | None = None,
    schedule: CronSchedule | None = None,
) -> tuple[CronService, CronJob]:
    service = CronService(tmp_path / "cron" / "jobs.json")
    job = service.add_job(
        name="job",
        schedule=schedule or CronSchedule(kind="cron", expr="0 9 * * *", tz="UTC"),
        message="hello",
        retry=retry,
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
    )
    service._running = True
    service._load_store()
    return service, job


def _stored(service: CronService, job_id: str) -> CronJob:
    found = service.get_job(job_id)
    assert found is not None
    return found


async def test_a_failure_without_a_policy_still_waits_for_the_next_slot(tmp_path: Path) -> None:
    """The default is off, so upgrading changes nothing about an existing job."""
    service, job = _service(tmp_path)

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    assert stored.state.last_status == "error"
    assert stored.state.retry_pending is False
    assert stored.state.retry_attempts == 0


async def test_a_failure_schedules_a_retry(tmp_path: Path) -> None:
    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=3, base_delay_ms=1_000, max_delay_ms=60_000),
    )

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    assert stored.state.last_status == "error"
    assert stored.state.retry_attempts == 1
    assert stored.state.retry_pending is True
    # The retry displaces the scheduled slot rather than sitting beside it.
    assert stored.state.next_run_at_ms is not None


async def test_retries_are_spent_then_the_job_waits_for_its_slot(tmp_path: Path) -> None:
    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=2, base_delay_ms=1_000, max_delay_ms=60_000),
    )

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    for expected in (1, 2):
        await service.run_job(job.id, force=True)
        assert _stored(service, job.id).state.retry_attempts == expected
        assert _stored(service, job.id).state.retry_pending is True

    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    assert stored.state.retry_attempts == 0
    assert stored.state.retry_pending is False
    # Every attempt is its own record: three failures, not one with a counter.
    assert [record.status for record in stored.state.run_history] == ["error"] * 3


async def test_a_success_ends_the_outage(tmp_path: Path) -> None:
    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=3, base_delay_ms=1_000, max_delay_ms=60_000),
    )
    outcomes = iter([RuntimeError("host unreachable"), None])

    async def _flaky(_job: CronJob) -> None:
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    service.on_job = _flaky
    await service.run_job(job.id, force=True)
    assert _stored(service, job.id).state.retry_attempts == 1

    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    assert stored.state.last_status == "ok"
    assert stored.state.retry_attempts == 0
    assert stored.state.retry_pending is False


async def test_a_skipped_run_does_not_consume_an_attempt(tmp_path: Path) -> None:
    """Something declined to run it. That is not the job failing."""
    from nanoinfra.cron.service import CronJobSkippedError

    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=3, base_delay_ms=1_000, max_delay_ms=60_000),
    )

    async def _skip(_job: CronJob) -> None:
        raise CronJobSkippedError("channel disabled")

    service.on_job = _skip
    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    assert stored.state.last_status == "skipped"
    assert stored.state.retry_attempts == 0
    assert stored.state.retry_pending is False


async def test_a_one_shot_with_retries_left_is_not_disabled(tmp_path: Path) -> None:
    """A one-shot that failed on its only slot would otherwise be switched off unrun."""
    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=2, base_delay_ms=1_000, max_delay_ms=60_000),
        schedule=CronSchedule(kind="at", at_ms=1),
    )

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    assert stored.enabled is True
    assert stored.state.retry_pending is True
    assert stored.state.next_run_at_ms is not None


async def test_a_restart_does_not_drop_a_pending_retry(tmp_path: Path) -> None:
    """_recompute_next_runs would otherwise overwrite the retry with the next scheduled slot."""
    store_path = tmp_path / "cron" / "jobs.json"
    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=3, base_delay_ms=600_000, max_delay_ms=600_000),
    )

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    await service.run_job(job.id, force=True)
    retry_at = _stored(service, job.id).state.next_run_at_ms
    assert retry_at is not None

    restarted = CronService(store_path)
    restarted._load_store()
    restarted._recompute_next_runs()

    stored = _stored(restarted, job.id)
    assert stored.state.retry_pending is True
    assert stored.state.next_run_at_ms == retry_at


async def test_the_policy_and_retry_state_survive_a_reload(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=4, base_delay_ms=1_500, max_delay_ms=90_000),
    )

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    await service.run_job(job.id, force=True)

    raw = json.loads(store_path.read_text(encoding="utf-8"))
    assert raw["jobs"][0]["retry"] == {
        "attempts": 4,
        "baseDelayMs": 1_500,
        "maxDelayMs": 90_000,
    }
    assert raw["jobs"][0]["state"]["retryAttempts"] == 1
    assert raw["jobs"][0]["state"]["retryPending"] is True

    reloaded = CronService(store_path)
    reloaded._load_store()
    stored = _stored(reloaded, job.id)
    assert stored.retry == CronRetryPolicy(attempts=4, base_delay_ms=1_500, max_delay_ms=90_000)
    assert stored.state.retry_attempts == 1


def test_a_job_written_before_retries_existed_loads_with_the_policy_off(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "legacy",
                        "name": "legacy job",
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "hello",
                            "sessionKey": "websocket:chat-1",
                            "originChannel": "websocket",
                            "originChatId": "chat-1",
                        },
                        "state": {"lastStatus": "error"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)
    service._load_store()

    stored = _stored(service, "legacy")
    assert stored.retry.enabled is False
    assert stored.state.retry_attempts == 0
    assert stored.state.retry_pending is False


@pytest.mark.parametrize("attempts", [0, -1])
def test_a_non_positive_attempt_count_disables_retrying(attempts: int) -> None:
    assert CronRetryPolicy(attempts=attempts).enabled is False


async def test_a_forced_run_is_distinguishable_from_a_scheduled_one(tmp_path: Path) -> None:
    """"Why did this daily 09:00 job run at 14:07" had no answer before (#163)."""
    service, job = _service(tmp_path)

    async def _ok(_job: CronJob) -> None:
        return None

    service.on_job = _ok
    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    assert [record.reason for record in stored.state.run_history] == ["manual"]


async def test_a_retry_is_recorded_as_a_retry_not_a_manual_run(tmp_path: Path) -> None:
    service, job = _service(
        tmp_path,
        retry=CronRetryPolicy(attempts=2, base_delay_ms=1_000, max_delay_ms=60_000),
    )

    async def _boom(_job: CronJob) -> None:
        raise RuntimeError("host unreachable")

    service.on_job = _boom
    await service.run_job(job.id, force=True)
    await service.run_job(job.id, force=True)

    stored = _stored(service, job.id)
    # The first run was the operator's; the second happened because a retry was pending.
    assert [record.reason for record in stored.state.run_history] == ["manual", "retry"]


async def test_the_reason_survives_a_reload(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service, job = _service(tmp_path)

    async def _ok(_job: CronJob) -> None:
        return None

    service.on_job = _ok
    await service.run_job(job.id, force=True)

    reloaded = CronService(store_path)
    reloaded._load_store()
    stored = _stored(reloaded, job.id)

    assert [record.reason for record in stored.state.run_history] == ["manual"]


def test_a_record_written_before_reasons_existed_reads_as_scheduled(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "legacy",
                        "name": "legacy",
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "hello",
                            "sessionKey": "websocket:chat-1",
                            "originChannel": "websocket",
                            "originChatId": "chat-1",
                        },
                        "state": {
                            "runHistory": [
                                {"runAtMs": 1, "status": "ok", "durationMs": 5},
                            ]
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)
    service._load_store()
    stored = _stored(service, "legacy")

    assert [record.reason for record in stored.state.run_history] == ["scheduled"]
