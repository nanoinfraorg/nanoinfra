"""Delegation: who may ask whom, and what a delegated turn may reach (#249, #250, #251).

The invariant under test, in one line: **a delegated turn never holds more authority than the turn
that spawned it.** Everything here is a consequence of it -- one level deep, the roster read from
config at call time rather than trusted from the arguments, and an unattended chain that carries no
human actor.
"""

from __future__ import annotations

from nanoinfra.agent.delegation import (
    DelegateBinding,
    DelegatedAnswer,
    allowed_delegates,
    refuse_second_level,
    tools_for_groups,
)
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    ToolContext,
    request_context,
)
from nanoinfra.agent.tools.delegate import DelegateToAgentTool, ListDelegatesTool
from nanoinfra.config.schema import AgentsConfig, ToolsConfig

ROSTER = AgentsConfig.model_validate({
    "named": {
        "sre-prod": {"description": "hands-on checks", "toolGroups": ["servers"]},
        "db-oncall": {"description": "Postgres and Valkey"},
        "manager": {"description": "plans", "delegates": ["sre-prod", "db-oncall"]},
    }
}).named


def _ctx(**kwargs: object) -> ToolContext:
    return ToolContext(
        config=ToolsConfig(),
        workspace="/tmp",
        named_agents=ROSTER,
        **kwargs,  # type: ignore[arg-type]
    )


class _FakeManager:
    """Records what a delegation would have run, so the test reads the binding rather than a log."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_inline(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "peer answered"


def turn(**kwargs: object):
    """A bound turn, for the body of a `with`.

    Not a fixture: an async test runs in its own context, so a fixture that binds outside it
    cannot reset the token it created -- the bind and the reset have to sit in the same call.
    """
    return request_context(
        RequestContext(channel="cli", chat_id="direct", **kwargs)  # type: ignore[arg-type]
    )


# --- the pure rules -------------------------------------------------------------------------


def test_a_turn_that_is_not_a_delegation_may_delegate() -> None:
    assert refuse_second_level(None) is None


def test_a_delegated_turn_may_not_delegate_and_is_told_who_decides() -> None:
    reason = refuse_second_level(DelegateBinding(name="sre-prod", delegated_by="manager"))

    assert reason is not None
    # The refusal has to name the next step, or the peer's only option is to give up.
    assert "manager" in reason
    assert "one level" in reason


def test_the_roster_is_read_from_config_not_from_the_call() -> None:
    assert allowed_delegates("manager", ROSTER) == ("sre-prod", "db-oncall")
    assert allowed_delegates("sre-prod", ROSTER) == ()
    # The default agent, which is what every deployment has today.
    assert allowed_delegates(None, ROSTER) == ()
    assert allowed_delegates("ghost", ROSTER) == ()


def test_the_audit_chain_names_the_human_when_there_is_one() -> None:
    binding = DelegateBinding(name="sre-prod", delegated_by="manager", actor="alberto")

    assert binding.audit_chain() == "alberto -> manager -> sre-prod"


def test_the_audit_chain_says_so_when_no_human_is_behind_it() -> None:
    """An unattended chain. A reader must not be able to mistake it for a human-authorised one."""
    binding = DelegateBinding(name="sre-prod", delegated_by="manager")

    assert binding.audit_chain() == "manager -> sre-prod"


MEMBERSHIP = {"execute_on_server": ["servers"], "device_notes": ["servers"],
              "create_diagram": ["diagrams"]}
ALL_TOOLS = ["read_file", "shell", "execute_on_server", "device_notes", "create_diagram"]


def test_no_declared_groups_keeps_every_tool() -> None:
    """A two-line agent has to be meaningful, so an omitted list means *the default*."""
    assert tools_for_groups(ALL_TOOLS, (), MEMBERSHIP) == frozenset(ALL_TOOLS)


def test_an_ungrouped_tool_survives_a_narrowed_agent() -> None:
    """Groups cover surfaces, not the whole tool set. Reading "ungrouped" as "denied" would leave
    a peer unable to read a file, which `tools.file` -- not the roster -- governs."""
    kept = tools_for_groups(ALL_TOOLS, ("servers",), MEMBERSHIP)

    assert "read_file" in kept
    assert "shell" in kept


def test_a_group_the_agent_was_not_given_is_dropped() -> None:
    kept = tools_for_groups(ALL_TOOLS, ("servers",), MEMBERSHIP)

    assert "execute_on_server" in kept
    assert "device_notes" in kept
    assert "create_diagram" not in kept


# --- the tools ------------------------------------------------------------------------------


def test_the_tools_are_absent_from_a_deployment_that_names_no_agent() -> None:
    bare = ToolContext(config=ToolsConfig(), workspace="/tmp")

    assert ListDelegatesTool.enabled(bare) is False
    assert DelegateToAgentTool.enabled(bare) is False


def test_the_tools_are_absent_when_no_agent_has_a_roster() -> None:
    """Named agents alone are not delegation. Without a roster there is nobody to ask."""
    flat = AgentsConfig.model_validate({"named": {"sre": {}, "db": {}}}).named

    assert ListDelegatesTool.enabled(
        ToolContext(config=ToolsConfig(), workspace="/tmp", named_agents=flat)
    ) is False


def test_a_delegated_turn_never_sees_the_tools_in_the_first_place() -> None:
    """Both default to the `core` scope, and a delegated turn runs under `subagent`. The execution
    check below is the second lock, not the only one."""
    for tool in (ListDelegatesTool, DelegateToAgentTool):
        assert "subagent" not in getattr(tool, "_scopes", {"core"})


async def test_list_delegates_answers_what_each_peer_is_for() -> None:
    with turn(agent="manager"):
        tool = ListDelegatesTool.create(_ctx())

        answer = await tool.execute()

        assert "sre-prod" in answer
        assert "hands-on checks" in answer
        assert "db-oncall" in answer


async def test_list_delegates_narrows_on_the_problem_rather_than_the_name() -> None:
    with turn(agent="manager"):
        tool = ListDelegatesTool.create(_ctx())

        answer = await tool.execute(query="postgres")

        assert "db-oncall" in answer
        assert "sre-prod" not in answer


async def test_list_delegates_says_plainly_that_the_default_agent_has_no_roster() -> None:
    with turn():
        tool = ListDelegatesTool.create(_ctx())

        answer = await tool.execute()

        assert "default agent" in answer
        assert "Nothing can be delegated" in answer


async def test_delegating_to_a_peer_outside_the_roster_is_refused() -> None:
    """The executor revalidates. A tool call is not evidence of authority."""
    manager = _FakeManager()
    with turn(agent="manager", runtime=object()):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        answer = await tool.execute(agent="db-oncall", task="check replication")
        assert "peer answered" == answer

        refused = await tool.execute(agent="finance-bot", task="pay an invoice")

        assert "not one of your delegates" in refused
        # And it says what may be asked, so the model can correct itself in one step.
        assert "db-oncall" in refused
        assert len(manager.calls) == 1


async def test_a_delegated_turn_refuses_to_delegate_again() -> None:
    manager = _FakeManager()
    with turn(agent="sre-prod", delegated_by="manager", runtime=object()):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        answer = await tool.execute(agent="db-oncall", task="check replication")

        assert "one level" in answer
        assert manager.calls == []


async def test_the_binding_carries_the_peers_own_bindings() -> None:
    manager = _FakeManager()
    with turn(
        agent="manager",
        runtime=object(),
        sender_id="alberto",
        execution_context=EXECUTION_CONTEXT_INTERACTIVE,
    ):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        await tool.execute(agent="sre-prod", task="check disk on db-01")

        binding = manager.calls[0]["binding"]
        assert isinstance(binding, DelegateBinding)
        assert binding.name == "sre-prod"
        assert binding.delegated_by == "manager"
        # Declared, not accumulated: the peer's own tool groups travel with the turn.
        assert binding.tool_groups == ("servers",)
        # The originating human, because a person is waiting on this turn.
        assert binding.actor == "alberto"
        assert binding.audit_chain() == "alberto -> manager -> sre-prod"


async def test_an_unattended_delegation_carries_no_human_actor() -> None:
    """A coordinator started by cron delegates as unattended, and a standing grant is the only
    thing that can authorise its peer's actions. Inheriting the human would be a silent widening."""
    manager = _FakeManager()
    with turn(agent="manager", runtime=object(), sender_id="alberto"):

        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))
        await tool.execute(agent="sre-prod", task="check disk")

        binding = manager.calls[0]["binding"]
        assert isinstance(binding, DelegateBinding)
        assert binding.actor is None


async def test_a_peer_answers_with_its_own_model_when_config_gave_it_one() -> None:
    """`agents.named[x].modelPreset` is not decoration: a preset names a provider as well as a
    model, so the delegated turn has to run on the runtime that preset builds."""
    from nanoinfra.providers.factory import ProviderSnapshot

    roster = AgentsConfig.model_validate({
        "named": {
            "sre-prod": {"modelPreset": "kimi-general"},
            "manager": {"delegates": ["sre-prod"]},
        }
    }).named
    manager = _FakeManager()
    asked: list[str] = []

    def loader(preset_name: str) -> ProviderSnapshot:
        asked.append(preset_name)
        return ProviderSnapshot(
            provider=object(),  # type: ignore[arg-type]
            model="kimi-k2",
            context_window_tokens=128_000,
            signature=("model_preset", preset_name),
            generation=None,
            model_preset=preset_name,
        )

    with turn(agent="manager", runtime=object()):
        tool = DelegateToAgentTool.create(ToolContext(
            config=ToolsConfig(),
            workspace="/tmp",
            named_agents=roster,
            subagent_manager=manager,  # type: ignore[arg-type]
            provider_snapshot_loader=loader,
        ))

        await tool.execute(agent="sre-prod", task="check disk")

        assert asked == ["kimi-general"]
        assert manager.calls[0]["runtime"].model == "kimi-k2"


async def test_a_preset_that_cannot_be_resolved_refuses_the_delegation() -> None:
    """Rather than quietly running the peer on the caller's model. The operator chose a model for
    this agent, and an answer from a different one is one they cannot trust."""
    roster = AgentsConfig.model_validate({
        "named": {"sre-prod": {"modelPreset": "typo"}, "manager": {"delegates": ["sre-prod"]}}
    }).named
    manager = _FakeManager()

    def loader(preset_name: str):
        raise KeyError(preset_name)

    with turn(agent="manager", runtime=object()):
        tool = DelegateToAgentTool.create(ToolContext(
            config=ToolsConfig(),
            workspace="/tmp",
            named_agents=roster,
            subagent_manager=manager,  # type: ignore[arg-type]
            provider_snapshot_loader=loader,
        ))

        answer = await tool.execute(agent="sre-prod", task="check disk")

    assert "could not be resolved" in answer
    assert manager.calls == []


async def test_a_peer_with_no_preset_answers_on_the_turns_own_runtime() -> None:
    """Which is the inheriting default, and the shape a two-line agent has."""
    manager = _FakeManager()
    runtime = object()
    with turn(agent="manager", runtime=runtime):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        await tool.execute(agent="sre-prod", task="check disk")

    assert manager.calls[0]["runtime"] is runtime


async def test_an_empty_task_is_refused_because_the_peer_sees_nothing_else() -> None:
    manager = _FakeManager()
    with turn(agent="manager", runtime=object()):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        answer = await tool.execute(agent="sre-prod", task="   ")

        assert "task is empty" in answer
        assert manager.calls == []


async def test_delegation_needs_a_runtime() -> None:
    manager = _FakeManager()
    with turn(agent="manager"):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        answer = await tool.execute(agent="sre-prod", task="check disk")

        assert "active model runtime" in answer


def test_the_description_carries_the_roster_so_the_model_need_not_ask_first() -> None:
    with turn(agent="manager"):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=_FakeManager()))

        assert "sre-prod" in tool.description
        assert "one level deep" in tool.description


# --- what a delegated turn's registry actually holds ------------------------------------------
#
# The two rules below were wrong in the first implementation, in opposite directions: the peer had
# no server tools at all, and the tools it did have reached no gate. Both are pinned here because
# neither is visible from the tool that asks for the delegation.


def _manager(tmp_path, gate: object | None = None):
    from nanoinfra.agent.subagent import SubagentManager
    from nanoinfra.bus.queue import MessageBus

    return SubagentManager(
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=4_000,
        gate=gate,
    )


def test_a_delegated_turn_gets_the_tools_its_groups_name(tmp_path) -> None:
    """`servers` and `diagrams` are core-scoped. Under the subagent scope -- which is what a
    delegation used to run -- an agent declared with `toolGroups: ["servers"]` received none of
    them, which makes the whole binding decorative."""
    manager = _manager(tmp_path)

    registry = manager._build_tools(
        binding=DelegateBinding(name="sre-prod", delegated_by="manager", tool_groups=("servers",))
    )

    assert "execute_on_server" in registry.tool_names
    assert "list_servers" in registry.tool_names


def test_a_delegated_turn_does_not_get_a_group_it_was_not_given(tmp_path) -> None:
    manager = _manager(tmp_path)

    registry = manager._build_tools(
        binding=DelegateBinding(name="sre-prod", delegated_by="manager", tool_groups=("servers",))
    )

    assert "create_diagram" not in registry.tool_names
    # And an ungrouped tool survives, because groups cover surfaces and not the whole tool set.
    assert "read_file" in registry.tool_names


def test_a_delegated_turn_can_never_delegate_further(tmp_path) -> None:
    """Structurally, not only by the check in the tool: the context a delegated turn is built with
    carries no roster, so the delegation tools are not even registered for it."""
    manager = _manager(tmp_path)

    registry = manager._build_tools(
        binding=DelegateBinding(name="sre-prod", delegated_by="manager")
    )

    assert "delegate_to_agent" not in registry.tool_names
    assert "list_delegates" not in registry.tool_names


def test_a_plain_subagent_keeps_the_narrower_scope_it_has_always_had(tmp_path) -> None:
    """No binding, no change. A subagent is a helper of the same agent, not a peer."""
    manager = _manager(tmp_path)

    registry = manager._build_tools()

    assert "execute_on_server" not in registry.tool_names
    assert "read_file" in registry.tool_names


def _captured_context(manager, monkeypatch, **build_kwargs):
    """The `ToolContext` a build produced. Captured rather than inferred: the gate is not visible
    from the registry, and it is the field that decides whether a peer can be asked to wait."""
    from nanoinfra.agent.tools.loader import ToolLoader

    seen: list[object] = []
    original = ToolLoader.load

    def spy(self, ctx, registry, *, scope="core"):
        seen.append(ctx)
        return original(self, ctx, registry, scope=scope)

    monkeypatch.setattr(ToolLoader, "load", spy)
    manager._build_tools(**build_kwargs)
    return seen[0]


def test_a_delegated_turns_tools_reach_the_gate(tmp_path, monkeypatch) -> None:
    """Otherwise they fall back to policy alone -- no approval path -- and a peer could execute
    where the turn that spawned it would have stopped to ask. That is the invariant inverted."""
    gate = object()
    manager = _manager(tmp_path, gate=gate)

    ctx = _captured_context(
        manager,
        monkeypatch,
        binding=DelegateBinding(name="sre-prod", delegated_by="manager"),
    )

    assert ctx.gate is gate


def test_a_plain_subagent_is_left_exactly_as_it_was(tmp_path, monkeypatch) -> None:
    """Handing a subagent a gate is a different decision from this one, and not this issue's to
    make: it would turn an unattended helper's turn into one that waits for a person."""
    manager = _manager(tmp_path, gate=object())

    ctx = _captured_context(manager, monkeypatch)

    assert ctx.gate is None


# --- the peer's cost reaches the row that shows the delegation --------------------------------


async def test_a_peers_answer_carries_what_the_peers_turn_cost() -> None:
    """A delegated turn is its own turn, so its cost travels beside the answer instead of joining
    the asking agent's usage line -- which would print one turn's cost twice."""
    class _CostingManager(_FakeManager):
        async def run_inline(self, **kwargs: object) -> str:
            sink = kwargs.get("usage_sink")
            assert isinstance(sink, dict), "the tool has to offer somewhere to put it"
            sink["usage"] = {"prompt_tokens": 9000, "completion_tokens": 400}
            return await super().run_inline(**kwargs)

    manager = _CostingManager()
    with turn(agent="manager", runtime=object()):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        answer = await tool.execute(agent="sre-prod", task="check disk")

    assert answer == "peer answered", "it still reads as the answer it is"
    assert isinstance(answer, DelegatedAnswer)
    assert answer.usage == {"prompt_tokens": 9000, "completion_tokens": 400}


async def test_a_peer_that_reported_no_cost_returns_a_plain_answer() -> None:
    """Rather than an empty usage object. The thread then prints no cost for that row, which is
    the truth -- borrowing the asking agent's number would be a fabricated one."""
    manager = _FakeManager()
    with turn(agent="manager", runtime=object()):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        answer = await tool.execute(agent="sre-prod", task="check disk")

    assert getattr(answer, "usage", None) is None


def test_the_wire_payload_copies_the_peers_cost_out() -> None:
    """The answer is a `str` subclass and the attribute does not survive serialisation, so the
    payload builder has to copy it deliberately. If it stops doing so the plan shows no cost."""
    from nanoinfra.agent.hook import AgentHookContext
    from nanoinfra.utils.progress_events import build_tool_event_finish_payloads

    class _Call:
        id = "call-1"
        name = "delegate_to_agent"
        arguments = "{}"

    context = AgentHookContext(iteration=0, messages=[])
    context.tool_calls = [_Call()]  # type: ignore[assignment]
    context.tool_results = [DelegatedAnswer("done", usage={"prompt_tokens": 9000})]
    context.tool_events = [{"name": "delegate_to_agent", "status": "ok", "detail": "done"}]

    payload = build_tool_event_finish_payloads(context)[0]

    assert payload["usage"] == {"prompt_tokens": 9000}


def test_the_wire_payload_of_an_ordinary_tool_carries_no_usage_key() -> None:
    from nanoinfra.agent.hook import AgentHookContext
    from nanoinfra.utils.progress_events import build_tool_event_finish_payloads

    class _Call:
        id = "call-2"
        name = "read_file"
        arguments = "{}"

    context = AgentHookContext(iteration=0, messages=[])
    context.tool_calls = [_Call()]  # type: ignore[assignment]
    context.tool_results = ["file contents"]
    context.tool_events = [{"name": "read_file", "status": "ok", "detail": "ok"}]

    assert "usage" not in build_tool_event_finish_payloads(context)[0]


# --- the ceiling does not leak one level down --------------------------------------------------


def test_a_capped_turn_hands_its_ceiling_to_anything_it_spawns() -> None:
    """Otherwise a capped turn reaches, through a subagent, exactly the tools its agent was
    declared not to have: the ceiling holds for the turn and leaks one level down."""
    from nanoinfra.agent.subagent import SubagentManager
    from nanoinfra.session.automation_turns import (
        AUTOMATION_AGENT_META,
        automation_agent_metadata,
    )

    with turn(agent="sre-prod", metadata=automation_agent_metadata("sre-prod", ["servers"])):
        inherited = SubagentManager._inherited_ceiling()

    assert inherited == {AUTOMATION_AGENT_META: {"tool_groups": ["servers"]}}


def test_the_acting_agents_name_is_not_inherited() -> None:
    """A subagent is a helper of the turn, not a second turn by that agent. Carrying the name
    would record it as one, which is the misattribution turn attribution exists to prevent."""
    from nanoinfra.agent.subagent import SubagentManager
    from nanoinfra.session.automation_turns import TURN_AGENT_META, automation_agent_metadata

    with turn(agent="sre-prod", metadata=automation_agent_metadata("sre-prod", ["servers"])):
        inherited = SubagentManager._inherited_ceiling()

    assert TURN_AGENT_META not in inherited


def test_an_uncapped_turn_inherits_nothing() -> None:
    """Which is every turn in a deployment that names no agent, and an empty ceiling has to mean
    *unrestricted* rather than *nothing allowed*."""
    from nanoinfra.agent.subagent import SubagentManager

    with turn(agent="manager"):
        assert SubagentManager._inherited_ceiling() == {}


# --- the ceiling has a producer, and the human reaches the gate --------------------------------


def test_the_ceiling_is_what_the_acting_turn_can_actually_reach() -> None:
    """Computed from the turn's registry, not from config: a tool that failed to construct for
    want of a collaborator is not authority the manager holds, and config cannot know that."""
    from nanoinfra.agent.delegation import acting_capabilities

    classes = {
        "read_file": "read",
        "execute_on_server": "mutate.remote",
        "create_diagram": "mutate.inventory",
    }

    # A manager narrowed to `servers` reaches its own remote class and the ungrouped read.
    narrowed = acting_capabilities(
        classes, ("servers",), MEMBERSHIP | {"create_diagram": ["diagrams"]}, classes.__getitem__
    )
    assert narrowed == frozenset({"read", "mutate.remote"})

    # An unrestricted manager reaches everything its registry holds.
    everything = acting_capabilities(classes, (), MEMBERSHIP, classes.__getitem__)
    assert everything == frozenset(classes.values())


async def test_the_binding_carries_the_ceiling_the_turn_declared() -> None:
    manager = _FakeManager()
    with turn(agent="manager", runtime=object(), acting_capabilities=frozenset({"read"})):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        await tool.execute(agent="sre-prod", task="check disk")

    binding = manager.calls[0]["binding"]
    assert isinstance(binding, DelegateBinding)
    # A manager that can only read must not reach a remote mutation by asking a peer to do it.
    assert binding.inherited_capabilities == frozenset({"read"})


async def test_a_turn_that_declared_no_ceiling_binds_nothing() -> None:
    """Empty means *not declared*, never *nothing allowed* -- which would refuse a peer the right
    to read a file and break every deployment shipping today."""
    manager = _FakeManager()
    with turn(agent="manager", runtime=object()):
        tool = DelegateToAgentTool.create(_ctx(subagent_manager=manager))

        await tool.execute(agent="sre-prod", task="check disk")

    assert manager.calls[0]["binding"].inherited_capabilities == frozenset()
