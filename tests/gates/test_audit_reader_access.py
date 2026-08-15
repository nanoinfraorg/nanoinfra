# tests/gates/test_audit_reader_access.py
"""An unreadable audit root must not read as "no latched sessions".

#32 rebuilds denial latches from the audit log. The restore runs in the agent process, and #18
gave the log to the executor account at mode 700. `Path.glob` swallows `PermissionError` and
answers with an empty list, so `segments()` reported empty, `restore_latches` reported healthy,
and every latch vanished on every boot. #32 was void in the split container through permissions
alone, with no rename needed.

Two halves fix it, and both are necessary. The store must tell an unreadable root from an empty
one, so the restore degrades rather than reports nothing. And the log must stay readable to the
process that restores from it, or the container degrades forever and every session stays latched.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.latch_restore import restore_latches


def _store_with_a_denial(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "gates")
    store.record(
        decision="denied",
        capability_class="mutate.remote",
        execution_context="automation",
        session_id="s1",
    )
    return store


@pytest.fixture
def unreadable(tmp_path: Path):
    """A root the current process cannot enter, the way the split container arranges it."""
    store = _store_with_a_denial(tmp_path)
    root = tmp_path / "gates"
    root.chmod(0o000)
    try:
        yield store
    finally:
        root.chmod(0o700)


def test_an_unreadable_root_raises_instead_of_reporting_no_segments(unreadable) -> None:
    """`Path.glob` hides the difference, and the difference is the whole bypass."""
    with pytest.raises(OSError):
        unreadable.segments()


def test_an_unreadable_root_degrades_the_latch_restore(unreadable) -> None:
    """Fail closed. A log this process cannot read must not clear a latch."""
    restored = restore_latches(unreadable)

    assert restored.degraded is True
    assert restored.is_latched("s1", "mutate.remote")


def test_an_unreadable_root_keeps_an_unrelated_session_latched_too(unreadable) -> None:
    """Degraded means every pair waits, because the log cannot name the ones it lost."""
    restored = restore_latches(unreadable)

    assert restored.is_latched("a-session-nobody-recorded", "mutate.remote")


def test_a_segment_stays_readable_to_the_group(tmp_path: Path) -> None:
    """The restore and the #29 viewer both run in the agent process, so it must read the log.

    Write access stays with the executor. A reader that cannot open a segment leaves the container
    permanently degraded, which is worse than the bypass it would close.
    """
    store = _store_with_a_denial(tmp_path)
    segment = store.segments()[0]

    mode = segment.stat().st_mode
    assert mode & stat.S_IRGRP
    assert not mode & stat.S_IWGRP
    assert not mode & (stat.S_IRWXO)


def test_a_created_root_stays_traversable_by_the_group(tmp_path: Path) -> None:
    """A directory the store creates lets the group enter and list, and never write."""
    store = _store_with_a_denial(tmp_path)

    mode = store.root.stat().st_mode
    assert mode & stat.S_IRGRP
    assert mode & stat.S_IXGRP
    assert not mode & stat.S_IWGRP
    assert not mode & stat.S_IRWXO


def test_an_existing_root_keeps_the_mode_the_deployment_set(tmp_path: Path) -> None:
    """The deployment owns that decision, exactly as it owns the socket directory in #18."""
    root = tmp_path / "gates"
    root.mkdir()
    root.chmod(0o2750)
    before = root.stat().st_mode & 0o7777

    AuditStore(root).record(
        decision="denied",
        capability_class="mutate.remote",
        execution_context="automation",
        session_id="s1",
    )

    assert root.stat().st_mode & 0o7777 == before


def test_a_reader_still_sees_an_empty_log_as_empty(tmp_path: Path) -> None:
    """A fresh install has no log, and that is not a failure."""
    restored = restore_latches(AuditStore(tmp_path / "never-written"))

    assert restored.degraded is False
    assert restored.latched == {}


def test_the_owner_can_still_write_after_the_mode_change(tmp_path: Path) -> None:
    store = _store_with_a_denial(tmp_path)

    store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="interactive",
        session_id="s2",
    )

    assert len(store.read_all()) == 2


def test_a_group_reader_cannot_append(tmp_path: Path) -> None:
    """Read is enough for the restore and the viewer. Write stays with the executor."""
    store = _store_with_a_denial(tmp_path)
    segment = store.segments()[0]

    assert not segment.stat().st_mode & stat.S_IWGRP
    assert os.access(segment, os.R_OK)
