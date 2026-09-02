"""Agent loop: the core processing engine."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import time
import weakref
from collections.abc import Coroutine, Iterable, Mapping
from contextlib import AbstractContextManager, ExitStack, nullcontext, suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar, cast

from loguru import logger

from nanoinfra.agent import context as agent_context
from nanoinfra.agent import model_presets as preset_helpers
from nanoinfra.agent.autocompact import AutoCompact
from nanoinfra.agent.automation_turns import (
    execution_context_for_turn,
    publish_next_deferred_turn,
)
from nanoinfra.agent.context import ContextBuilder
from nanoinfra.agent.cron_turns import CronTurnCoordinator
from nanoinfra.agent.hook import AgentHook, AgentTurnHookFactory
from nanoinfra.agent.memory import Consolidator
from nanoinfra.agent.model_runtime import ModelRuntimeResolver
from nanoinfra.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from nanoinfra.agent.subagent import SubagentManager
from nanoinfra.agent.tools import groups as tool_groups
from nanoinfra.agent.tools import mcp as mcp_tools
from nanoinfra.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from nanoinfra.agent.tools.exec_session import ExecSessionManager
from nanoinfra.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from nanoinfra.agent.tools.message import MessageTool
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.agent.tools.self import MyTool
from nanoinfra.agent.turn_delivery import (
    TurnDelivery,
    TurnDeliveryFactory,
)
from nanoinfra.agent.turn_delivery import TurnRoute as TurnRoute
from nanoinfra.agent.turn_hooks import AgentTurnHookSpec, build_agent_turn_hook
from nanoinfra.automations.commissioning import bind_commissioning
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.bus.outbound_events import StreamedResponseEvent
from nanoinfra.bus.queue import MessageBus
from nanoinfra.bus.runtime_events import RuntimeEventBus
from nanoinfra.command import CommandContext, CommandRouter, register_builtin_commands
from nanoinfra.config.schema import AgentDefaults, ModelPresetConfig
from nanoinfra.connectors import attachment as connector_attachment
from nanoinfra.llm_usage.context import llm_usage_source, source_from_request
from nanoinfra.providers.base import LLMProvider, LLMUsage, ProviderConversationState
from nanoinfra.providers.factory import ProviderSnapshot
from nanoinfra.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    RUNTIME_CONTEXT_MESSAGE_META,
    RuntimeContextBlock,
    RuntimeContextProvider,
    append_runtime_context,
    resolve_runtime_context,
    runtime_context_blocks_from_metadata,
)
from nanoinfra.security.workspace_access import (
    WorkspaceScopeResolver,
    bind_workspace_scope,
    reset_workspace_scope,
)
from nanoinfra.session import turn_continuation
from nanoinfra.session.automation_turns import (
    automation_declared_skills,
    automation_history_overrides,
)
from nanoinfra.session.goal_state import (
    goal_state_runtime_lines,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from nanoinfra.session.history_visibility import HIDDEN_HISTORY_META
from nanoinfra.session.keys import UNIFIED_SESSION_KEY, remember_last_channel
from nanoinfra.session.manager import (
    Session,
    SessionManager,
    replay_max_messages_for_context,
)
from nanoinfra.session.model_selection import (
    SESSION_MODEL_PRESET_METADATA_KEY,
    model_preset_from_metadata,
)
from nanoinfra.triggers.local_turns import LocalTriggerTurnCoordinator
from nanoinfra.utils.cancellation import task_is_cancelling
from nanoinfra.utils.document import reference_non_image_attachments
from nanoinfra.utils.helpers import count_text_tokens, image_placeholder_text, open_tool_call_ids
from nanoinfra.utils.helpers import declared_tool_call_ids as declared_tool_call_ids_of
from nanoinfra.utils.helpers import truncate_text as truncate_text_fn
from nanoinfra.utils.llm_runtime import LLMRuntime
from nanoinfra.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
)

if TYPE_CHECKING:
    from nanoinfra.agent.tools.mcp import MCPConnection
    from nanoinfra.config.connectors import ConnectorRuntimeConfig
    from nanoinfra.config.schema import (
        ChannelsConfig,
        Config,
        MCPServerConfig,
        ProviderConfig,
        ToolsConfig,
    )
    from nanoinfra.cron.service import CronService
    from nanoinfra.triggers.local_store import LocalTriggerStore

_T = TypeVar("_T")
_SUBAGENT_PROVIDER_TASK_META = "subagent_provider_task_id"


class TurnKind(Enum):
    USER = auto()
    SYSTEM = auto()


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    turn_id: str
    runtime: LLMRuntime | None
    kind: TurnKind
    delivery: TurnDelivery
    original_user_text: str | None = None
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)
    provider_state: ProviderConversationState | None = field(default=None, repr=False)
    request_context: RequestContext | None = None
    runtime_context_blocks: list[RuntimeContextBlock] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    final_content: str | None = None
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False
    streamed_content: bool = False

    input_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None
    suppress_response: bool = False

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_runtime_admitted: Callable[[LLMRuntime], Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue[InboundMessage] | None = None
    pending_summary: str | None = None

    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False
    hooks: list[AgentHook] = field(default_factory=list)
    hook_factories: list[AgentTurnHookFactory] = field(default_factory=list)
    turn_scopes: list[AbstractContextManager[Any]] = field(default_factory=list)
    tools: ToolRegistry | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    visible_run_started_at: float | None = None
    turn_latency_ms: int | None = None
    #: What this turn's prompt was made of, by section (#203). `None` when it could not be built,
    #: which is a missing diagnostic rather than a failed turn.
    prompt_manifest: dict[str, Any] | None = None

    def require_runtime(self) -> LLMRuntime:
        """Return the runtime established by the BUILD stage."""
        if self.runtime is None:
            raise RuntimeError("turn runtime is not initialized; BUILD must run before this stage")
        return self.runtime

    def require_session(self) -> Session:
        """Return the session established by the RESTORE stage."""
        if self.session is None:
            raise RuntimeError("turn session is not initialized; RESTORE must run before this stage")
        return self.session



def _redacted_for_session(
    messages: list[dict[str, Any]], workspace: Path | str | None
) -> list[dict[str, Any]]:
    """Scrub stored secret values out of the messages a turn persists (#17).

    The executor holds the sentinels and performs the scrub (#41). A workspace with no stored
    secret asks nothing, so the common case costs no round trip.

    A turn that no scrubber answered persists its text as a marker. That direction is the point
    of #41: the old code logged the failure and persisted the turn unredacted, which is fail
    open on the one path #17 exists to close. The record keeps its shape, so the turn is still
    there and the marker says why its text is not.
    """
    from nanoinfra.agent.redaction import TranscriptRedactor

    # max_tool_result_chars=None: this loop applies the session's own bound a few lines later,
    # and two different bounds on one string would truncate twice.
    return TranscriptRedactor.for_workspace(workspace).messages(
        messages, max_tool_result_chars=None
    )


def _redacted_checkpoint(
    payload: dict[str, Any], workspace: Path | str | None
) -> dict[str, Any]:
    """Scrub stored secret values out of the checkpoint a turn persists (#51).

    The checkpoint is the second path into ``sessions/*.jsonl``, and it carried no redactor.
    ``for_workspace`` is the same construction ``_redacted_for_session`` uses, so the executor
    holds the sentinels and this process decrypts nothing (#41).

    The cost is one round trip per text, on top of the round trips the message path already
    spends for the same texts, and only for a workspace that holds a stored secret. A
    checkpoint lands on every turn that runs a tool, so that cost is real. It buys the one
    thing a cheaper answer cannot: the metadata line and the message lines of one file scrub
    by one rule.
    """
    from nanoinfra.agent.redaction import TranscriptRedactor

    return TranscriptRedactor.for_workspace(workspace).checkpoint(payload)


def mid_turn_route(
    raw: str,
    *,
    turn_active: bool,
    mode: str,
    commands: Any,
) -> str:
    """Where a message goes when a turn is already running for its session (#209).

    Three answers. `command` dispatches inline, because `/status` behind a seven-minute turn is a
    command nobody can use. `inject` folds the message into the running turn, which is what this
    did unconditionally before -- three corrections arriving in 47 seconds became one turn with one
    `turn_id`, and the agent worked seven more minutes on the plan it had already made. `dispatch`
    creates a task that takes the session lock, so the message waits and then gets a turn of its
    own.

    A mode this function does not recognise dispatches. The schema refuses a bad value, so that
    only fires if one arrives another way, and a message routed nowhere is a message dropped.
    """
    if turn_active and commands.is_dispatchable_command(raw):
        return "command"
    if turn_active and mode == "inject":
        return "inject"
    return "dispatch"


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    @property
    def provider(self) -> LLMProvider:
        """Provider selected for future turn admissions."""
        return self.runtime_resolver.runtime.provider

    @property
    def model(self) -> str:
        """Model selected for future turn admissions."""
        return self.runtime_resolver.runtime.model

    @property
    def context_window_tokens(self) -> int:
        """Context limit selected for future turn admissions."""
        return self.runtime_resolver.runtime.context_window_tokens

    @property
    def model_presets(self) -> Mapping[str, ModelPresetConfig]:
        """Configured model presets exposed for selection and display."""
        return self.runtime_resolver.model_presets

    @property
    def model_preset(self) -> str | None:
        return self.runtime_resolver.model_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def llm_runtime(self) -> LLMRuntime:
        """Resolve the immutable default used to admit the next turn."""
        previous = self.runtime_resolver.runtime
        runtime = self.runtime_resolver.admit()
        if (
            runtime.model != previous.model
            or runtime.model_preset != previous.model_preset
            or runtime.snapshot_signature != previous.snapshot_signature
        ):
            self._publish_runtime_selection(runtime)
        return runtime

    def dream_runtime(self) -> LLMRuntime | None:
        """Resolve the optional preset used for Dream without changing defaults."""
        if not self.dream_model_preset:
            return None
        return self.runtime_resolver.resolve_preset(self.dream_model_preset)

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"
    _PROVIDER_STATE_CHECKPOINT_VERSION_KEY = "provider_state_checkpoint_version"
    _PROVIDER_STATE_CHECKPOINT_VERSION = "v1"

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        fail_on_tool_error: bool | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict[str, MCPServerConfig] | None = None,
        connectors_config: ConnectorRuntimeConfig | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        hooks: list[AgentHook] | None = None,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        image_generation_provider_config: ProviderConfig | None = None,
        image_generation_provider_configs: dict[str, ProviderConfig] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        preset_catalog_loader: preset_helpers.PresetCatalogLoader | None = None,
        model_preset: str | None = None,
        dream_model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_events: RuntimeEventBus | None = None,
        turn_delivery_factory: TurnDeliveryFactory | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        restart_mode: str = "auto",
        local_trigger_store: LocalTriggerStore | None = None,
        idle_compact_check_interval_seconds: int = 0,
        mid_turn_messages: str = "queue",
        gate: Any = None,
    ):
        from nanoinfra.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        if turn_delivery_factory is not None:
            if turn_delivery_factory.bus is not bus:
                raise ValueError("turn delivery factory must use the agent message bus")
            if (
                runtime_events is not None
                and turn_delivery_factory.runtime_events is not runtime_events
            ):
                raise ValueError("turn delivery factory must use the agent runtime event bus")
            self.turn_delivery_factory = turn_delivery_factory
            self.runtime_events = turn_delivery_factory.runtime_events
        else:
            self.runtime_events = runtime_events or RuntimeEventBus()
            self.turn_delivery_factory = TurnDeliveryFactory(bus, self.runtime_events)
        self.runtime_event_publisher = self.turn_delivery_factory.runtime_event_publisher
        self.channels_config = channels_config
        self.restart_mode = restart_mode
        self._runtime_model_publisher = runtime_model_publisher
        self.workspace = workspace
        initial_model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        initial_context_window = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        configured_presets = model_presets or {}
        self.runtime_resolver = ModelRuntimeResolver(
            LLMRuntime.capture(
                provider,
                initial_model,
                context_window_tokens=initial_context_window,
                snapshot_signature=provider_signature,
            ),
            model_presets=configured_presets,
            preset_catalog_loader=preset_catalog_loader,
            configured_default_preset=model_preset,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
        )
        self.dream_model_preset = dream_model_preset
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self._image_generation_provider_configs = dict(image_generation_provider_configs or {})
        if (
            image_generation_provider_config is not None
            and "openrouter" not in self._image_generation_provider_configs
        ):
            self._image_generation_provider_configs["openrouter"] = image_generation_provider_config
        self.cron_service = cron_service
        self.local_trigger_store = local_trigger_store
        # #33: the gate runtime, built once by the gateway. It holds the gate half only.
        self.gate = gate
        self.restrict_to_workspace = restrict_to_workspace
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        self._start_time = time.time()
        self._last_usage: LLMUsage | None = None
        self._extra_hooks: list[AgentHook] = hooks or []
        self._hook_factories: list[AgentTurnHookFactory] = hook_factories or []

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self.sessions.set_file_cap_archiver(self.context.memory.raw_archive)
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        # SessionManager owns every durable deletion entrypoint, the WebUI and the fork rollback
        # paths included, so the boundary is observed once here rather than each caller
        # remembering to drop this process-local state (#145, upstream 2f19068e).
        self.sessions.set_delete_observer(self._file_state_store.discard)
        self._exec_session_manager = ExecSessionManager()
        self.runner = AgentRunner()
        self.subagents = SubagentManager(
            workspace=workspace,
            bus=bus,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=max_concurrent_subagents,
            fail_on_tool_error=fail_on_tool_error,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
        )
        self._unified_session = unified_session
        self._running = False
        self._mcp_servers = mcp_servers or {}
        # Which of them wait to be named (#204). Recorded from the same map the loop connects from,
        # so an `available()` check cannot consult a mode for a server that is not there.
        mcp_tools.set_server_attach_modes(self._mcp_servers)
        # And which groups of built-in tools do (#210). Seeded from the same `ToolsConfig` the
        # registry is built from, for the same reason: a mode for a group nobody declared would be
        # a gate on nothing.
        tool_groups.set_tool_groups(_tc.groups)
        # Data connectors (#connectors). None in an embedded construction, and no connector
        # then activates -- the same posture as an absent gates block: an omission widens
        # nothing.
        self._connectors_config = connectors_config
        self._mcp_stacks: dict[str, MCPConnection] = {}
        self._mcp_connecting = False
        self._runtime_context_providers: list[RuntimeContextProvider] = []
        self._active_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._close_mcp_lock = asyncio.Lock()
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._deferred_automation_turns: dict[str, list[InboundMessage]] = {}
        self._cron_turns = CronTurnCoordinator(
            publish_inbound=self.bus.publish_inbound,
            dispatch=self._dispatch,
            is_running=lambda: self._running,
            deferred_queues=self._deferred_automation_turns,
        )
        self._local_trigger_turns = LocalTriggerTurnCoordinator(
            publish_inbound=self.bus.publish_inbound,
            dispatch=self._dispatch,
            is_running=lambda: self._running,
            deferred_queues=self._deferred_automation_turns,
        )
        self._automation_turn_coordinators = (
            ("cron", self._cron_turns),
            ("local trigger", self._local_trigger_turns),
        )
        # NANOINFRA_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOINFRA_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            sessions=self.sessions,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            consolidation_ratio=consolidation_ratio,
            unified_session=unified_session,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self._idle_compact_check_interval_s = idle_compact_check_interval_seconds
        self._mid_turn_messages = mid_turn_messages
        self._next_idle_compact_check_at = time.monotonic()
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools(provider_snapshot_loader=provider_snapshot_loader)
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Config,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from nanoinfra.agent.plugins import merged_mcp_servers
        from nanoinfra.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop("preset_snapshot_loader", None) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            max_concurrent_subagents=defaults.max_concurrent_subagents,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            fail_on_tool_error=defaults.fail_on_tool_error,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            # Merged, not raw: this is the map the loop actually connects from, so a paused
            # server (#206) and a plugin-declared one (#140) both have to be resolved here rather
            # than only on the next hot reload, which is what `reload_servers` already does.
            mcp_servers=merged_mcp_servers(config),
            connectors_config=config.connectors,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            disabled_skills=defaults.disabled_skills,
            session_ttl_minutes=defaults.session_ttl_minutes,
            idle_compact_check_interval_seconds=defaults.idle_compact_check_interval_seconds,
            mid_turn_messages=defaults.mid_turn_messages,
            consolidation_ratio=defaults.consolidation_ratio,
            tools_config=config.tools,
            model_presets=preset_helpers.configured_model_presets(config),
            model_preset=defaults.model_preset,
            dream_model_preset=defaults.dream.model_override,
            restart_mode=config.gateway.restart_mode,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
            **extra,
        )

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def invalidate_runtime_config(self) -> None:
        """Invalidate runtime config and notify clients to refresh its catalog."""
        self.runtime_resolver.invalidate()
        self._publish_runtime_selection(self.runtime_resolver.runtime)

    def runtime_for_session(
        self,
        session: Session,
        *,
        recover_removed: bool = True,
    ) -> LLMRuntime:
        """Resolve the immutable runtime selected by one session."""
        name = model_preset_from_metadata(session.metadata)
        if name is None:
            return self.llm_runtime()
        try:
            return self.runtime_resolver.resolve_preset(name)
        except KeyError:
            if not recover_removed or name in self.runtime_resolver.model_presets:
                raise
            logger.warning(
                "Session '{}' references removed model preset '{}'; falling back to default",
                session.key,
                name,
            )
            session.metadata.pop(SESSION_MODEL_PRESET_METADATA_KEY, None)
            self.sessions.save(session)
            return self.llm_runtime()

    def set_session_model_preset(
        self,
        session_key: str,
        name: str,
    ) -> LLMRuntime:
        """Validate and persist one session's preset selection."""
        runtime = self.runtime_resolver.resolve_preset(name)
        session = self.sessions.get_or_create(session_key)
        session.metadata[SESSION_MODEL_PRESET_METADATA_KEY] = runtime.model_preset
        self.sessions.save(session)
        return runtime

    def _publish_runtime_selection(
        self,
        runtime: LLMRuntime,
        *,
        publish_update: bool = True,
    ) -> None:
        if not publish_update:
            return
        if self._runtime_model_publisher is not None:
            self._runtime_model_publisher(runtime.model, runtime.model_preset)
        self.runtime_event_publisher.runtime_model_changed(
            runtime.model,
            runtime.model_preset,
        )

    def set_model_preset(
        self,
        name: str | None,
        *,
        publish_update: bool = True,
    ) -> LLMRuntime:
        """Select a named default runtime for future turns."""
        old_model = self.model
        runtime = self.runtime_resolver.select_preset(name)
        self._publish_runtime_selection(runtime, publish_update=publish_update)
        logger.info(
            "Runtime model switched for next turn: {} -> {}",
            old_model,
            runtime.model,
        )
        return runtime

    def set_runtime_model(self, model: str) -> LLMRuntime:
        """Select a model on the current provider for future turns."""
        return self.runtime_resolver.select_model(model)

    def set_runtime_context_window(self, context_window_tokens: int) -> LLMRuntime:
        """Select a context limit for future turns."""
        return self.runtime_resolver.select_context_window(context_window_tokens)

    def build_tool_context(
        self,
        *,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
    ) -> Any:
        """The context a tool is constructed with.

        Public because a reload needs it too (#194): connector tools are rebuilt against config
        that changed after boot, and rebuilding them needs the same collaborators the first
        registration used.
        """
        from nanoinfra.agent.tools.context import ToolContext

        return ToolContext(
            config=self.tools_config,
            # #33: the gate runtime the gateway built at boot. None in an embedded or a
            # test construction, and the tool then falls back to policy alone.
            gate=self.gate,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            commission_automation=self.commission_automation_later,
            exec_session_manager=self._exec_session_manager,
            sessions=self.sessions,
            provider_snapshot_loader=provider_snapshot_loader,
            image_generation_provider_configs=self._image_generation_provider_configs,
            timezone=self.context.timezone or "UTC",
            disabled_skills=frozenset(self.context.skills.disabled_skills),
            workspace_sandbox=self.workspace_scopes.sandbox_status,
            runtime_events=self.runtime_events,
        )

    def _register_default_tools(
        self,
        *,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None,
    ) -> None:
        """Register the default set of tools via plugin loader."""
        from nanoinfra.agent.tools.loader import ToolLoader

        ctx = self.build_tool_context(provider_snapshot_loader=provider_snapshot_loader)
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # One tool per enabled operation of every active connector, each carrying the class
        # its manifest declared. Registered here rather than discovered by the loader scan,
        # because a connector's tools exist per configured connector and not per class in the
        # tree.
        from nanoinfra.connectors.registration import register_connector_tools

        registered.extend(
            register_connector_tools(ctx, self.tools, self._connectors_config)
        )

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        logger.info("Registered {} tools: {}", len(registered), registered)

    async def _connect_mcp(self) -> None:
        """Connect configured MCP servers."""
        await agent_context.connect_mcp(self, self.tools)

    def register_runtime_context_provider(
        self,
        provider: RuntimeContextProvider,
    ) -> Callable[[], None]:
        """Register a per-turn context provider and return an unsubscribe callback."""
        if provider in self._runtime_context_providers:
            return lambda: None
        self._runtime_context_providers.append(provider)

        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._runtime_context_providers.remove(provider)

        return _unsubscribe

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        return await self._cron_turns.submit(msg)

    async def submit_local_trigger_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        return await self._local_trigger_turns.submit(msg)

    def pending_cron_job_ids_for_session(self, session_key: str) -> set[str]:
        return self._cron_turns.pending_job_ids_for_session(session_key)

    def pending_local_trigger_ids_for_session(self, session_key: str) -> set[str]:
        return self._local_trigger_turns.pending_trigger_ids_for_session(session_key)

    async def _publish_next_deferred_automation_turn(self, session_key: str) -> None:
        await publish_next_deferred_turn(
            deferred_queues=self._deferred_automation_turns,
            publish_inbound=self.bus.publish_inbound,
            session_key=session_key,
        )

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        runtime_context_blocks: list[RuntimeContextBlock] | None = None,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        if not turn_continuation.should_persist_user_message(msg.metadata):
            return False
        media_paths = [
            path
            for path in (msg.media or [])
            if isinstance(cast(object, path), str) and path
        ]
        content_value = cast(object, msg.content)
        has_text = isinstance(content_value, str) and content_value.strip()
        if has_text or media_paths or runtime_context_blocks:
            extra: dict[str, Any] = ({"media": list(media_paths)} if media_paths else {}) | agent_context.session_extra(msg.metadata)
            extra.update(kwargs)
            text = content_value if isinstance(content_value, str) else ""
            text_override, automation_extra = automation_history_overrides(msg.metadata)
            if text_override is not None:
                text = text_override
            extra.update(automation_extra)
            text, runtime_context_meta = append_runtime_context(
                text,
                runtime_context_blocks or (),
            )
            if runtime_context_meta is not None:
                extra[RUNTIME_CONTEXT_HISTORY_META] = runtime_context_meta
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(self, ctx: TurnContext) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        assert ctx.session is not None
        scope = self.workspace_scopes.for_message(ctx.msg, ctx.session.metadata)
        messages = self.context.build_messages(
            history=ctx.history,
            current_message=ctx.msg.content,
            media=ctx.msg.media if ctx.kind is TurnKind.USER and ctx.msg.media else None,
            channel=ctx.delivery.route.channel,
            session_summary=ctx.pending_summary,
            workspace=scope.project_path,
            runtime_context_blocks=ctx.runtime_context_blocks,
            include_memory_recent_history=not ctx.ephemeral,
            session_key=ctx.session.key,
            unified_session=self._unified_session,
            declared_skills=automation_declared_skills(ctx.msg.metadata),
            mcp_advertisement=mcp_tools.advertisement(self.tools),
            connector_advertisement=connector_attachment.advertisement(self.tools),
            group_advertisement=tool_groups.advertisement(self.tools),
        )
        ctx.prompt_manifest = self._prompt_manifest_for(ctx, messages)
        return messages

    def _prompt_manifest_for(
        self, ctx: TurnContext, messages: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """What this turn's prompt is made of, for the debug panel (#203).

        Read here rather than returned by the builder, because the builder has a dozen call sites
        and only this one is about to send the result.

        Two things are added on top of the system prompt's own sections. The **tool schemas**,
        which are the larger half of a turn -- ~23K against a 7.4K prompt on the deployment this
        was measured on -- and are not part of the message list at all. And the **messages**,
        counted as one row: their content is the conversation, and a manifest records sizes.

        Never fails a turn: a diagnostic that can raise is worse than one that is missing.
        """
        try:
            payload = self.context.last_manifest.as_dict()
            sections: list[dict[str, Any]] = payload["sections"]

            tools = ctx.tools or self.tools
            for row in tools.schema_breakdown():
                sections.append({
                    "name": row["source"],
                    "chars": row["chars"],
                    "tokens": row["tokens"],
                    "group": "tools",
                    "items": row["items"],
                    # Which tools, largest first. The panel's `×31` was a count that raised the
                    # question it could not answer (#203).
                    "tools": row["tools"],
                })

            conversation = [
                message for message in messages if message.get("role") != "system"
            ]
            if conversation:
                text = "\n".join(
                    content if isinstance(content := message.get("content"), str) else ""
                    for message in conversation
                )
                sections.append({
                    "name": "Messages",
                    "chars": len(text),
                    "tokens": count_text_tokens(text),
                    "group": "messages",
                    "items": len(conversation),
                })

            groups: dict[str, int] = {}
            for section in sections:
                group = str(section["group"])
                groups[group] = groups.get(group, 0) + int(section["tokens"])
            payload["groups"] = groups
            payload["total_tokens"] = sum(int(section["tokens"]) for section in sections)
            return payload
        except Exception:
            logger.debug("prompt manifest unavailable for this turn", exc_info=True)
            return None

    def _request_context_for_turn(self, ctx: TurnContext) -> RequestContext:
        assert ctx.session is not None
        scope = self.workspace_scopes.for_turn(
            channel=ctx.delivery.route.channel,
            message_metadata=ctx.msg.metadata,
            session_metadata=ctx.session.metadata,
        )
        return RequestContext(
            channel=ctx.delivery.route.channel,
            chat_id=ctx.delivery.route.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            session_key=ctx.session_key,
            original_user_text=ctx.original_user_text,
            runtime=ctx.runtime,
            metadata=dict(ctx.msg.metadata or {}),
            attributes=dict(ctx.attributes),
            sender_id=ctx.msg.sender_id,
            turn_id=ctx.turn_id,
            workspace=scope.project_path,
            # A cron run and a local trigger reach this same builder. So the value comes
            # from the turn, and never from the delivery route. The inbound channel is
            # passed only to catch the "system" channel, which carries a subagent's own
            # announcement and therefore no human intent.
            execution_context=execution_context_for_turn(
                ctx.msg.metadata,
                ctx.session.metadata,
                channel=ctx.msg.channel,
            ),
        )

    async def _resolve_runtime_context_for_turn(
        self,
        ctx: TurnContext,
    ) -> list[RuntimeContextBlock]:
        assert ctx.request_context is not None
        return await self._resolve_runtime_context_for_request(
            ctx.request_context,
            ctx.tools or self.tools,
        )

    async def _resolve_runtime_context_for_request(
        self,
        request: RequestContext,
        tools: ToolRegistry,
    ) -> list[RuntimeContextBlock]:
        providers = [
            *tools.get_runtime_context_providers(),
            *self._runtime_context_providers,
        ]
        blocks = runtime_context_blocks_from_metadata(request.metadata)
        blocks.extend(await resolve_runtime_context(providers, request))
        return blocks

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = tuple(self._active_tasks.pop(key, set()))
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _remember_unified_session_route(
        self,
        session: Session,
        msg: InboundMessage,
        *,
        is_user_turn: bool,
    ) -> None:
        """Remember the latest user-facing route for unified-session delivery."""
        if (
            not self._unified_session
            or session.key != UNIFIED_SESSION_KEY
            or not is_user_turn
            or msg.channel in {"cli", "system"}
            or msg.sender_id == "subagent"
        ):
            return
        _, automation_metadata = automation_history_overrides(msg.metadata)
        if automation_metadata:
            return
        remember_last_channel(session.metadata, msg.channel, msg.chat_id)

    @staticmethod
    def _replay_token_budget(runtime: LLMRuntime) -> int:
        """Derive a token budget for session history replay from the context window."""
        if runtime.context_window_tokens <= 0:
            return 0
        max_output = runtime.generation.max_tokens
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = runtime.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, runtime.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict[str, Any]],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        runtime: LLMRuntime,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        original_user_text: str | None = None,
        pending_queue: asyncio.Queue[InboundMessage] | None = None,
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
        hooks: list[AgentHook] | None = None,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        turn_scopes: list[AbstractContextManager[Any]] | None = None,
        tools: ToolRegistry | None = None,
        request_context: RequestContext | None = None,
        provider_state: ProviderConversationState | None = None,
    ) -> tuple[str | None, list[str], list[dict[str, Any]], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming, merge_next)*: called when a streaming session finishes.
        ``resuming=True`` means the active turn continues. ``merge_next=True`` means
        the next text segment belongs to the same user-visible assistant message.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            public_payload = dict(payload)
            private_state = public_payload.pop("provider_state", None)
            public_payload.pop(self._PROVIDER_STATE_CHECKPOINT_VERSION_KEY, None)
            if "provider_state" in payload and (
                private_state is None
                or isinstance(private_state, ProviderConversationState)
            ):
                session.provider_state = private_state
                public_payload[self._PROVIDER_STATE_CHECKPOINT_VERSION_KEY] = (
                    self._PROVIDER_STATE_CHECKPOINT_VERSION
                )
            self._set_runtime_checkpoint(session, public_payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            async def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                image_paths = pending_msg.media if pending_msg.media else None
                if image_paths:
                    content, image_paths = reference_non_image_attachments(
                        content,
                        image_paths,
                    )
                    image_paths = image_paths or None
                user_content = self.context.build_user_content(
                    content,
                    image_paths=image_paths,
                )
                row: dict[str, Any] = {"role": "user", "content": user_content}
                metadata_value = cast(object, pending_msg.metadata)
                metadata = (
                    pending_msg.metadata
                    if isinstance(metadata_value, dict)
                    else {}
                )
                if pending_msg.channel != "system":
                    scope = self.workspace_scopes.for_turn(
                        channel=pending_msg.channel,
                        message_metadata=metadata,
                        session_metadata=session.metadata if session is not None else None,
                    )
                    pending_request = RequestContext(
                        channel=pending_msg.channel,
                        chat_id=pending_msg.chat_id,
                        message_id=metadata.get("message_id"),
                        session_key=active_session_key,
                        original_user_text=pending_msg.content,
                        runtime=runtime,
                        metadata=dict(metadata),
                        attributes=dict(request_ctx.attributes),
                        sender_id=pending_msg.sender_id,
                        turn_id=request_ctx.turn_id,
                        workspace=scope.project_path,
                        # A mid-turn injection carries its own trigger metadata. The session
                        # may also hold an active goal. So classify it like any other turn,
                        # and pass the inbound channel for the same reason as above: an
                        # injected "system" turn is a subagent announcement, not a person.
                        execution_context=execution_context_for_turn(
                            metadata,
                            session.metadata if session is not None else None,
                            channel=pending_msg.channel,
                        ),
                    )
                    blocks = await self._resolve_runtime_context_for_request(
                        pending_request,
                        effective_tools,
                    )
                    row["content"], runtime_marker = append_runtime_context(
                        user_content,
                        blocks,
                    )
                    if runtime_marker is not None:
                        row["_meta"] = {
                            RUNTIME_CONTEXT_MESSAGE_META: runtime_marker,
                        }
                if (
                    pending_msg.sender_id == "subagent"
                    and metadata.get("injected_event") == "subagent_result"
                ):
                    subagent_marker: dict[str, Any] = {"kind": "subagent_result"}
                    task_id = metadata.get("subagent_task_id")
                    if isinstance(task_id, str) and task_id:
                        subagent_marker["subagent_task_id"] = task_id
                        row["subagent_task_id"] = task_id
                    row[HIDDEN_HISTORY_META] = subagent_marker
                    row["injected_event"] = "subagent_result"
                return row

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(await _to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(await _to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(await _to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        effective_scope = self.workspace_scopes.for_turn(
            channel=channel,
            message_metadata=metadata,
            session_metadata=session.metadata if session is not None else None,
        )
        effective_tools = tools or self.tools
        # Every turn passes a context from _request_context_for_turn. This fallback covers
        # a caller that passes none. So it keeps the fail-closed execution context.
        request_ctx = request_context or RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=active_session_key,
            original_user_text=original_user_text,
            runtime=runtime,
            metadata=dict(metadata or {}),
            workspace=effective_scope.project_path,
        )
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        request_token = bind_request_context(request_ctx)
        workspace_token = bind_workspace_scope(effective_scope)
        turn_scope_stack = ExitStack()
        # A commissioning turn previews every gated action instead of taking it (#182). The
        # binding happens here because this is the task the tools run in: the turn crossed the
        # bus, so a context variable set by whoever submitted it never reached them.
        turn_scope_stack.enter_context(bind_commissioning(request_ctx.metadata))
        # What kind of turn this is, for the store's rows (#176). Bound on the same stack and for
        # the same reason as the line above: the turn crossed the bus, so anything set by whoever
        # submitted it never reached the task the provider calls run in. The classification is
        # coarse on purpose -- `nanoinfra/llm_usage/context.py` keeps none of the key it read.
        turn_scope_stack.enter_context(
            llm_usage_source(
                source_from_request(session_key, channel=channel, metadata=metadata)
            )
        )
        # Compute lazily because create_goal may create goal metadata during this run.
        def _goal_continue() -> str | None:
            _goal_lines = goal_state_runtime_lines(session.metadata if session is not None else None)
            if not _goal_lines:
                return None
            return (
                "You have an active sustained goal:\n\n"
                + "\n".join(_goal_lines)
                + "\n\nPlease continue working toward the objective using your tools, "
                "or call update_goal with action='complete' if the work is truly finished."
            )

        session_metadata = session.metadata if session is not None else None
        try:
            for scope in turn_scopes or ():
                turn_scope_stack.enter_context(scope)
            hook = build_agent_turn_hook(AgentTurnHookSpec(
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                channel=channel,
                chat_id=chat_id,
                message_id=message_id,
                metadata=metadata,
                attributes=dict(request_ctx.attributes),
                session_key=active_session_key,
                workspace=effective_scope.project_path,
                tool_hint_max_length=self.tool_hint_max_length,
                on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
                registered_hook_factories=self._hook_factories,
                turn_hook_factories=list(hook_factories or []),
                registered_hooks=self._extra_hooks,
                turn_hooks=list(hooks or []),
                ephemeral=ephemeral,
                run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
            ))
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=effective_tools,
                runtime=runtime,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=effective_scope.project_path,
                session_key=session.key if session else None,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                # Sustained goals may legitimately exceed NANOINFRA_LLM_TIMEOUT_S; idle stall
                # is still capped by NANOINFRA_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=session_metadata,
                    message_metadata=metadata,
                ),
                goal_active_predicate=lambda: sustained_goal_active(session.metadata) if session is not None else False,
                goal_continue_message=_goal_continue,
                finalize_on_max_iterations=turn_continuation.should_finalize_on_max_iterations(
                    pending_queue_available=pending_queue is not None and session is not None,
                    session_metadata=session_metadata,
                    message_metadata=metadata,
                ),
                provider_state=provider_state,
            ))
        finally:
            turn_scope_stack.close()
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        # Recorded here rather than read from `_last_usage` at delivery time: that attribute is
        # loop-global, and between this line and the outbound frame there are awaits during which
        # another session's turn can overwrite it. The publisher keys by session (#202).
        if session_key:
            self.runtime_event_publisher.record_turn_usage(session_key, result.usage)
        if session is not None and not ephemeral:
            session.provider_state = result.provider_state
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            should_stream = turn_continuation.should_stream_budget_response(
                stop_reason=result.stop_reason,
                pending_queue_available=pending_queue is not None and session is not None,
                session_metadata=session_metadata,
                message_metadata=metadata,
            )
            # Push final content through stream so streaming channels (e.g. Telegram)
            # update the message instead of leaving it empty.
            if on_stream and on_stream_end and should_stream:
                stream_content = (
                    result.pending_stream_content
                    if result.pending_stream_content is not None
                    else result.final_content or ""
                )
                await on_stream(stream_content)
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    def _check_expired_sessions_if_due(self) -> None:
        """Scan idle sessions no more often than the configured interval."""
        now = time.monotonic()
        if now < self._next_idle_compact_check_at:
            return
        self._next_idle_compact_check_at = now + self._idle_compact_check_interval_s
        self.auto_compact.check_expired(
            self.schedule_background,
            self.runtime_for_session,
            active_session_keys=self._pending_queues.keys(),
        )

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        try:
            await self._connect_mcp()
            logger.info("Agent loop started")

            while self._running:
                try:
                    msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    self._check_expired_sessions_if_due()
                    continue
                except asyncio.CancelledError:
                    # Preserve real task cancellation so shutdown can complete cleanly.
                    # Only ignore non-task CancelledError signals that may leak from integrations.
                    if not self._running or task_is_cancelling():
                        raise
                    logger.warning(
                        "Ignoring leaked CancelledError while consuming inbound messages"
                    )
                    continue
                except Exception as e:
                    logger.warning("Error consuming inbound message: {}, continuing...", e)
                    continue

                raw = msg.content.strip()
                effective_key = self._effective_session_key(msg)
                if await agent_context.handle_runtime_control(self, msg, self.tools):
                    continue
                if self.commands.is_priority(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch_priority,
                    )
                    continue
                deferred = False
                for label, coordinator in self._automation_turn_coordinators:
                    if coordinator.defer_if_active(
                        msg,
                        session_key=effective_key,
                        active_session_keys=self._pending_queues.keys(),
                    ):
                        logger.info(
                            "Deferred {} turn for active session {}",
                            label,
                            effective_key,
                        )
                        deferred = True
                        break
                if deferred:
                    continue
                # Where this goes when a turn is already running for the session (#209).
                # `mid_turn_messages` decides: fold into the turn in flight, or wait for one of
                # its own. `dispatch` falls through to the task below, which takes the session
                # lock and therefore queues behind the turn already holding it.
                route = mid_turn_route(
                    raw,
                    turn_active=effective_key in self._pending_queues,
                    mode=self._mid_turn_messages,
                    commands=self.commands,
                )
                if route == "command":
                    # Non-priority commands must not be queued for injection;
                    # dispatch them directly (same pattern as priority commands).
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                if route == "inject":
                    pending_msg = msg
                    if effective_key != msg.session_key:
                        pending_msg = dataclasses.replace(
                            msg,
                            session_key_override=effective_key,
                        )
                    try:
                        self._pending_queues[effective_key].put_nowait(pending_msg)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Pending queue full for session {}, falling back to queued task",
                            effective_key,
                        )
                    else:
                        logger.info(
                            "Routed follow-up message to pending queue for session {}",
                            effective_key,
                        )
                        continue
                # Compute the effective session key before dispatching
                # This ensures /stop command can find tasks correctly when unified session is enabled
                task = asyncio.create_task(self._dispatch(msg))
                active_tasks = self._active_tasks.setdefault(effective_key, set())
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)
        finally:
            # MCP stdio transports use AnyIO cancel scopes; close them from the task that opened them.
            await self.close_mcp()

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._get_session_lock(session_key)
        gate = self._concurrency_gate or nullcontext()

        delivery = self.turn_delivery_factory.unrouted(msg, session_key)
        pending: asyncio.Queue[InboundMessage] | None = None
        try:
            async with lock, gate:
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                pending = asyncio.Queue(maxsize=20)
                self._pending_queues[session_key] = pending
                try:
                    delivery = self.turn_delivery_factory.create(
                        msg,
                        session_key,
                        enable_stream=True,
                    )
                    response = await self._process_message(
                        msg,
                        on_stream=delivery.on_stream,
                        on_stream_end=delivery.on_stream_end,
                        pending_queue=pending,
                        delivery=delivery,
                    )
                    continuing = turn_continuation.internal_continuation_pending(msg.metadata)
                    await delivery.complete(
                        response,
                        publish_completion=not continuing,
                    )
                    for _, coordinator in self._automation_turn_coordinators:
                        coordinator.complete(msg, response=response)
                except asyncio.CancelledError:
                    for _, coordinator in self._automation_turn_coordinators:
                        coordinator.complete(msg, error=asyncio.CancelledError())
                    logger.info("Task cancelled for session {}", session_key)
                    try:
                        await delivery.abort_stream()
                    except Exception:
                        logger.debug(
                            "Could not close stream for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    raise
                except Exception as exc:
                    logger.exception("Error processing message for session {}", session_key)
                    await delivery.fail(
                        publish_completion=not turn_continuation.internal_continuation_pending(
                            msg.metadata
                        )
                    )
                    for _, coordinator in self._automation_turn_coordinators:
                        coordinator.complete(msg, error=exc)
                finally:
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self._pending_queues.get(session_key) is pending:
                        queue = self._pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover, session_key,
                            )
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        await delivery.idle()
                    await self._publish_next_deferred_automation_turn(session_key)
        finally:
            if pending is None:
                await delivery.idle()
                await self._publish_next_deferred_automation_turn(session_key)

    async def close_mcp(self) -> None:
        """Stop active work, then close exec, subagent, and MCP resources.

        Resource teardown must still run if cancellation interrupts task draining.
        Gateway shutdown deliberately bounds this coroutine, so keeping the cleanup
        phase in ``finally`` prevents a timed-out background task from leaving
        subprocess transports alive after the event loop closes.
        """
        # The agent loop closes itself from ``run()`` while gateway shutdown also
        # performs a guaranteed final close. Serialize those owners so they cannot
        # tear down the same subprocess transports concurrently.
        close_lock = getattr(self, "_close_mcp_lock", None)
        if close_lock is None:
            close_lock = self._close_mcp_lock = asyncio.Lock()
        async with close_lock:
            await self._close_mcp_unlocked()

    async def _close_mcp_unlocked(self) -> None:
        errors: list[BaseException] = []
        active_task_groups = getattr(self, "_active_tasks", {})
        active_tasks = tuple({task for tasks in active_task_groups.values() for task in tasks})
        active_task_groups.clear()
        current_task = asyncio.current_task()
        active_tasks = tuple(task for task in active_tasks if task is not current_task)
        for task in active_tasks:
            if not task.done():
                task.cancel()
        try:
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            self._background_tasks.clear()

        cleanup_steps = (
            self.subagents.close,
            self._exec_session_manager.close_all,
            lambda: agent_context.close_mcp(self),
        )
        for cleanup in cleanup_steps:
            try:
                await cleanup()
            except BaseException as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("failed to close agent resources", errors)

    def commission_automation_later(self, job_id: str) -> None:
        """Rehearse a newly created cron job once the creating turn is done (#183).

        Fire and forget on purpose. The rehearsal is itself a turn, and awaiting it from inside
        the turn that created the automation would wait on a turn that cannot start until this
        one ends -- the message carries defer-until-idle, exactly as a scheduled run does.
        """
        if self.cron_service is None:
            return
        self.schedule_background(self._commission_automation(job_id))

    async def commission_automation_now(self, job_id: str) -> dict[str, Any]:
        """Rehearse an automation on request, and answer what was found (#183, Verify).

        Awaited, unlike the creation path: the caller is an operator route with nobody's turn to
        finish first. The verdict is written and the finding is delivered exactly as it is after
        a creation, so the two entry points cannot drift into telling different stories.
        """
        from nanoinfra.automations.commissioning_runner import commission_cron_job

        service = self.cron_service
        if service is None:
            raise RuntimeError("this deployment has no automation service")
        job = service.get_job(job_id)
        if job is None:
            raise RuntimeError("automation not found")
        report = await commission_cron_job(
            job,
            agent=self,
            workspace_path=Path(self.workspace),
            latches=self.gate,
        )
        service.set_commissioning(job_id, report.state, disable=report.refused)
        await self._deliver_commissioning_finding(job, report)
        return {
            "id": job.id,
            "name": job.name,
            "refused": report.refused,
            "commissioning": report.state.to_dict(),
        }

    async def _commission_automation(self, job_id: str) -> None:
        from nanoinfra.automations.commissioning_runner import commission_cron_job

        service = self.cron_service
        if service is None:
            return
        job = service.get_job(job_id)
        if job is None:
            return
        report = await commission_cron_job(
            job,
            agent=self,
            workspace_path=Path(self.workspace),
            latches=self.gate,
        )
        service.set_commissioning(job_id, report.state, disable=report.refused)
        await self._deliver_commissioning_finding(job, report)

    async def _deliver_commissioning_finding(self, job: Any, report: Any) -> None:
        """Tell the operator what the rehearsal found, in the chat that created it."""
        channel = job.payload.origin_channel or job.payload.channel
        chat_id = job.payload.origin_chat_id or job.payload.to
        if not channel or not chat_id:
            return
        if report.refused:
            header = (
                f"Automation '{job.name}' is saved but disabled: a rehearsal found it would be "
                "refused on its schedule."
            )
        else:
            header = f"Automation '{job.name}' rehearsed clean and is ready to run."
        lines = [header, "", report.state.finding]
        if report.state.proposed_grants:
            lines += [
                "",
                "Grant it in Settings, or add to gates.standingGrants:",
                json.dumps(list(report.state.proposed_grants), ensure_ascii=False, indent=2),
                "",
                "A grant covers that command on that host in any unattended turn, not this "
                "automation alone.",
            ]
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="\n".join(lines),
                metadata={"render_as": "text"},
            )
        )

    def schedule_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue[InboundMessage] | None = None,
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
        hooks: list[AgentHook] | None = None,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        tools: ToolRegistry | None = None,
        runtime: LLMRuntime | None = None,
        delivery: TurnDelivery | None = None,
        on_runtime_admitted: Callable[[LLMRuntime], Awaitable[None]] | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        kind = TurnKind.SYSTEM if msg.channel == "system" else TurnKind.USER
        if kind is TurnKind.SYSTEM:
            destination = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            key = session_key or msg.session_key_override or f"{destination[0]}:{destination[1]}"
        else:
            key = session_key or msg.session_key
        if delivery is None:
            delivery = self.turn_delivery_factory.create(msg, key)
        elif delivery.session_key != key:
            raise ValueError("turn delivery session does not match the processing session")
        if on_stream is None:
            on_stream = delivery.on_stream
        if on_stream_end is None:
            on_stream_end = delivery.on_stream_end
        t0 = time.time()
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            turn_id=f"{key}:{time.time_ns()}",
            runtime=runtime,
            kind=kind,
            delivery=delivery,
            original_user_text=(
                None
                if kind is TurnKind.SYSTEM
                or turn_continuation.internal_continuation_inbound(msg.metadata)
                else msg.content
            ),
            turn_wall_started_at=t0,
            visible_run_started_at=turn_continuation.internal_continuation_run_started_at(
                msg.metadata,
            ),
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_runtime_admitted=on_runtime_admitted,
            pending_queue=pending_queue,
            ephemeral=ephemeral,
            run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
            hooks=list(hooks or []),
            hook_factories=list(hook_factories or []),
            tools=tools,
            attributes=dict(attributes or {}),
        )
        # A streaming callback may be present even when the final text comes from a
        # non-streaming recovery. Only the last completed segment can suppress the
        # regular outbound message.
        if ctx.on_stream is not None:
            stream_callback = ctx.on_stream
            stream_end_callback = ctx.on_stream_end
            stream_end_accepts_merge_next = False
            if stream_end_callback is not None:
                try:
                    stream_end_signature = inspect.signature(stream_end_callback)
                    stream_end_accepts_merge_next = (
                        "merge_next" in stream_end_signature.parameters
                        or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in stream_end_signature.parameters.values()
                        )
                    )
                except (TypeError, ValueError):
                    pass
            segment_streamed_content = False

            async def _tracked_stream(delta: str) -> None:
                nonlocal segment_streamed_content
                if delta:
                    segment_streamed_content = True
                await stream_callback(delta)

            async def _tracked_stream_end(
                *,
                resuming: bool = False,
                merge_next: bool = False,
            ) -> None:
                nonlocal segment_streamed_content
                ctx.streamed_content = segment_streamed_content
                segment_streamed_content = False
                if stream_end_callback is not None:
                    if merge_next and stream_end_accepts_merge_next:
                        await stream_end_callback(resuming=resuming, merge_next=True)
                    else:
                        await stream_end_callback(resuming=resuming)

            ctx.on_stream = _tracked_stream
            ctx.on_stream_end = _tracked_stream_end

        await self._run_turn_stage(ctx, "restore", self._restore_turn)
        await self._run_turn_stage(ctx, "compact", self._compact_session)
        if await self._run_turn_stage(ctx, "command", self._dispatch_command):
            return ctx.outbound
        await self._run_turn_stage(ctx, "build", self._build_turn)
        await self._run_turn_stage(ctx, "run", self._run_turn)
        await self._run_turn_stage(ctx, "save", self._persist_turn)
        await self._run_turn_stage(ctx, "respond", self._prepare_outbound)
        return ctx.outbound

    async def _run_turn_stage(
        self,
        ctx: TurnContext,
        name: str,
        handler: Callable[[TurnContext], Awaitable[_T]],
    ) -> _T:
        started_at = time.perf_counter()
        try:
            result = await handler(ctx)
        except Exception:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.debug(
                "[turn {}] Stage {} failed after {:.1f}ms",
                ctx.turn_id,
                name,
                duration_ms,
            )
            raise
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.debug(
            "[turn {}] Stage {} completed in {:.1f}ms",
            ctx.turn_id,
            name,
            duration_ms,
        )
        return result

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        stop_reason: str,
        had_injections: bool,
        streamed_content: bool,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        event = None
        meta = dict(msg.metadata or {})
        if streamed_content and stop_reason not in {"error", "tool_error"}:
            event = StreamedResponseEvent()
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            event=event,
            metadata=meta,
        )

    async def _restore_turn(self, ctx: TurnContext) -> None:
        """Restore checkpoint / pending user turn; reference non-image attachments."""
        msg = ctx.msg

        if ctx.kind is TurnKind.USER and msg.media:
            new_content, image_paths = reference_non_image_attachments(
                msg.content,
                msg.media,
            )
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_paths)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        if ctx.kind is TurnKind.SYSTEM:
            logger.info("Processing system message from {}", msg.sender_id)
        else:
            logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        session = ctx.session
        self._remember_unified_session_route(
            session,
            msg,
            is_user_turn=ctx.original_user_text is not None,
        )
        await ctx.delivery.started()
        if ctx.kind is TurnKind.USER:
            self.workspace_scopes.persist_message_scope(session, msg)

        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

    async def _compact_session(self, ctx: TurnContext) -> None:
        session = ctx.require_session()
        ctx.session, pending = self.auto_compact.prepare_session(
            session,
            ctx.session_key,
        )
        ctx.pending_summary = pending

    async def _dispatch_command(self, ctx: TurnContext) -> bool:
        if ctx.kind is TurnKind.SYSTEM:
            return False
        session = ctx.require_session()
        raw = ctx.msg.content.strip()
        _, automation_metadata = automation_history_overrides(ctx.msg.metadata)
        is_user_turn = (
            ctx.original_user_text is not None
            and not automation_metadata
            and ctx.msg.channel != "system"
            and ctx.msg.sender_id != "subagent"
        )
        cmd_ctx = CommandContext(
            msg=ctx.msg,
            session=session,
            key=ctx.session_key,
            raw=raw,
            loop=self,
            runtime=ctx.runtime,
            is_user_turn=is_user_turn,
            turn_scopes=ctx.turn_scopes,
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if cmd_ctx.raw.lower() != "/new":
                ctx.input_persisted_early = self._persist_user_message_early(
                    ctx.msg, session, _command=True
                )
                session.add_message(
                    "assistant", result.content, _command=True
                )
                self._clear_pending_user_turn(session)
                self.sessions.save(session)
                if not ctx.ephemeral:
                    await self.runtime_event_publisher.session_turn_persisted(
                        ctx.msg,
                        ctx.session_key,
                        turn_id=ctx.turn_id,
                        attributes=ctx.attributes,
                    )
            return True
        return False

    async def _build_turn(self, ctx: TurnContext) -> None:
        session = ctx.require_session()
        runtime = ctx.runtime
        if runtime is None:
            runtime = self.runtime_for_session(session)
            ctx.runtime = runtime
        if ctx.session_key.startswith("dream:"):
            logger.info(
                "Dream run using model={} (preset={})",
                runtime.model,
                runtime.model_preset or "default",
            )
        if ctx.on_runtime_admitted is not None:
            await ctx.on_runtime_admitted(runtime)
        replay_max_messages = replay_max_messages_for_context(
            runtime.context_window_tokens
        )
        if not ctx.ephemeral:
            await self.consolidator.maybe_consolidate_by_tokens(
                session,
                runtime=runtime,
                replay_max_messages=replay_max_messages,
            )
        is_subagent = ctx.kind is TurnKind.SYSTEM and ctx.msg.sender_id == "subagent"

        if ctx.kind is TurnKind.USER and (message_tool := self.tools.get("message")):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": replay_max_messages,
            "max_tokens": self._replay_token_budget(runtime),
            "extend_to_user": is_subagent,
        }
        ctx.history = session.get_history(**_hist_kwargs)
        stored_state = session.provider_state
        subagent_followup_persisted = False
        if is_subagent:
            # Keep the durable internal delivery as an assistant record, but
            # present this completion to the model as fresh follow-up input.
            # Providers without assistant-prefill support drop trailing
            # assistant messages, so using the persisted record as the current
            # prompt would hide an independently dispatched subagent result.
            subagent_followup_persisted = self._persist_subagent_followup(
                session,
                ctx.msg,
            )
            if subagent_followup_persisted:
                logger.debug("Subagent result persisted for session {}", ctx.session_key)
                # Establish a durable, replay-safe baseline before any fallible
                # provider compatibility or prompt assembly work. A compatible
                # staged state replaces this in a second atomic save below.
                session.provider_state = None
                self.sessions.save(session)
            ctx.input_persisted_early = True
        ctx.delivery.record_runtime(runtime)

        ctx.request_context = self._request_context_for_turn(ctx)
        if ctx.kind is TurnKind.USER:
            ctx.runtime_context_blocks = await self._resolve_runtime_context_for_turn(ctx)
        staged_provider_state = False
        if stored_state is not None and runtime.provider.can_resume_conversation_state(
            stored_state,
            runtime.model,
        ):
            current_provider_message = self.context.build_current_message(
                ctx.msg.content,
                media=ctx.msg.media if ctx.kind is TurnKind.USER and ctx.msg.media else None,
                runtime_context_blocks=ctx.runtime_context_blocks,
            )
            task_id = ctx.msg.metadata.get("subagent_task_id") if is_subagent else None
            already_staged = False
            if isinstance(task_id, str) and task_id:
                internal_meta = current_provider_message.get("_meta")
                current_provider_message["_meta"] = {
                    **(
                        cast(dict[str, Any], internal_meta)
                        if isinstance(internal_meta, dict)
                        else {}
                    ),
                    _SUBAGENT_PROVIDER_TASK_META: task_id,
                }
                already_staged = any(
                    isinstance(message.get("_meta"), dict)
                    and cast(dict[str, Any], message["_meta"]).get(
                        _SUBAGENT_PROVIDER_TASK_META
                    )
                    == task_id
                    for message in stored_state.pending_messages
                )
            ctx.provider_state = (
                stored_state
                if already_staged
                else stored_state.with_pending_messages([
                    *stored_state.pending_messages,
                    current_provider_message,
                ])
            )
            if (
                not ctx.ephemeral
                and (ctx.kind is TurnKind.USER or subagent_followup_persisted)
            ):
                session.provider_state = ctx.provider_state
                staged_provider_state = True
        elif stored_state is not None:
            session.provider_state = None
        if ctx.kind is TurnKind.USER:
            ctx.input_persisted_early = self._persist_user_message_early(
                ctx.msg,
                session,
                runtime_context_blocks=ctx.runtime_context_blocks,
            )
            if staged_provider_state and not ctx.input_persisted_early:
                session.provider_state = stored_state
        elif subagent_followup_persisted and staged_provider_state:
            # Upgrade the replay-safe baseline to the resumable state before
            # prompt assembly and the first model checkpoint.
            self.sessions.save(session)
        ctx.initial_messages = self._build_initial_messages(ctx)

        if ctx.on_progress is None:
            ctx.on_progress = ctx.delivery.progress_callback()
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = ctx.delivery.retry_wait_callback()

    async def _run_turn(self, ctx: TurnContext) -> None:
        runtime = ctx.require_runtime()
        if ctx.visible_run_started_at is None:
            ctx.visible_run_started_at = time.time()
        await ctx.delivery.running(started_at=ctx.visible_run_started_at)
        result = await self._run_agent_loop(
            ctx.initial_messages,
            runtime=runtime,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.delivery.route.channel,
            chat_id=ctx.delivery.route.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            original_user_text=ctx.original_user_text,
            pending_queue=ctx.pending_queue,
            ephemeral=ctx.ephemeral,
            run_extra_hooks_for_ephemeral=ctx.run_extra_hooks_for_ephemeral,
            hooks=ctx.hooks,
            hook_factories=ctx.hook_factories,
            turn_scopes=ctx.turn_scopes,
            tools=ctx.tools,
            request_context=ctx.request_context,
            provider_state=ctx.provider_state,
        )
        final_content, _, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        if ctx.kind is TurnKind.USER:
            await turn_continuation.maybe_continue_turn(ctx)

    async def _persist_turn(self, ctx: TurnContext) -> None:
        runtime = ctx.require_runtime()
        session = ctx.require_session()
        turn_continuation.prepare_save_boundary(ctx)

        if (
            ctx.kind is TurnKind.USER
            and (ctx.final_content is None or not ctx.final_content.strip())
            and not ctx.suppress_response
        ):
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        latency_started_at = (
            ctx.visible_run_started_at
            if (
                ctx.kind is TurnKind.SYSTEM
                or turn_continuation.internal_continuation_inbound(ctx.msg.metadata)
            )
            and ctx.visible_run_started_at is not None
            else ctx.turn_wall_started_at
        )
        ctx.turn_latency_ms = max(0, int((time.time() - latency_started_at) * 1000))
        self._save_turn(
            session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        ctx.delivery.record_latency(ctx.turn_latency_ms)
        # Recorded beside the latency, and keyed by session for the same reason (#203): the
        # manifest belongs to the turn that built it, and two sessions can be mid-turn at once.
        self.runtime_event_publisher.record_turn_prompt(ctx.session_key, ctx.prompt_manifest)
        if not ctx.ephemeral:
            session.enforce_file_cap(
                on_archive=partial(self.context.memory.raw_archive, session_key=ctx.session_key)
            )
            self.schedule_background(
                self.consolidator.maybe_consolidate_by_tokens(
                    session,
                    runtime=runtime,
                    replay_max_messages=replay_max_messages_for_context(
                        runtime.context_window_tokens
                    ),
                )
            )
        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        if not ctx.ephemeral:
            await self.runtime_event_publisher.session_turn_persisted(
                ctx.msg,
                ctx.session_key,
                turn_id=ctx.turn_id,
                attributes=ctx.attributes,
            )

    async def _prepare_outbound(self, ctx: TurnContext) -> None:
        if ctx.suppress_response:
            ctx.outbound = None
            return
        if ctx.kind is TurnKind.SYSTEM:
            ctx.outbound = ctx.delivery.background_response(
                ctx.final_content,
                stop_reason=ctx.stop_reason,
                streamed=ctx.streamed_content,
                latency_ms=ctx.turn_latency_ms,
            )
            return
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            cast(str, ctx.final_content),
            ctx.stop_reason,
            ctx.had_injections,
            ctx.streamed_content,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.ephemeral and ctx.outbound is not None:
            ctx.outbound.metadata["_stop_reason"] = ctx.stop_reason

    def _sanitize_persisted_blocks(
        self,
        content: list[object],
        *,
        should_truncate_text: bool = False,
    ) -> list[object]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[object] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            block_data = cast(dict[str, Any], block)
            image_url = cast(dict[str, Any], block_data.get("image_url", {}))
            if block_data.get("type") == "image_url" and str(
                image_url.get("url", "")
            ).startswith("data:image/"):
                internal_meta = cast(dict[str, Any], block_data.get("_meta") or {})
                path = cast(str, internal_meta.get("path", ""))
                filtered.append(
                    {"type": "text", "text": image_placeholder_text(path)}
                )
                continue

            if block_data.get("type") == "text" and isinstance(
                block_data.get("text"),
                str,
            ):
                text = cast(str, block_data["text"])
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block_data, "text": text})
                continue

            filtered.append(block_data)

        return filtered

    def _bounded_tool_result(self, message: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of one ``role="tool"`` record at the transcript budget (#55).

        Two paths write such a record. ``_save_turn`` writes the record of a turn that ended,
        and ``_restore_runtime_checkpoint`` writes the record of a turn that a restart
        interrupted. Both call this, so a restart cannot change the size of what persists.

        The in-flight budget is the larger one, and it is larger for a reason: the model reads
        the result of the tool it just called. The transcript budget is what an operator needs
        to see what the tool did, months later, in a file that holds every other turn as well.
        ``ContextGovernor.normalize_tool_result`` exempts ``read_file`` from the in-flight
        offload, so an unbounded result does reach this point.

        ``self.max_tool_result_chars`` is the budget, and it is the only budget: this method and
        ``_sanitize_persisted_blocks`` both read that one attribute, which ``__init__`` sets from
        ``AgentDefaults``. No path repeats the number.

        ``truncate_text`` returns the text it received when the text already fits, so this
        method needs no length test of its own.

        The input is never mutated. The runner holds these same dicts in the message list of a
        turn that may still be running.
        """
        entry = dict(message)
        content = cast(object, entry.get("content"))
        if isinstance(content, str):
            entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
        elif isinstance(content, list):
            entry["content"] = self._sanitize_persisted_blocks(
                cast(list[object], content),
                should_truncate_text=True,
            )
        return entry

    def _save_turn(
        self,
        session: Session,
        messages: list[dict[str, Any]],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        # The ids still waiting for a result, and never every id the session has ever seen. A
        # model that names a call after its slot repeats the name on every turn that calls that
        # tool in that slot, so a session-wide "already fulfilled" set reads the second
        # legitimate call as a duplicate and drops its result. The assistant message then holds
        # an unanswered call, the provider refuses the request, and the refusal is not
        # fallbackable -- so one collision made a session permanently unusable.
        open_tool_calls = open_tool_call_ids(session.messages)
        last_assistant_idx: int | None = None
        # Redact before the loop truncates anything (#17). The chat transcript is
        # sessions/*.jsonl, and it holds role="tool" records, so a resolved credential in
        # remote output landed here. The scrub runs first because truncation keeps a head and
        # a tail, and a bound applied first can cut through a secret and leave both halves.
        # getattr, because tests build a loop with AgentLoop.__new__ and set only the
        # fields they exercise. Such a stand-in has no workspace and therefore no Secret
        # store to resolve sentinels from, so it redacts nothing. A real loop always has
        # one: __init__ requires the argument.
        new_messages = _redacted_for_session(
            messages[skip:], getattr(self, "workspace", None)
        )
        for m in new_messages:
            entry = dict(m)
            internal_meta = cast(object, entry.pop("_meta", None))
            runtime_context_meta = (
                cast(dict[str, Any], internal_meta).get(
                    RUNTIME_CONTEXT_MESSAGE_META
                )
                if isinstance(internal_meta, dict)
                else None
            )
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                tool_call_id = entry.get("tool_call_id")
                tool_call_id_str = str(tool_call_id) if tool_call_id else ""
                if not tool_call_id_str or tool_call_id_str not in open_tool_calls:
                    # A result for a call nobody made, or a second result for one call, corrupts
                    # every future provider request.
                    logger.warning(
                        "Dropping invalid tool result {} from session {} during persistence",
                        tool_call_id_str or "(missing id)",
                        session.key,
                    )
                    continue
                open_tool_calls.discard(tool_call_id_str)
                entry = self._bounded_tool_result(entry)
                if entry.get("content") == []:
                    # Preserve the tool_call/result pair after block filtering. The test names
                    # the empty list rather than any falsy value, because an empty string is a
                    # legal result and keeps its own shape.
                    entry["content"] = [
                        {"type": "text", "text": "[tool result omitted during persistence]"}
                    ]
            elif role == "user":
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(
                        cast(list[object], content),
                    )
                    if not filtered:
                        continue
                    entry["content"] = filtered
                if isinstance(runtime_context_meta, dict):
                    entry[RUNTIME_CONTEXT_HISTORY_META] = runtime_context_meta
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
                # An assistant message opens its own calls and closes whatever came before it:
                # a call the model has already spoken past can no longer be answered.
                open_tool_calls = set(declared_tool_call_ids_of(entry))
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        metadata_value = cast(object, msg.metadata)
        task_id = (
            msg.metadata.get("subagent_task_id")
            if isinstance(metadata_value, dict)
            else None
        )
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata.

        The scrub runs here, and this is the only scrub on this path (#51). The method is the
        one funnel every emitter passes through: the three call sites in
        ``nanoinfra/agent/runner.py`` all reach the file through it, so a fourth emitter
        inherits the scrub rather than forgets it.

        getattr, because a test builds a loop with ``AgentLoop.__new__`` and sets only the
        fields it exercises. ``_save_turn`` reads the workspace the same way, and the full
        reason is written there.

        No size bound belongs here (#55). This is the write, and #51 requires that a checkpoint
        which held no secret reaches the file byte for byte. A bound here would break that pin,
        and it would shorten a payload the restore still has to close a turn with. The bound
        runs in ``_restore_runtime_checkpoint``, where the payload becomes a message record.
        """
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = _redacted_checkpoint(
            payload, getattr(self, "workspace", None)
        )
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request.

        The records restore as they are, and no scrub runs here (#51).
        ``_set_runtime_checkpoint`` scrubbed them before they reached the file, so a scrubbed
        record restores as itself. A second pass would be a second implementation of one rule,
        and two implementations of one rule are how two paths start to disagree. It would also
        spend a round trip per text on the path that runs before the first request of a turn.

        The size bound is the other case, and it does run here (#55). A scrub is a rule about a
        value, and the checkpoint already obeyed it. A bound is a rule about a record, and the
        checkpoint is not a record: it becomes one at this method. ``_save_turn`` bounds a tool
        result, this method bounds the same shape through ``_bounded_tool_result``, and a
        restart therefore changes no size in the transcript.

        Only a tool result carries a bound, because only a tool result carries one on the
        normal path. ``_save_turn`` reads the budget inside its ``role == "tool"`` branch, so
        an assistant content and the arguments of a tool call persist whole there. This method
        matches that rather than inventing a second rule. The arguments of a pending call never
        reach the transcript at all: the record below carries a fixed sentence.
        """
        from datetime import datetime

        checkpoint = cast(
            object,
            session.metadata.get(self._RUNTIME_CHECKPOINT_KEY),
        )
        if not isinstance(checkpoint, dict):
            return False
        checkpoint_data = cast(dict[str, Any], checkpoint)

        assistant_message = cast(object, checkpoint_data.get("assistant_message"))
        completed_tool_results = cast(
            Iterable[object],
            checkpoint_data.get("completed_tool_results") or [],
        )
        pending_tool_calls = cast(
            Iterable[object],
            checkpoint_data.get("pending_tool_calls") or [],
        )

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(cast(dict[str, Any], assistant_message))
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                # The bound runs here, and never at the checkpoint write (#55). The write is
                # the obvious place and the wrong one, and the docstring of
                # _set_runtime_checkpoint holds the reason. The entry becomes a message record
                # at this line, so this is where the budget of a record applies.
                restored = self._bounded_tool_result(cast(dict[str, Any], message))
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_call_data = cast(dict[str, Any], tool_call)
            tool_id = tool_call_data.get("id")
            function_data = cast(
                dict[str, Any],
                tool_call_data.get("function") or {},
            )
            name = function_data.get("name") or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        appended_messages = restored_messages[overlap:]
        session.messages.extend(appended_messages)
        assistant_message_data = (
            cast(dict[str, Any], assistant_message)
            if isinstance(assistant_message, dict)
            else None
        )
        provider_state_is_synchronized = (
            checkpoint_data.get(self._PROVIDER_STATE_CHECKPOINT_VERSION_KEY)
            == self._PROVIDER_STATE_CHECKPOINT_VERSION
        )
        phase = checkpoint_data.get("phase")
        exact_final_response = (
            phase == "final_response"
            and assistant_message_data is not None
            and assistant_message_data.get("role") == "assistant"
            and not bool(checkpoint_data.get("completed_tool_results"))
            and not bool(checkpoint_data.get("pending_tool_calls"))
        )
        exact_completed_tools = (
            phase == "tools_completed"
            and assistant_message_data is not None
            and assistant_message_data.get("role") == "assistant"
            and not bool(checkpoint_data.get("pending_tool_calls"))
        )
        if not (
            provider_state_is_synchronized
            and (exact_final_response or exact_completed_tools)
        ):
            session.provider_state = None

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.provider_state = None
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        sender_id: str = "user",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        _run_extra_hooks_for_ephemeral: bool = False,
        hooks: list[AgentHook] | None = None,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        tools: ToolRegistry | None = None,
        persist_user_message: bool = True,
        runtime: LLMRuntime | None = None,
        on_runtime_admitted: Callable[[LLMRuntime], Awaitable[None]] | None = None,
        attributes: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """Process an external message directly and return the outbound payload.

        ``metadata`` reaches the turn, so a caller that runs from a schedule can mark it. #5
        classifies a turn from that metadata, and a caller that could not pass any read as
        interactive: #49 shows the built-in heartbeat running model-written text at interactive
        privilege for that reason.
        """
        if channel == "system":
            raise ValueError("channel 'system' is reserved for internal messages")
        await self._connect_mcp()
        turn_metadata: dict[str, Any] = dict(metadata or {})
        if not persist_user_message:
            turn_metadata[turn_continuation.SKIP_USER_PERSIST_META] = True
        msg = InboundMessage(
            channel=channel, sender_id=sender_id, chat_id=chat_id,
            content=content, media=media or [], metadata=turn_metadata,
        )
        # Share the dispatch lock so direct calls serialize with bus turns.
        lock = self._get_session_lock(session_key)
        try:
            async with lock:
                kwargs: dict[str, Any] = {
                    "session_key": session_key,
                    "on_progress": on_progress,
                    "on_stream": on_stream,
                    "on_stream_end": on_stream_end,
                    "ephemeral": ephemeral,
                }
                if _run_extra_hooks_for_ephemeral:
                    kwargs["run_extra_hooks_for_ephemeral"] = True
                if hooks is not None:
                    kwargs["hooks"] = hooks
                if hook_factories is not None:
                    kwargs["hook_factories"] = hook_factories
                if tools is not None:
                    kwargs["tools"] = tools
                if runtime is not None:
                    kwargs["runtime"] = runtime
                if on_runtime_admitted is not None:
                    kwargs["on_runtime_admitted"] = on_runtime_admitted
                if attributes is not None:
                    kwargs["attributes"] = dict(attributes)
                return await self._process_message(
                    msg,
                    **kwargs,
                )
        finally:
            await self.runtime_event_publisher.run_status_changed(msg, session_key, "idle")
            self.runtime_event_publisher.clear_turn(session_key)

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared lock while allowing idle session entries to expire."""
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock
