"""Public SDK value objects and event constants."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias, cast

from nanoinfra.gates.executor.client import ExecutorUnavailableError
from nanoinfra.runtime_context import public_history_messages

# How an embedded agent reaches a server -- nanoinfraorg/nanoinfra#21.
#
# There are two modes and there is no third one. Either the SDK owns an executor process, or it
# has no remote execution at all. An in-process transport would put a credential and a dialer
# back beside the model, and the SDK would then be a supported way around #18.
RemoteExecutionMode: TypeAlias = Literal["executor_process", "disabled"]

REMOTE_EXECUTION_EXECUTOR_PROCESS: RemoteExecutionMode = "executor_process"
REMOTE_EXECUTION_DISABLED: RemoteExecutionMode = "disabled"

REMOTE_EXECUTION_MODES: tuple[RemoteExecutionMode, ...] = (
    REMOTE_EXECUTION_EXECUTOR_PROCESS,
    REMOTE_EXECUTION_DISABLED,
)

# What a caller reads when it asked for no executor and then asked to reach a server. The words
# name the choice that removed the capability, and they name the one line that restores it. A
# generic "not reachable" would send an operator to look for a process that nobody started.
REMOTE_EXECUTION_DISABLED_MESSAGE = (
    "this embedded agent has no executor process, because it was built with "
    "remote_execution='disabled'. The SDK runs no remote command inside the caller's own "
    "process: a model must not sit beside a transport or a credential. Build the instance "
    "with Nanoinfra.from_config(remote_execution='executor_process') to get an executor "
    "child, or send the work to a gateway that has one."
)


class RemoteExecutionUnavailableError(ExecutorUnavailableError):
    """The SDK declined to start an executor, so this call reaches nothing.

    It derives from ``ExecutorUnavailableError`` on purpose. The condition is the one that
    class already names -- no executor answers -- so the tool renders it as a deployment fault
    rather than as a refusal. The separate type lets a caller catch this exact cause.
    """


StreamEventType: TypeAlias = Literal[
    "run.started",
    "text.delta",
    "text.completed",
    "reasoning.delta",
    "reasoning.completed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "run.completed",
    "run.failed",
]

STREAM_EVENT_RUN_STARTED: StreamEventType = "run.started"
STREAM_EVENT_TEXT_DELTA: StreamEventType = "text.delta"
STREAM_EVENT_TEXT_COMPLETED: StreamEventType = "text.completed"
STREAM_EVENT_REASONING_DELTA: StreamEventType = "reasoning.delta"
STREAM_EVENT_REASONING_COMPLETED: StreamEventType = "reasoning.completed"
STREAM_EVENT_TOOL_STARTED: StreamEventType = "tool.started"
STREAM_EVENT_TOOL_COMPLETED: StreamEventType = "tool.completed"
STREAM_EVENT_TOOL_FAILED: StreamEventType = "tool.failed"
STREAM_EVENT_RUN_COMPLETED: StreamEventType = "run.completed"
STREAM_EVENT_RUN_FAILED: StreamEventType = "run.failed"

STREAM_EVENT_TYPES: tuple[StreamEventType, ...] = (
    STREAM_EVENT_RUN_STARTED,
    STREAM_EVENT_TEXT_DELTA,
    STREAM_EVENT_TEXT_COMPLETED,
    STREAM_EVENT_REASONING_DELTA,
    STREAM_EVENT_REASONING_COMPLETED,
    STREAM_EVENT_TOOL_STARTED,
    STREAM_EVENT_TOOL_COMPLETED,
    STREAM_EVENT_TOOL_FAILED,
    STREAM_EVENT_RUN_COMPLETED,
    STREAM_EVENT_RUN_FAILED,
)


@dataclass(slots=True)
class RunResult:
    """Result of a single agent run."""

    content: str
    tools_used: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamEvent:
    """A typed event emitted by ``Nanoinfra.stream()`` and ``RunStream``."""

    type: StreamEventType
    delta: str = ""
    content: str = ""
    result: RunResult | None = None
    name: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, Any] | None = None
    iteration: int | None = None
    resuming: bool | None = None
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionSnapshot:
    """A serializable session snapshot; trusted exports may include internal context."""

    key: str
    messages: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of the snapshot."""
        return {
            "key": self.key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": deepcopy(self.metadata),
            "messages": deepcopy(self.messages),
        }


@dataclass(slots=True)
class SessionInfo:
    """Compact session metadata for listings."""

    key: str
    created_at: str | None = None
    updated_at: str | None = None
    title: str = ""
    preview: str = ""
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of the listing row."""
        return {
            "key": self.key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "preview": self.preview,
            "path": self.path,
        }


def snapshot_from_session(
    session: Any,
    *,
    include_runtime_context: bool = False,
) -> SessionSnapshot:
    messages = cast(list[dict[str, Any]], deepcopy(session.messages))
    if not include_runtime_context:
        messages = public_history_messages(messages)
    return SessionSnapshot(
        key=session.key,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        metadata=deepcopy(session.metadata),
        messages=messages,
    )


def snapshot_from_payload(
    payload: Mapping[str, Any],
    *,
    include_runtime_context: bool = False,
) -> SessionSnapshot:
    raw_messages: list[Any] = list(payload.get("messages") or [])
    messages: list[dict[str, Any]] = [
        deepcopy(dict(cast(Mapping[str, Any], message)))
        for message in raw_messages
        if isinstance(message, Mapping)
    ]
    if not include_runtime_context:
        messages = public_history_messages(messages)
    return SessionSnapshot(
        key=str(payload.get("key") or ""),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        metadata=deepcopy(dict(cast(Mapping[str, Any], payload.get("metadata") or {}))),
        messages=messages,
    )


def result_from_response(response: Any, capture: Any) -> RunResult:
    content = (response.content if response else None) or ""
    metadata = dict(response.metadata) if response and response.metadata else {}
    return RunResult(
        content=content,
        tools_used=capture.tools_used,
        messages=capture.messages,
        # The compact projection, not the type: `RunResult` is what an SDK consumer receives, so
        # it keeps the shape and the names it already had (#175).
        usage=capture.usage.to_turn_dict() if capture.usage is not None else {},
        stop_reason=capture.stop_reason,
        error=capture.error,
        metadata=metadata,
    )
