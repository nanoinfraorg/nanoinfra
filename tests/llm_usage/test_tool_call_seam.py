"""What the seam writes, from the paths a tool call really takes (#232, #233).

The store tests next door assert what a row may hold. These assert that the row is *written*, by
the two callers that execute a tool, with the turn's own identifiers and with the gate's answer
joined onto it -- and that the arguments the call carried are nowhere in the database afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.tools.base import Tool, ToolResult
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.gates.audit import AuditStore
from nanoinfra.llm_usage import get_llm_usage_store, reset_llm_usage_stores
from nanoinfra.llm_usage.context import llm_usage_source
from nanoinfra.llm_usage.gate_join import note_gate_decision
from nanoinfra.llm_usage.store import LLMUsageStore
from nanoinfra.llm_usage.tool_calls import (
    classify_tool_error,
    reset_tool_call_seq,
    tool_call_record,
)

#: The argument the tests look for afterwards. Shaped like the case the design is about: a command
#: line with a credential in it.
_SECRET_ARGUMENT = "psql --password=hunter2-NOT-A-REAL-SECRET -c 'select 1'"


class _CommandTool(Tool):
    """A tool that takes a command, so a test can hand it something worth not storing."""

    capability_class = "mutate.local"

    def __init__(
        self,
        *,
        name: str = "run_command",
        raises: bool = False,
        error: bool = False,
        before: Any = None,
    ) -> None:
        self._name = name
        self._raises = raises
        self._error = error
        self._before = before
        self.commands: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "run a command"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, command: str = "", **kwargs: Any
    ) -> Any:
        self.commands.append(command)
        if self._before is not None:
            self._before()
        if self._raises:
            raise RuntimeError(f"the command failed: {command}")
        if self._error:
            return ToolResult.error(f"Refusing to run {command}")
        return f"ran {command}"


@pytest.fixture
def store() -> Any:
    """The store the seam writes to. HOME is per test (conftest #45), so this is per test too."""
    reset_llm_usage_stores()
    reset_tool_call_seq()
    yield get_llm_usage_store()
    reset_llm_usage_stores()


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_CommandTool())
    return registry


def _turn(session_key: str = "webui:alberto", turn_id: str = "turn-1") -> RequestContext:
    return RequestContext(
        channel="webui",
        chat_id="1",
        session_key=session_key,
        turn_id=turn_id,
        sender_id="alberto",
        execution_context="interactive",
    )


# --- one row per call --------------------------------------------------------------------


async def test_a_tool_call_writes_exactly_one_row_with_its_class_and_outcome(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    rows = store.tool_calls()

    assert len(rows) == 1
    assert rows[0]["tool"] == "run_command"
    assert rows[0]["capability_class"] == "mutate.local"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["error_kind"] is None


async def test_the_row_addresses_the_call_in_the_session_history(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    with request_context(_turn(session_key="webui:alberto", turn_id="turn-42")):
        await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["session_key"] == "webui:alberto"
    assert row["turn_id"] == "turn-42"
    assert row["seq"] == 0
    assert row["actor"] == "alberto"


async def test_seq_counts_the_calls_within_one_turn(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    with request_context(_turn(turn_id="turn-1")):
        await registry.execute("run_command", {"command": "one"})
        await registry.execute("run_command", {"command": "two"})
    with request_context(_turn(turn_id="turn-2")):
        await registry.execute("run_command", {"command": "three"})

    rows = sorted(store.tool_calls(), key=lambda row: (str(row["turn_id"]), int(row["seq"])))

    assert [(row["turn_id"], row["seq"]) for row in rows] == [
        ("turn-1", 0),
        ("turn-1", 1),
        ("turn-2", 0),
    ]


async def test_the_row_names_the_source_the_turn_was_bound_with(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    """The same vocabulary as the heatmap, from the same contextvar `agent/loop.py` binds."""
    with request_context(_turn()), llm_usage_source("cron"):
        await registry.execute("run_command", {"command": "uptime"})

    assert store.tool_calls()[0]["source"] == "cron"


async def test_a_call_outside_a_turn_still_gets_a_row(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    """An SDK caller binds no request. The row loses its address and keeps everything else."""
    await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["session_key"] is None
    assert row["outcome"] == "ok"
    assert row["source"] == "system"


async def test_a_nested_scope_does_not_write_a_second_row(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    """The runner opens a scope and may then reach `ToolRegistry.execute` for the same call."""
    with request_context(_turn()):
        with tool_call_record(tool="run_command", capability_class="mutate.local"):
            await registry.execute("run_command", {"command": "uptime"})

    assert len(store.tool_calls()) == 1


# --- how a call ended --------------------------------------------------------------------


async def test_a_raising_tool_is_recorded_as_an_exception(store: LLMUsageStore) -> None:
    registry = ToolRegistry()
    registry.register(_CommandTool(raises=True))

    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["outcome"] == "error"
    assert row["error_kind"] == "exception"


async def test_an_error_result_is_recorded_with_a_coarse_kind(store: LLMUsageStore) -> None:
    registry = ToolRegistry()
    registry.register(_CommandTool(error=True))

    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["outcome"] == "error"
    assert row["error_kind"] == "tool_error"


async def test_a_call_to_a_tool_that_is_not_there_is_recorded_as_not_found(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    with request_context(_turn()):
        await registry.execute("run_commmand", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["tool"] == "run_commmand"
    assert row["error_kind"] == "not_found"
    # A name that never resolved has no capability class. The fail-closed `mutate.remote` is the
    # right answer to a policy question and the wrong one to write into a history.
    assert row["capability_class"] is None


async def test_bad_parameters_are_recorded_as_invalid_params(store: LLMUsageStore) -> None:
    registry = ToolRegistry()
    registry.register(_CommandTool())

    with request_context(_turn()):
        await registry.execute("run_command", {"timeout": 5})

    assert store.tool_calls()[0]["error_kind"] == "invalid_params"


def test_the_error_classifier_keeps_a_kind_and_not_the_message() -> None:
    assert classify_tool_error("Error: Tool 'nope' not found. Available: a, b") == "not_found"
    assert classify_tool_error("Error: Tool 'x' is unavailable") == "unavailable"
    assert classify_tool_error("Error: Invalid parameters for tool 'x': bad") == "invalid_params"
    assert classify_tool_error("This is a non-bypassable security boundary.") == "blocked"
    assert classify_tool_error("psql: FATAL: password authentication failed") == "tool_error"


# --- the gate's answer -------------------------------------------------------------------


async def test_no_gate_leaves_the_decision_empty_rather_than_a_fabricated_allow(
    store: LLMUsageStore, registry: ToolRegistry
) -> None:
    """#233. A deployment with no gate configured still gets rows."""
    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["gate_decision"] is None
    assert row["gate_reason"] is None


async def test_a_denied_call_is_recorded_as_denied_rather_than_as_an_error(
    store: LLMUsageStore,
) -> None:
    registry = ToolRegistry()
    registry.register(
        _CommandTool(
            error=True,
            before=lambda: note_gate_decision(
                decision="denied", reason="mutate.remote at host scope is deny"
            ),
        )
    )

    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["outcome"] == "denied"
    assert row["gate_decision"] == "denied"
    assert row["gate_reason"] == "mutate.remote at host scope is deny"
    # A refusal is not a failure, so it carries no error kind to be counted as breakage.
    assert row["error_kind"] is None


async def test_an_approved_call_names_the_approver_on_the_same_row(
    store: LLMUsageStore,
) -> None:
    """*suspended -> approved by alberto -> allowed* on one row, which is the audit trail."""

    def three_records() -> None:
        note_gate_decision(decision="suspended", reason="waiting for an answer")
        note_gate_decision(
            decision="approve",
            reason="gates.interactive.mutate.remote.host is 'approve'",
            actor="webui:alberto",
        )
        note_gate_decision(decision="allow", reason="the approval authorized this action")

    registry = ToolRegistry()
    registry.register(_CommandTool(before=three_records))

    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["gate_decision"] == "allow"
    assert row["outcome"] == "ok"
    # The record that finally allows the action holds no actor. Losing the name there would lose
    # the half of the trail a log does not already have.
    assert row["actor"] == "webui:alberto"


async def test_a_gate_decision_does_not_leak_into_the_next_call(
    store: LLMUsageStore,
) -> None:
    """Two sequential calls in one turn share a task, so the note has to be scoped per call."""
    registry = ToolRegistry()
    registry.register(_CommandTool(name="gated", before=lambda: note_gate_decision(
        decision="denied", reason="no grant covers this action"
    )))
    registry.register(_CommandTool(name="ungated"))

    with request_context(_turn()):
        await registry.execute("gated", {"command": "uptime"})
        await registry.execute("ungated", {"command": "uptime"})

    by_tool = {str(row["tool"]): row for row in store.tool_calls()}

    assert by_tool["gated"]["gate_decision"] == "denied"
    assert by_tool["ungated"]["gate_decision"] is None


async def test_the_audit_store_is_what_joins_the_decision_onto_the_row(
    store: LLMUsageStore, tmp_path: Path
) -> None:
    """The join #233 asked for, through the real writer rather than through the contextvar."""
    audit = AuditStore(tmp_path / "gates")
    registry = ToolRegistry()
    registry.register(
        _CommandTool(
            before=lambda: audit.record(
                decision="denied",
                capability_class="mutate.local",
                execution_context="interactive",
                tool="run_command",
                reason="mutate.local in an interactive context is deny",
                actor="webui:alberto",
            )
        )
    )

    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    row = store.tool_calls()[0]

    assert row["gate_decision"] == "denied"
    assert row["outcome"] == "denied"
    assert row["actor"] == "webui:alberto"


async def test_a_completion_record_does_not_overwrite_the_decision(
    store: LLMUsageStore, tmp_path: Path
) -> None:
    """#46 writes a second record when the action ends. The row already carries the outcome."""
    audit = AuditStore(tmp_path / "gates")

    def decide_then_complete() -> None:
        decision = audit.record(
            decision="allow",
            capability_class="mutate.local",
            execution_context="interactive",
            tool="run_command",
        )
        audit.record_completion(follows=decision, exit_code=0, duration_ms=12)

    registry = ToolRegistry()
    registry.register(_CommandTool(before=decide_then_complete))

    with request_context(_turn()):
        await registry.execute("run_command", {"command": "uptime"})

    assert store.tool_calls()[0]["gate_decision"] == "allow"


# --- the highest-consequence tool, end to end --------------------------------------------


class _RefusingExecutorClient:
    """Stands in for the executor socket, returning the refusal it returns for a denial."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def execute(self, **kwargs: Any) -> Any:
        from nanoinfra.gates.executor.protocol import ExecuteResponse

        return ExecuteResponse(
            ok=False,
            output="Preview (not executed): server='prod-web-01' command='systemctl reload nginx'",
            exit_code=None,
            error=None,
            reason=self.reason,
        )


async def test_a_refused_remote_execution_lands_on_the_row_as_denied(
    store: LLMUsageStore, tmp_path: Path
) -> None:
    """`execute_on_server` through the real gate runtime: the row reads denied, with the reason.

    The whole join, end to end, for the tool the design names -- and the answer to "who ran
    `execute_on_server`, did it succeed, and why not" from one query.
    """
    from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
    from nanoinfra.config.gates import GatesConfig
    from nanoinfra.gates.runtime import build_gate_runtime

    reason = "an operator denied this action: the change window is closed."
    gate, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    registry = ToolRegistry()
    registry.register(
        ExecuteOnServerTool(
            client=_RefusingExecutorClient(reason),  # pyright: ignore[reportArgumentType]
            gate=gate,
        )
    )

    with request_context(_turn()):
        await registry.execute(
            "execute_on_server",
            {
                "server_id_or_name": "prod-web-01",
                "command": "systemctl reload nginx",
                "dry_run": False,
            },
        )

    row = store.tool_calls()[0]

    assert row["tool"] == "execute_on_server"
    assert row["capability_class"] == "mutate.remote"
    assert row["gate_decision"] == "denied"
    assert row["gate_reason"] == reason
    assert row["outcome"] == "denied"
    assert row["error_kind"] is None
    # The command reached the executor and no part of it reached the row.
    assert "nginx" not in " ".join(str(value) for value in row.values())


async def test_an_allowed_remote_execution_has_no_decision_to_join_yet(
    store: LLMUsageStore, tmp_path: Path
) -> None:
    """The boundary this join stops at, pinned rather than left to be discovered (#261).

    A **refusal** of a remote action is decided again on the agent side -- the tool calls
    `refuse_action` to make it terminal -- so the record lands in this process and the note is
    taken. An **allow** or an **approve** is decided and recorded in the executor process, and
    `ExecuteResponse` carries no decision field, so nothing on this side has anything to note.
    The row is therefore correct about the action and silent about the answer.

    Closing it means carrying the decision (and the approver) back on `ExecuteResponse`, which is
    a change to `nanoinfra/gates/executor/protocol.py` and its server, not to this seam.
    """
    from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
    from nanoinfra.config.gates import GatesConfig
    from nanoinfra.gates.executor.protocol import ExecuteResponse
    from nanoinfra.gates.runtime import build_gate_runtime

    class _AllowingClient:
        def execute(self, **kwargs: Any) -> Any:
            return ExecuteResponse(
                ok=True, output="up 3 days", exit_code=0, error=None, reason=""
            )

    gate, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    registry = ToolRegistry()
    registry.register(
        ExecuteOnServerTool(client=_AllowingClient(), gate=gate)  # pyright: ignore[reportArgumentType]
    )

    with request_context(_turn()):
        await registry.execute(
            "execute_on_server",
            {"server_id_or_name": "prod-web-01", "command": "uptime", "dry_run": False},
        )

    row = store.tool_calls()[0]

    assert row["outcome"] == "ok"
    assert row["gate_decision"] is None


# --- the arguments are addressed, never copied -------------------------------------------


async def test_no_row_holds_the_argument_text_anywhere_in_the_database(
    store: LLMUsageStore, tmp_path: Path
) -> None:
    """Asserted of the database *file*, not of the columns, because a schema can grow one."""
    tool = _CommandTool(
        before=lambda: note_gate_decision(decision="denied", reason="no grant covers this"),
    )
    registry = ToolRegistry()
    registry.register(tool)

    with request_context(_turn()):
        await registry.execute("run_command", {"command": _SECRET_ARGUMENT})

    assert tool.commands == [_SECRET_ARGUMENT], "the tool really received the argument"
    row = store.tool_calls()[0]
    assert row["session_key"] and row["turn_id"] is not None and row["seq"] == 0

    values = " ".join(str(value) for value in row.values())
    assert "hunter2" not in values
    assert "psql" not in values
    store.close()
    blob = Path(store.path).read_bytes()
    for sidecar in ("-wal", "-shm"):
        companion = Path(str(store.path) + sidecar)
        if companion.exists():
            blob += companion.read_bytes()
    assert b"hunter2" not in blob
    assert b"psql" not in blob


async def test_a_failing_store_does_not_break_the_call(
    store: LLMUsageStore, registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry that can break a turn is worse than telemetry that is missing a row."""

    def explode(_: object) -> None:
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(LLMUsageStore, "record_tool_call", explode)

    with request_context(_turn()):
        result = await registry.execute("run_command", {"command": "uptime"})

    assert result == "ran uptime"
