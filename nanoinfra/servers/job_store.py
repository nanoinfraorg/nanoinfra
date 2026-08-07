"""Durable Server execution job records — one JSON file per job.

Same per-entity-file, atomic-write shape as nanoinfra/servers/store.py
and nanoinfra/diagrams/store.py. The one new behavior: create() writes
status="queued" to disk *before* the caller (Task 8's execute_on_server)
ever starts a backend, and reconcile_interrupted_jobs() is called once
at gateway startup to flip any job a prior crash left stuck at
"running" to an honest "failed" -- see the module-level note in the
design spec: this is "the record survives," not "the connection
resumes itself," which isn't technically possible.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanoinfra.servers.job_types import ServerJob
from nanoinfra.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    ensure_dir,
)

_VALID_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / "servers" / "jobs"

    def _path(self, job_id: str) -> Path | None:
        if not _VALID_ID_RE.match(job_id):
            return None
        return self.root / f"{job_id}.json"

    def _read(self, job_id: str) -> ServerJob | None:
        path = self._path(job_id)
        if path is None or not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable job file {}: {}", path, exc)
            return None
        try:
            return ServerJob.from_dict(cast(dict[str, Any], data))
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed job file {}", path)
            return None

    def _write(self, job: ServerJob) -> None:
        path = self._path(job.id)
        if path is None:
            raise ValueError(f"Refusing to write job with invalid id: {job.id!r}")
        ensure_dir(self.root)
        _write_text_atomic(path, json.dumps(job.to_dict(), ensure_ascii=False, indent=2))

    def create(self, *, server_id: str, provider_id: str, command: str, timeout_s: int) -> ServerJob:
        job = ServerJob(
            id=uuid.uuid4().hex,
            server_id=server_id,
            provider_id=provider_id,
            command=command,
            status="queued",
            created_at=_now_iso(),
            started_at=None,
            ended_at=None,
            exit_code=None,
            output="",
            error=None,
            timeout_s=timeout_s,
        )
        self._write(job)
        return job

    def mark_running(self, job_id: str) -> None:
        job = self._read(job_id)
        if job is None:
            raise KeyError(job_id)
        job.status = "running"
        job.started_at = _now_iso()
        self._write(job)

    def update_output(self, job_id: str, output: str) -> None:
        """Called periodically while a job runs, so a crash mid-run still
        leaves the most recent known output on disk instead of nothing."""
        job = self._read(job_id)
        if job is None:
            raise KeyError(job_id)
        job.output = output
        self._write(job)

    def complete(
        self,
        job_id: str,
        *,
        exit_code: int | None,
        output: str,
        error: str | None,
        status: str,
    ) -> None:
        job = self._read(job_id)
        if job is None:
            raise KeyError(job_id)
        job.status = status
        job.exit_code = exit_code
        job.output = output
        job.error = error
        job.ended_at = _now_iso()
        self._write(job)

    def get(self, job_id: str) -> ServerJob | None:
        return self._read(job_id)

    def list_jobs(self, server_id: str | None = None) -> list[ServerJob]:
        if not self.root.is_dir():
            return []
        jobs: list[ServerJob] = []
        for path in self.root.glob("*.json"):
            job = self._read(path.stem)
            if job is not None and (server_id is None or job.server_id == server_id):
                jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def reconcile_interrupted_jobs(self) -> int:
        """Call once at gateway startup. Any job still "running" was left
        that way by a crash or SIGKILL -- there is nothing to resume, so
        this records an honest terminal state instead of a stale status
        that would otherwise never change again."""
        count = 0
        for job in self.list_jobs():
            if job.status != "running":
                continue
            job.status = "failed"
            job.error = "Interrupted by gateway restart -- the connection could not survive the process exiting."
            job.ended_at = _now_iso()
            self._write(job)
            count += 1
        return count


__all__ = ["JobStore"]
