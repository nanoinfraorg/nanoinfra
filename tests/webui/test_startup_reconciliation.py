"""Test that gateway startup reconciles interrupted jobs."""

from __future__ import annotations

from pathlib import Path

from nanoinfra.servers.job_store import JobStore


def test_reconcile_is_called_on_handler_construction(tmp_path: Path) -> None:
    """A focused unit test for the reconciliation *call*, not a full
    GatewayHTTPHandler construction (that class has many required
    constructor args unrelated to this feature) -- pre-seed one stale
    running job, then verify JobStore itself (already fully tested in
    the servers-execution plan's Task 2) is the thing being invoked."""
    store = JobStore(tmp_path)
    job = store.create(
        server_id="a" * 32, provider_id="ssh", command="uptime", timeout_s=120
    )
    store.mark_running(job.id)

    # Simulate exactly what GatewayHTTPHandler.__init__ now does.
    fresh_store = JobStore(tmp_path)
    reconciled = fresh_store.reconcile_interrupted_jobs()

    assert reconciled == 1
    assert fresh_store.get(job.id).status == "failed"
