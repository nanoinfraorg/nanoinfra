"""Small convenience clients exposed by the high-level Python SDK."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanoinfra.agent.redaction import (
    redact_mapping,
    redact_messages,
    workspace_secret_sentinels,
)
from nanoinfra.agent.tools.capabilities import capability_class_of
from nanoinfra.bus.runtime_events import SessionTurnPersisted
from nanoinfra.gates.executor.client import ExecutorClient
from nanoinfra.gates.executor.protocol import ExecuteResponse
from nanoinfra.runtime_context import RUNTIME_CONTEXT_HISTORY_META, RuntimeContextProvider
from nanoinfra.sdk.types import (
    REMOTE_EXECUTION_DISABLED_MESSAGE,
    RemoteExecutionUnavailableError,
    SessionInfo,
    SessionSnapshot,
    snapshot_from_payload,
    snapshot_from_session,
)
from nanoinfra.session.manager import replay_max_messages_for_context

if TYPE_CHECKING:
    from nanoinfra.agent.loop import AgentLoop

# The stand-in below carries a path so that it stays an ``ExecutorClient``. It connects to
# nothing, so the value is a label for a log line rather than a destination.
DISABLED_EXECUTOR_SOCKET = Path("/nonexistent/nanoinfra-remote-execution-disabled.sock")


class DisabledExecutorClient(ExecutorClient):
    """Sits where the executor client would sit when a caller starts no executor (#21).

    It refuses at call time, and it opens nothing. The refusal has to arrive here rather than
    at construction: a caller that never reaches a server must still be able to build an agent
    in a deployment that forbids child processes.

    This class exists because the alternative is worse. If the real client stayed in place, an
    embedded agent would reach whichever process holds the default socket path. The answer
    would then depend on the machine rather than on the caller's own choice.
    """

    def __init__(self) -> None:
        super().__init__(DISABLED_EXECUTOR_SOCKET)

    def execute(
        self,
        *,
        server_id_or_name: str,
        command: str,
        session_id: str | None,
        execution_context: str,
        preview_requested: bool,
        timeout_s: str | None,
        token_nonce: str | None = None,
    ) -> ExecuteResponse:
        """Raise. A preview is refused too, because no process can resolve the server."""
        del server_id_or_name, command, session_id, execution_context
        del preview_requested, timeout_s, token_nonce
        raise RemoteExecutionUnavailableError(REMOTE_EXECUTION_DISABLED_MESSAGE)


def _capability_class_of_tool(loop: AgentLoop, tool_name: str) -> str | None:
    """Return the capability class of a registered tool, else ``None``.

    A ``credential.access`` result is credential material by definition, so
    redaction drops it whole instead of a value-by-value scrub.
    """
    tool = loop.tools.get(tool_name)
    return capability_class_of(tool) if tool is not None else None


def _redacted_snapshot(loop: AgentLoop, snapshot: Any) -> Any:
    """Scrub stored secret values out of one snapshot before a caller gets it (#34).

    Every reader that returns a snapshot passes it through here. #31 scrubbed ``export()`` alone,
    and its siblings returned the session unchanged. ``export_unredacted_with_secrets`` is the one
    exception, and its name and docstring say who may call it.

    In-place update is safe. Both snapshot builders deep copy, so neither the caller nor the
    session cache shares this object. The live session and the session file keep the true values,
    because the model replays them to complete its work.

    The function applies no length bound. A snapshot exists to reproduce a session, and the #17
    bound protects the durable transcript instead of this reader.
    """
    if snapshot is None:
        return None
    sentinels = workspace_secret_sentinels(loop.workspace)
    snapshot.messages = redact_messages(
        snapshot.messages,
        sentinels,
        capability_of=lambda name: _capability_class_of_tool(loop, name),
        max_tool_result_chars=None,
    )
    snapshot.metadata = redact_mapping(snapshot.metadata, sentinels)
    return snapshot


class SessionClient:
    """Session management helpers exposed through ``bot.sessions``."""

    _RESERVED_MESSAGE_KEYS = {"role", "content", RUNTIME_CONTEXT_HISTORY_META}
    _VALID_ROLES = {"user", "assistant", "tool", "system"}

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def ingest(
        self,
        session_key: str,
        messages: Iterable[Mapping[str, Any]],
        *,
        metadata: Mapping[str, Any] | None = None,
        source: str | None = None,
        save: bool = True,
    ) -> SessionSnapshot:
        """Import an existing transcript without running the model."""
        session = self._loop.sessions.get_or_create(session_key)
        if metadata:
            session.metadata.update(deepcopy(dict(metadata)))

        for raw in messages:
            if "role" not in raw:
                raise ValueError("ingested messages must include a role")
            if "content" not in raw:
                raise ValueError("ingested messages must include content")
            role = str(raw["role"]).strip()
            if role not in self._VALID_ROLES:
                raise ValueError(f"unsupported message role: {role!r}")
            extra = {
                key: deepcopy(value)
                for key, value in raw.items()
                if key not in self._RESERVED_MESSAGE_KEYS
            }
            if source is not None and "source" not in extra:
                extra["source"] = source
            session.add_message(role, deepcopy(raw["content"]), **extra)

        if save:
            self._loop.sessions.save(session)
        return _redacted_snapshot(self._loop, snapshot_from_session(session))

    def get(self, session_key: str) -> SessionSnapshot | None:
        """Return a display-safe snapshot without creating a new session on disk."""
        cached = self._loop.sessions.get_cached(session_key)
        if cached is not None:
            return _redacted_snapshot(self._loop, snapshot_from_session(cached))
        payload = self._loop.sessions.read_session_file(session_key)
        if payload is None:
            return None
        return _redacted_snapshot(self._loop, snapshot_from_payload(payload))

    def list(self) -> list[SessionInfo]:
        """List persisted sessions."""
        return [
            SessionInfo(
                key=str(row.get("key") or ""),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                title=str(row.get("title") or ""),
                preview=str(row.get("preview") or ""),
                path=row.get("path"),
            )
            for row in self._loop.sessions.list_sessions()
        ]


    def export(self, session_key: str) -> SessionSnapshot | None:
        """Return a full snapshot with model-only runtime context, secrets redacted.

        This is a share path, so the #17 scrub runs here at the read. The live
        session and the session file both keep the real values, because the
        model replays them to finish its work. #17 scrubs the derived durable
        transcripts at the write, and this method reads around all of them.

        Redaction is best-effort (see nanoinfra/agent/redaction.py). Treat an
        exported snapshot as sensitive material anyway.
        """
        return _redacted_snapshot(self._loop, self.export_unredacted_with_secrets(session_key))

    def export_unredacted_with_secrets(self, session_key: str) -> SessionSnapshot | None:
        """Return the raw snapshot, which MAY CARRY LIVE CREDENTIALS.

        Only trusted in-process library code may call this, and only when a
        scrubbed copy cannot do the job: a workspace-local backup, a session
        migration, or a ``restore()`` round trip that must reproduce the
        session exactly.

        Never send this result outside the workspace trust boundary. An HTTP
        response, a CLI output, a log line, a chat reply, or a bug-report
        attachment must use ``export()``.
        """
        cached = self._loop.sessions.get_cached(session_key)
        if cached is not None:
            return snapshot_from_session(cached, include_runtime_context=True)
        payload = self._loop.sessions.read_session_file(session_key)
        if payload is None:
            return None
        return snapshot_from_payload(payload, include_runtime_context=True)

    async def restore(
        self,
        snapshot: SessionSnapshot,
        *,
        session_key: str | None = None,
        save: bool = True,
    ) -> SessionSnapshot:
        """Restore a trusted snapshot into an empty session."""
        key = session_key or snapshot.key
        if not key:
            raise ValueError("restored snapshots must include a session key")
        session = self._loop.sessions.get_or_create(key)
        if session.messages:
            raise ValueError(f"restore target session is not empty: {key}")

        prepared: list[tuple[str, Any, dict[str, Any]]] = []
        for raw in snapshot.messages:
            if "role" not in raw or "content" not in raw:
                raise ValueError("restored messages must include role and content")
            role = str(raw["role"]).strip()
            if role not in self._VALID_ROLES:
                raise ValueError(f"unsupported message role: {role!r}")
            extra = {
                field: deepcopy(value)
                for field, value in raw.items()
                if field not in {"role", "content"}
            }
            prepared.append((role, deepcopy(raw["content"]), extra))

        session.metadata.update(deepcopy(snapshot.metadata))
        for role, content, extra in prepared:
            session.add_message(role, content, **extra)

        if save:
            self._loop.sessions.save(session)
        return _redacted_snapshot(self._loop, snapshot_from_session(session))

    def clear(self, session_key: str) -> SessionSnapshot:
        """Clear one session and persist the empty session."""
        session = self._loop.sessions.get_or_create(session_key)
        session.clear()
        self._loop.sessions.save(session)
        return _redacted_snapshot(self._loop, snapshot_from_session(session))

    def delete(self, session_key: str) -> bool:
        """Delete one session from disk and cache."""
        return self._loop.sessions.delete_session(session_key)

    def flush(self) -> int:
        """Flush cached sessions to durable storage."""
        return self._loop.sessions.flush_all()


class MemoryClient:
    """Long-term memory helpers exposed through ``bot.memory``."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def read(self) -> str:
        """Read ``memory/MEMORY.md``."""
        return self._loop.context.memory.read_memory()

    def write(self, text: str) -> None:
        """Overwrite ``memory/MEMORY.md``."""
        self._loop.context.memory.write_memory(text)

    def append_history(self, text: str, *, session_key: str | None = None) -> int:
        """Append one entry to ``memory/history.jsonl`` and return its cursor."""
        return self._loop.context.memory.append_history(text, session_key=session_key)

    def read_history(self, *, session_key: str | None = None) -> list[dict[str, Any]]:
        """Read memory history entries, optionally filtered by session."""
        entries = self._loop.context.memory.read_unprocessed_history(since_cursor=0)
        if session_key is not None:
            entries = [entry for entry in entries if entry.get("session_key") == session_key]
        return deepcopy(entries)


class RuntimeClient:
    """Runtime control helpers exposed through ``bot.runtime``."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @property
    def model(self) -> str:
        """Current runtime model name."""
        return self._loop.model

    @property
    def workspace(self) -> Path:
        """Current runtime workspace."""
        return self._loop.workspace

    def add_context_provider(
        self,
        provider: RuntimeContextProvider,
    ) -> Callable[[], None]:
        """Register per-turn model context and return an unsubscribe callback."""
        return self._loop.register_runtime_context_provider(provider)

    def on_session_turn_persisted(
        self,
        handler: Callable[[SessionTurnPersisted], Awaitable[None] | None],
    ) -> Callable[[], None]:
        """Register a persisted-turn callback and return an unsubscribe callback."""
        return self._loop.runtime_events.subscribe(handler, SessionTurnPersisted)

    async def compact_session(self, session_key: str) -> SessionSnapshot:
        """Run token/replay-window consolidation for one session."""
        session = self._loop.sessions.get_or_create(session_key)
        runtime = self._loop.runtime_for_session(session)
        await self._loop.consolidator.maybe_consolidate_by_tokens(
            session,
            runtime=runtime,
            replay_max_messages=replay_max_messages_for_context(
                runtime.context_window_tokens
            ),
        )
        return _redacted_snapshot(
            self._loop, snapshot_from_session(self._loop.sessions.get_or_create(session_key))
        )

    async def compact_idle_session(self, session_key: str, *, max_suffix: int = 8) -> str | None:
        """Run idle-session compaction for one session and return the summary."""
        session = self._loop.sessions.get_or_create(session_key)
        runtime = self._loop.runtime_for_session(session)
        return await self._loop.consolidator.compact_idle_session(
            session_key,
            runtime=runtime,
            max_suffix=max_suffix,
        )
