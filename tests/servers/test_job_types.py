# tests/servers/test_job_types.py
from __future__ import annotations

from nanoinfra.servers.job_types import ServerJob


def test_to_dict_round_trips():
    job = ServerJob(
        id="a" * 32,
        server_id="b" * 32,
        provider_id="ssh",
        command="uptime",
        status="completed",
        created_at="t0",
        started_at="t1",
        ended_at="t2",
        exit_code=0,
        output="up 3 days",
        error=None,
        timeout_s=120,
    )
    assert ServerJob.from_dict(job.to_dict()) == job
