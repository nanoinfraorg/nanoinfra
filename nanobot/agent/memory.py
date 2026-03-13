"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None

class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into MEMORY.md + HISTORY.md."""
        if not messages:
            return True

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        chat_messages = [
            {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice={"type": "function", "function": {"name": "save_memory"}},
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return False

            if entry := args.get("history_entry"):
                self.append_history(_ensure_text(entry))
            if update := args.get("memory_update"):
                update = _ensure_text(update)
                if update != current_memory:
                    self.write_long_term(update)

            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates.

    Consolidation runs asynchronously in the background when sessions are idle,
    so it doesn't block user interactions.
    """

    _MAX_CONSOLIDATION_ROUNDS = 5
    _IDLE_CHECK_INTERVAL = 30  # seconds between idle checks

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._background_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._session_last_activity: dict[str, float] = {}  # session_key -> last activity timestamp

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def record_activity(self, session_key: str) -> None:
        """Record that a session is active (for idle detection)."""
        self._session_last_activity[session_key] = asyncio.get_event_loop().time()

    async def start_background_task(self) -> None:
        """Start the background task that checks for idle sessions and consolidates."""
        if self._background_task is not None and not self._background_task.done():
            return  # Already running
        self._stop_event.clear()
        self._background_task = asyncio.create_task(self._idle_consolidation_loop())

    async def stop_background_task(self) -> None:
        """Stop the background task."""
        self._stop_event.set()
        if self._background_task is not None and not self._background_task.done():
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
            self._background_task = None

    async def _idle_consolidation_loop(self) -> None:
        """Background loop that checks for idle sessions and triggers consolidation."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._IDLE_CHECK_INTERVAL)
                if self._stop_event.is_set():
                    break

                # Check all sessions for idleness
                current_time = asyncio.get_event_loop().time()
                for session in list(self.sessions.all()):
                    last_active = self._session_last_activity.get(session.key, 0)
                    if current_time - last_active > self._IDLE_CHECK_INTERVAL * 2:
                        # Session is idle, trigger consolidation
                        await self.maybe_consolidate_by_tokens_async(session)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in background consolidation loop")

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive a selected message chunk into persistent memory."""
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_unconsolidated(self, session: Session) -> bool:
        """Archive the full unconsolidated tail for /new-style session rollover."""
        lock = self.get_lock(session.key)
        async with lock:
            snapshot = session.messages[session.last_consolidated:]
            if not snapshot:
                return True
            return await self.consolidate_messages(snapshot)

    def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Schedule token-based consolidation to run asynchronously in background.

        This method is synchronous and just schedules the consolidation task.
        The actual consolidation runs in the background when the session is idle.
        """
        if not session.messages or self.context_window_tokens <= 0:
            return
        # Schedule for background execution
        asyncio.create_task(self._schedule_consolidation(session))

    async def _schedule_consolidation(self, session: Session) -> None:
        """Internal method to run consolidation asynchronously."""
        await self.maybe_consolidate_by_tokens_async(session)

    async def maybe_consolidate_by_tokens_async(self, session: Session) -> None:
        """Async version: Loop and archive old messages until prompt fits within half the context window.

        This is called from the background task when a session is idle.
        """
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            target = self.context_window_tokens // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < self.context_window_tokens:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                if not await self.consolidate_messages(chunk):
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return

            logger.debug(
                "Token consolidation complete for {}: {}/{} via {}",
                session.key,
                estimated,
                self.context_window_tokens,
                source,
            )
