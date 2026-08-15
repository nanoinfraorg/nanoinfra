# tests/gates/test_runtime.py
"""Item 31 (#33): the gate runtime the gateway builds once at boot.

Five components shipped as libraries and the gate called none of them. This joins them, and it
keeps the split #15 built: the caller that holds the runtime can deny and refuse, and only the
operator side holds the object that clears a latch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.latch import LatchController, TerminalDenial
from nanoinfra.gates.policy import Outcome
from nanoinfra.gates.runtime import GateRuntime, build_gate_runtime

SESSION = "session-a"


def _build(tmp_path: Path, gates: GatesConfig | None = None):
    return build_gate_runtime(gates or GatesConfig(), root=tmp_path / "gates")


def test_the_runtime_carries_the_audit_store_and_the_latch(tmp_path: Path) -> None:
    runtime, _controller = _build(tmp_path)

    assert isinstance(runtime, GateRuntime)
    assert runtime.audit is not None
    assert runtime.tokens is not None


def test_the_runtime_exposes_no_way_to_clear_a_latch(tmp_path: Path) -> None:
    """#15 splits the halves at birth. The runtime is the gate half, so it must stay that way.

    A runtime that carried the controller would hand every holder of the tool path a clear.
    """
    runtime, controller = _build(tmp_path)

    assert isinstance(controller, LatchController)
    assert not hasattr(runtime, "clear")
    assert not hasattr(runtime, "clear_session")
    assert not isinstance(getattr(runtime, "latch", None), LatchController)


def test_a_denial_returns_a_terminal_denial_and_latches(tmp_path: Path) -> None:
    runtime, _controller = _build(tmp_path)

    refusal = runtime.refuse_action(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant covers this",
        execution_context="automation",
    )

    assert isinstance(refusal, TerminalDenial)
    assert runtime.latched_refusal(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
    ) is not None


def test_a_second_attempt_refuses_with_no_new_prompt(tmp_path: Path) -> None:
    runtime, _controller = _build(tmp_path)
    runtime.refuse_action(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant",
        execution_context="automation",
    )

    second = runtime.latched_refusal(
        session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
    )

    assert isinstance(second, TerminalDenial)


def test_an_unlatched_class_returns_no_refusal(tmp_path: Path) -> None:
    runtime, _controller = _build(tmp_path)
    runtime.refuse_action(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant",
        execution_context="automation",
    )

    assert (
        runtime.latched_refusal(
            session_id=SESSION, capability_class=CREDENTIAL_ACCESS, tool="execute_on_server"
        )
        is None
    )


def test_an_operator_clears_the_latch_through_the_controller(tmp_path: Path) -> None:
    runtime, controller = _build(tmp_path)
    runtime.refuse_action(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant",
        execution_context="automation",
    )

    controller.clear(session_id=SESSION, capability_class=MUTATE_REMOTE, actor="operator-1")

    assert (
        runtime.latched_refusal(
            session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
        )
        is None
    )


def test_every_decision_reaches_the_audit_log(tmp_path: Path) -> None:
    runtime, _controller = _build(tmp_path)

    runtime.record_decision(
        outcome=Outcome.ALLOW,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id=SESSION,
        tool="execute_on_server",
        scope="host",
        hosts=("10.0.1.5",),
        command="uptime",
        reason="grant reload covers this action",
        grant_id="reload",
    )

    records = runtime.audit.read_all()
    assert [r["decision"] for r in records] == ["allow"]
    assert records[0]["grant_id"] == "reload"


def test_a_refusal_is_recorded_with_its_reason(tmp_path: Path) -> None:
    runtime, _controller = _build(tmp_path)
    runtime.refuse_action(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant covers this",
        execution_context="automation",
    )

    records = runtime.audit.read_all()
    assert records[0]["decision"] == "denied"
    assert records[0]["session_id"] == SESSION


def test_a_latched_refusal_is_recorded_too(tmp_path: Path) -> None:
    """A latched session that keeps trying must be visible as exactly that."""
    runtime, _controller = _build(tmp_path)
    runtime.refuse_action(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant",
        execution_context="automation",
    )
    for _ in range(2):
        runtime.latched_refusal(
            session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
        )

    refused = [r for r in runtime.audit.read_all() if r["decision"] == "refused"]
    assert len(refused) == 2


def test_the_command_never_reaches_the_audit_record_as_text(tmp_path: Path) -> None:
    """#16 stores a digest by default, because resolved commands embed secrets."""
    runtime, _controller = _build(tmp_path)

    runtime.record_decision(
        outcome=Outcome.DENY,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id=SESSION,
        tool="execute_on_server",
        scope="host",
        hosts=("10.0.1.5",),
        command="mysql -u root -p'hunter2'",
        reason="no grant",
    )

    assert "hunter2" not in str(runtime.audit.read_all())


def test_an_audit_write_failure_raises_so_the_caller_fails_closed(tmp_path: Path) -> None:
    """#16 raises OSError rather than swallow a write failure.

    An action nothing recorded must not run, so the runtime does not hide the error.
    """
    runtime, _controller = _build(tmp_path)
    # The first record creates the segment. The store opens it with O_APPEND, so a read-only
    # segment is what an unwritable audit log looks like to the next write.
    runtime.record_decision(
        outcome=Outcome.ALLOW,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id=SESSION,
        tool="execute_on_server",
        scope="host",
        hosts=("10.0.1.5",),
        command="uptime",
        reason="ok",
    )
    segment = runtime.audit.segments()[0]
    segment.chmod(0o400)
    try:
        with pytest.raises(OSError):
            runtime.record_decision(
                outcome=Outcome.DENY,
                capability_class=MUTATE_REMOTE,
                execution_context="automation",
                session_id=SESSION,
                tool="execute_on_server",
                scope="host",
                hosts=("10.0.1.5",),
                command="uptime",
                reason="no grant",
            )
    finally:
        segment.chmod(0o600)


def test_a_restored_latch_seeds_the_runtime(tmp_path: Path) -> None:
    """#32 restores across a restart, and the runtime must honour that state."""
    first, _controller = _build(tmp_path)
    first.refuse_action(
        session_id=SESSION,
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="no grant",
        execution_context="automation",
    )

    second, _second_controller = _build(tmp_path)

    assert (
        second.latched_refusal(
            session_id=SESSION, capability_class=MUTATE_REMOTE, tool="execute_on_server"
        )
        is not None
    )
