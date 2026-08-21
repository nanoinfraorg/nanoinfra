"""Per-automation state: bounded, isolated, and scoped by the turn rather than by an argument.

Before this, remembering across runs meant asking the model in prose to maintain a JSON file at a
path the operator invented (nanoinfraorg/nanoinfra#158).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent.tools.automation_state import AutomationStateTool
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.automations.state import (
    MAX_KEY_LENGTH,
    MAX_KEYS,
    MAX_VALUE_BYTES,
    AutomationStateError,
    AutomationStateStore,
    AutomationStateTooLargeError,
)
from nanoinfra.cron.session_turns import CRON_TRIGGER_META
from nanoinfra.triggers.local_session_turns import LOCAL_TRIGGER_META


def _cron_ctx(job_id: str) -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
        metadata={CRON_TRIGGER_META: {"job_id": job_id, "job_name": "nightly"}},
    )


def _trigger_ctx(trigger_id: str) -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
        metadata={LOCAL_TRIGGER_META: {"trigger_id": trigger_id, "trigger_name": "ci"}},
    )


# --- store ---


def test_state_round_trips(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)

    store.set("job-a", "reported", [47, 51])

    assert store.get("job-a", "reported") == [47, 51]
    assert store.snapshot("job-a") == {"reported": [47, 51]}


def test_an_automation_cannot_see_another_ones_state(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)

    store.set("job-a", "reported", [47])
    store.set("job-b", "reported", [99])

    assert store.get("job-a", "reported") == [47]
    assert store.get("job-b", "reported") == [99]


def test_an_unknown_automation_reads_as_empty(tmp_path: Path) -> None:
    """First run is the common case, so it must not be an error."""
    store = AutomationStateStore(tmp_path)

    assert store.snapshot("never-ran") == {}
    assert store.get("never-ran", "anything") is None


def test_clear_forgets_everything(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)
    store.set("job-a", "reported", [47])

    assert store.clear("job-a") is True
    assert store.snapshot("job-a") == {}
    assert store.clear("job-a") is False


def test_delete_reports_whether_anything_was_there(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)
    store.set("job-a", "reported", [47])

    assert store.delete("job-a", "reported") is True
    assert store.delete("job-a", "reported") is False


def test_state_survives_a_new_store_instance(tmp_path: Path) -> None:
    AutomationStateStore(tmp_path).set("job-a", "reported", [47])

    assert AutomationStateStore(tmp_path).get("job-a", "reported") == [47]


def test_an_oversized_value_is_refused_not_truncated(tmp_path: Path) -> None:
    """A caller told its write was refused can adapt. One handed altered state cannot."""
    store = AutomationStateStore(tmp_path)

    with pytest.raises(AutomationStateTooLargeError):
        store.set("job-a", "blob", "x" * (MAX_VALUE_BYTES + 1))

    assert store.snapshot("job-a") == {}


def test_the_key_count_is_capped(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)
    for index in range(MAX_KEYS):
        store.set("job-a", f"k{index}", index)

    with pytest.raises(AutomationStateTooLargeError):
        store.set("job-a", "one-too-many", 1)

    # Overwriting an existing key is still allowed at the cap: it does not grow the document.
    store.set("job-a", "k0", 999)
    assert store.get("job-a", "k0") == 999


def test_an_overlong_key_is_rejected(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)

    with pytest.raises(AutomationStateError):
        store.set("job-a", "k" * (MAX_KEY_LENGTH + 1), 1)


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_a_missing_automation_id_is_rejected(tmp_path: Path, bad_id: str) -> None:
    store = AutomationStateStore(tmp_path)

    with pytest.raises(AutomationStateError):
        store.set(bad_id, "k", 1)


def test_an_id_cannot_escape_the_state_directory(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)

    store.set("../../escape", "k", 1)

    assert not (tmp_path.parent / "escape.json").exists()
    written = list(store.root.glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == store.root


def test_a_corrupted_document_reads_as_empty(tmp_path: Path) -> None:
    """One truncated write must not make an automation unable to run again."""
    store = AutomationStateStore(tmp_path)
    store.set("job-a", "reported", [47])
    store._path("job-a").write_text("{not json", encoding="utf-8")

    assert store.snapshot("job-a") == {}
    # And it recovers on the next write rather than staying broken.
    store.set("job-a", "reported", [51])
    assert store.get("job-a", "reported") == [51]


def test_a_non_serialisable_value_is_rejected(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)

    with pytest.raises(AutomationStateError):
        store.set("job-a", "k", object())


def test_the_document_carries_a_version(tmp_path: Path) -> None:
    store = AutomationStateStore(tmp_path)
    store.set("job-a", "reported", [47])

    raw = json.loads(store._path("job-a").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["values"] == {"reported": [47]}


# --- tool ---


async def test_the_tool_refuses_on_an_interactive_turn(tmp_path: Path) -> None:
    """There is no automation to be scoped to, so writing anywhere would be arbitrary."""
    tool = AutomationStateTool(tmp_path)
    ctx = RequestContext(channel="websocket", chat_id="chat-1", session_key="websocket:chat-1")

    with request_context(ctx):
        result = await tool.execute(action="list")

    assert result.is_error
    assert "scheduled or triggered turn" in str(result)


async def test_the_tool_refuses_with_no_bound_context(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    result = await tool.execute(action="list")

    assert result.is_error


async def test_the_tool_scopes_writes_to_the_running_cron_job(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    with request_context(_cron_ctx("job-a")):
        assert not (await tool.execute(action="set", key="reported", value="[47, 51]")).is_error

    store = AutomationStateStore(tmp_path)
    assert store.get("job-a", "reported") == [47, 51]
    assert store.snapshot("job-b") == {}


async def test_the_tool_scopes_writes_to_the_running_trigger(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    with request_context(_trigger_ctx("tdl-1")):
        assert not (await tool.execute(action="set", key="seen", value='"yes"')).is_error

    assert AutomationStateStore(tmp_path).get("tdl-1", "seen") == "yes"


async def test_one_automation_cannot_reach_another_through_the_tool(tmp_path: Path) -> None:
    """The id is not a parameter, so there is nothing to point elsewhere."""
    tool = AutomationStateTool(tmp_path)
    AutomationStateStore(tmp_path).set("job-b", "secret", "theirs")

    with request_context(_cron_ctx("job-a")):
        listed = await tool.execute(action="list")

    assert "theirs" not in str(listed)


async def test_a_missing_key_is_distinguished_from_a_stored_null(tmp_path: Path) -> None:
    """First run has to be tellable apart from "set to nothing"."""
    tool = AutomationStateTool(tmp_path)

    with request_context(_cron_ctx("job-a")):
        missing = await tool.execute(action="get", key="reported")
        await tool.execute(action="set", key="reported", value="null")
        stored = await tool.execute(action="get", key="reported")

    assert "Nothing is stored" in str(missing)
    assert str(stored) == "null"


async def test_a_plain_string_does_not_need_quoting(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    with request_context(_cron_ctx("job-a")):
        await tool.execute(action="set", key="phase", value="done")

    assert AutomationStateStore(tmp_path).get("job-a", "phase") == "done"


async def test_the_tool_reports_a_refused_write_as_an_error(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    with request_context(_cron_ctx("job-a")):
        result = await tool.execute(action="set", key="blob", value="x" * (MAX_VALUE_BYTES + 1))

    assert result.is_error
    assert "limit" in str(result)


async def test_delete_and_list_through_the_tool(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    with request_context(_cron_ctx("job-a")):
        await tool.execute(action="set", key="a", value="1")
        await tool.execute(action="set", key="b", value="2")
        listed = await tool.execute(action="list")
        removed = await tool.execute(action="delete", key="a")
        after = await tool.execute(action="list")

    assert json.loads(str(listed)) == {"a": 1, "b": 2}
    assert "Forgot 'a'" in str(removed)
    assert json.loads(str(after)) == {"b": 2}


async def test_an_unknown_action_is_an_error(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    with request_context(_cron_ctx("job-a")):
        result = await tool.execute(action="obliterate")

    assert result.is_error


def test_per_action_requirements_are_validated(tmp_path: Path) -> None:
    tool = AutomationStateTool(tmp_path)

    assert tool.validate_params({"action": "get"})
    assert tool.validate_params({"action": "set", "key": "a"})
    assert tool.validate_params({"action": "delete"})
    assert not tool.validate_params({"action": "list"})
    assert not tool.validate_params({"action": "set", "key": "a", "value": "1"})
