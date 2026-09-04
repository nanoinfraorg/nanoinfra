"""Which agent answered a turn, recorded rather than inferred (#248).

This has to ship with or before the composer's agent selector. If the selector arrives first, a
thread can hold turns from two agents with nothing in the record saying which was which -- and that
history cannot be reconstructed afterwards, because nothing else in a row distinguishes them.

Three properties, one per layer: config decides who may act, the turn carries it, and the reload
shows what the live turn showed.
"""

from __future__ import annotations

from typing import Any

# --- config decides, the client only asks ----------------------------------------------------
from nanoinfra.agent.loop import AgentLoop
from nanoinfra.bus.runtime_events import RuntimeEventPublisher
from nanoinfra.webui.transcript import replay_transcript_to_ui_messages


class _Loop:
    """The resolver under test, with the one field it reads.

    Constructed directly rather than through `AgentLoop.from_config`, because the rule is about
    the resolver and building a loop would drag a provider, a bus and a workspace into a test of
    four lines of logic.
    """

    def __init__(self, named: dict[str, object]) -> None:
        self.named_agents = named

    resolve = None
    # The text half of the resolver (#269). Borrowed rather than stubbed, so these tests keep
    # covering the metadata rule while the mention rule is covered next door in
    # `test_agent_mention_routing.py`.
    _agent_from_mention = AgentLoop._agent_from_mention


def _resolve(named: dict[str, object], metadata: dict[str, Any] | None) -> str | None:
    loop = _Loop(named)
    return AgentLoop._acting_agent_for(loop, metadata)  # type: ignore[arg-type]


def test_a_turn_that_names_no_agent_is_the_default_agent() -> None:
    """Which is every turn in every deployment today."""
    assert _resolve({"sre": object()}, None) is None
    assert _resolve({"sre": object()}, {}) is None


def test_a_turn_naming_a_configured_agent_is_answered_by_it() -> None:
    assert _resolve({"sre": object()}, {"agent": "sre"}) == "sre"


def test_a_name_the_deployment_never_configured_falls_back_to_the_default() -> None:
    """A name is a *request*; the authority to act as an agent is the roster in config.

    So an invented name gets the default agent -- which grants nothing extra -- rather than an
    error that would tell a caller which names exist.
    """
    assert _resolve({"sre": object()}, {"agent": "root"}) is None
    assert _resolve({}, {"agent": "sre"}) is None


def test_a_non_string_agent_is_ignored_rather_than_coerced() -> None:
    for junk in (7, True, ["sre"], {"name": "sre"}, ""):
        assert _resolve({"sre": object()}, {"agent": junk}) is None, junk


# --- the turn carries it, per session ---------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


async def test_the_completed_turn_names_the_agent_that_answered() -> None:
    bus = _Bus()
    publisher = RuntimeEventPublisher(bus)  # type: ignore[arg-type]

    publisher.record_turn_agent("ws:c1", "sre-prod")
    await publisher.turn_completed(
        channel="websocket", chat_id="c1", session_key="ws:c1", metadata={}
    )

    assert bus.published[-1].agent == "sre-prod"


async def test_the_default_agent_records_nothing_to_report() -> None:
    """So the frame omits the field rather than carrying a name config never declared."""
    bus = _Bus()
    publisher = RuntimeEventPublisher(bus)  # type: ignore[arg-type]

    publisher.record_turn_agent("ws:c1", None)
    await publisher.turn_completed(
        channel="websocket", chat_id="c1", session_key="ws:c1", metadata={}
    )

    assert bus.published[-1].agent is None


async def test_two_sessions_mid_turn_do_not_borrow_each_others_agent() -> None:
    """Per session for the same reason usage and latency are: one loop-global value would
    attribute one chat's turn to the other chat's agent."""
    bus = _Bus()
    publisher = RuntimeEventPublisher(bus)  # type: ignore[arg-type]

    publisher.record_turn_agent("ws:a", "sre-prod")
    publisher.record_turn_agent("ws:b", "db-oncall")
    await publisher.turn_completed(
        channel="websocket", chat_id="b", session_key="ws:b", metadata={}
    )
    await publisher.turn_completed(
        channel="websocket", chat_id="a", session_key="ws:a", metadata={}
    )

    assert [event.agent for event in bus.published[-2:]] == ["db-oncall", "sre-prod"]


async def test_a_turn_consumes_its_agent_so_the_next_one_starts_clean() -> None:
    """Otherwise a turn answered by the default agent would inherit the previous turn's name --
    which is exactly the misattribution this feature exists to prevent."""
    bus = _Bus()
    publisher = RuntimeEventPublisher(bus)  # type: ignore[arg-type]

    publisher.record_turn_agent("ws:c1", "sre-prod")
    await publisher.turn_completed(
        channel="websocket", chat_id="c1", session_key="ws:c1", metadata={}
    )
    await publisher.turn_completed(
        channel="websocket", chat_id="c1", session_key="ws:c1", metadata={}
    )

    assert bus.published[-1].agent is None


def test_clearing_a_turn_drops_the_agent_with_the_rest_of_it() -> None:
    bus = _Bus()
    publisher = RuntimeEventPublisher(bus)  # type: ignore[arg-type]

    publisher.record_turn_agent("ws:c1", "sre-prod")
    publisher.clear_turn("ws:c1")

    assert publisher._turn_agent == {}


# --- the reload shows what the live turn showed -----------------------------------------------


def _thread(**turn_end: Any) -> list[dict[str, Any]]:
    return [
        {"event": "message", "chat_id": "c", "role": "user", "text": "check db-01",
         "turn_id": "t1"},
        {"event": "message", "chat_id": "c", "role": "assistant", "text": "disk is at 71%",
         "turn_id": "t1"},
        {"event": "turn_end", "chat_id": "c", "turn_id": "t1", "latency_ms": 900, **turn_end},
    ]


def test_a_reloaded_turn_still_names_the_agent_that_answered() -> None:
    messages = replay_transcript_to_ui_messages(_thread(agent="sre-prod"))

    answers = [m for m in messages if m.get("role") == "assistant" and m.get("kind") != "trace"]
    assert answers[-1]["agent"] == "sre-prod"
    # On the answer, not on every row of the turn: one turn, one attribution.
    assert [m.get("agent") for m in answers].count("sre-prod") == 1


def test_a_reloaded_turn_from_the_default_agent_carries_no_name() -> None:
    messages = replay_transcript_to_ui_messages(_thread())

    answers = [m for m in messages if m.get("role") == "assistant" and m.get("kind") != "trace"]
    assert "agent" not in answers[-1]


def test_a_thread_that_switched_agents_keeps_both_turns_attributed() -> None:
    """The case that makes the whole feature load-bearing: switching mid-thread is allowed, and
    without a per-turn record a reader has to infer from the model or the tools."""
    records = _thread(agent="sre-prod")
    records += [
        {"event": "message", "chat_id": "c", "role": "user", "text": "and the replica?",
         "turn_id": "t2"},
        {"event": "message", "chat_id": "c", "role": "assistant", "text": "lag is 40ms",
         "turn_id": "t2"},
        {"event": "turn_end", "chat_id": "c", "turn_id": "t2", "agent": "db-oncall"},
    ]

    messages = replay_transcript_to_ui_messages(records)

    attributed = {m["turnId"]: m["agent"] for m in messages if m.get("agent")}
    assert attributed == {"t1": "sre-prod", "t2": "db-oncall"}


def test_a_junk_agent_in_a_persisted_record_is_ignored() -> None:
    """A transcript is a file on disk. A replay that trusted its shape would render whatever it
    found beside the answer."""
    for junk in (7, {"name": "sre"}, [], ""):
        messages = replay_transcript_to_ui_messages(_thread(agent=junk))
        answers = [
            m for m in messages if m.get("role") == "assistant" and m.get("kind") != "trace"
        ]
        assert "agent" not in answers[-1], junk


# --- the acting agent's prompt reaches the turn -----------------------------------------------


def _prompt_kwargs(
    named: dict[str, object], agent: str | None, bot_name: str = "nanobot"
) -> dict[str, object]:
    """What the loop hands the context builder for a turn answered by *agent*."""
    from nanoinfra.agent.loop import AgentLoop, TurnContext
    from nanoinfra.config.schema import AgentDefaults

    loop = _Loop(named)
    loop._agent_defaults = AgentDefaults(bot_name=bot_name)  # type: ignore[attr-defined]
    ctx = object.__new__(TurnContext)
    ctx.agent = agent
    return AgentLoop._agent_prompt_for(loop, ctx)  # type: ignore[arg-type]


def test_the_acting_agents_addendum_and_sections_reach_the_prompt() -> None:
    """`build_system_prompt` accepted both from the start and **nothing passed them**, so a named
    agent's addendum and its replaced sections were stored, shown, editable — and inert. This is
    the test that would have caught an editor over config that reached no turn."""
    from nanoinfra.config.schema import AgentsConfig

    named = AgentsConfig.model_validate({
        "named": {"sre": {
            "description": "hands-on checks",
            "addendum": "Prefer read-only checks.",
            "promptSections": {"Memory": "The database is on db-01."},
        }}
    }).named

    kwargs = _prompt_kwargs(named, "sre")

    assert kwargs["agent_addendum"] == "Prefer read-only checks."
    assert kwargs["section_overrides"] == {"Memory": "The database is on db-01."}


def test_a_named_agent_is_told_who_it_is() -> None:
    """Found by asking one: with `sre` selected, "quién eres" answered "I am nanobot" -- the
    deployment's persona from `SOUL.md`, which every agent shares. A roster of specialists was
    five badges over one identity, so the name and the purpose now travel with the turn."""
    from nanoinfra.config.schema import AgentsConfig

    named = AgentsConfig.model_validate(
        {"named": {"sre": {"description": "hands-on checks on your hosts"}}}
    ).named

    kwargs = _prompt_kwargs(named, "sre")

    assert kwargs["agent_name"] == "sre"
    assert kwargs["agent_description"] == "hands-on checks on your hosts"


def test_the_default_agent_is_named_by_nobody_and_adds_no_section() -> None:
    """The identity *parameters* always travel, because a persona that writes `{{ agent_name }}`
    has to read sensibly on a default-agent turn too. What is empty is the agent name, and an
    empty name renders no section at all -- so a deployment that names nothing sees the prompt it
    had before any of this."""
    from nanoinfra.agent.context import _agent_identity_text

    kwargs = _prompt_kwargs({}, None)

    assert kwargs["agent_name"] == ""
    assert kwargs["agent_description"] == ""
    assert "section_overrides" not in kwargs
    assert "agent_addendum" not in kwargs
    assert _agent_identity_text("", "") == ""


def test_the_deployments_bot_name_fills_a_persona_placeholder() -> None:
    """`agents.defaults.botName` reaches the CLI banner and a device note's signature and, until
    this, no prompt at all. It is the fallback for `{{ agent_name }}` so a `SOUL.md` written once
    reads on every turn."""
    assert _prompt_kwargs({}, None, bot_name="nanobot")["bot_name"] == "nanobot"


def test_an_agent_that_declares_neither_overrides_nothing_but_is_still_named() -> None:
    """Two different absences. It has no addendum and replaces no section, so those keys are not
    passed at all -- passing empty ones would be an override set that means "replace with
    nothing". Its *name* still travels, because it is still that agent answering."""
    from nanoinfra.config.schema import AgentsConfig

    named = AgentsConfig.model_validate({"named": {"sre": {}}}).named

    kwargs = _prompt_kwargs(named, "sre")

    assert "section_overrides" not in kwargs
    assert "agent_addendum" not in kwargs
    assert kwargs["agent_name"] == "sre"


def test_a_name_the_roster_does_not_hold_falls_back_to_the_deployments_own_agent() -> None:
    """The loop resolves an unknown name to the default agent before this, so reaching here with
    one means something bypassed that -- and the safe answer is the deployment's own prompt, not
    another agent's."""
    from nanoinfra.config.schema import AgentsConfig

    named = AgentsConfig.model_validate({"named": {"sre": {"addendum": "mine"}}}).named

    kwargs = _prompt_kwargs(named, "ghost")

    assert kwargs.get("agent_addendum", "") == ""
    assert kwargs["agent_name"] == "ghost"
