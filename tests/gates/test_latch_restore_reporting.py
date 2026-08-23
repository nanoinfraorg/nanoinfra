"""An unreadable audit log is reported once, and its recovery is reported too.

The demo host logged the same warning every fifteen seconds for an hour, all of them naming one
file. The condition is real and fails closed; the repetition only buries the rest of the log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.gates import latch_restore
from nanoinfra.gates.audit import AuditStore


class _UnreadableStore(AuditStore):
    def __init__(self, root: Path, error: OSError) -> None:
        super().__init__(root)
        self._error = error

    def segments(self) -> list[Path]:
        raise self._error


@pytest.fixture(autouse=True)
def _forget_previous_reports():
    latch_restore._last_unreadable_report = None
    yield
    latch_restore._last_unreadable_report = None


def test_the_same_failure_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = _UnreadableStore(tmp_path, PermissionError(13, "Permission denied", "gate-2026-08-23.jsonl"))

    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m), level="WARNING")
    try:
        for _ in range(4):
            assert latch_restore.restore_latches(store).degraded is True
    finally:
        logger.remove(sink)

    warnings = [line for line in lines if "latches stay closed" in line]
    assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"


def test_a_different_failure_warns_again(tmp_path: Path) -> None:
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m), level="WARNING")
    try:
        latch_restore.restore_latches(_UnreadableStore(tmp_path, PermissionError(13, "Permission denied")))
        latch_restore.restore_latches(_UnreadableStore(tmp_path, FileNotFoundError(2, "No such file")))
    finally:
        logger.remove(sink)

    assert len([line for line in lines if "latches stay closed" in line]) == 2


def test_recovery_is_reported(tmp_path: Path) -> None:
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m), level="INFO")
    try:
        latch_restore.restore_latches(_UnreadableStore(tmp_path, PermissionError(13, "Permission denied")))
        healthy = AuditStore(tmp_path)
        result = latch_restore.restore_latches(healthy)
    finally:
        logger.remove(sink)

    assert result.degraded is False
    assert any("can be read again" in line for line in lines)
