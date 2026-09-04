"""A local trigger may name an agent, and that agent is the ceiling (#257).

The cron half of this rule is tested in ``tests/automations/test_job_agent.py``. What these tests
are for is the half that could drift: a trigger is the *other* way an unattended turn starts, and
it must not answer "what may this automation reach" differently. So the refusal, the binding and
the ceiling all come from ``nanoinfra/cron/agent_binding.py``, and these tests exercise them
through the trigger's own store and queue.

One difference is real and deliberate: a trigger's message comes from whatever fired it, so the
agent's own instructions go *in front* of that text rather than after it.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import pytest

from nanoinfra.agent.tools import groups
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.config.schema import NamedAgentConfig
from nanoinfra.session.automation_turns import (
    AUTOMATION_AGENT_META,
    AUTOMATION_SKILLS_META,
    TURN_AGENT_META,
    automation_agent_tool_groups,
    turn_agent,
)
from nanoinfra.triggers.local_runner import run_local_trigger_queue
from nanoinfra.triggers.local_store import LocalTriggerStore
from nanoinfra.triggers.local_types import LocalTrigger


def _channel_is_enabled(_name: str) -> bool:
    return True


def _sre() -> NamedAgentConfig:
    """An agent scoped to the Servers surface, and told what it is for."""
    return NamedAgentConfig(
        description="Checks hosts",
        toolGroups=["servers"],
        skills=["github", "cron"],
        addendum="Only ever inspect. Never restart a service.",
    )


def _store(tmp_path: Path, **agents: NamedAgentConfig) -> LocalTriggerStore:
    # The roster is injected rather than read from config, because a test must not depend on the
    # machine's own `~/.nanoinfra/config.json`.
    return LocalTriggerStore(tmp_path, named_agents=dict(agents))


def _trigger(store: LocalTriggerStore) -> LocalTrigger:
    return store.create(
        name="CI review",
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )


async def _run_one_delivery(store: LocalTriggerStore) -> list[InboundMessage]:
    """Drain the queue until one turn is submitted, and return what was submitted."""
    submitted: list[InboundMessage] = []

    async def _submit_turn(msg: InboundMessage):
        submitted.append(msg)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="done")

    task = asyncio.create_task(
        run_local_trigger_queue(
            store=store,
            submit_turn=_submit_turn,
            is_channel_enabled=_channel_is_enabled,
            poll_interval_s=0.01,
        )
    )
    try:
        for _ in range(200):
            if submitted:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    return submitted


def _bound(msg: InboundMessage) -> RequestContext:
    """The context the loop binds for a turn like this one.

    ``agent`` is the *resolved* name: ``AgentLoop._acting_agent_for`` reads it off the metadata and
    checks it against the roster before the turn runs.
    """
    return RequestContext(
        channel=msg.channel,
        chat_id=msg.chat_id,
        metadata=msg.metadata,
        agent=turn_agent(msg.metadata),
    )


# --- the record -------------------------------------------------------------------------------


def test_a_named_agent_survives_a_round_trip_through_the_store(tmp_path: Path) -> None:
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)

    assert store.update(trigger.id, agent="sre") is not None

    reloaded = _store(tmp_path, sre=_sre()).get(trigger.id)
    assert reloaded is not None
    assert reloaded.agent == "sre"


def test_a_trigger_written_before_agents_existed_names_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trigger = _trigger(store)
    stored = json.loads(store.store_path.read_text(encoding="utf-8"))
    for entry in stored["triggers"]:
        entry.pop("agent", None)
    store.store_path.write_text(json.dumps(stored), encoding="utf-8")

    reloaded = _store(tmp_path).get(trigger.id)
    assert reloaded is not None
    assert reloaded.agent == ""


def test_an_unknown_agent_is_refused_and_leaves_the_trigger_untouched(tmp_path: Path) -> None:
    """Named in the message, because "unknown agent" in a log is not a reason anybody can act
    on -- and the refusal lands before a field is assigned, so nothing half-applied is saved."""
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)
    assert store.update(trigger.id, agent="sre") is not None

    with pytest.raises(ValueError, match="'ghost'"):
        store.update(trigger.id, agent="ghost", name="Renamed")

    stored = _store(tmp_path, sre=_sre()).get(trigger.id)
    assert stored is not None
    assert stored.agent == "sre"
    assert stored.name == "CI review"


def test_a_skill_the_agent_does_not_have_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)
    assert store.update(trigger.id, agent="sre") is not None

    with pytest.raises(ValueError, match="'terraform'"):
        store.update(trigger.id, skills=["terraform"])

    stored = store.get(trigger.id)
    assert stored is not None
    assert stored.skills == []


def test_the_editor_can_clear_the_agent_it_named(tmp_path: Path) -> None:
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)
    assert store.update(trigger.id, agent="sre") is not None

    assert store.update(trigger.id, agent="") is not None

    stored = store.get(trigger.id)
    assert stored is not None
    assert stored.agent == ""


# --- the turn ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_trigger_that_names_no_agent_behaves_exactly_as_it_did_before(
    tmp_path: Path,
) -> None:
    """Every trigger in every store today names nothing, so this is the whole existing fleet."""
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)
    assert store.update(trigger.id, skills=["github"]) is not None
    store.enqueue(trigger.id, "Review PR #4502")

    submitted = await _run_one_delivery(store)

    assert len(submitted) == 1
    msg = submitted[0]
    # Absent, not blank: absent and "the deployment's default agent" have to be one state.
    assert TURN_AGENT_META not in msg.metadata
    assert AUTOMATION_AGENT_META not in msg.metadata
    assert msg.metadata[AUTOMATION_SKILLS_META] == ["github"]
    assert msg.content == "Review PR #4502"
    with request_context(_bound(msg)):
        assert groups.is_attached("create_diagram") is True
        assert groups.is_attached("execute_on_server") is True


@pytest.mark.asyncio
async def test_a_trigger_with_an_agent_runs_as_that_agent(tmp_path: Path) -> None:
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)
    assert store.update(trigger.id, agent="sre") is not None
    store.enqueue(trigger.id, "Review PR #4502")

    submitted = await _run_one_delivery(store)

    assert len(submitted) == 1
    msg = submitted[0]
    # The name travels on the seam a person's chosen agent uses, so the loop resolves it against
    # the roster and the turn record says which agent answered.
    assert msg.metadata[TURN_AGENT_META] == "sre"
    assert automation_agent_tool_groups(msg.metadata) == ("servers",)
    # Picking no skill means the agent's own list, not the whole catalogue.
    assert msg.metadata[AUTOMATION_SKILLS_META] == ["github", "cron"]
    # The agent's instructions precede the message, because the message came from whatever fired
    # the trigger and trusted text has to come before untrusted text.
    assert msg.content.startswith("You are `sre`")
    assert msg.content.endswith("Review PR #4502")


@pytest.mark.asyncio
async def test_a_trigger_turn_keeps_only_its_agents_tools(tmp_path: Path) -> None:
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)
    assert store.update(trigger.id, agent="sre") is not None
    store.enqueue(trigger.id, "Draw me a diagram")

    submitted = await _run_one_delivery(store)

    with request_context(_bound(submitted[0])):
        assert groups.is_attached("execute_on_server") is True
        assert groups.is_attached("create_diagram") is False
        # A tool in no group at all stays: groups cover surfaces, not the whole tool set.
        assert groups.is_attached("read_file") is True


@pytest.mark.asyncio
async def test_a_delivery_stops_when_its_agent_is_no_longer_configured(tmp_path: Path) -> None:
    """Terminal rather than a retry, and never a fall back to the default agent -- falling back
    would widen a trigger somebody narrowed on purpose."""
    store = _store(tmp_path, sre=_sre())
    trigger = _trigger(store)
    assert store.update(trigger.id, agent="sre") is not None
    store.enqueue(trigger.id, "Review PR #4502")

    # The agent is gone from config by the time the queue runs, which is the case a retry cannot
    # fix: the name has to be repointed or removed.
    forgetful = LocalTriggerStore(tmp_path, named_agents={})
    submitted = await _run_one_delivery(forgetful)

    assert submitted == []
    stored = forgetful.get(trigger.id)
    assert stored is not None
    assert stored.last_status == "error"
    assert "'sre'" in (stored.last_error or "")
    assert forgetful.claim_deliveries() == []
