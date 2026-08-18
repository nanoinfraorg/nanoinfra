"""A single bad tick must not stop the scheduler, and a failed save must not replay a job.

Two failure modes, both ported after verifying they exist here (nanoinfraorg/nanoinfra#145,
upstream 8bdf5ed2 and ecef2b05):

- ``_arm_timer`` used to run after the ``try``, so an exception anywhere in a tick skipped it and
  every future job stopped, silently, because the tick runs inside a task nobody awaits.
- A ``_save_store`` failure left no marker, so the next ``_load_store`` replaced the in-memory
  snapshot with the older disk one. A job that had already run became due again and repeated its
  side effect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.cron.service import CronService
from nanoinfra.cron.types import CronSchedule


def _service(tmp_path: Path) -> CronService:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service.add_job(
        name="job",
        schedule=CronSchedule(kind="cron", expr="* * * * *", tz="UTC"),
        message="hello",
    )
    service._running = True
    service._load_store()
    return service


def _due_now(service: CronService) -> None:
    store = service._store
    assert store is not None
    for job in store.jobs:
        job.state.next_run_at_ms = 1


async def test_a_failing_job_still_rearms_the_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One exploding job must not take every future job with it."""
    service = _service(tmp_path)
    _due_now(service)
    armed: list[int] = []
    monkeypatch.setattr(service, "_arm_timer", lambda: armed.append(1))

    async def boom(_job: object) -> None:
        raise RuntimeError("job blew up")

    monkeypatch.setattr(service, "_execute_job", boom)

    await service._on_timer()

    assert armed, "the timer must be re-armed even when a tick fails"


async def test_a_failing_save_still_rearms_the_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    armed: list[int] = []
    monkeypatch.setattr(service, "_arm_timer", lambda: armed.append(1))

    def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(service, "_save_store", boom)

    await service._on_timer()

    assert armed


async def test_a_failing_load_still_rearms_the_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    armed: list[int] = []
    monkeypatch.setattr(service, "_arm_timer", lambda: armed.append(1))

    def boom(**_kwargs: object) -> None:
        raise OSError("unreadable")

    monkeypatch.setattr(service, "_load_store", boom)

    await service._on_timer()

    assert armed


async def test_an_unpersisted_snapshot_is_not_replaced_by_the_disk_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay guard: in-memory state stays authoritative until a save succeeds."""
    service = _service(tmp_path)

    def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(service, "_save_store", boom)
    with pytest.raises(OSError):
        service._save_store()
    service._store_dirty = True

    live = service._store
    marker = object()
    setattr(live, "_test_marker", marker)

    reloaded = service._load_store(reload_during_execution=True)

    assert reloaded is live, "a dirty snapshot must not be replaced from disk"
    assert getattr(reloaded, "_test_marker", None) is marker


async def test_a_dirty_store_is_flushed_before_the_next_tick_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick that finds unpersisted state persists it and stops, rather than running jobs."""
    service = _service(tmp_path)
    _due_now(service)
    service._store_dirty = True
    monkeypatch.setattr(service, "_arm_timer", lambda: None)

    executed: list[object] = []

    async def record(job: object) -> None:
        executed.append(job)

    monkeypatch.setattr(service, "_execute_job", record)
    saved: list[int] = []
    monkeypatch.setattr(service, "_save_store", lambda: saved.append(1))

    await service._on_timer()

    assert saved, "the dirty snapshot must be persisted first"
    assert executed == [], "no job may run until the previous result is on disk"


async def test_a_successful_save_clears_the_dirty_flag(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._store_dirty = True

    service._save_store()

    assert service._store_dirty is False
