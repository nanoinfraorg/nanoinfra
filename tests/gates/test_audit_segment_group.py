"""A fresh audit segment must be readable by the account that restores latches.

The demo host produced the fault this covers: the segment for a new UTC day was created with the
executor's own primary group instead of the group its directory carries, so the agent could not
read it, `restore_latches` failed closed, and every session in the deployment stayed latched --
hours after a boot that had reported the log as readable.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nanoinfra.gates.audit import AuditStore


def _groups_available() -> list[int]:
    return [gid for gid in os.getgroups() if gid != os.getgid()]


def test_a_fresh_segment_takes_the_group_of_its_directory(tmp_path: Path) -> None:
    other = _groups_available()
    if not other:
        pytest.skip("this account belongs to no second group, so there is no group to share with")
    root = tmp_path / "gates"
    root.mkdir()
    os.chown(root, -1, other[0])

    store = AuditStore(root)
    store.record(
        capability_class="mutate.remote",
        decision="allow",
        execution_context="unattended",
        tool="execute_on_server",
    )

    segments = store.segments()
    assert len(segments) == 1
    assert segments[0].stat().st_gid == other[0], (
        "a new segment kept the writer's primary group, which is the fault that latches "
        "every session once the log rotates"
    )
    assert segments[0].stat().st_mode & 0o777 == 0o640


def test_the_group_is_the_directory_s_and_not_a_constant(tmp_path: Path) -> None:
    """A single-group host changes nothing, and no failure reaches the caller."""
    root = tmp_path / "gates"
    root.mkdir()
    store = AuditStore(root)
    store.record(
        capability_class="read",
        decision="allow",
        execution_context="interactive",
        tool="read",
    )
    segment = store.segments()[0]
    assert segment.stat().st_gid == root.stat().st_gid


def test_a_segment_written_the_next_day_is_shared_too(tmp_path: Path) -> None:
    """Rotation is what created the fault, so the second day gets the same treatment."""
    other = _groups_available()
    if not other:
        pytest.skip("this account belongs to no second group, so there is no group to share with")
    root = tmp_path / "gates"
    root.mkdir()
    os.chown(root, -1, other[0])
    store = AuditStore(root)

    store.record(
        capability_class="mutate.remote",
        decision="allow",
        execution_context="unattended",
        tool="execute_on_server",
        ts=datetime(2026, 8, 22, 23, 59, tzinfo=UTC),
    )
    store.record(
        capability_class="mutate.remote",
        decision="allow",
        execution_context="unattended",
        tool="execute_on_server",
        ts=datetime(2026, 8, 23, 0, 1, tzinfo=UTC),
    )

    segments = store.segments()
    assert len(segments) == 2
    for segment in segments:
        assert segment.stat().st_gid == other[0]
