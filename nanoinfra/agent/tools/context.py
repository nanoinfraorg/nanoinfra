"""Runtime context for tool construction."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nanoinfra.agent.subagent import SubagentManager
    from nanoinfra.agent.tools.exec_session import ExecSessionManager
    from nanoinfra.agent.tools.file_state import FileStates
    from nanoinfra.bus.queue import MessageBus
    from nanoinfra.bus.runtime_events import RuntimeEventBus
    from nanoinfra.config.schema import NamedAgentConfig, ProviderConfig, ToolsConfig
    from nanoinfra.cron.service import CronService
    from nanoinfra.providers.factory import ProviderSnapshot
    from nanoinfra.security.workspace_access import WorkspaceSandboxStatus
    from nanoinfra.session.manager import SessionManager
    from nanoinfra.utils.llm_runtime import LLMRuntime

#: The empty roster, as a default a dataclass may share: read-only, so no context can mutate
#: the one every other context is holding.
_NO_NAMED_AGENTS: "Mapping[str, NamedAgentConfig]" = MappingProxyType({})

_CURRENT_REQUEST_CONTEXT: ContextVar["RequestContext | None"] = ContextVar(
    "nanoinfra_tool_request_context",
    default=None,
)

# Who drives the turn. Policy in nanoinfraorg/nanoinfra#8 and #13 keys on these values.
# The channel alone cannot answer the question. A subagent inherits the origin channel of
# the chat above it. That channel reads interactive for a run that nobody watches.
EXECUTION_CONTEXT_INTERACTIVE = "interactive"
EXECUTION_CONTEXT_AUTOMATION = "automation"
EXECUTION_CONTEXT_SUBAGENT = "subagent"

EXECUTION_CONTEXTS = frozenset({
    EXECUTION_CONTEXT_INTERACTIVE,
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_SUBAGENT,
})

# Nobody waits on these two. Policy treats them the same way. The record keeps them apart,
# so an operator who debugs a denial sees which one ran.
UNATTENDED_EXECUTION_CONTEXTS = frozenset({
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_SUBAGENT,
})

# The value a construction site gets when it states nothing. Fail closed. New sites arrive
# over time. An omission must cost a refusal. It must never buy attended trust.
FAIL_CLOSED_EXECUTION_CONTEXT = EXECUTION_CONTEXT_AUTOMATION


@dataclass(frozen=True)
class RequestContext:
    """Per-request context injected into tools at message-processing time."""
    channel: str
    chat_id: str
    message_id: str | None = None
    session_key: str | None = None
    original_user_text: str | None = None
    runtime: LLMRuntime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sender_id: str | None = None
    turn_id: str | None = None
    workspace: Path | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Which named agent is answering this turn (#248). ``None`` is the deployment default agent,
    #: which is what every turn is today. It is read rather than trusted: what the agent may do
    #: comes from ``agents.named`` in config, never from this string.
    agent: str | None = None
    #: Set on a turn that is itself a delegation, to the agent that asked for it (#251). Its
    #: presence is what makes the turn refuse to delegate again -- one level, checked locally.
    delegated_by: str | None = None
    #: The capability classes this turn can reach, for a peer it delegates to (#251). Its own
    #: field rather than an entry in ``attributes``, because ``attributes`` is what a caller hands
    #: a context provider and this is the runtime's own answer about the turn. Empty means *not
    #: computed*, never *nothing allowed*.
    acting_capabilities: frozenset[str] = frozenset()
    # Last, and defaulted, so an existing positional call keeps its meaning. Only an
    # explicit channel-driven turn may pass EXECUTION_CONTEXT_INTERACTIVE.
    execution_context: str = FAIL_CLOSED_EXECUTION_CONTEXT

    @property
    def is_unattended(self) -> bool:
        """True when no person waits on this turn."""
        return is_unattended_execution_context(self.execution_context)


def is_unattended_execution_context(value: str | None) -> bool:
    """True when no person waits on a turn that carries *value*.

    The test asks for ``interactive`` and refuses every other value. A membership test
    against :data:`UNATTENDED_EXECUTION_CONTEXTS` would read a typo as attended. It would
    read a value from a later item as attended too.
    """
    return value != EXECUTION_CONTEXT_INTERACTIVE


@runtime_checkable
class ContextAware(Protocol):
    def set_context(self, ctx: RequestContext) -> None:
        ...


def bind_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _CURRENT_REQUEST_CONTEXT.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _CURRENT_REQUEST_CONTEXT.reset(token)


@contextmanager
def request_context(ctx: RequestContext):
    """Bind one immutable request snapshot and restore the previous value."""
    token = bind_request_context(ctx)
    try:
        yield ctx
    finally:
        reset_request_context(token)


def current_request_context() -> RequestContext | None:
    return _CURRENT_REQUEST_CONTEXT.get()


def current_request_session_key() -> str | None:
    ctx = current_request_context()
    return ctx.session_key if ctx else None


def current_request_execution_context() -> str:
    """Return who drives the current turn, or the fail-closed value.

    An unbound context proves nothing about a person being present, so the answer stays
    unattended. The return type is never ``None``, so a caller cannot compare against a
    missing value by accident.
    """
    ctx = current_request_context()
    return ctx.execution_context if ctx else FAIL_CLOSED_EXECUTION_CONTEXT


@dataclass
class ToolContext:
    config: ToolsConfig
    workspace: str
    bus: MessageBus | None = None
    subagent_manager: SubagentManager | None = None
    cron_service: CronService | None = None
    exec_session_manager: ExecSessionManager | None = None
    sessions: SessionManager | None = None
    file_state_store: FileStates | None = None
    provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None
    image_generation_provider_configs: dict[str, ProviderConfig] | None = None
    timezone: str = "UTC"
    #: Skills the operator switched off, from ``agents.defaults.disabled_skills``.
    #:
    #: The diagram catalog computes ``skill_enabled`` from this, and the tool used to omit it while
    #: the WebUI route passed it -- so the model was told a component was operable through a skill the
    #: operator had disabled (#99). Two views of one fact, and the one the model read was wrong.
    disabled_skills: frozenset[str] = frozenset()
    workspace_sandbox: WorkspaceSandboxStatus | None = None
    runtime_events: RuntimeEventBus | None = None
    #: The configured named agents (#247), for the tools that offer delegation. The registry is
    #: built once at boot, so this answers "does this deployment delegate at all"; *which* peer a
    #: given turn may reach is re-read from here when the tool runs, because that depends on who
    #: is answering and a tool call is not evidence of authority.
    named_agents: "Mapping[str, NamedAgentConfig]" = _NO_NAMED_AGENTS
    # The gate runtime from nanoinfra/gates/runtime.py (#33). Typed loosely on purpose:
    # that module imports the agent tree, so a real annotation here would close a cycle.
    # It carries the gate half only, so nothing reached through a tool can clear a latch.
    gate: Any = None
    #: Rehearse a newly created automation once this turn is idle (#183). None in an embedded or
    #: a test construction, and creating an automation then simply skips the rehearsal.
    commission_automation: Callable[[str], None] | None = None
