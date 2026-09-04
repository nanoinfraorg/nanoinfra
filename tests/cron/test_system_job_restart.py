"""A system job survives a restart with what it already knew (#263).

`register_system_job` runs on every gateway start. It used to replace `state` wholesale, and the
consequences were all user-visible on the Automations page:

- **The next run was pushed a full interval on every boot**, so a gateway restarted more often
  than every two hours never ran Dream at all. The symptom in the field was a workspace whose
  memory cursor was two weeks old under a job that read "every 2 hours".
- The run history was erased, so a job that had run for months read "no runs recorded yet" — and
  the one piece of evidence an operator would use to check was the thing being destroyed.
- `created_at_ms` moved to now, so a long-standing job claimed to have been created minutes ago.
"""

from __future__ import annotations

from pathlib import Path

from nanoinfra.cron.service import CronService
from nanoinfra.cron.types import CronJob, CronPayload, CronRunRecord, CronSchedule

_TWO_HOURLY = CronSchedule(kind="every", every_ms=2 * 60 * 60 * 1000)


def _job(schedule: CronSchedule = _TWO_HOURLY) -> CronJob:
    return CronJob(
        id="dream", name="dream", schedule=schedule, payload=CronPayload(kind="system_event")
    )


def _service(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "cron" / "jobs.json")


def _persist(service: CronService, **state: object) -> None:
    """Mutate the stored job's state and write it, the way a real run does.

    Mutating the object `register_system_job` returned is not enough: the store is re-read from
    disk on the next call, so an in-memory edit is invisible to the very code under test — and a
    test that "passes" against a value nothing persisted proves nothing.
    """
    job = next(j for j in service._require_store().jobs if j.id == "dream")
    for key, value in state.items():
        setattr(job.state, key, value)
    service._save_store()


def test_a_restart_does_not_push_the_next_run_another_interval(tmp_path: Path) -> None:
    """The bug, stated as the thing that broke: two restarts inside one interval used to mean the
    job never came due, and a machine restarted hourly never dreamed."""
    service = _service(tmp_path)
    first = service.register_system_job(_job())
    scheduled = first.state.next_run_at_ms

    again = service.register_system_job(_job())

    assert again.state.next_run_at_ms == scheduled


def test_a_missed_slot_stays_due_rather_than_being_skipped(tmp_path: Path) -> None:
    """A next run in the past means the process was down when it came due. Keeping it in the past
    is what makes the timer fire it; recomputing would silently skip the slot."""
    service = _service(tmp_path)
    service.register_system_job(_job())
    _persist(service, next_run_at_ms=1_000)  # long overdue

    again = service.register_system_job(_job())

    assert again.state.next_run_at_ms == 1_000


def test_the_run_history_survives_a_restart(tmp_path: Path) -> None:
    """It was the evidence an operator would use to answer "has this ever run", and it was being
    erased by the act of starting the gateway that would answer."""
    service = _service(tmp_path)
    service.register_system_job(_job())
    _persist(
        service,
        last_run_at_ms=42,
        last_status="ok",
        run_history=[CronRunRecord(run_at_ms=42, status="ok")],
    )

    again = service.register_system_job(_job())

    assert again.state.last_run_at_ms == 42
    assert again.state.last_status == "ok"
    assert len(again.state.run_history) == 1


def test_the_creation_date_is_when_the_job_first_appeared(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.register_system_job(_job()).created_at_ms

    again = service.register_system_job(_job())

    assert again.created_at_ms == created
    # And the update stamp does move, because re-registering is a real event.
    assert again.updated_at_ms >= created


def test_a_changed_schedule_is_recomputed(tmp_path: Path) -> None:
    """The one case where the stored slot is wrong: a release that changes an interval must not
    keep scheduling the old one."""
    service = _service(tmp_path)
    first = service.register_system_job(_job())

    faster = service.register_system_job(
        _job(CronSchedule(kind="every", every_ms=15 * 60 * 1000))
    )

    assert faster.state.next_run_at_ms != first.state.next_run_at_ms


def test_a_pending_retry_survives_a_restart(tmp_path: Path) -> None:
    """`retry_pending` exists so a recompute cannot drop a retry. A wholesale state replacement
    dropped it on every boot, which is the case that flag was added to prevent."""
    service = _service(tmp_path)
    service.register_system_job(_job())
    _persist(service, retry_pending=True, retry_attempts=2)

    again = service.register_system_job(_job())

    assert again.state.retry_pending is True
    assert again.state.retry_attempts == 2


def test_registering_still_never_duplicates_the_row(tmp_path: Path) -> None:
    """The property the old docstring claimed, kept."""
    service = _service(tmp_path)
    service.register_system_job(_job())
    service.register_system_job(_job())

    store = service._require_store()
    assert [j.id for j in store.jobs].count("dream") == 1
