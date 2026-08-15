# tests/gates/test_completion_records.py
"""The store side of the completion record (nanoinfraorg/nanoinfra#46).

#16 writes one record before an action runs. That record answers one question. It says what the
gate decided. It cannot say what happened next, because ``exit_code`` and ``duration_ms`` stay
null on every executor record.

A completion record answers the second question. The store appends it when the action ends, and
it never edits the decision record. An edit would make an append-only log mutable.

The completion record names the decision record it follows. A reader then pairs the two records
by an id, and not by a guess about two timestamps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.tools.capabilities import command_digest
from nanoinfra.config.gates import AuditConfig
from nanoinfra.gates.audit import DECISION_COMPLETION, AuditStore

_COMMAND = "systemctl reload nginx"


def _store(root: Path, *, record_command_text: bool = False) -> AuditStore:
    return AuditStore(
        root / "gates" / "audit",
        config=AuditConfig(retention_days=90, record_command_text=record_command_text),
    )


def _decision(store: AuditStore) -> dict[str, Any]:
    return store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="automation",
        tool="execute_on_server",
        session_id="telegram:42",
        scope="host",
        hosts=["10.0.1.5"],
        command=_COMMAND,
        reason="a standing grant matched this command",
        grant_id="reload",
    )


def test_every_record_carries_an_id_that_no_other_record_holds(tmp_path: Path) -> None:
    """A completion names the decision it follows, so a decision needs a name."""
    store = _store(tmp_path)

    first = _decision(store)
    second = _decision(store)

    assert isinstance(first["record_id"], str)
    assert first["record_id"]
    assert first["record_id"] != second["record_id"]


def test_a_completion_record_holds_the_exit_code_and_the_duration(tmp_path: Path) -> None:
    """The two dead fields of #16. The completion record is the record that fills them."""
    store = _store(tmp_path)
    decision = _decision(store)

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=1234)

    assert completion["decision"] == DECISION_COMPLETION
    assert completion["exit_code"] == 0
    assert completion["duration_ms"] == 1234


def test_a_completion_record_names_the_decision_it_follows(tmp_path: Path) -> None:
    """The join is explicit. A reader pairs the two records without a timestamp guess."""
    store = _store(tmp_path)
    decision = _decision(store)

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    assert completion["follows"] == decision["record_id"]
    assert decision["follows"] is None


def test_a_completion_record_repeats_the_fields_a_reviewer_reads(tmp_path: Path) -> None:
    """The completion stands on its own row. A filtered view must still show the blast radius."""
    store = _store(tmp_path)
    decision = _decision(store)

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    assert completion["session_id"] == "telegram:42"
    assert completion["capability_class"] == "mutate.remote"
    assert completion["execution_context"] == "automation"
    assert completion["scope"] == "host"
    assert completion["hosts"] == ["10.0.1.5"]
    assert completion["host_count"] == 1
    assert completion["command_digest"] == command_digest(_COMMAND)
    assert completion["tool"] == "execute_on_server"


def test_a_completion_record_carries_the_keys_a_decision_record_carries(tmp_path: Path) -> None:
    """Every line holds the same keys, so a reader can load the whole log into one table."""
    store = _store(tmp_path)
    decision = _decision(store)

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    assert set(completion) == set(decision)


def test_a_completion_record_states_the_authorization_of_the_decision_alone(
    tmp_path: Path,
) -> None:
    """The completion copies no authorization. The decision record it names holds that answer."""
    store = _store(tmp_path)
    decision = _decision(store)

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    assert completion["grant_id"] is None
    assert completion["approval_id"] is None
    assert completion["actor"] is None
    assert completion["secret_ref"] is None


def test_a_completion_record_holds_no_command_output(tmp_path: Path) -> None:
    """#16 keeps a digest of the command for a reason. Output carries the same risk."""
    store = _store(tmp_path)
    decision = _decision(store)

    completion = store.record_completion(
        follows=decision, exit_code=0, duration_ms=7, reason="the action ended"
    )

    assert "command_text" not in completion
    assert "output" not in completion
    with pytest.raises(TypeError):
        store.record_completion(
            follows=decision,
            exit_code=0,
            duration_ms=7,
            output="root:x:0:0:root:/root:/bin/bash",
        )


def test_a_completion_record_holds_no_command_text_under_the_opt_in(tmp_path: Path) -> None:
    """The opt-in covers the decision record. A copy in a second record adds risk and nothing."""
    store = _store(tmp_path, record_command_text=True)
    decision = _decision(store)

    completion = store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    assert decision["command_text"] == _COMMAND
    assert "command_text" not in completion


def test_an_action_that_ends_with_no_exit_code_still_gets_a_record(tmp_path: Path) -> None:
    """A timeout and a lost transport leave the outcome unknown.

    Unknown and never ran are opposite facts for a reviewer. So the record exists, and the exit
    code stays null inside it.
    """
    store = _store(tmp_path)
    decision = _decision(store)

    completion = store.record_completion(
        follows=decision,
        exit_code=None,
        duration_ms=30000,
        reason="the action timed out, so the exit code is unknown",
    )

    assert completion["decision"] == DECISION_COMPLETION
    assert completion["exit_code"] is None
    assert completion["duration_ms"] == 30000
    assert "unknown" in str(completion["reason"])


def test_a_completion_leaves_the_decision_record_byte_identical(tmp_path: Path) -> None:
    """The completion appends. It never edits the record that authorized the action."""
    store = _store(tmp_path)
    decision = _decision(store)
    before = store.segments()[0].read_bytes()

    store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    after = store.segments()[0].read_bytes()
    assert after.startswith(before)
    lines = after.splitlines()
    assert len(lines) == 2
    assert lines[0] == before.splitlines()[0]


def test_the_two_records_read_back_in_order(tmp_path: Path) -> None:
    """The decision lands before the action runs. The order is the property #16 exists for."""
    store = _store(tmp_path)
    decision = _decision(store)
    store.record_completion(follows=decision, exit_code=0, duration_ms=7)

    records = store.read_all()

    assert [record["decision"] for record in records] == ["allow", DECISION_COMPLETION]
    assert records[1]["follows"] == records[0]["record_id"]


def test_a_write_failure_on_a_completion_raises(tmp_path: Path) -> None:
    """The caller decides what an unrecorded outcome costs. The store must not hide the failure."""
    blocked = tmp_path / "gates"
    blocked.write_text("a file sits where the audit root belongs", encoding="utf-8")
    store = AuditStore(blocked)

    with pytest.raises(OSError):
        store.record_completion(follows={"record_id": "abc123"}, exit_code=0, duration_ms=7)
