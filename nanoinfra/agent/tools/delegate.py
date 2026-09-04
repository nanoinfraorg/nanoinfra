"""Delegation tools: ask a peer, and find out who the peers are (#250, #249).

Two tools rather than one long description. The roster reaches the model as counts and names; the
detail -- what each peer is for -- is resolved on demand, because a deployment with a dozen agents
would otherwise pay for a dozen descriptions in every prompt of every turn.

Neither tool declares ``_scopes``, so both default to ``{"core"}`` and a delegated turn -- which
runs under the ``subagent`` scope -- never sees them. The one-level rule is also checked at
execution time, because a scope is a registry decision and the rule is an authorization one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, cast

from loguru import logger

from nanoinfra.agent.delegation import (
    DelegateBinding,
    DelegatedAnswer,
    allowed_delegates,
    refuse_second_level,
)
from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.capabilities import MUTATE_LOCAL, READ
from nanoinfra.agent.tools.context import current_request_context
from nanoinfra.agent.tools.schema import StringSchema, tool_parameters_schema
from nanoinfra.security.workspace_access import current_workspace_scope
from nanoinfra.utils.llm_runtime import runtime_from_provider_snapshot

if TYPE_CHECKING:
    from nanoinfra.agent.subagent import SubagentManager
    from nanoinfra.agent.tools.context import ToolContext
    from nanoinfra.config.schema import NamedAgentConfig
    from nanoinfra.providers.factory import ProviderSnapshot


#: What the deployment's own agent is called in a record. It has no name in config -- it is the
#: absence of one -- and an audit chain reading `alberto -> -> db` says nothing.
DEFAULT_AGENT_NAME = "default"


def _delegating_deployment(ctx: ToolContext) -> bool:
    """True when at least one configured agent has a roster, the default agent included.

    The registry is built once at boot, so this is the only question available here. *Which* peer a
    given turn may reach depends on who is answering, and that is re-read when the tool runs.
    """
    if getattr(ctx.agent_defaults, "delegates", ()):
        return True
    return any(getattr(agent, "delegates", ()) for agent in ctx.named_agents.values())


class _RosterMixin:
    """Shared roster access. The config mapping, not a copy taken at registration."""

    def __init__(
        self,
        named_agents: dict[str, NamedAgentConfig],
        agent_defaults: Any = None,
    ) -> None:
        self._named_agents = named_agents
        self._agent_defaults = agent_defaults

    def _acting_agent(self) -> str | None:
        request_ctx = current_request_context()
        return request_ctx.agent if request_ctx else None

    def _peers(self) -> tuple[str, ...]:
        """The peers this turn may reach, whoever is answering.

        A turn that names no agent is answered by the deployment's own, and that agent has a
        roster of its own (#265). Reading only `named_agents` here meant a default-agent turn
        found no peers however config was written -- and since a composer choice that did not
        survive a reload fell back to the default agent, delegation quietly became impossible.
        """
        acting = self._acting_agent()
        if acting is None:
            return tuple(getattr(self._agent_defaults, "delegates", ()) or ())
        return allowed_delegates(acting, self._named_agents)


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema(
            "Optional filter. Matched against each peer's name and description, so a problem "
            "-- 'postgres', 'firewall' -- finds the agent for it."
        ),
    )
)
class ListDelegatesTool(_RosterMixin, Tool):
    """Who this agent may ask, and what each is for."""

    #: `read`, not a mutate class: it answers from config and reaches nothing. An undeclared or
    #: misspelled class falls back to `mutate.remote`, which would gate a roster read.
    capability_class = READ

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _delegating_deployment(ctx)

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(named_agents=dict(ctx.named_agents), agent_defaults=ctx.agent_defaults)

    @property
    def name(self) -> str:
        return "list_delegates"

    @property
    def description(self) -> str:
        return (
            "List the agents you may delegate to, with what each one is for. "
            "Use this before delegating, so the task goes to a peer that covers the target "
            "rather than to the first name you recall."
        )

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        query: str | None = None,
        **kwargs: Any,
    ) -> str:
        peers = self._peers()
        if not peers:
            acting = self._acting_agent()
            if acting is None:
                return (
                    "This turn is answered by the deployment's own agent, and it lists no "
                    "delegates. Nothing can be delegated; do the work or say what is missing."
                )
            return f"`{acting}` has no delegates configured. Do the work or say what is missing."
        needle = (query or "").strip().lower()
        rows: list[str] = []
        for peer in peers:
            entry = self._named_agents.get(peer)
            description = getattr(entry, "description", "") if entry else ""
            if needle and needle not in f"{peer} {description}".lower():
                continue
            rows.append(f"- `{peer}`: {description or 'no description configured'}")
        if not rows:
            return (
                f"No delegate matches {query!r}. Available: "
                + ", ".join(f"`{peer}`" for peer in peers)
            )
        return "\n".join(rows)


@tool_parameters(
    tool_parameters_schema(
        agent=StringSchema(
            "The peer to ask. Must be one of your configured delegates; use list_delegates "
            "when you are not sure which one covers the target."
        ),
        task=StringSchema(
            "The complete task. The peer sees this and nothing else -- not this conversation "
            "-- so include the target, the goal and any constraint that matters."
        ),
        label=StringSchema("Optional short label for the delegation (for display)"),
        required=["agent", "task"],
    )
)
class DelegateToAgentTool(_RosterMixin, Tool):
    """Hand one task to one peer, and wait for its answer."""

    #: A delegation can do whatever the peer can, so it is classed by its widest effect rather
    #: than by the act of asking.
    capability_class = MUTATE_LOCAL

    def __init__(
        self,
        named_agents: dict[str, NamedAgentConfig],
        manager: SubagentManager,
        snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        agent_defaults: Any = None,
    ) -> None:
        super().__init__(named_agents=named_agents, agent_defaults=agent_defaults)
        self._manager = manager
        self._snapshot_loader = snapshot_loader

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _delegating_deployment(ctx) and ctx.subagent_manager is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        manager = ctx.subagent_manager
        if manager is None:
            raise RuntimeError("DelegateToAgentTool requires an initialized subagent manager")
        return cls(
            named_agents=dict(ctx.named_agents),
            agent_defaults=ctx.agent_defaults,
            manager=manager,
            snapshot_loader=ctx.provider_snapshot_loader,
        )

    @property
    def name(self) -> str:
        return "delegate_to_agent"

    @property
    def description(self) -> str:
        peers = self._peers()
        roster = (
            f" You have {len(peers)} delegate(s): " + ", ".join(f"`{p}`" for p in peers) + "."
            if peers
            else ""
        )
        return (
            "Delegate one task to one peer agent and wait for its answer. "
            "The peer runs as itself, with its own tools, and sees only the task you write. "
            "Delegation is one level deep: the peer cannot delegate further." + roster
        )

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        agent: str,
        task: str,
        label: str | None = None,
        **kwargs: Any,
    ) -> str:
        request_ctx = current_request_context()
        if request_ctx is None or request_ctx.runtime is None:
            return ToolResult.error("Error: delegate_to_agent requires an active model runtime")

        # One level, checked here as well as by the registry scope. A turn that carries
        # `delegated_by` is already a delegation.
        if request_ctx.delegated_by:
            return ToolResult.error(
                "Error: "
                + (
                    refuse_second_level(
                        DelegateBinding(
                            name=request_ctx.agent or "this agent",
                            delegated_by=request_ctx.delegated_by,
                        )
                    )
                    or ""
                )
            )

        acting = request_ctx.agent
        peers = self._peers()
        if not peers:
            if acting is None:
                return ToolResult.error(
                    "Error: this turn is answered by the deployment's own agent, and it lists no "
                    "delegates. Add them to agents.defaults.delegates, or choose an agent that "
                    "has some."
                )
            return ToolResult.error(f"Error: `{acting}` has no delegates configured.")

        # The roster is the authorization, so this is a fresh read of config rather than trust in
        # the argument. An agent removed from a roster stops being reachable on the next turn.
        if agent not in peers:
            return ToolResult.error(
                f"Error: `{agent}` is not one of your delegates. You may ask: "
                + ", ".join(f"`{peer}`" for peer in peers)
            )
        if not task.strip():
            return ToolResult.error("Error: the task is empty. The peer sees nothing else.")

        entry = self._named_agents.get(agent)
        binding = DelegateBinding(
            name=agent,
            delegated_by=acting or DEFAULT_AGENT_NAME,
            # The originating human, when there is one. On an unattended turn this stays None and
            # a standing grant is the only thing that can authorise the peer's actions -- which is
            # what "a delegated turn never holds more authority than the turn that spawned it"
            # means in practice.
            actor=None if request_ctx.is_unattended else request_ctx.sender_id,
            tool_groups=tuple(getattr(entry, "tool_groups", ()) or ()),
            skills=tuple(getattr(entry, "skills", ()) or ()),
            addendum=getattr(entry, "addendum", "") or "",
            # The ceiling: what *this* turn can reach, computed from its own registry by the loop
            # and carried on the context. A manager that can only read must not be able to reach
            # a remote mutation by asking a peer to do it. Empty means no ceiling was declared,
            # which is every deployment that never narrowed its manager.
            inherited_capabilities=frozenset(request_ctx.acting_capabilities),
        )
        # The peer answers with *its own* model when config gave it one. Resolved here, because a
        # preset names a provider as well as a model and only the snapshot loader can build that.
        runtime = request_ctx.runtime
        preset = getattr(entry, "model_preset", None)
        if preset:
            if self._snapshot_loader is None:
                return ToolResult.error(
                    f"Error: `{agent}` is configured to answer with the model preset "
                    f"{preset!r}, and this turn cannot resolve a preset. Delegating would run "
                    "the peer on the wrong model."
                )
            try:
                runtime = runtime_from_provider_snapshot(
                    self._snapshot_loader(preset_name=preset)
                )
            except Exception as exc:
                # Refused rather than run on the caller's model. The operator chose a model for
                # this agent, and an answer produced by a different one is one they cannot trust
                # -- and the misconfiguration would otherwise never surface.
                logger.warning("Delegation to {} could not resolve preset {}: {}", agent, preset, exc)
                return ToolResult.error(
                    f"Error: `{agent}` names the model preset {preset!r}, which could not be "
                    "resolved. Fix the preset in config, or remove it from the agent."
                )
        session_key = (
            request_ctx.session_key or f"{request_ctx.channel}:{request_ctx.chat_id}"
        )
        # The peer's own cost, so the thread can show it on the plan's row for this delegation
        # (#252). Its turn is its own turn, so this never joins the asking agent's usage line.
        peer_usage: dict[str, Any] = {}
        answer = await self._manager.run_inline(
            task=task,
            runtime=runtime,
            label=label or f"{agent}: {task[:24]}",
            origin_channel=request_ctx.channel,
            origin_chat_id=request_ctx.chat_id,
            session_key=session_key,
            origin_message_id=request_ctx.message_id,
            workspace_scope=current_workspace_scope(),
            binding=binding,
            usage_sink=peer_usage,
        )
        usage = peer_usage.get("usage")
        # A refusal keeps its own type: `ToolResult.error` is what makes the failure terminal, and
        # a peer that failed has no cost worth printing beside a row that says it failed.
        if isinstance(answer, ToolResult) or not isinstance(usage, dict):
            return answer
        return DelegatedAnswer(answer, usage=cast("dict[str, object]", usage))
