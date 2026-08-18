"""Session management for conversation history."""

import base64
import errno
import json
import os
import re
import secrets
import shutil
from collections import OrderedDict
from collections.abc import Generator
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict, cast
from weakref import WeakValueDictionary

from filelock import FileLock
from loguru import logger

from nanoinfra.config.paths import get_legacy_sessions_dir, get_runtime_subdir
from nanoinfra.providers.base import ProviderConversationState
from nanoinfra.runtime_context import (
    RUNTIME_CONTEXT_HISTORY_META,
    public_history_message,
)
from nanoinfra.utils.helpers import (
    content_with_media_breadcrumbs,
    ensure_dir,
    estimate_message_tokens,
    find_legal_message_start,
    recent_message_start_index,
    safe_filename,
    strip_think,
)
from nanoinfra.utils.subagent_channel_display import scrub_subagent_announce_body

FILE_MAX_MESSAGES = 2000
SESSION_CACHE_MAX_SIZE = 128
MIN_REPLAY_MAX_MESSAGES = 120
MIN_COMPACTED_REPLAY_MESSAGES = 8
REPLAY_TOKENS_PER_MESSAGE = 100
_MESSAGE_TIME_PREFIX_RE = re.compile(r"^\[Message Time: [^\]]+\]\n?")
_LOCAL_IMAGE_BREADCRUMB_RE = re.compile(r"^\[image: (?:/|~)[^\]]+\]\s*$")
_TOOL_CALL_ECHO_RE = re.compile(r'^\s*(?:generate_image|message)\([^)]*\)\s*$')
_SESSION_PREVIEW_MAX_CHARS = 120
_SESSION_LIST_PREVIEW_MAX_RECORDS = 200
_SESSION_LIST_PREVIEW_MAX_CHARS = 1_000_000
_SESSION_DATA_ERRORS = (ValueError, TypeError, AttributeError, KeyError)

# Session history lives outside the workspace (#136), so each workspace needs a namespace under the
# shared root. The namespace is a random id recorded *inside* the workspace rather than a hash of
# its path: an operator who renames or relocates a workspace would otherwise orphan every
# transcript it owns. The marker travels with the directory, so the history travels with it.
_WORKSPACE_STATE_DIR = ".nanoinfra"
_WORKSPACE_ID_FILE = "workspace-id"
_WORKSPACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_WORKSPACE_BACKREF_FILE = ".workspace"
_SESSION_MIGRATION_LOCK_TIMEOUT_SECONDS = 30
# Guards the canonical *.jsonl files. Longer than the migration timeout because a save may follow a
# large history read, and a spurious timeout here would surface as a lost turn.
_SESSION_FILES_LOCK_FILENAME = ".session-files.lock"
_SESSION_FILES_LOCK_TIMEOUT_SECONDS = 60
_PROVIDER_STATE_RECORD_TYPE = "provider_state"
_PROVIDER_STATE_RECORD_PREFIX_RE = re.compile(
    r'^\s*\{\s*"_type"\s*:\s*"provider_state"\s*(?:,|\})'
)
_FORK_VOLATILE_METADATA_KEYS = {
    "goal_state",
    "pending_user_turn",
    "runtime_checkpoint",
    "thread_goal",
    "title",
    "title_user_edited",
}


def _json_object(value: object) -> dict[str, Any]:
    """Narrow a decoded JSON object while preserving its original values."""
    if not isinstance(value, dict):
        raise ValueError("session records must be JSON objects")
    return cast(dict[str, Any], value)


def _is_provider_state_record_line(line: str) -> bool:
    """Recognize the canonical private record without decoding its opaque payload."""
    return _PROVIDER_STATE_RECORD_PREFIX_RE.match(line) is not None


def replay_max_messages_for_context(context_window_tokens: int | None) -> int:
    if not context_window_tokens or context_window_tokens <= 0:
        return FILE_MAX_MESSAGES
    return min(
        FILE_MAX_MESSAGES,
        max(MIN_REPLAY_MAX_MESSAGES, context_window_tokens // REPLAY_TOKENS_PER_MESSAGE),
    )


def _sanitize_assistant_replay_text(content: str) -> str:
    """Remove internal replay artifacts that the model may have copied before.

    These strings are useful as runtime/session metadata, but when they appear
    in assistant examples they become demonstrations for the model to repeat.
    """
    content = _MESSAGE_TIME_PREFIX_RE.sub("", content, count=1)
    lines = [
        line
        for line in content.splitlines()
        if not _LOCAL_IMAGE_BREADCRUMB_RE.match(line)
        and not _TOOL_CALL_ECHO_RE.match(line)
    ]
    return "\n".join(lines).strip()


def _is_scrubbed_thinking_block(block: object, marker_key: str) -> bool:
    """Report whether a scrub changed this block, and therefore unsigned it (#48)."""
    if not isinstance(block, dict):
        return False
    return marker_key in cast("dict[str, Any]", block)


def _replayable_thinking_blocks(value: object) -> object:
    """Return the thinking blocks a provider may still receive (#48).

    A scrub changes the text of a block, so the persisted block loses its
    signature and carries a marker. A provider needs a signature that matches
    the text, and a mismatched pair is worse than no block at all. So a marked
    block stays in the file for a human, and it replays as nothing.

    The import is local, because ``nanoinfra.agent.redaction`` reaches this
    module through its own imports. A module level import here closes a cycle.
    """
    if not isinstance(value, list):
        return value
    from nanoinfra.agent.redaction import REASONING_SCRUB_MARKER_KEY

    return [
        block
        for block in cast("list[object]", value)
        if not _is_scrubbed_thinking_block(block, REASONING_SCRUB_MARKER_KEY)
    ]


def _drop_unreplayable_thinking_blocks(entry: dict[str, Any]) -> bool:
    """Remove the thinking blocks a provider may no longer receive, in place (#48).

    Report whether the entry changed. Two replay paths read this: the message
    history below, and the pending messages of a provider state (#52). Both send
    an assistant record to a provider, so both drop the same block.

    An empty list goes as well. A key with no block carries nothing, and a
    provider that rejects an empty list would fail on a record that says
    nothing.
    """
    if "thinking_blocks" not in entry:
        return False
    original = cast(object, entry["thinking_blocks"])
    blocks = _replayable_thinking_blocks(original)
    if blocks:
        entry["thinking_blocks"] = blocks
        return blocks != original
    del entry["thinking_blocks"]
    return True


def _replayable_provider_state(
    state: ProviderConversationState | None,
) -> ProviderConversationState | None:
    """Return the provider state a provider may still receive (#52).

    The pending messages of a state replay to the provider on the next request,
    so a thinking block the scrub changed reaches a provider through this path
    as well. The scrub unsigned that block and marked it (#48), and a provider
    needs a signature that matches the text, so the block goes here too.

    The filter runs on the read side rather than the write side, for two
    reasons. The file stays a faithful record of what the turn produced, which
    is the same split #48 chose for a message. And a record written before this
    change still holds a marked block, so a read-side filter covers the old
    files as well as the new ones.
    """
    if state is None or not state.pending_messages:
        return state
    pending: list[dict[str, Any]] = []
    changed = False
    for message in state.pending_messages:
        entry = dict(message)
        changed = _drop_unreplayable_thinking_blocks(entry) or changed
        pending.append(entry)
    return state.with_pending_messages(pending) if changed else state


def _persistable_provider_state_record(
    state: ProviderConversationState,
    workspace: Path | str | None,
) -> dict[str, Any] | None:
    """Return the private record for the file, or None to write no state (#52, #54).

    Both halves of a state scrub now, and each one for its own reason.

    ``to_private_record`` copies ``pending_messages`` verbatim, and those are
    Chat-style messages this repository built after the last committed turn. So
    they hold tool arguments, tool output, and reasoning: exactly the content
    #41 and #48 scrub on the message path of this same file (#52).

    ``payload`` is the provider's own handle, and #52 left it byte-exact on the
    stated ground that only the provider that issued a field knows whether that
    field must stay byte-exact. #54 measured that half and found message text in
    it: the Responses builders write the resolved command into the items, and
    ``pending_messages`` is empty on that path, so #52 covered none of it. The
    named carriers therefore live with the provider that issues them, in
    ``nanoinfra/providers/openai_responses/redaction.py``, and a payload of any
    other kind still reaches the file byte-exact.

    The executor holds the sentinels in both cases, so this process decrypts
    nothing (#41).

    **One round trip covers the whole record (#54).** A Responses payload holds
    one item per message plus one per tool call, so the per-text wire would open
    one connection per item on every save. ``in_one_batch`` collects every text
    of both halves, asks once, and fills the answers back in.

    **A scrub that cannot run writes no state at all.** The message path
    persists a marker in place of the text (#41), and that answer is wrong here.
    A provider state is replay input rather than a record a human reads, so a
    marker would send the model a sentence it never wrote. A missing state
    degrades to a normal replay from the message history, so fail-closed costs
    a provider-side cache and never the session.

    The imports are local, because ``nanoinfra.agent.redaction`` reaches this
    module through its own imports. A module level import here closes a cycle.
    """
    record = state.to_private_record()
    raw_pending = cast(object, record.get("pending_messages"))
    raw_payload = cast(object, record.get("payload"))
    # A field of an unexpected type stays out of the scrub and out of the rewrite below, so a
    # malformed record reaches the file exactly as ``to_private_record`` built it.
    pending: list[dict[str, Any]] = (
        cast("list[dict[str, Any]]", raw_pending) if isinstance(raw_pending, list) else []
    )
    payload: dict[str, Any] | None = (
        cast("dict[str, Any]", raw_payload) if isinstance(raw_payload, dict) else None
    )

    from nanoinfra.agent.redaction import (
        SCRUB_UNAVAILABLE_MARKER,
        TranscriptRedactor,
        redact_messages,
    )
    from nanoinfra.providers.openai_responses.redaction import scrub_provider_state_payload

    def _scrub_halves(
        scrub: Callable[[str, str | None], str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Scrub both halves with one scrubber. ``in_one_batch`` runs this twice, so it is pure.

        ``redact_messages`` is the module function rather than the redactor method, because the
        method answers markers for a failure and this caller needs the failure itself. The
        fail-closed answer here is no record, and only a raise can reach that.
        """
        messages = (
            # max_tool_result_chars=None: a pending message replays to the provider, and the
            # persisted copy of the same message already carries the bound the agent loop
            # applied. A second, smaller bound here would shorten what the provider receives.
            redact_messages(pending, scrub, max_tool_result_chars=None) if pending else pending
        )
        items = (
            scrub_provider_state_payload(state.kind, payload, scrub)
            if payload is not None
            else None
        )
        return messages, items

    redactor = TranscriptRedactor.for_workspace(workspace)
    try:
        scrubbed_pending, scrubbed_payload = redactor.in_one_batch(_scrub_halves)
    except Exception as exc:  # noqa: BLE001 -- no scrub means no state persists
        logger.warning("Persisted no provider state, because no scrub ran: {}", exc)
        return None

    # A marker can still arrive inside a pending message, because ``redact_message`` withholds a
    # field it cannot scrub even when the scrubber itself did not raise (#52).
    #
    # The test covers the pending half only, and never the payload. The payload items are built
    # from the session history, so an item can legitimately quote a marker the message path wrote
    # in an earlier turn. A test there would drop the provider state of every session that ever
    # withheld one text, for the rest of that session's life.
    #
    # Only a redactor that asks the executor can withhold anything, so a workspace with no stored
    # secret pays nothing for this test.
    if pending and redactor.asks_the_executor:
        marker_prefix = SCRUB_UNAVAILABLE_MARKER.split("{reason}", 1)[0]
        if marker_prefix in json.dumps(scrubbed_pending, ensure_ascii=False):
            logger.warning("Persisted no provider state, because the scrub withheld a text")
            return None

    if pending:
        record["pending_messages"] = scrubbed_pending
    if scrubbed_payload is not None:
        record["payload"] = scrubbed_payload
    return record


def _text_preview(content: object) -> str:
    """Return compact display text for session lists."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in cast(list[object], content):
            if isinstance(block, dict):
                block_data = cast(dict[object, object], block)
                if block_data.get("type") != "text":
                    continue
                value = block_data.get("text")
                if isinstance(value, str):
                    parts.append(value)
        text = " ".join(parts)
    else:
        return ""
    text = _sanitize_assistant_replay_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _SESSION_PREVIEW_MAX_CHARS:
        text = text[: _SESSION_PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


def _message_preview_text(message: dict[str, Any]) -> str:
    """Session list preview text; subagent inject blobs are shortened for display."""
    message = public_history_message(message)
    content = cast(object, message.get("content"))
    if message.get("injected_event") == "subagent_result" and isinstance(content, str):
        content = scrub_subagent_announce_body(content)
    return _text_preview(content)


def _metadata_title(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    metadata_data = cast(dict[object, object], metadata)
    title = metadata_data.get("title")
    if not isinstance(title, str):
        return ""
    if metadata_data.get("title_user_edited") is True:
        return title
    return strip_think(title)


@dataclass
class RetentionResult:
    dropped: list[dict[str, Any]]
    already_consolidated_count: int


@dataclass
class Session:
    """A conversation session."""

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    provider_state: ProviderConversationState | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.metadata), dict):
            self.metadata = {}
        if not isinstance(cast(object, self.provider_state), ProviderConversationState):
            self.provider_state = None
        # An out-of-range offset (corrupt metadata) would hide all history; reset it.
        last_consolidated = cast(object, self.last_consolidated)
        if (
            isinstance(last_consolidated, bool)
            or not isinstance(last_consolidated, int)
            or not 0 <= last_consolidated <= len(self.messages)
        ):
            self.last_consolidated = 0

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = FILE_MAX_MESSAGES,
        *,
        max_tokens: int = 0,
        extend_to_user: bool = False,
        include_runtime_context: bool = True,
    ) -> list[dict[str, Any]]:
        """Return recent replayable messages for LLM input.

        History is sliced by message count first (``max_messages``), then by
        token budget from the tail (``max_tokens``) when provided.
        """
        replay_start = self.last_consolidated
        if replay_start:
            # ``last_consolidated`` is archive progress, not a replay boundary.
            # Keep a small raw suffix for continuity, extending back to the user
            # that started an assistant/tool sequence when necessary.
            recent_start = recent_message_start_index(
                self.messages,
                MIN_COMPACTED_REPLAY_MESSAGES,
                extend_to_user=True,
            )
            replay_start = min(replay_start, recent_start)

        replayable = self.messages[replay_start:]
        max_messages = max_messages if max_messages > 0 else FILE_MAX_MESSAGES
        unarchived_count = len(self.messages) - self.last_consolidated
        if replay_start < self.last_consolidated and unarchived_count < max_messages:
            # The archived replay suffix can exceed the nominal count when one
            # tool-heavy turn spans the boundary. Preserve that complete turn.
            start_idx = 0
        else:
            start_idx = recent_message_start_index(
                replayable,
                max_messages,
                extend_to_user=extend_to_user,
            )
        sliced = replayable[start_idx:]

        # Avoid starting mid-turn when possible, except for proactive
        # assistant deliveries that the user may be replying to.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        # Drop orphan tool results at the front.
        start = find_legal_message_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            if message.get("_command"):
                continue
            has_persisted_runtime_context = isinstance(
                message.get(RUNTIME_CONTEXT_HISTORY_META),
                dict,
            )
            if not include_runtime_context:
                message = public_history_message(message)
            content = message.get("content", "")
            role = message.get("role")
            if role == "assistant" and isinstance(content, str):
                content = _sanitize_assistant_replay_text(content)
            # Synthesize an ``[image: path]`` breadcrumb from the persisted
            # ``media`` kwarg so LLM replay still sees *something* where the
            # image used to be. Without this, an image-only user turn
            # replays as an empty user message — the assistant's reply then
            # looks like it's responding to nothing.
            content = content_with_media_breadcrumbs(
                role,
                content,
                message.get("media"),
            )
            cli_apps = cast(object, message.get("cli_apps"))
            if (
                include_runtime_context
                and not has_persisted_runtime_context
                and role == "user"
                and isinstance(cli_apps, list)
                and cli_apps
                and isinstance(content, str)
            ):
                cli_lines: list[str] = []
                for item in cast(list[object], cli_apps[:8]):
                    if not isinstance(item, dict):
                        continue
                    item_data = cast(dict[object, object], item)
                    name = str(item_data.get("name") or "").strip().lower()
                    if not name:
                        continue
                    entry_point = (
                        str(item_data.get("entry_point") or "unknown").strip() or "unknown"
                    )
                    cli_lines.append(
                        f"[CLI App Attachment: @{name}; tool=run_cli_app; entry_point={entry_point}; "
                        f"skill=skills/cli-app-{name}/SKILL.md]"
                    )
                if cli_lines:
                    breadcrumbs = "\n".join(cli_lines)
                    content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            if role == "assistant" and isinstance(content, str) and not content.strip():
                if not any(key in message for key in ("tool_calls", "reasoning_content", "thinking_blocks")):
                    continue
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
                if key in message:
                    entry[key] = message[key]
            # A scrubbed block lost its signature (#48), so it replays as nothing.
            _drop_unreplayable_thinking_blocks(entry)
            out.append(entry)

        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for message in reversed(out):
                tokens = estimate_message_tokens(message)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(message)
                used += tokens
            kept.reverse()

            # Keep history aligned to the first visible user turn.
            first_user = next((i for i, m in enumerate(kept) if m.get("role") == "user"), None)
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # Tight token budgets can otherwise leave assistant-only tails.
                # If a user turn exists in the unsliced output, recover the
                # nearest one even if it slightly exceeds the token budget.
                recovered_user = next(
                    (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
                    None,
                )
                if recovered_user is not None:
                    kept = out[recovered_user:]

            # And keep a legal tool-call boundary at the front.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
            out = kept
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.provider_state = None
        self.updated_at = datetime.now()
        self.metadata.pop("_last_summary", None)

    def retain_recent_legal_suffix(
        self,
        max_messages: int,
        *,
        extend_to_user: bool = False,
    ) -> RetentionResult:
        """Keep a legal recent suffix, optionally extending it back to a user turn.

        Returns a RetentionResult with dropped messages and how many of those
        were in the already-consolidated prefix. This method mutates
        self.messages and self.last_consolidated in place.
        """
        if max_messages <= 0:
            dropped = list(self.messages)
            lc = self.last_consolidated
            self.clear()
            return RetentionResult(
                dropped=dropped,
                already_consolidated_count=min(lc, len(dropped)),
            )
        if len(self.messages) <= max_messages:
            return RetentionResult(
                dropped=[],
                already_consolidated_count=0,
            )

        original = list(self.messages)
        before_lc = self.last_consolidated

        start_idx = max(0, len(self.messages) - max_messages)
        if extend_to_user:
            recovered_user = next(
                (i for i in range(start_idx, -1, -1) if self.messages[i].get("role") == "user"),
                None,
            )
            if recovered_user is not None:
                start_idx = recovered_user
                if start_idx > 0 and self.messages[start_idx - 1].get("_channel_delivery"):
                    start_idx -= 1

        retained = self.messages[start_idx:]

        # Prefer starting at a user turn (or its preceding _channel_delivery) when one exists within the retained window.
        first_user = next((i for i, m in enumerate(retained) if m.get("role") == "user"), None)
        if first_user is not None:
            if first_user > 0 and retained[first_user - 1].get("_channel_delivery"):
                retained = retained[first_user - 1:]
            else:
                retained = retained[first_user:]
        elif not extend_to_user:
            # If the hard-capped tail is assistant/tool-only, anchor to the
            # latest user in the full session and take a capped forward window.
            latest_user = next(
                (i for i in range(len(self.messages) - 1, -1, -1)
                 if self.messages[i].get("role") == "user"),
                None,
            )
            if latest_user is not None:
                retained = self.messages[latest_user: latest_user + max_messages]

        # Mirror get_history(): avoid persisting orphan tool results at the front.
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        # Hard-cap guarantee unless the caller requested user-turn extension.
        if not extend_to_user and len(retained) > max_messages:
            retained = retained[-max_messages:]
            start = find_legal_message_start(retained)
            if start:
                retained = retained[start:]

        # Compute actually-dropped messages using identity comparison so that
        # even when retained is a non-contiguous slice of original (the else
        # branch above), we never duplicate or lose messages.
        retained_ids = set(id(m) for m in retained)
        dropped = [m for m in original if id(m) not in retained_ids]

        # Count how many dropped messages were in the already-consolidated
        # prefix of the original list.  This cannot be a simple min() because
        # dropped may include messages from *after* the consolidated prefix
        # (e.g. in the else branch).
        already_consolidated = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) not in retained_ids
        )

        # New last_consolidated = count of retained messages that were inside
        # the old consolidated prefix.
        new_lc = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) in retained_ids
        )

        self.messages = retained
        self.last_consolidated = new_lc
        if dropped:
            self.provider_state = None
        self.updated_at = datetime.now()
        return RetentionResult(
            dropped=dropped,
            already_consolidated_count=already_consolidated,
        )

    def enforce_file_cap(
        self,
        on_archive: Callable[[list[dict[str, Any]]], None] | None = None,
        limit: int = FILE_MAX_MESSAGES,
    ) -> None:
        """Bound session message growth by archiving and trimming old prefixes."""
        if limit <= 0 or len(self.messages) <= limit:
            return

        result = self.retain_recent_legal_suffix(limit)
        if not result.dropped:
            return

        archive_chunk = result.dropped[result.already_consolidated_count:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)
        logger.info(
            "Session file cap hit for {}: dropped {}, raw-archived {}, kept {}",
            self.key,
            len(result.dropped),
            len(archive_chunk),
            len(self.messages),
        )


class SessionPayload(TypedDict):
    key: str
    created_at: str | None
    updated_at: str | None
    metadata: dict[str, Any]
    messages: list[dict[str, Any]]


class SessionMetadataPayload(TypedDict):
    key: str
    created_at: str | None
    updated_at: str | None
    metadata: dict[str, Any]


class SessionInfo(TypedDict):
    key: str
    created_at: str
    updated_at: str
    title: str
    preview: str
    path: str


class SessionStore(Protocol):
    def load(self, key: str) -> Session | None: ...

    def save(self, session: Session, *, fsync: bool = False) -> None: ...

    def delete(self, key: str) -> bool: ...

    def read(self, key: str) -> SessionPayload | None: ...

    def read_metadata(self, key: str) -> SessionMetadataPayload | None: ...

    def list_sessions(self) -> list[SessionInfo]: ...


class JsonlSessionStore:
    """JSONL implementation of session persistence."""

    def __init__(self, workspace: Path, *, sessions_root: Path | None = None):
        # The workspace stays, because a save scrubs the pending messages of a provider state
        # and the redactor needs it to tell a workspace with a stored secret from one without
        # (#52). A workspace with no secret then costs no round trip.
        canonical_workspace = Path(workspace).expanduser().resolve(strict=False)
        ensure_dir(canonical_workspace)
        root = (
            Path(sessions_root).expanduser().resolve(strict=False)
            if sessions_root is not None
            else get_runtime_subdir("sessions").resolve(strict=False)
        )
        # The whole point of #136 is that the agent's filesystem tools cannot reach transcripts.
        # A root inside the workspace would restore exactly what this moves away from, so it is
        # refused rather than silently accepted.
        if root == canonical_workspace or root.is_relative_to(canonical_workspace):
            raise RuntimeError(
                "session storage must be outside the agent workspace; "
                "move --config outside --workspace or choose a nested workspace directory"
            )
        ensure_dir(root)
        with suppress(OSError):
            os.chmod(root, 0o700)
        self.workspace = canonical_workspace
        # The gateway and a CLI invocation can start at once, and both would migrate. One lock on
        # the shared root makes the namespace claim and the migration happen once.
        self._migration_lock = FileLock(
            str(root / ".workspace-migration.lock"),
            timeout=_SESSION_MIGRATION_LOCK_TIMEOUT_SECONDS,
        )
        with self._migration_lock:
            workspace_id = self._load_or_create_workspace_id(canonical_workspace, root)
            self.sessions_dir = ensure_dir(root / workspace_id)
            self.legacy_sessions_dir = get_legacy_sessions_dir()
            # Serializes access to the canonical session files across processes (#152). Taken
            # *inside* the migration lock here and never the other way round, so the one place
            # that holds both fixes the order and no caller can invert it into a deadlock.
            self._session_files_lock = FileLock(
                str(self.sessions_dir / _SESSION_FILES_LOCK_FILENAME),
                timeout=_SESSION_FILES_LOCK_TIMEOUT_SECONDS,
            )
            self._write_workspace_backref(self.sessions_dir, canonical_workspace)
            self._migrate_from_workspace(canonical_workspace)

    @contextmanager
    def locked_session_files(self) -> Generator[Path, None, None]:
        """Hold the session-file lock for a whole read-modify-write.

        This is the only way to be safe against a lost update. Serializing the individual
        operations is not enough: two writers that each load, mutate and save will still have the
        second overwrite the first, because the mutation happens between the two locked calls. A
        caller that reads a session, changes it, and writes it back has to hold the lock across all
        three, which is what this is for.

        Re-entrant: ``FileLock`` counts acquisitions per instance, so the locked public methods
        below can be called inside this block without deadlocking.
        """
        with self._session_files_lock:
            yield self.sessions_dir

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        with suppress(PermissionError, NotImplementedError):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno != errno.EINVAL:
                    raise
            finally:
                os.close(fd)

    @classmethod
    def _write_text_atomic(cls, path: Path, content: str, *, mode: int = 0o600) -> None:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with open(tmp, "x", encoding="utf-8") as handle:
                os.chmod(tmp, mode)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            cls._fsync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _workspace_id_path(workspace: Path) -> Path:
        state_dir = workspace / _WORKSPACE_STATE_DIR
        if state_dir.is_symlink():
            raise RuntimeError(f"workspace state directory must not be a symlink: {state_dir}")
        ensure_dir(state_dir)
        return state_dir / _WORKSPACE_ID_FILE

    @staticmethod
    def _read_workspace_id(marker: Path) -> str:
        if marker.is_symlink():
            raise RuntimeError(f"workspace identity marker must not be a symlink: {marker}")
        value = marker.read_text(encoding="utf-8").strip()
        if not _WORKSPACE_ID_RE.fullmatch(value):
            raise RuntimeError(
                f"workspace identity marker is invalid: {marker}; "
                "restore its original 32-character identifier before starting nanoinfra"
            )
        return value

    @classmethod
    def _load_or_create_workspace_id(cls, workspace: Path, root: Path) -> str:
        """Return this workspace's stable namespace, creating it on first use."""
        marker = cls._workspace_id_path(workspace)
        if marker.is_symlink() or marker.exists():
            return cls._read_workspace_id(marker)
        # Workspace cleanup can delete the marker while the store it names still holds history.
        # The backref written into each store lets that history be reclaimed instead of stranded.
        recovered = cls._find_workspace_namespace(workspace, root)
        workspace_id = recovered or secrets.token_hex(16)
        cls._write_text_atomic(marker, f"{workspace_id}\n")
        return workspace_id

    @classmethod
    def _find_workspace_namespace(cls, workspace: Path, root: Path) -> str | None:
        """Recover the namespace of an existing store that names this workspace."""
        matches: list[str] = []
        with suppress(OSError):
            for sessions_dir in root.iterdir():
                if (
                    not _WORKSPACE_ID_RE.fullmatch(sessions_dir.name)
                    or sessions_dir.is_symlink()
                    or not sessions_dir.is_dir()
                ):
                    continue
                backref = sessions_dir / _WORKSPACE_BACKREF_FILE
                if backref.is_symlink() or not backref.is_file():
                    continue
                with suppress(OSError, ValueError):
                    recorded = Path(backref.read_text(encoding="utf-8").strip()).expanduser()
                    if recorded.resolve(strict=False) == workspace:
                        matches.append(sessions_dir.name)
        # Two stores claiming one workspace is ambiguous, and picking one could hide history.
        if len(matches) != 1:
            if matches:
                logger.warning(
                    "Multiple session stores name workspace {}; starting a new one", workspace
                )
            return None
        return matches[0]

    @classmethod
    def _write_workspace_backref(cls, sessions_dir: Path, workspace: Path) -> None:
        backref = sessions_dir / _WORKSPACE_BACKREF_FILE
        if backref.exists():
            return
        try:
            cls._write_text_atomic(backref, f"{workspace}\n")
        except OSError as exc:
            logger.debug("Failed to write sessions workspace backref: {}", exc)

    def _migrate_from_workspace(self, workspace: Path) -> None:
        """Move legacy in-workspace session files into the out-of-workspace store."""
        old_dir = workspace / "sessions"
        if old_dir.is_symlink():
            # The link could name anything, including a directory that is not ours to empty.
            logger.warning("Skipping symlinked legacy sessions directory: {}", old_dir)
            return
        if not old_dir.is_dir():
            return
        for src in sorted(old_dir.glob("*.jsonl")):
            if src.is_symlink() or not src.is_file():
                logger.warning("Skipping unsafe legacy session file: {}", src)
                continue
            dst = self.sessions_dir / src.name
            # Never clobber: a file already in the new store is the live one.
            if dst.exists():
                continue
            try:
                shutil.move(str(src), str(dst))
            except OSError as exc:
                logger.warning("Failed to migrate session {}: {}", src, exc)

    @staticmethod
    def safe_key(key: str) -> str:
        return safe_filename(key.replace(":", "_"))

    @staticmethod
    def storage_key(key: str) -> str:
        return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")

    @staticmethod
    def decode_storage_key(stem: str) -> str | None:
        try:
            padding = 4 - len(stem) % 4
            if padding != 4:
                stem += "=" * padding
            return base64.urlsafe_b64decode(stem).decode("utf-8")
        except _SESSION_DATA_ERRORS:
            return None

    @classmethod
    def session_key_from_path(cls, path: Path) -> str | None:
        key = cls.decode_storage_key(path.stem)
        if key is None or cls.storage_key(key) != path.stem:
            return None
        return key

    def get_session_path(self, key: str) -> Path:
        return self.sessions_dir / f"{self.storage_key(key)}.jsonl"

    def get_legacy_lossy_path(self, key: str) -> Path:
        return self.sessions_dir / f"{safe_filename(key.replace(':', '_'))}.jsonl"

    def get_legacy_session_path(self, key: str) -> Path:
        return self.legacy_sessions_dir / f"{self.safe_key(key)}.jsonl"

    def load(self, key: str) -> Session | None:
        """Serialized against other processes (#152); see locked_session_files."""
        with self._session_files_lock:
            return self._load_unlocked(key)

    def _load_unlocked(self, key: str) -> Session | None:
        path = self.get_session_path(key)
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: datetime | None = None
            updated_at: datetime | None = None
            last_consolidated = 0
            provider_state: ProviderConversationState | None = None

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    raw_data: object = json.loads(line)
                    data = _json_object(raw_data)

                    record_type = data.get("_type")
                    if record_type == "metadata":
                        metadata_value = cast(object, data.get("metadata", {}))
                        metadata = (
                            cast(dict[str, Any], metadata_value)
                            if isinstance(metadata_value, dict)
                            else {}
                        )
                        created_at_value = cast(object, data.get("created_at"))
                        updated_at_value = cast(object, data.get("updated_at"))
                        created_at = (
                            datetime.fromisoformat(created_at_value)
                            if isinstance(created_at_value, str) and created_at_value
                            else None
                        )
                        updated_at = (
                            datetime.fromisoformat(updated_at_value)
                            if isinstance(updated_at_value, str) and updated_at_value
                            else None
                        )
                        offset = cast(object, data.get("last_consolidated", 0))
                        last_consolidated = (
                            offset
                            if isinstance(offset, int) and not isinstance(offset, bool)
                            else 0
                        )
                    elif record_type == _PROVIDER_STATE_RECORD_TYPE:
                        # A block the scrub unsigned reaches no provider (#52).
                        provider_state = _replayable_provider_state(
                            ProviderConversationState.from_private_record(data.get("state"))
                        )
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
                provider_state=provider_state,
            )
        except _SESSION_DATA_ERRORS as e:
            logger.warning("Failed to load session {}: {}", key, e)
            repaired = self.repair(key)
            if repaired is not None:
                logger.info(
                    "Recovered session {} from corrupt file ({} messages)",
                    key,
                    len(repaired.messages),
                )
            return repaired

    def repair(self, key: str, *, path: Path | None = None) -> Session | None:
        if path is None:
            path = self.get_session_path(key)
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: datetime | None = None
            updated_at: datetime | None = None
            last_consolidated = 0
            provider_state: ProviderConversationState | None = None
            skipped = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw_data: object = json.loads(line)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue
                    if not isinstance(raw_data, dict):
                        skipped += 1
                        continue
                    data = cast(dict[str, Any], raw_data)

                    record_type = data.get("_type")
                    if record_type == "metadata":
                        metadata_value = cast(object, data.get("metadata", {}))
                        metadata = (
                            cast(dict[str, Any], metadata_value)
                            if isinstance(metadata_value, dict)
                            else {}
                        )
                        created_at_value = cast(object, data.get("created_at"))
                        if isinstance(created_at_value, str) and created_at_value:
                            with suppress(ValueError):
                                created_at = datetime.fromisoformat(created_at_value)
                        updated_at_value = cast(object, data.get("updated_at"))
                        if isinstance(updated_at_value, str) and updated_at_value:
                            with suppress(ValueError):
                                updated_at = datetime.fromisoformat(updated_at_value)
                        offset = cast(object, data.get("last_consolidated", 0))
                        last_consolidated = (
                            offset
                            if isinstance(offset, int) and not isinstance(offset, bool)
                            else 0
                        )
                    elif record_type == _PROVIDER_STATE_RECORD_TYPE:
                        # A block the scrub unsigned reaches no provider (#52).
                        candidate = _replayable_provider_state(
                            ProviderConversationState.from_private_record(data.get("state"))
                        )
                        if candidate is None:
                            skipped += 1
                        else:
                            provider_state = candidate
                    else:
                        messages.append(data)

            if skipped:
                logger.warning("Skipped {} corrupt lines in session {}", skipped, key)

            if not messages and not metadata and provider_state is None:
                return None

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
                provider_state=provider_state,
            )
        except _SESSION_DATA_ERRORS as e:
            logger.warning("Repair failed for session {}: {}", key, e)
            return None

    @staticmethod
    def session_payload(session: Session) -> SessionPayload:
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """Serialized against other processes (#152); see locked_session_files."""
        with self._session_files_lock:
            self._save_unlocked(session, fsync=fsync)

    def _save_unlocked(self, session: Session, *, fsync: bool = False) -> None:
        path = self.get_session_path(session.key)
        tmp_path = path.with_suffix(".jsonl.tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                metadata_line = {
                    "_type": "metadata",
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated,
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                if session.provider_state is not None:
                    # The pending messages of a state scrub before they reach the file, and a
                    # state nobody scrubbed writes no line at all (#52).
                    state_record = _persistable_provider_state_record(
                        session.provider_state, self.workspace
                    )
                    if state_record is not None:
                        provider_state_line = {
                            "_type": _PROVIDER_STATE_RECORD_TYPE,
                            "state": state_record,
                        }
                        f.write(json.dumps(provider_state_line, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            os.replace(tmp_path, path)

            if fsync:
                with suppress(PermissionError):
                    fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    except OSError as exc:
                        if exc.errno != errno.EINVAL:
                            raise
                    finally:
                        os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def delete(self, key: str) -> bool:
        """Serialized against other processes (#152); see locked_session_files."""
        with self._session_files_lock:
            return self._delete_unlocked(key)

    def _delete_unlocked(self, key: str) -> bool:
        paths = [
            self.get_session_path(key),
            self.get_legacy_lossy_path(key),
            self.get_legacy_session_path(key),
        ]
        deleted = False
        for path in paths:
            if not path.exists():
                continue
            try:
                path.unlink()
                deleted = True
            except OSError as e:
                logger.warning("Failed to delete session file {}: {}", path, e)
        return deleted

    def read(self, key: str) -> SessionPayload | None:
        """Serialized against other processes (#152); see locked_session_files."""
        with self._session_files_lock:
            return self._read_unlocked(key)

    def _read_unlocked(self, key: str) -> SessionPayload | None:
        path = self.get_session_path(key)
        if not path.exists():
            return None
        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: str | None = None
            updated_at: str | None = None
            stored_key: str | None = None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    raw_data: object = json.loads(line)
                    data = _json_object(raw_data)
                    record_type = data.get("_type")
                    if record_type == "metadata":
                        metadata_value = cast(object, data.get("metadata", {}))
                        metadata = (
                            cast(dict[str, Any], metadata_value)
                            if isinstance(metadata_value, dict)
                            else {}
                        )
                        created_at_value = cast(object, data.get("created_at"))
                        updated_at_value = cast(object, data.get("updated_at"))
                        stored_key_value = cast(object, data.get("key"))
                        created_at = (
                            created_at_value if isinstance(created_at_value, str) else None
                        )
                        updated_at = (
                            updated_at_value if isinstance(updated_at_value, str) else None
                        )
                        stored_key = (
                            stored_key_value if isinstance(stored_key_value, str) else None
                        )
                    elif record_type == _PROVIDER_STATE_RECORD_TYPE:
                        continue
                    else:
                        messages.append(data)
            return {
                "key": stored_key or key,
                "created_at": created_at,
                "updated_at": updated_at,
                "metadata": metadata,
                "messages": messages,
            }
        except _SESSION_DATA_ERRORS as e:
            logger.warning("Failed to read session {}: {}", key, e)
            repaired = self.repair(key, path=path)
            if repaired is not None:
                logger.info("Recovered read-only session view {} from corrupt file", key)
                return self.session_payload(repaired)
            return None

    def read_metadata(self, key: str) -> SessionMetadataPayload | None:
        """Serialized against other processes (#152); see locked_session_files."""
        with self._session_files_lock:
            return self._read_metadata_unlocked(key)

    def _read_metadata_unlocked(self, key: str) -> SessionMetadataPayload | None:
        path = self.get_session_path(key)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    raw_data: object = json.loads(line)
                    data = _json_object(raw_data)
                    if data.get("_type") != "metadata":
                        return None
                    metadata_value = cast(object, data.get("metadata", {}))
                    key_value = cast(object, data.get("key"))
                    created_at_value = cast(object, data.get("created_at"))
                    updated_at_value = cast(object, data.get("updated_at"))
                    return {
                        "key": key_value if isinstance(key_value, str) and key_value else key,
                        "created_at": (
                            created_at_value if isinstance(created_at_value, str) else None
                        ),
                        "updated_at": (
                            updated_at_value if isinstance(updated_at_value, str) else None
                        ),
                        "metadata": (
                            cast(dict[str, Any], metadata_value)
                            if isinstance(metadata_value, dict)
                            else {}
                        ),
                    }
            return None
        except _SESSION_DATA_ERRORS as e:
            logger.warning("Failed to read session metadata {}: {}", key, e)
            repaired = self.repair(key, path=path)
            if repaired is not None:
                logger.info("Recovered read-only session metadata {} from corrupt file", key)
                return {
                    "key": repaired.key,
                    "created_at": repaired.created_at.isoformat(),
                    "updated_at": repaired.updated_at.isoformat(),
                    "metadata": repaired.metadata,
                }
            return None

    def list_sessions(self) -> list[SessionInfo]:
        """Serialized against other processes (#152); see locked_session_files."""
        with self._session_files_lock:
            return self._list_sessions_unlocked()

    def _list_sessions_unlocked(self) -> list[SessionInfo]:
        sessions: list[SessionInfo] = []

        for path in self.sessions_dir.glob("*.jsonl"):
            storage_key = self.session_key_from_path(path)
            if storage_key is None:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        raw_data: object = json.loads(first_line)
                        data = _json_object(raw_data)
                        if data.get("_type") == "metadata":
                            key_value = cast(object, data.get("key"))
                            key = (
                                key_value
                                if isinstance(key_value, str) and key_value
                                else storage_key
                            )
                            metadata = cast(object, data.get("metadata", {}))
                            title = _metadata_title(metadata)
                            preview = ""
                            fallback_preview = ""
                            scanned_records = 0
                            scanned_chars = 0
                            for line in f:
                                if not line.strip():
                                    continue
                                if _is_provider_state_record_line(line):
                                    continue
                                scanned_records += 1
                                scanned_chars += len(line)
                                if (
                                    scanned_records > _SESSION_LIST_PREVIEW_MAX_RECORDS
                                    or scanned_chars > _SESSION_LIST_PREVIEW_MAX_CHARS
                                ):
                                    break
                                raw_item: object = json.loads(line)
                                item = _json_object(raw_item)
                                if item.get("_type") in {
                                    "metadata",
                                    _PROVIDER_STATE_RECORD_TYPE,
                                }:
                                    continue
                                text = _message_preview_text(item)
                                if not text:
                                    continue
                                if item.get("role") == "user":
                                    preview = text
                                    break
                                if not fallback_preview and item.get("role") == "assistant":
                                    fallback_preview = text
                            preview = preview or fallback_preview
                            fallback_time = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
                            created_at = cast(object, data.get("created_at"))
                            updated_at = cast(object, data.get("updated_at"))
                            sessions.append(
                                {
                                    "key": key,
                                    "created_at": (
                                        created_at
                                        if isinstance(created_at, str) and created_at
                                        else fallback_time
                                    ),
                                    "updated_at": (
                                        updated_at
                                        if isinstance(updated_at, str) and updated_at
                                        else fallback_time
                                    ),
                                    "title": title,
                                    "preview": preview,
                                    "path": str(path),
                                }
                            )
            except FileNotFoundError:
                continue
            except _SESSION_DATA_ERRORS:
                repaired = self.repair(storage_key, path=path)
                if repaired is not None:
                    sessions.append(
                        {
                            "key": repaired.key,
                            "created_at": repaired.created_at.isoformat(),
                            "updated_at": repaired.updated_at.isoformat(),
                            "title": _metadata_title(repaired.metadata),
                            "preview": next(
                                (
                                    text
                                    for msg in repaired.messages
                                    if (text := _message_preview_text(msg))
                                ),
                                "",
                            ),
                            "path": str(path),
                        }
                    )
                continue
        return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)


class SessionManager:
    """Manage session identity, caching, retention, and persistence."""

    def __init__(
        self,
        workspace: Path,
        *,
        store: SessionStore | None = None,
        sessions_root: Path | None = None,
    ):
        self.workspace = workspace
        self._jsonl_store = JsonlSessionStore(workspace, sessions_root=sessions_root)
        self._store: SessionStore = store if store is not None else self._jsonl_store
        self.sessions_dir = self._jsonl_store.sessions_dir
        self.legacy_sessions_dir = self._jsonl_store.legacy_sessions_dir
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._delete_observer: Callable[[str], None] | None = None
        # Preserve identity for sessions held by active callers without retaining idle ones.
        self._overflow_cache: WeakValueDictionary[str, Session] = WeakValueDictionary()
        self._max_cached_sessions = SESSION_CACHE_MAX_SIZE
        self._file_cap_archiver: Callable[..., None] | None = None

    def _remember(self, session: Session) -> None:
        """Keep recent sessions strongly cached without duplicating live objects."""
        self._overflow_cache.pop(session.key, None)
        self._cache[session.key] = session
        self._cache.move_to_end(session.key)
        while len(self._cache) > self._max_cached_sessions:
            key, evicted = self._cache.popitem(last=False)
            self._overflow_cache[key] = evicted

    def _cached(self, key: str) -> Session | None:
        session = self._cache.get(key)
        if session is not None:
            self._cache.move_to_end(key)
            return session

        session = self._overflow_cache.get(key)
        if session is not None:
            self._remember(session)
        return session

    def get_cached(self, key: str) -> Session | None:
        """Return a cached session without creating or loading one from disk."""
        return self._cached(key)

    def set_file_cap_archiver(self, archiver: Callable[..., None]) -> None:
        """Archive unconsolidated overflow whenever a session is persisted."""
        self._file_cap_archiver = archiver

    @staticmethod
    def safe_key(key: str) -> str:
        """Public helper used by HTTP handlers to map an arbitrary key to a stable filename stem."""
        return JsonlSessionStore.safe_key(key)

    @staticmethod
    def _storage_key(key: str) -> str:
        """Collision-resistant encoding for internal session storage filenames."""
        return JsonlSessionStore.storage_key(key)

    @staticmethod
    def _decode_storage_key(stem: str) -> str | None:
        """Reverse _storage_key(): decode a base64url (no-padding) stem back to the original key."""
        return JsonlSessionStore.decode_storage_key(stem)

    @staticmethod
    def decode_storage_key(stem: str) -> str | None:
        """Public decoder for components that inspect canonical session filenames."""
        return SessionManager._decode_storage_key(stem)

    @classmethod
    def _session_key_from_path(cls, path: Path) -> str | None:
        """Decode a session key only from a canonical collision-resistant filename."""
        return JsonlSessionStore.session_key_from_path(path)

    def _get_session_path(self, key: str) -> Path:
        """Get the collision-resistant workspace path for a session."""
        return self._jsonl_store.get_session_path(key)

    def _get_legacy_lossy_path(self, key: str) -> Path:
        """Previous workspace session path using lossy ':' to '_' replacement."""
        return self._jsonl_store.get_legacy_lossy_path(key)

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanoinfra/sessions/)."""
        return self._jsonl_store.get_legacy_session_path(key)

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        session = self._cached(key)
        if session is not None:
            return session

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._remember(session)
        return session

    def _load(self, key: str) -> Session | None:
        return self._store.load(key)

    def _repair(self, key: str, *, path: Path | None = None) -> Session | None:
        """Attempt to recover a session from a corrupt JSONL file."""
        return self._jsonl_store.repair(key, path=path)

    @staticmethod
    def _session_payload(session: Session) -> SessionPayload:
        return JsonlSessionStore.session_payload(session)

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """Persist a session and retain it in the cache."""
        archiver = self._file_cap_archiver
        if archiver is not None:
            session.enforce_file_cap(
                on_archive=lambda messages: archiver(
                    messages,
                    session_key=session.key,
                )
            )

        self._store.save(session, fsync=fsync)
        self._remember(session)

    def flush_all(self) -> int:
        """Re-save every cached session with fsync for durable shutdown.

        Returns the number of sessions flushed.  Errors on individual
        sessions are logged but do not prevent other sessions from being
        flushed.
        """
        flushed = 0
        cached = dict(self._overflow_cache.items())
        cached.update(self._cache)
        for key, session in cached.items():
            try:
                self.save(session, fsync=True)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)
        self._overflow_cache.pop(key, None)

    def set_delete_observer(self, observer: Callable[[str], None]) -> None:
        """Observe explicit session deletion for process-local state cleanup.

        SessionManager owns every durable deletion entrypoint, the WebUI and the fork rollback
        paths included, so a consumer that has per-session state in memory watches this boundary
        once instead of remembering to clean up at each caller (#145, upstream 2f19068e).
        """
        self._delete_observer = observer

    def delete_session(self, key: str) -> bool:
        """Delete a persisted session and invalidate its cache entry."""
        self.invalidate(key)
        deleted = self._store.delete(key)
        if self._delete_observer is not None:
            self._delete_observer(key)
        return deleted

    def fork_session_before_user_index(
        self,
        source_key: str,
        target_key: str,
        before_user_index: int,
    ) -> Session | None:
        """Create *target_key* from *source_key* before a global user-message index.

        ``before_user_index`` is zero-based over user messages in the full session:
        ``0`` means "before the first user message", ``1`` means "before the
        second user message", and so on. A value equal to the total user-message
        count copies the full session prefix. WebUI assistant-reply forks pass
        the next user index so the selected completed assistant turn is included.
        """
        if before_user_index < 0:
            return None
        # Held across the read of the source and the write of the target (#152). Without it a
        # concurrent write to the source mid-copy yields a fork of a state that never existed,
        # which is worse than a caller waiting.
        with self._jsonl_store.locked_session_files():
            return self._fork_locked(source_key, target_key, before_user_index)

    def _fork_locked(
        self,
        source_key: str,
        target_key: str,
        before_user_index: int,
    ) -> Session | None:
        source = self._cached(source_key) or self._load(source_key)
        if source is None:
            return None

        copied: list[dict[str, Any]] = []
        user_index = 0
        found_target = False
        for message in source.messages:
            if message.get("role") == "user":
                if user_index == before_user_index:
                    found_target = True
                    break
                user_index += 1
            copied.append(public_history_message(message))
        if user_index == before_user_index:
            found_target = True
        if not found_target:
            return None

        metadata = deepcopy(source.metadata)
        for key in _FORK_VOLATILE_METADATA_KEYS:
            metadata.pop(key, None)

        last_consolidated = min(source.last_consolidated, len(copied))
        if source.last_consolidated > len(copied):
            metadata.pop("_last_summary", None)
            last_consolidated = 0

        now = datetime.now()
        target = Session(
            key=target_key,
            messages=copied,
            created_at=now,
            updated_at=now,
            metadata=metadata,
            last_consolidated=last_consolidated,
        )
        self.save(target, fsync=True)
        return target

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """Read a session without populating the cache."""
        return cast(dict[str, Any] | None, self._store.read(key))

    def read_session_metadata(self, key: str) -> dict[str, Any] | None:
        """Read session metadata without loading the transcript."""
        return cast(dict[str, Any] | None, self._store.read_metadata(key))

    def list_sessions(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._store.list_sessions())
