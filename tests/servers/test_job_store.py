# tests/servers/test_job_store.py
from __future__ import annotations

from pathlib import Path

from nanoinfra.servers.job_store import JobStore


def test_create_writes_queued_job_before_anything_runs(tmp_path: Path):
    store = JobStore(tmp_path)
    job = store.create(server_id="s" * 32, provider_id="ssh", command="uptime", timeout_s=120)

    assert job.status == "queued"
    assert (tmp_path / "servers" / "jobs" / f"{job.id}.json").is_file()
    on_disk = store.get(job.id)
    assert on_disk is not None
    assert on_disk.status == "queued"


def test_mark_running_then_complete(tmp_path: Path):
    store = JobStore(tmp_path)
    job = store.create(server_id="s" * 32, provider_id="ssh", command="uptime", timeout_s=120)

    store.mark_running(job.id)
    assert store.get(job.id).status == "running"
    assert store.get(job.id).started_at is not None

    store.complete(job.id, exit_code=0, output="up 3 days", error=None, status="completed")
    completed = store.get(job.id)
    assert completed.status == "completed"
    assert completed.exit_code == 0
    assert completed.output == "up 3 days"
    assert completed.ended_at is not None


def test_list_jobs_filters_by_server(tmp_path: Path):
    store = JobStore(tmp_path)
    job_a = store.create(server_id="a" * 32, provider_id="ssh", command="uptime", timeout_s=120)
    store.create(server_id="b" * 32, provider_id="ssh", command="uptime", timeout_s=120)

    jobs_for_a = store.list_jobs(server_id="a" * 32)
    assert [j.id for j in jobs_for_a] == [job_a.id]

    all_jobs = store.list_jobs()
    assert len(all_jobs) == 2


def test_reconcile_flips_stale_running_jobs_to_failed(tmp_path: Path):
    store = JobStore(tmp_path)
    job = store.create(server_id="s" * 32, provider_id="ssh", command="uptime", timeout_s=120)
    store.mark_running(job.id)

    # Simulate a fresh JobStore after a gateway restart -- the in-memory
    # asyncio task that was running this job is gone, only the file remains.
    fresh_store = JobStore(tmp_path)
    reconciled_count = fresh_store.reconcile_interrupted_jobs()

    assert reconciled_count == 1
    reconciled = fresh_store.get(job.id)
    assert reconciled.status == "failed"
    assert "restart" in reconciled.error.lower()
    assert reconciled.ended_at is not None


def test_reconcile_does_not_touch_completed_jobs(tmp_path: Path):
    store = JobStore(tmp_path)
    job = store.create(server_id="s" * 32, provider_id="ssh", command="uptime", timeout_s=120)
    store.mark_running(job.id)
    store.complete(job.id, exit_code=0, output="ok", error=None, status="completed")

    assert store.reconcile_interrupted_jobs() == 0
    assert store.get(job.id).status == "completed"
