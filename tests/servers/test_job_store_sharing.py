# tests/servers/test_job_store_sharing.py
"""The job store is written by both accounts of the privilege split.

The executor creates a job and updates its output; the agent reconciles jobs a restart
interrupted. A file inherits its creator's primary group, so whoever wrote first produced a file
the other could not rewrite -- and in a container that made every remote action fail *after* the
gate permitted it.

A setgid directory is the usual answer and it is not enough: the container's job directory comes
from an image layer, and overlayfs drops `S_ISGID` when a chmod copies it up.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from nanoinfra.servers.job_store import JobStore


def _job(store: JobStore) -> Path:
    job = store.create(server_id="s1", provider_id="ssh", command="uptime", timeout_s=30)
    return store.root / f"{job.id}.json"


def test_a_record_in_a_shared_directory_is_group_writable(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.root.mkdir(parents=True)
    os.chmod(store.root, 0o770)

    path = _job(store)

    assert stat.S_IMODE(path.stat().st_mode) == 0o660
    # The group follows the directory, which is what the other account belongs to.
    assert path.stat().st_gid == store.root.stat().st_gid


def test_a_private_directory_leaves_the_record_alone(tmp_path: Path) -> None:
    """A single-uid host shares nothing, and this must not widen its files."""
    store = JobStore(tmp_path)
    store.root.mkdir(parents=True)
    os.chmod(store.root, 0o700)

    path = _job(store)

    assert not stat.S_IMODE(path.stat().st_mode) & stat.S_IWGRP


def test_an_update_keeps_the_shared_mode(tmp_path: Path) -> None:
    """`update_output` runs while a job streams, so it rewrites the record repeatedly."""
    store = JobStore(tmp_path)
    store.root.mkdir(parents=True)
    os.chmod(store.root, 0o770)
    path = _job(store)
    job_id = path.stem

    store.update_output(job_id, "up 3 days")

    assert stat.S_IMODE(path.stat().st_mode) == 0o660
