# tests/config/test_gates_audit_floor.py
"""`gates.audit.retentionDays` must not accept a value that disables retention silently.

`AuditStore.prune` keeps every segment when retention is zero or less, which is the correct
fail-safe for a store that must never empty itself by accident. The config then has to refuse such a
value, or a hand-edited file turns retention off with no message.

The WebUI already refuses a value below one day. A schema floor covers the file an operator edits
by hand, which the WebUI never sees.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanoinfra.config.gates import AuditConfig


def test_the_shipped_default_keeps_ninety_days() -> None:
    assert AuditConfig().retention_days == 90


@pytest.mark.parametrize("days", [0, -1, -90])
def test_a_value_below_one_day_is_refused(days: int) -> None:
    """Zero reads as "keep nothing" and means "keep everything", so the schema refuses it."""
    with pytest.raises(ValidationError):
        AuditConfig.model_validate({"retentionDays": days})


def test_one_day_is_allowed() -> None:
    """A short retention is a real choice. Only the values that mean nothing are refused."""
    assert AuditConfig.model_validate({"retentionDays": 1}).retention_days == 1
