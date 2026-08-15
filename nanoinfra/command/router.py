"""Minimal command routing table for slash commands."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from nanoinfra.agent.loop import AgentLoop
    from nanoinfra.bus.events import InboundMessage, OutboundMessage
    from nanoinfra.session.manager import Session
    from nanoinfra.utils.llm_runtime import LLMRuntime

Handler = Callable[["CommandContext"], Awaitable["OutboundMessage | None"]]
_BOT_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_]+$")


def normalize_command_text(text: str) -> str:
    """Normalize slash-command transport variants before routing.

    Telegram and Discord-style command dispatch can produce ``/cmd@bot args``.
    The bot suffix belongs to the transport, not the command name, so strip it
    once at the router boundary while preserving user arguments verbatim.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return stripped
    first, sep, rest = stripped.partition(" ")
    if "@" not in first:
        return stripped
    command, suffix = first.rsplit("@", 1)
    if command and suffix and _BOT_SUFFIX_RE.fullmatch(suffix):
        return f"{command}{sep}{rest}" if sep else command
    return stripped


@dataclass
class CommandContext:
    """Everything a command handler needs to produce a response."""

    msg: InboundMessage
    session: Session | None
    key: str
    raw: str
    args: str = ""
    loop: AgentLoop = field(kw_only=True)
    runtime: LLMRuntime | None = None
    is_user_turn: bool = False
    turn_scopes: list[AbstractContextManager[Any]] = field(default_factory=list)


class CommandRouter:
    """Pure dict-based command dispatch.

    Four tiers checked in order:
      1. *priority* — exact-match commands handled before the dispatch lock
         (e.g. /stop, /restart).
      2. *priority prefix*, longest-prefix-first match, also handled before the
         dispatch lock (e.g. "/approve ").
      3. *exact* — exact-match commands handled inside the dispatch lock.
      4. *prefix* — longest-prefix-first match (e.g. "/team ").

    The priority tiers run in ``AgentLoop.run`` before every other branch, so a command
    there never reaches a model turn. A command that must stay out of the transcript needs
    an argument sometimes, and tier 1 matches one exact string. Tier 2 exists for that
    case, and nanoinfraorg/nanoinfra#43 is the first caller.
    """

    def __init__(self) -> None:
        self._priority: dict[str, Handler] = {}
        self._priority_prefix: list[tuple[str, Handler]] = []
        self._exact: dict[str, Handler] = {}
        self._prefix: list[tuple[str, Handler]] = []

    def priority(self, cmd: str, handler: Handler) -> None:
        self._priority[cmd] = handler

    def priority_prefix(self, pfx: str, handler: Handler) -> None:
        """Register a priority command that takes arguments.

        The prefix carries its own trailing space, the way ``prefix`` does. Register the
        bare command in tier 1 beside it, so a caller who sends no argument reads the two
        forms instead of reaching a model turn.
        """
        self._priority_prefix.append((pfx, handler))
        self._priority_prefix.sort(key=lambda entry: len(entry[0]), reverse=True)

    def exact(self, cmd: str, handler: Handler) -> None:
        self._exact[cmd] = handler

    def prefix(self, pfx: str, handler: Handler) -> None:
        self._prefix.append((pfx, handler))
        self._prefix.sort(key=lambda p: len(p[0]), reverse=True)

    def is_priority(self, text: str) -> bool:
        cmd = normalize_command_text(text).lower()
        if cmd in self._priority:
            return True
        return any(cmd.startswith(pfx) for pfx, _ in self._priority_prefix)

    def is_dispatchable_command(self, text: str) -> bool:
        """Check whether *text* matches any non-priority command tier (exact or prefix).

        Does NOT check either priority tier.
        If this returns True, ``dispatch()`` is guaranteed to match a handler.
        """
        cmd = normalize_command_text(text).lower()
        if cmd in self._exact:
            return True
        for pfx, _ in self._prefix:
            if cmd.startswith(pfx):
                return True
        return False

    async def dispatch_priority(self, ctx: CommandContext) -> OutboundMessage | None:
        """Dispatch a priority command. Called from run() without the lock."""
        ctx.raw = normalize_command_text(ctx.raw)
        cmd = ctx.raw.lower()
        if handler := self._priority.get(cmd):
            return await handler(ctx)

        for pfx, prefix_handler in self._priority_prefix:
            if cmd.startswith(pfx):
                # The slice comes from the original text, so an argument keeps its case.
                ctx.args = ctx.raw[len(pfx):]
                return await prefix_handler(ctx)

        return None

    async def dispatch(self, ctx: CommandContext) -> OutboundMessage | None:
        """Try exact, then prefix handlers. Returns None if unhandled."""
        ctx.raw = normalize_command_text(ctx.raw)
        cmd = ctx.raw.lower()

        if handler := self._exact.get(cmd):
            return await handler(ctx)

        for pfx, handler in self._prefix:
            if cmd.startswith(pfx):
                ctx.args = ctx.raw[len(pfx):]
                return await handler(ctx)

        return None
