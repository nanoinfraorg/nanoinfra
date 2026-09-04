"""Every job may name an agent, and that agent is the ceiling (#257).

A job used to run an unattended turn with the deployment's default agent and a list of skills of
its own. Naming an agent narrows it. The rule under test, in one line:

    The agent sets the ceiling and the job may only narrow it.

Which is why the widening cases are refusals rather than merges: if a job could name a small agent
and then ask for a tool that agent does not have, naming an agent would become the way around its
configuration -- the same laundering the delegation design refuses, in a different costume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.tools import groups
from nanoinfra.agent.tools.base import Tool
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.config.schema import NamedAgentConfig
from nanoinfra.cron.bound_runner import build_bound_turn
from nanoinfra.cron.service import CronJobTerminalError, CronService
from nanoinfra.cron.types import CronJob, CronPayload, CronSchedule
from nanoinfra.session.automation_turns import (
    AUTOMATION_AGENT_META,
    AUTOMATION_SKILLS_META,
    TURN_AGENT_META,
    automation_agent_tool_groups,
    turn_agent,
)


def _roster(**agents: NamedAgentConfig) -> dict[str, NamedAgentConfig]:
    return dict(agents)


def _sre() -> NamedAgentConfig:
    """An agent scoped to the Servers surface, and told what it is for."""
    return NamedAgentConfig(
        description="Checks hosts",
        toolGroups=["servers"],
        skills=["github", "cron"],
        addendum="Only ever inspect. Never restart a service.",
    )


def _job(**overrides: Any) -> CronJob:
    fields: dict[str, Any] = {
        "id": "job-1",
        "name": "apt-package-check-daily",
        "schedule": CronSchedule(kind="every", every_ms=3_600_000),
        "payload": CronPayload(
            kind="agent_turn",
            message="Check for pending package updates",
            session_key="websocket:chat-1",
            origin_channel="websocket",
            origin_chat_id="chat-1",
        ),
    }
    fields.update(overrides)
    return CronJob(**fields)


def _bound(turn: Any) -> RequestContext:
    """The context the loop binds for a turn like this one.

    ``agent`` is the *resolved* name: ``AgentLoop._acting_agent_for`` reads it off the metadata
    and checks it against the roster before the turn runs. Rebuilding that here rather than
    asserting on the metadata alone is what keeps these tests honest about the seam.
    """
    return RequestContext(
        channel="cron",
        chat_id="chat-1",
        metadata=turn.metadata,
        agent=turn_agent(turn.metadata),
    )


def _service(tmp_path: Path, roster: dict[str, NamedAgentConfig] | None = None) -> CronService:
    # The roster is injected rather than read from config, because a test must not depend on the
    # machine's `~/.nanoinfra/config.json` -- and because injecting it is how the gateway will
    # hand the service the agents it booted with.
    return CronService(tmp_path / "cron" / "jobs.json", named_agents=roster or {})


# --- 1. a job may name an agent -------------------------------------------------------------


def test_a_job_that_names_no_agent_behaves_exactly_as_it_did_before(tmp_path: Path) -> None:
    """Every job in every store today names nothing, so this is the whole existing fleet."""
    turn = build_bound_turn(_job(skills=["github"]), named_agents=_roster(sre=_sre()))

    # Absent, not blank: absent and "the deployment's default agent" have to be one state, or
    # the loop's resolver and this record would have two ways to spell the same thing.
    assert TURN_AGENT_META not in turn.metadata
    assert AUTOMATION_AGENT_META not in turn.metadata
    assert turn.metadata[AUTOMATION_SKILLS_META] == ["github"]
    assert "You are" not in turn.prompt
    assert turn_agent(turn.metadata) is None
    # `None`, not `()`: no ceiling was declared. An empty tuple now means "declared, and it is
    # empty" -- a real narrowing, and the opposite of what this job wants.
    assert automation_agent_tool_groups(turn.metadata) is None


def test_a_named_agent_survives_a_round_trip_through_the_store(tmp_path: Path) -> None:
    service = _service(tmp_path, _roster(sre=_sre()))

    job = service.add_job(
        name="Host check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the host",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        agent="sre",
    )

    assert job.agent == "sre"
    reloaded = _service(tmp_path, _roster(sre=_sre()))
    reloaded._load_store()
    stored = reloaded.get_job(job.id)
    assert stored is not None
    assert stored.agent == "sre"


def test_a_job_written_before_agents_existed_names_none(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "legacy",
                        "name": "legacy",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 3_600_000},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "hello",
                            "sessionKey": "websocket:chat-1",
                            "originChannel": "websocket",
                            "originChatId": "chat-1",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    service = _service(tmp_path)
    service._load_store()
    stored = service.get_job("legacy")

    assert stored is not None
    assert stored.agent == ""


def test_an_unknown_agent_is_refused_when_the_job_is_saved(tmp_path: Path) -> None:
    """Named in the message, because "unknown agent" in a 03:00 log is not actionable."""
    service = _service(tmp_path, _roster(sre=_sre()))

    with pytest.raises(ValueError, match="'ghost'"):
        service.add_job(
            name="Host check",
            schedule=CronSchedule(kind="every", every_ms=3_600_000),
            message="Check the host",
            session_key="websocket:chat-1",
            origin_channel="websocket",
            origin_chat_id="chat-1",
            agent="ghost",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_an_unknown_agent_on_an_update_leaves_the_job_untouched(tmp_path: Path) -> None:
    """The refusal is raised before a field is assigned: this method edits the live store, so a
    half-applied edit would be an edit nobody accepted."""
    service = _service(tmp_path, _roster(sre=_sre()))
    job = service.add_job(
        name="Host check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the host",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        agent="sre",
    )

    with pytest.raises(ValueError, match="'ghost'"):
        service.update_job(job.id, agent="ghost", name="Renamed")

    stored = service.get_job(job.id)
    assert stored is not None
    assert stored.agent == "sre"
    assert stored.name == "Host check"


def test_the_editor_can_clear_the_agent_it_named(tmp_path: Path) -> None:
    """An empty string is how the picker says "the deployment's default agent", so the field is
    removable without a second verb for "unset"."""
    from nanoinfra.cron.agent_binding import parse_automation_agent

    assert parse_automation_agent({}) == {}
    assert parse_automation_agent({"agent": " sre "}) == {"agent": "sre"}
    assert parse_automation_agent({"agent": ""}) == {"agent": ""}
    assert parse_automation_agent({"agent": 7}) == "agent must be a string"

    service = _service(tmp_path, _roster(sre=_sre()))
    job = service.add_job(
        name="Host check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the host",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        agent="sre",
    )

    assert service.update_job(job.id, agent="") != "not_found"
    stored = service.get_job(job.id)
    assert stored is not None
    assert stored.agent == ""


# --- 2. the agent is a ceiling ---------------------------------------------------------------


def test_a_job_with_an_agent_runs_with_that_agents_tool_groups() -> None:
    turn = build_bound_turn(_job(agent="sre"), named_agents=_roster(sre=_sre()))

    # The name travels on the seam a person's chosen agent uses, so `AgentLoop` resolves it
    # against the roster and the turn record says which agent answered.
    assert turn.metadata[TURN_AGENT_META] == "sre"
    assert turn_agent(turn.metadata) == "sre"
    assert automation_agent_tool_groups(turn.metadata) == ("servers",)


def test_a_turn_bound_to_an_agent_keeps_only_that_agents_tools() -> None:
    """The ceiling is asked of the built-in groups even when config declares none, because a
    deployment that declared nothing still ships diagram tools and server tools."""
    turn = build_bound_turn(_job(agent="sre"), named_agents=_roster(sre=_sre()))

    with request_context(_bound(turn)):
        assert groups.is_attached("execute_on_server") is True
        assert groups.is_attached("create_diagram") is False
        # A tool in no group at all stays: groups cover surfaces, not the whole tool set, so
        # "ungrouped" cannot mean "denied" or an agent could not read a file.
        assert groups.is_attached("read_file") is True


def test_a_default_agent_turn_keeps_the_tools_the_agent_turn_lost() -> None:
    """The other half of the test above: without an agent nothing is capped, which is the
    behaviour every deployment has today."""
    turn = build_bound_turn(_job(), named_agents=_roster(sre=_sre()))

    with request_context(_bound(turn)):
        assert groups.is_attached("create_diagram") is True
        assert groups.is_attached("execute_on_server") is True


def test_an_agent_reaches_the_groups_it_declared_even_when_they_wait_to_be_mentioned() -> None:
    """A `mention` group would otherwise withhold its schemas from the agent whose whole tool set
    it is, and an unattended turn has nobody to type `@servers`."""
    from nanoinfra.config.schema import ToolGroupConfig

    try:
        groups.set_tool_groups({"servers": ToolGroupConfig(attach="mention")})
        turn = build_bound_turn(_job(agent="sre"), named_agents=_roster(sre=_sre()))

        with request_context(_bound(turn)):
            assert groups.is_attached("execute_on_server") is True
    finally:
        groups.set_tool_groups(None)


def test_the_ceiling_follows_a_group_the_deployment_declared_itself() -> None:
    """A group config declares governs, so an operator who regrouped their tools gets a ceiling
    over *their* groups rather than over the shipped ones."""
    from nanoinfra.config.schema import ToolGroupConfig

    try:
        groups.set_tool_groups({"reporting": ToolGroupConfig(tools=["list_diagrams"])})
        agent = NamedAgentConfig(description="Reads diagrams", toolGroups=["reporting"])
        turn = build_bound_turn(_job(agent="reporter"), named_agents=_roster(reporter=agent))

        with request_context(_bound(turn)):
            assert groups.is_attached("list_diagrams") is True
            # Still in the shipped `diagrams` group, which this agent did not declare.
            assert groups.is_attached("create_diagram") is False
    finally:
        # Module state, like the MCP and connector sides: leaving it set would give the next test
        # a deployment it never configured.
        groups.set_tool_groups(None)


class _Stub(Tool):
    """A tool that would run if it were reached. It must not be."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "a tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover - the point is it is not
        raise AssertionError("a tool outside the ceiling was executed")


def test_a_tool_outside_the_agents_groups_cannot_be_called_by_name() -> None:
    """Withholding the schema is not enough on its own. A model that names a tool it never saw
    must be refused, or "the agent is the ceiling" would mean "the agent is undocumented"."""
    registry = ToolRegistry()
    registry.register(_Stub("create_diagram"))
    registry.register(_Stub("execute_on_server"))
    turn = build_bound_turn(_job(agent="sre"), named_agents=_roster(sre=_sre()))

    with request_context(_bound(turn)):
        _tool, _params, error = registry.prepare_call("create_diagram", {})
        assert error is not None
        assert "sre" in str(error)
        allowed, _params, no_error = registry.prepare_call("execute_on_server", {})
        assert no_error is None
        assert allowed is not None


def test_the_agents_own_instructions_reach_the_prompt() -> None:
    turn = build_bound_turn(_job(agent="sre"), named_agents=_roster(sre=_sre()))

    assert "Only ever inspect. Never restart a service." in turn.prompt
    # Appended to the platform's text, never substituted for it: the job's own instruction and
    # the rules around it are still there.
    assert "Check for pending package updates" in turn.prompt


def test_a_run_stops_when_its_agent_is_no_longer_configured() -> None:
    """Terminal rather than a retry, and never a fall back to the default agent -- falling back
    would widen a job somebody narrowed on purpose."""
    with pytest.raises(CronJobTerminalError, match="'sre'"):
        build_bound_turn(_job(agent="sre"), named_agents={})


def test_changing_the_agent_invalidates_what_a_rehearsal_found() -> None:
    """A commissioning finding describes what an automation will do. Swapping the agent changes
    the tools it may call and the instructions it carries, so the old verdict no longer applies
    -- while a job that names no agent keeps the fingerprint it already has on disk."""
    from nanoinfra.automations.commissioning_state import commissioning_fingerprint

    no_agent = commissioning_fingerprint(message="Check the host", skills=["github"])

    assert no_agent == commissioning_fingerprint(
        message="Check the host", skills=["github"], agent=""
    )
    assert no_agent != commissioning_fingerprint(
        message="Check the host", skills=["github"], agent="sre"
    )
    assert commissioning_fingerprint(message="m", agent="sre") != commissioning_fingerprint(
        message="m", agent="db-expert"
    )


# --- 3. the skills picker narrows -----------------------------------------------------------


def test_a_job_may_narrow_the_skills_its_agent_carries(tmp_path: Path) -> None:
    service = _service(tmp_path, _roster(sre=_sre()))

    job = service.add_job(
        name="Host check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the host",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        agent="sre",
        skills=["github"],
    )

    turn = build_bound_turn(job, named_agents=_roster(sre=_sre()))
    assert turn.metadata[AUTOMATION_SKILLS_META] == ["github"]


def test_a_job_that_picks_no_skill_carries_its_agents_list() -> None:
    """Empty is not "none" here: the picker narrows the agent's list, so picking nothing means
    the agent's own skills rather than the whole catalogue."""
    turn = build_bound_turn(_job(agent="sre"), named_agents=_roster(sre=_sre()))

    assert turn.metadata[AUTOMATION_SKILLS_META] == ["github", "cron"]


def test_a_skill_the_agent_does_not_have_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path, _roster(sre=_sre()))

    with pytest.raises(ValueError, match="'terraform'"):
        service.add_job(
            name="Host check",
            schedule=CronSchedule(kind="every", every_ms=3_600_000),
            message="Check the host",
            session_key="websocket:chat-1",
            origin_channel="websocket",
            origin_chat_id="chat-1",
            agent="sre",
            skills=["terraform"],
        )


def test_adding_a_skill_outside_the_agent_is_refused_on_an_update(tmp_path: Path) -> None:
    """The widening can arrive from either side of the pair, so the check runs on what the job
    would become rather than on the field that changed."""
    service = _service(tmp_path, _roster(sre=_sre()))
    job = service.add_job(
        name="Host check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the host",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        agent="sre",
    )

    with pytest.raises(ValueError, match="'terraform'"):
        service.update_job(job.id, skills=["terraform"])

    stored = service.get_job(job.id)
    assert stored is not None
    assert stored.skills == []


def test_an_agent_that_declares_no_skills_leaves_the_picker_alone(tmp_path: Path) -> None:
    """An agent with no skill list summarises the whole catalogue, so there is no ceiling to
    narrow and the job's picker is the independent list it always was."""
    service = _service(tmp_path, _roster(anything=NamedAgentConfig(description="Broad")))

    job = service.add_job(
        name="Host check",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check the host",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        agent="anything",
        skills=["terraform"],
    )

    assert job.skills == ["terraform"]
