# tests/gates/test_latch_restore.py
"""Item 30 (#32): rebuild latch state from the audit log at start.

#15 keeps latch state in the process, and rejected a latch file for a sound reason: the model
holds filesystem tools, so a file in their reach is more model-reachable than memory. The
consequence is that a restart drops every latch, and the agent can cause the restart. ExecTool
is `mutate.local`, #8 does not gate that class, and `kill -TERM $PPID` under systemd Restart=
or a Docker restart policy brings the gateway back with no latches.

The audit log is the right store. It appends, it lives outside the workspace, and a model
cannot un-append a line.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.latch import LatchEventKind, new_denial_latch
from nanoinfra.gates.latch_restore import RestoredLatches, restore_latches

SESSION = "session-a"


def _store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "gates")


def _latch_with_audit(tmp_path: Path):
    """A latch whose events land in a real audit store, the way #8 will wire it."""
    store = _store(tmp_path)

    def record(event) -> None:
        store.record(
            decision=event.kind.value,
            capability_class=event.capability_class,
            execution_context="automation",
            session_id=event.session_id,
            tool=event.tool,
            actor=event.actor,
            reason=event.reason,
        )

    latch, controller = new_denial_latch(record=record)
    return latch, controller, store


def test_a_denial_survives_a_restart(tmp_path: Path) -> None:
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant",
    )

    restored = restore_latches(store)

    assert restored.is_latched(SESSION, MUTATE_REMOTE)


def test_a_clear_before_the_restart_leaves_the_class_unlatched(tmp_path: Path) -> None:
    latch, controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )
    controller.clear(session_id=SESSION, capability_class=MUTATE_REMOTE, actor="operator-1")

    restored = restore_latches(store)

    assert not restored.is_latched(SESSION, MUTATE_REMOTE)


def test_a_denial_after_a_clear_latches_again(tmp_path: Path) -> None:
    """Order matters, not presence. The latest event per pair decides."""
    latch, controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )
    controller.clear(session_id=SESSION, capability_class=MUTATE_REMOTE, actor="operator-1")
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="y"
    )

    restored = restore_latches(store)

    assert restored.is_latched(SESSION, MUTATE_REMOTE)


def test_the_refusal_count_continues_across_the_restart(tmp_path: Path) -> None:
    """#28's banner must not reset to zero and hide a session that keeps trying."""
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )
    for _ in range(3):
        latch.refuse(
            session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
        )

    restored = restore_latches(store)

    assert restored.refusal_count(SESSION, MUTATE_REMOTE) == 3


def test_one_class_latches_without_the_other(tmp_path: Path) -> None:
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )

    restored = restore_latches(store)

    assert restored.is_latched(SESSION, MUTATE_REMOTE)
    assert not restored.is_latched(SESSION, CREDENTIAL_ACCESS)


def test_an_empty_store_restores_nothing_and_is_not_degraded(tmp_path: Path) -> None:
    """A fresh install has no segments. That is empty, and it is not a read failure."""
    restored = restore_latches(_store(tmp_path))

    assert restored.latched == {}
    assert restored.degraded is False


def _corrupt_mid_file(store: AuditStore) -> None:
    """Put an unparseable line before a valid one.

    Corruption in the middle is unambiguous: a torn write reaches the last line only, so a bad
    line with valid content after it means the log lost content it should hold.
    """
    segment = store.segments()[0]
    good = segment.read_text(encoding="utf-8").splitlines()[0]
    segment.write_text(f"{{ this is not json\n{good}\n", encoding="utf-8")


def test_a_corrupt_audit_log_reports_degraded(tmp_path: Path) -> None:
    """An unreadable log must not read as "no latches"."""
    store = _store(tmp_path)
    store.record(
        decision=LatchEventKind.DENIED.value,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id=SESSION,
    )
    _corrupt_mid_file(store)

    restored = restore_latches(store)

    assert restored.degraded is True


def test_a_degraded_restore_latches_every_class(tmp_path: Path) -> None:
    """Fail closed. The log cannot say which sessions to latch, so every gated action waits."""
    store = _store(tmp_path)
    store.record(
        decision=LatchEventKind.DENIED.value,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id=SESSION,
    )
    _corrupt_mid_file(store)

    restored = restore_latches(store)

    assert restored.is_latched("a-session-nobody-recorded", MUTATE_REMOTE)
    assert restored.is_latched("another", CREDENTIAL_ACCESS)


def test_a_segment_whose_only_line_is_corrupt_counts_as_a_torn_tail(tmp_path: Path) -> None:
    """A deliberate ambiguity, pinned so nobody 'fixes' it by accident.

    One corrupt line and nothing after it is exactly what a process death during the day's
    first write leaves behind. That case is indistinguishable from deliberate truncation, and
    treating every such segment as corruption would latch every session after any unclean
    shutdown. The tail loses one record, and every other segment still restores.
    """
    store = _store(tmp_path)
    store.record(
        decision=LatchEventKind.DENIED.value,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id=SESSION,
    )
    store.segments()[0].write_text("{ torn\n", encoding="utf-8")

    restored = restore_latches(store)

    assert restored.degraded is False
    assert restored.latched == {}


def test_a_healthy_restore_does_not_latch_an_unknown_session(tmp_path: Path) -> None:
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )

    restored = restore_latches(store)

    assert not restored.is_latched("some-other-session", MUTATE_REMOTE)


def test_a_record_without_a_session_is_skipped(tmp_path: Path) -> None:
    """A gate decision can carry no session. It cannot latch one either."""
    store = _store(tmp_path)
    store.record(
        decision=LatchEventKind.DENIED.value,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
    )

    restored = restore_latches(store)

    assert restored.latched == {}
    assert restored.degraded is False


def test_a_non_latch_decision_never_latches(tmp_path: Path) -> None:
    """The store holds every gate decision. Only denied and cleared move latch state."""
    store = _store(tmp_path)
    for decision in ("allow", "approve", "granted"):
        store.record(
            decision=decision,
            capability_class=MUTATE_REMOTE,
            execution_context="automation",
            session_id=SESSION,
        )

    restored = restore_latches(store)

    assert not restored.is_latched(SESSION, MUTATE_REMOTE)


def test_the_restored_state_seeds_a_running_latch(tmp_path: Path) -> None:
    """The whole point: a new process refuses what the old process denied."""
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )
    restored = restore_latches(store)

    fresh_latch, _fresh_controller = new_denial_latch(restored=restored)
    refusal = fresh_latch.refuse(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
    )

    assert refusal is not None


def test_a_restored_latch_still_clears_from_the_controller(tmp_path: Path) -> None:
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )
    restored = restore_latches(store)
    fresh_latch, fresh_controller = new_denial_latch(restored=restored)

    fresh_controller.clear(
        session_id=SESSION, capability_class=MUTATE_REMOTE, actor="operator-1"
    )

    assert (
        fresh_latch.refuse(
            session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
        )
        is None
    )


def test_restored_state_reports_what_it_found_for_the_startup_line(tmp_path: Path) -> None:
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )
    latch.deny(
        session_id="session-b",
        capability_class=CREDENTIAL_ACCESS,
        tool="execute_on_server",
        reason="x",
    )

    summary = restore_latches(store).summary()

    assert "2" in summary


def test_a_partly_written_tail_line_does_not_degrade_the_whole_restore(
    tmp_path: Path,
) -> None:
    """#16 documents that only the last line can tear, and every earlier record survives.

    A torn tail must cost the tail, not every latch the log holds.
    """
    latch, _controller, store = _latch_with_audit(tmp_path)
    latch.deny(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server", reason="x"
    )
    segment = store.segments()[0]
    with segment.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"decision": "denied", "session_id": "torn"})[:20])

    restored = restore_latches(store)

    assert restored.is_latched(SESSION, MUTATE_REMOTE)
    assert restored.degraded is False


def test_restored_latches_is_immutable_from_the_gate_side() -> None:
    """The model must not reach clearing through the restored state either (#15)."""
    restored = RestoredLatches(latched={}, refusals={}, degraded=False)

    assert not hasattr(restored, "clear")
    assert not hasattr(restored, "clear_session")
