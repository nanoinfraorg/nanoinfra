"""High-level programmatic interface to nanoinfra."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import finalize

from loguru import logger

from nanoinfra.agent.hook import AgentHook, SDKCaptureHook
from nanoinfra.agent.hooks import create_file_edit_activity_hook
from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool, default_socket_path
from nanoinfra.config.schema import Config
from nanoinfra.gates.executor.client import ExecutorClient
from nanoinfra.providers.image_generation import image_gen_provider_configs
from nanoinfra.sdk.clients import (
    DisabledExecutorClient,
    MemoryClient,
    RuntimeClient,
    SessionClient,
)
from nanoinfra.sdk.runtime import (
    build_process_direct_kwargs,
    ensure_single_model_selector,
)
from nanoinfra.sdk.streaming import RunStream, SDKStreamEmitter, SDKStreamingHook
from nanoinfra.sdk.types import (
    REMOTE_EXECUTION_DISABLED,
    REMOTE_EXECUTION_DISABLED_MESSAGE,
    REMOTE_EXECUTION_EXECUTOR_PROCESS,
    REMOTE_EXECUTION_MODES,
    STREAM_EVENT_REASONING_COMPLETED,
    STREAM_EVENT_REASONING_DELTA,
    STREAM_EVENT_RUN_COMPLETED,
    STREAM_EVENT_RUN_FAILED,
    STREAM_EVENT_RUN_STARTED,
    STREAM_EVENT_TEXT_COMPLETED,
    STREAM_EVENT_TEXT_DELTA,
    STREAM_EVENT_TOOL_COMPLETED,
    STREAM_EVENT_TOOL_FAILED,
    STREAM_EVENT_TOOL_STARTED,
    STREAM_EVENT_TYPES,
    RemoteExecutionMode,
    RemoteExecutionUnavailableError,
    RunResult,
    SessionInfo,
    SessionSnapshot,
    StreamEvent,
    StreamEventType,
    result_from_response,
)
from nanoinfra.utils.llm_runtime import LLMRuntime

if TYPE_CHECKING:
    from nanoinfra.gates.executor.supervisor import ExecutorProcess

__all__ = [
    "Nanoinfra",
    "REMOTE_EXECUTION_DISABLED",
    "REMOTE_EXECUTION_DISABLED_MESSAGE",
    "REMOTE_EXECUTION_EXECUTOR_PROCESS",
    "REMOTE_EXECUTION_MODES",
    "RemoteExecutionMode",
    "RemoteExecutionUnavailableError",
    "RunResult",
    "RunStream",
    "SessionInfo",
    "SessionSnapshot",
    "STREAM_EVENT_REASONING_COMPLETED",
    "STREAM_EVENT_REASONING_DELTA",
    "STREAM_EVENT_RUN_COMPLETED",
    "STREAM_EVENT_RUN_FAILED",
    "STREAM_EVENT_RUN_STARTED",
    "STREAM_EVENT_TEXT_COMPLETED",
    "STREAM_EVENT_TEXT_DELTA",
    "STREAM_EVENT_TOOL_COMPLETED",
    "STREAM_EVENT_TOOL_FAILED",
    "STREAM_EVENT_TOOL_STARTED",
    "STREAM_EVENT_TYPES",
    "StreamEvent",
    "StreamEventType",
]

# How long the SDK waits for its executor child to exit before it kills the process group.
_EXECUTOR_STOP_TIMEOUT_S = 10

# The one tool that reaches a server. The SDK rebinds it, so it never keeps the default socket
# path that the tool loader gives it.
_REMOTE_EXECUTION_TOOL = "execute_on_server"


def _checked_remote_execution_mode(value: str) -> RemoteExecutionMode:
    """Return a known mode, or raise.

    A typo must not read as ``disabled``, because the caller would then lose remote execution
    and learn it from a refusal much later. A typo must not read as ``executor_process``
    either. So an unknown value fails at once, and it names the values that work.
    """
    for mode in REMOTE_EXECUTION_MODES:
        if value == mode:
            return mode
    known = ", ".join(repr(mode) for mode in REMOTE_EXECUTION_MODES)
    raise ValueError(f"unknown remote_execution mode {value!r}. Pass one of: {known}")


def _sdk_executor_socket_path() -> Path:
    """Return a socket path this instance alone owns.

    The default path belongs to a gateway's executor. Two supervisors on one path share one
    state file, and a stop from this process would then end another process's child. So an
    embedded agent binds a name of its own.
    """
    return default_socket_path().with_name(f"sdk-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")


def _spawn_executor(
    *, socket_path: Path, workspace: Path, user: str | None
) -> ExecutorProcess:
    """Start the executor child through the supervisor.

    The import stays local. A deployment that cannot spawn children never loads the supervisor,
    and ``import nanoinfra`` still works there.
    """
    from nanoinfra.gates.executor.supervisor import start_executor

    return start_executor(socket_path=socket_path, workspace=workspace, user=user)


def _stop_executor_child(executor: ExecutorProcess) -> None:
    """Stop one executor child, and never raise while doing it.

    This runs from a finalizer, so it also runs at interpreter exit. A raise there would print
    an ignored exception and would tell an operator nothing useful.
    """
    try:
        executor.stop(timeout_s=_EXECUTOR_STOP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- shutdown must not raise
        logger.warning("SDK could not stop its executor child: {}", exc)


class Nanoinfra:
    """Programmatic facade for running the nanoinfra agent.

    Usage::

        bot = Nanoinfra.from_config()
        result = await bot.run("Summarize this repo", hooks=[MyHook()])
        print(result.content)

    Remote execution needs a second process (#21). An embedded agent has no supervisor above
    it, so this class starts one when the caller asks for ``remote_execution``, and it owns
    that child until the caller closes the instance.
    """

    def __init__(
        self,
        loop: AgentLoop,
        *,
        config: Config | None = None,
        remote_execution: RemoteExecutionMode | str = REMOTE_EXECUTION_DISABLED,
        executor_socket: str | Path | None = None,
        executor_user: str | None = None,
    ) -> None:
        self._loop = loop
        self._config = config
        self.sessions = SessionClient(loop)
        self.memory = MemoryClient(loop)
        self.runtime = RuntimeClient(loop)
        self._remote_execution: RemoteExecutionMode = _checked_remote_execution_mode(
            remote_execution
        )
        self._executor: ExecutorProcess | None = None
        # finalize's second type argument is the referent, and a subclass narrows it. Any keeps
        # this attribute usable in a subclass without a second annotation there.
        self._executor_stop: finalize[..., Any] | None = None
        self._bind_remote_execution(socket_path=executor_socket, user=executor_user)

    @property
    def remote_execution(self) -> RemoteExecutionMode:
        """The remote-execution mode in force for this instance."""
        return self._remote_execution

    @property
    def executor(self) -> ExecutorProcess | None:
        """The executor child this instance owns, or ``None`` when it started none."""
        return self._executor

    def _bind_remote_execution(
        self, *, socket_path: str | Path | None, user: str | None
    ) -> None:
        """Point the remote-execution tool at an executor process, or at a refusal.

        There is no third branch. An in-process transport here would make the split in #18
        false for every SDK user, and the release notes would say the opposite.
        """
        client: ExecutorClient
        if self._remote_execution == REMOTE_EXECUTION_DISABLED:
            client = DisabledExecutorClient()
        else:
            path = (
                Path(socket_path).expanduser()
                if socket_path is not None
                else _sdk_executor_socket_path()
            )
            executor = _spawn_executor(
                socket_path=path, workspace=self._loop.workspace, user=user
            )
            self._executor = executor
            # A finalizer stops the child when this instance dies, and again at interpreter
            # exit, whichever comes first. So a caller that forgets aclose() leaves no orphan.
            self._executor_stop = finalize(self, _stop_executor_child, executor)
            client = ExecutorClient(executor.socket_path)
        self._rebind_remote_execution_tool(client)

    def _rebind_remote_execution_tool(self, client: ExecutorClient) -> None:
        """Bind the loaded tool to *client*.

        The tool loader gives the tool the default socket path, which belongs to a gateway's
        executor. An embedded agent must reach the child it started, or reach nothing at all. A
        path that some other process may own would make the answer depend on the host rather
        than on the caller's own choice.
        """
        tool = self._loop.tools.get(_REMOTE_EXECUTION_TOOL)
        if isinstance(tool, ExecuteOnServerTool):
            tool.client = client

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
        model: str | None = None,
        model_preset: str | None = None,
        remote_execution: RemoteExecutionMode | str = REMOTE_EXECUTION_DISABLED,
        executor_socket: str | Path | None = None,
        executor_user: str | None = None,
    ) -> Nanoinfra:
        """Create a Nanoinfra instance from a config file.

        Args:
            config_path: Path to ``config.json``.  Defaults to
                ``~/.nanoinfra/config.json``.
            workspace: Override the workspace directory from config.
            model: Override the instance default model.
            model_preset: Override the instance default model preset.
            remote_execution: ``"executor_process"`` starts an executor child and routes
                ``execute_on_server`` to it. ``"disabled"``, the default, starts no child, and
                a remote-execution call then fails with
                :class:`~nanoinfra.sdk.types.RemoteExecutionUnavailableError`. The default
                declines, because a library import must not fork a process for a caller that
                never reaches a server, and a deployment may forbid child processes at all.
                There is no in-process mode: see #18 and #21.
            executor_socket: Where the executor child listens. Defaults to a private name
                under the instance data directory. Used only with ``"executor_process"``.
            executor_user: Account for the executor child. Without it the child shares this
                process's uid, so the split is organisational and the kernel does not enforce
                it. Used only with ``"executor_process"``.
        """
        from nanoinfra.config.loader import load_config, resolve_config_env_vars

        ensure_single_model_selector(model=model, model_preset=model_preset)
        # Check the mode before the loop is built. A typo must cost nothing.
        mode = _checked_remote_execution_mode(remote_execution)
        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        config: Config = resolve_config_env_vars(
            load_config(resolved),
            config_path=resolved,
        )
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )
        if model is not None:
            config.agents.defaults.model_preset = None
            config.agents.defaults.model = model
            config.agents.defaults.provider = "auto"
        elif model_preset is not None:
            config.agents.defaults.model_preset = model_preset

        loop = AgentLoop.from_config(
            config,
            image_generation_provider_configs=image_gen_provider_configs(config),
            hook_factories=[create_file_edit_activity_hook],
        )
        return cls(
            loop,
            config=config,
            remote_execution=mode,
            executor_socket=executor_socket,
            executor_user=executor_user,
        )

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        channel: str = "cli",
        chat_id: str = "direct",
        sender_id: str = "user",
        media: list[str] | None = None,
        ephemeral: bool = False,
        attributes: Mapping[str, Any] | None = None,
        hooks: list[AgentHook] | None = None,
        model: str | None = None,
        model_preset: str | None = None,
    ) -> RunResult:
        """Run the agent once and return the result.

        Args:
            message: The user message to process.
            session_key: Session identifier for conversation isolation.
                Different keys get independent history.
            channel: Logical channel label for runtime context.
            chat_id: Logical chat identifier for runtime context.
            sender_id: Logical sender identifier for runtime context.
            media: Optional local media paths attached to the message.
            ephemeral: If true, do not persist the turn or compact session history.
            attributes: Optional caller-owned request data exposed to context
                providers and turn-hook factories. Attributes are kept separate
                from nanoinfra's trusted internal message metadata.
            hooks: Optional lifecycle hooks for this run.
            model: Override the model for this run only.
            model_preset: Override the model preset for this run only.
        """
        capture = SDKCaptureHook()
        per_run_hooks = [capture, *(hooks or [])]
        runtime = self._loop.runtime_resolver.resolve_override(
            model=model,
            model_preset=model_preset,
            config=self._config,
        )
        kwargs = build_process_direct_kwargs(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            media=media,
            ephemeral=ephemeral,
            attributes=attributes,
        )
        if runtime is not None:
            kwargs["runtime"] = runtime
        response = await self._loop.process_direct(
            message,
            **kwargs,
            hooks=per_run_hooks,
        )

        return result_from_response(response, capture)

    async def run_streamed(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        channel: str = "cli",
        chat_id: str = "direct",
        sender_id: str = "user",
        media: list[str] | None = None,
        ephemeral: bool = False,
        attributes: Mapping[str, Any] | None = None,
        hooks: list[AgentHook] | None = None,
        model: str | None = None,
        model_preset: str | None = None,
    ) -> RunStream:
        """Start a streamed run and return a handle for events and final result."""
        override_runtime = self._loop.runtime_resolver.resolve_override(
            model=model,
            model_preset=model_preset,
            config=self._config,
        )
        queue: asyncio.Queue[StreamEvent | object] = asyncio.Queue(maxsize=256)
        emitter = SDKStreamEmitter(queue)
        stream_hook = SDKStreamingHook(emitter)
        capture = SDKCaptureHook()
        per_run_hooks = [capture, stream_hook, *(hooks or [])]
        run_started = False

        async def _emit_run_started(runtime: LLMRuntime | None = None) -> None:
            nonlocal run_started
            if run_started:
                return
            if runtime is None:
                runtime = override_runtime
            metadata: dict[str, Any] = {
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "sender_id": sender_id,
            }
            if runtime is not None:
                metadata.update({
                    "model": runtime.model,
                    "model_preset": runtime.model_preset,
                })
            await emitter.emit(StreamEvent(
                type=STREAM_EVENT_RUN_STARTED,
                metadata=metadata,
            ))
            run_started = True

        async def _on_stream(delta: str) -> None:
            await emitter.text_delta(delta)

        async def _on_stream_end(*_args: Any, resuming: bool = False, **_kwargs: Any) -> None:
            await emitter.text_completed(resuming=resuming)

        async def _run() -> RunResult:
            kwargs = build_process_direct_kwargs(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                sender_id=sender_id,
                media=media,
                ephemeral=ephemeral,
                attributes=attributes,
                on_stream=_on_stream,
                on_stream_end=_on_stream_end,
            )
            kwargs["on_runtime_admitted"] = _emit_run_started
            if override_runtime is not None:
                kwargs["runtime"] = override_runtime
            try:
                response = await self._loop.process_direct(
                    message,
                    **kwargs,
                    hooks=per_run_hooks,
                )
                await _emit_run_started()
                await emitter.text_completed(resuming=False, force=False)
                result = result_from_response(response, capture)
                await emitter.emit(StreamEvent(
                    type=STREAM_EVENT_RUN_COMPLETED,
                    content=result.content,
                    result=result,
                    usage=dict(result.usage),
                    metadata=dict(result.metadata),
                ))
                return result
            except Exception as exc:
                await _emit_run_started()
                await emitter.emit(StreamEvent(
                    type=STREAM_EVENT_RUN_FAILED,
                    error=str(exc),
                    metadata={"exception_type": type(exc).__name__},
                ))
                raise
            finally:
                emitter.close()

        task = asyncio.create_task(_run())
        return RunStream(task, queue)

    async def stream(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        channel: str = "cli",
        chat_id: str = "direct",
        sender_id: str = "user",
        media: list[str] | None = None,
        ephemeral: bool = False,
        attributes: Mapping[str, Any] | None = None,
        hooks: list[AgentHook] | None = None,
        model: str | None = None,
        model_preset: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream events for one agent turn."""
        run = await self.run_streamed(
            message,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            media=media,
            ephemeral=ephemeral,
            attributes=attributes,
            hooks=hooks,
            model=model,
            model_preset=model_preset,
        )
        try:
            async for event in run.stream_events():
                yield event
            await run.wait()
        finally:
            if not run.done:
                await run.aclose()

    async def aclose(self) -> None:
        """Release resources held by this instance (MCP connections, the executor child)."""
        await self._loop.close_mcp()
        stop = self._executor_stop
        if stop is not None:
            # The stop signals a process group and waits, so it runs off the event loop. The
            # finalizer runs at most once, so a second close cannot end a later child.
            await asyncio.to_thread(stop)

    async def __aenter__(self) -> Nanoinfra:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
