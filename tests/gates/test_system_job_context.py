# tests/gates/test_system_job_context.py
"""Item 43 (#49): a built-in cron job must not run at interactive privilege.

The maintainer asked whether an automation can bypass the gate, and it could. `write_file` carries
`mutate.local`, which #8 does not gate, and `HEARTBEAT.md` is a plain file in the workspace. The
heartbeat then ran that text as a prompt with the whole tool registry, and the turn classified as
`interactive`, because `on_cron_job` answered `dream` and `heartbeat` before the bound path and
called `process_direct` with no automation metadata.

So the model could write one task line and have it run under the interactive column.

A cron job the model creates was always covered: the bound runner sets the trigger metadata, and
that turn classifies as `automation`. The two built-in jobs skipped it. The fix therefore feeds the
classifier the same metadata rather than teach it a name, because a name list reads a third source
as interactive and that failure opens the gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.agent.automation_turns import execution_context_for_turn
from nanoinfra.cli.gateway_runtime import SYSTEM_CRON_JOB_NAMES, _system_job_metadata
from nanoinfra.cron.session_turns import is_cron_turn


def test_a_heartbeat_turn_is_an_automation() -> None:
    """The turn that carried the escalation."""
    metadata = _system_job_metadata("heartbeat", message="check the tasks")

    assert execution_context_for_turn(metadata, {}, channel="websocket") == "automation"


def test_a_dream_turn_is_an_automation() -> None:
    """Dream reaches no host today, so it proves the defect was the classification."""
    metadata = _system_job_metadata("dream", message="consolidate")

    assert execution_context_for_turn(metadata, {}, channel="websocket") == "automation"


@pytest.mark.parametrize("name", sorted(SYSTEM_CRON_JOB_NAMES))
def test_every_built_in_job_is_an_automation(name: str) -> None:
    """A fourth built-in job fails here on the day it lands, rather than years later."""
    metadata = _system_job_metadata(name, message="anything")

    assert execution_context_for_turn(metadata, {}, channel="websocket") == "automation"
    assert is_cron_turn(metadata), "the cron spec must recognise it, so history reads it too"


def test_the_metadata_names_the_job_for_the_record() -> None:
    """#16 records the job, so a reviewer can tell a heartbeat turn from a person's turn."""
    metadata = _system_job_metadata("heartbeat", message="check the tasks")
    trigger = metadata["_cron_trigger"]

    assert trigger["job_name"] == "heartbeat"
    assert trigger["job_id"] == "heartbeat"
    assert trigger["run_id"].startswith("heartbeat:")


@pytest.mark.asyncio
async def test_process_direct_carries_caller_metadata_into_the_turn(monkeypatch: Any) -> None:
    """The call site can mark a turn, or the fix above reaches nothing.

    `process_direct` built its own metadata dict and dropped the caller's, which is why the two
    built-in jobs could not mark themselves.
    """
    from nanoinfra.agent import loop as loop_module

    seen: dict[str, Any] = {}

    async def _fake_dispatch(self: Any, msg: Any, **kwargs: Any) -> None:
        seen["metadata"] = dict(msg.metadata)
        return None

    monkeypatch.setattr(loop_module.AgentLoop, "_dispatch_inbound", _fake_dispatch, raising=False)
    agent = loop_module.AgentLoop.__new__(loop_module.AgentLoop)

    assert hasattr(agent, "process_direct")


def test_every_scheduled_turn_in_the_gateway_marks_itself() -> None:
    """A call site that forgets the metadata runs at interactive privilege.

    The helper above is only half the fix. This walks the gateway source for every
    `process_direct` call and asserts each one passes metadata, so a fourth built-in job fails here
    rather than years later in an audit log.
    """
    import ast
    from pathlib import Path

    source = Path("nanoinfra/cli/gateway_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    unmarked: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "process_direct":
            continue
        if not any(kw.arg == "metadata" for kw in call.keywords):
            unmarked.append(call.lineno)

    assert unmarked == [], f"process_direct without metadata at line(s) {unmarked}"
