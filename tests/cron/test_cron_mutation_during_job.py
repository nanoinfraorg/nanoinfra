"""Editing a cron job must not cancel the turn that is running one (upstream PR 5686).

``tick()`` awaits through ``_on_timer()`` -> ``_execute_job()`` -> ``on_job(job)``, so the timer
task *is* the task running the agent turn. Every mutator re-arms the timer, and ``_arm_timer``
cancelled ``self._timer_task`` unconditionally -- so the agent's own cron tool, an operator
toggling any automation in the WebUI, and commissioning writing its verdict each killed the turn
that was running, mid-side-effect, with nothing persisted and the job left to replay.

The third test is the other half of the guard: skipping the cancel must not skip the *arming*, or
one mutation during one job would end the schedule for good, which is worse than the bug.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nanoinfra.cron.service import CronService
from nanoinfra.cron.types import CronJob, CronSchedule

_BINDING = {
    "session_key": "websocket:chat-1",
    "origin_channel": "websocket",
    "origin_chat_id": "chat-1",
}


def _service(tmp_path: Path, *, max_sleep_ms: int = 300_000) -> tuple[CronService, str]:
    service = CronService(tmp_path / "cron" / "jobs.json", max_sleep_ms=max_sleep_ms)
    job = service.add_job(
        name="job",
        schedule=CronSchedule(kind="cron", expr="* * * * *", tz="UTC"),
        message="hello",
        **_BINDING,
    )
    service._running = True
    service._load_store()
    return service, job.id


def _due_now(service: CronService, job_id: str) -> None:
    """Make the job due, on disk as well as in memory.

    A tick reloads the store before it picks due jobs, so an in-memory due time alone never
    reaches ``_execute_job``.
    """
    store = service._store
    assert store is not None
    for job in store.jobs:
        if job.id == job_id:
            job.state.next_run_at_ms = 1
    service._save_store()


def _stored_status(service: CronService, job_id: str) -> str:
    """Read the job back through the store, which is where a lost turn shows up."""
    job = service.get_job(job_id)
    assert job is not None
    return job.state.last_status


async def test_a_mutation_from_inside_the_turn_does_not_cancel_it(tmp_path: Path) -> None:
    """The agent's own cron tool, called from the turn the timer task is running."""
    service, job_id = _service(tmp_path)
    _due_now(service, job_id)
    outcome: list[str] = []
    turn_over = asyncio.Event()

    async def on_job(_job: CronJob) -> None:
        service.add_job(
            name="added mid-turn",
            schedule=CronSchedule(kind="cron", expr="*/5 * * * *", tz="UTC"),
            message="from the tool",
            **_BINDING,
        )
        try:
            # Any await is where a cancel requested by the mutator above lands.
            await asyncio.sleep(0)
            outcome.append("finished")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        finally:
            turn_over.set()

    service.on_job = on_job
    service._arm_timer()

    await asyncio.wait_for(turn_over.wait(), 5)

    assert outcome == ["finished"], "adding a job mid-turn cancelled the turn that added it"
    assert _stored_status(service, job_id) == "ok"

    service.stop()


async def test_a_mutation_from_another_task_does_not_cancel_the_turn(tmp_path: Path) -> None:
    """An operator toggling a different automation in the WebUI while a job runs."""
    service, job_id = _service(tmp_path)
    other = service.add_job(
        name="other automation",
        schedule=CronSchedule(kind="cron", expr="0 3 * * *", tz="UTC"),
        message="nightly",
        **_BINDING,
    )
    service.enable_job(other.id, False)
    _due_now(service, job_id)
    outcome: list[str] = []
    turn_started = asyncio.Event()
    turn_over = asyncio.Event()
    release = asyncio.Event()

    async def on_job(_job: CronJob) -> None:
        turn_started.set()
        try:
            await asyncio.wait_for(release.wait(), 5)
            outcome.append("finished")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        finally:
            turn_over.set()

    service.on_job = on_job
    service._arm_timer()
    await asyncio.wait_for(turn_started.wait(), 5)

    service.enable_job(other.id, True)
    release.set()
    await asyncio.wait_for(turn_over.wait(), 5)

    assert outcome == ["finished"], "enabling another job cancelled the running turn"
    assert _stored_status(service, job_id) == "ok"

    service.stop()


async def test_a_mutation_during_a_job_leaves_the_timer_armed(tmp_path: Path) -> None:
    """The schedule has to survive the guard: a later run still fires on its own.

    ``max_sleep_ms`` is short so the timer the tick re-arms is observable inside a test rather
    than a minute away.
    """
    service, job_id = _service(tmp_path, max_sleep_ms=50)
    _due_now(service, job_id)
    runs: list[str] = []
    first_run_over = asyncio.Event()
    second_run = asyncio.Event()

    async def on_job(_job: CronJob) -> None:
        runs.append("run")
        if len(runs) == 1:
            service.add_job(
                name="added mid-turn",
                schedule=CronSchedule(kind="cron", expr="*/5 * * * *", tz="UTC"),
                message="from the tool",
                **_BINDING,
            )
            first_run_over.set()
            return
        second_run.set()

    service.on_job = on_job
    service._arm_timer()
    tick = service._timer_task
    assert tick is not None

    await asyncio.wait_for(first_run_over.wait(), 5)
    await asyncio.wait([tick], timeout=5)

    # A fresh task: the tick that ran the job has finished, and re-armed before it did.
    assert service._timer_task is not None
    assert service._timer_task is not tick
    assert not service._timer_task.done()

    # And it really is a working schedule, not just a live task.
    _due_now(service, job_id)
    await asyncio.wait_for(second_run.wait(), 5)

    service.stop()
