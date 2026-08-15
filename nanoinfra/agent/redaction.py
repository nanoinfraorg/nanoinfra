"""Best-effort redaction of credential material at the persistence boundary.

Redaction here is BEST-EFFORT, and the docs must not claim more than that.
It removes the exact secret values this workspace can decrypt, and it drops
whole results that come from a ``credential.access`` tool. It cannot find a
credential the process never stored: an ad-hoc password a user typed into
chat, a token a remote command re-encoded (base64, gzip, JSON escapes, a
value split across two lines), or a value the operator keeps outside the
Secrets module. Treat a transcript as sensitive material even after
redaction.

Two gaps drove this module (nanoinfraorg/nanoinfra#17):

1. ``Tool.sensitive_params`` masks tool ARGUMENTS before they persist.
   Tool RESULTS were never scrubbed, so a resolved credential could land in
   a durable file.
2. Remote command output is the widest uncontrolled route for a credential
   into a transcript, because the agent does not choose what the remote host
   prints. The persisted copy is therefore bounded by default with
   ``truncate_output`` from nanoinfra/servers/execution/base.py, so the bound
   lives in exactly one place.

The placeholder keeps the secret NAME. An operator must still be able to
tell which secret a turn used, and a bare ``[redacted]`` would destroy that.
The name is data, not format: ``_placeholder_name`` strips the characters
that would let an operator-chosen name forge extra placeholder text.

**The executor scrubs, and this module sends text (#41).** This file used to
build the sentinels itself, which decrypted every secret of the workspace
inside the agent process on every turn that persisted. #18 had already moved
the credential store behind the executor, so the sentinels moved there too
(``nanoinfra/gates/executor/scrub.py``). What stays here is the structure:
which fields a transcript scrubs, which result drops whole, and where the
bound applies. ``TranscriptRedactor`` holds the two decisions a caller needs.

**A scrub that cannot run withholds the text.** The old code returned an
empty sentinel list for every failure, and the caller then persisted the text
unscrubbed. That is fail open on the one path #17 exists to close. With no
scrubber reachable the record keeps its shape, and every text it holds
becomes a marker that names the cause.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, cast

from loguru import logger

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS
from nanoinfra.servers.execution.base import truncate_output

#: Persisted budget for one tool result. Far below the 50 000-char in-flight
#: budget (``MAX_OUTPUT_CHARS``): a bounded head and tail is enough for an
#: operator to see what a command did, and the rest is mostly what makes an
#: accidental credential dump durable.
TRANSCRIPT_TOOL_RESULT_MAX_CHARS = 4_000

#: Shortest value usable as a sentinel. A four-character secret matches
#: ordinary words, so a scrub of it corrupts the transcript and protects
#: nothing. Substring search cannot protect a value that short.
MIN_REDACTABLE_SECRET_CHARS = 8

#: Cap for the name inside a placeholder. Names come from the operator.
_MAX_PLACEHOLDER_NAME_CHARS = 64

_UNKNOWN_SECRET_NAME = "unknown"

_SECRET_PLACEHOLDER = "[redacted secret: {name}]"
_CREDENTIAL_RESULT_PLACEHOLDER = "[redacted credential.access result: secret={name}]"

#: What a transcript holds in place of a text nobody scrubbed. An operator
#: reads this line months after the turn, so it says what happened, why the
#: text is gone, and what to do about it.
SCRUB_UNAVAILABLE_MARKER = (
    "[nanoinfra withheld this text. No executor scrubbed it, and unscrubbed text can hold a "
    "credential value. Start the executor to restore the scrub. Reason: {reason}]"
)

#: Cap for the reason inside a marker. The reason quotes a socket path and an
#: errno, and a transcript record must stay one readable line.
_MAX_MARKER_REASON_CHARS = 240

#: The key a withheld tool call keeps its marker under. Session history
#: replays to a provider, so the arguments must stay parseable JSON.
_WITHHELD_ARGUMENT_KEY = "withheld"

# Characters a name may not contribute to a placeholder. A name that carries
# a bracket or a newline could otherwise fake a second placeholder, or split
# one record into two lines of a JSONL transcript.
_NAME_FORBIDDEN = str.maketrans({"[": "", "]": "", "\n": " ", "\r": " ", "\t": " "})

# Where the local secrets of a workspace live, and the environment variable
# that names a shared backend. Both belong to nanoinfra/secrets/store.py, and
# they are repeated here because the agent must not import that module (#41).
# ``tests/agent/test_transcript_redactor.py`` pins both against the store, so
# a layout that moves fails a test rather than silently disabling the scrub.
_SECRETS_DIR_NAME = "secrets"
_SECRETS_RECORD_GLOB = "*.json"
_SECRETS_POSTGRES_DSN_ENV = "NANOINFRA_SECRETS_POSTGRES_DSN"

#: What one text costs to scrub: the text, plus the capability class of the
#: tool that produced it, or None when the caller knows none.
ScrubText = Callable[[str, str | None], str]


@dataclass(frozen=True)
class SecretSentinel:
    """One decrypted secret value, plus the name that replaces it.

    Only the executor builds these (#41). The agent process holds none.
    """

    name: str
    value: str


def _placeholder_name(name: str) -> str:
    cleaned = name.translate(_NAME_FORBIDDEN).strip()
    return cleaned[:_MAX_PLACEHOLDER_NAME_CHARS] or _UNKNOWN_SECRET_NAME


def _value_variants(value: str) -> list[str]:
    """The line-ending forms of one value that a transcript may hold.

    A stored SSH key keeps the line endings it arrived with. The remote host
    or the JSON round-trip can normalize them, so match both forms.
    """
    variants = [value]
    for candidate in (value.replace("\r\n", "\n"), value.replace("\n", "\r\n")):
        if candidate not in variants:
            variants.append(candidate)
    return variants


def usable_sentinels(sentinels: Iterable[SecretSentinel]) -> list[SecretSentinel]:
    """Drop values too short to match safely, longest value first.

    Longest first matters: one secret can contain another (a password reused
    inside a connection string). A short sentinel that runs first would leave
    the rest of the longer value in place.
    """
    usable = [s for s in sentinels if len(s.value) >= MIN_REDACTABLE_SECRET_CHARS]
    return sorted(usable, key=lambda s: len(s.value), reverse=True)


def workspace_may_hold_a_secret(workspace: Path | str) -> bool:
    """Report whether a stored secret could exist for this workspace.

    This is the guard that keeps the common case cheap (#41). A workspace with
    no secret needs no round trip, because no turn in it could have resolved
    a value that a scrub would remove.

    The check reads no secret and decrypts nothing. It counts records and it
    asks whether a shared backend is configured. An unreadable directory
    answers yes, because an unknown state must cost a round trip rather than
    a silent skip.
    """
    if os.environ.get(_SECRETS_POSTGRES_DSN_ENV, "").strip():
        return True
    root = Path(workspace) / _SECRETS_DIR_NAME
    try:
        return next(root.glob(_SECRETS_RECORD_GLOB), None) is not None
    except OSError:
        return True


def redact_text(text: str, sentinels: Sequence[SecretSentinel]) -> str:
    """Replace every known secret value in *text* with a named placeholder."""
    if not text or not sentinels:
        return text
    for sentinel in usable_sentinels(sentinels):
        placeholder = _SECRET_PLACEHOLDER.format(name=_placeholder_name(sentinel.name))
        for variant in _value_variants(sentinel.value):
            if variant in text:
                text = text.replace(variant, placeholder)
    return text


def scrub_one_text(
    text: str, capability_class: str | None, sentinels: Sequence[SecretSentinel]
) -> str:
    """Scrub one text against *sentinels*. This is the unit the wire carries (#41).

    The executor calls this with its own sentinels. The class decides which of
    the two placeholders the answer holds: a ``credential.access`` result is
    credential material by definition, so it drops whole rather than value by
    value (#17).
    """
    if capability_class == CREDENTIAL_ACCESS:
        return _credential_reference(text, sentinels)
    return redact_text(text, sentinels)


def withheld_text(reason: str) -> str:
    """The marker a transcript holds in place of a text nobody scrubbed."""
    return SCRUB_UNAVAILABLE_MARKER.format(reason=_marker_reason(reason))


def _marker_reason(reason: str) -> str:
    """One bounded line, so a marker cannot break a JSONL record or forge a placeholder."""
    cleaned = " ".join(reason.translate(_NAME_FORBIDDEN).split()).strip()
    return cleaned[:_MAX_MARKER_REASON_CHARS] or "no reason given"


def redact_mapping(values: Mapping[str, Any], scrub: ScrubText) -> dict[str, Any]:
    """Scrub the string values of a flat metadata mapping.

    Transcript metadata carries a subagent's error string, and a failure
    message can quote the credential that failed. This also walks nested
    containers, so the mapping stays safe when a caller adds one.
    """
    return {key: _redact_any(value, scrub) for key, value in values.items()}


def _redact_any(value: Any, scrub: ScrubText) -> Any:
    if isinstance(value, str):
        return scrub(value, None)
    if isinstance(value, Mapping):
        return redact_mapping(cast(Mapping[str, Any], value), scrub)
    if isinstance(value, list):
        return [_redact_any(item, scrub) for item in cast(list[Any], value)]
    return value


def _matched_secret_names(text: str, sentinels: Sequence[SecretSentinel]) -> list[str]:
    """Names whose value appears in *text*, so a dropped result stays traceable."""
    names: list[str] = []
    for sentinel in usable_sentinels(sentinels):
        if any(variant in text for variant in _value_variants(sentinel.value)):
            name = _placeholder_name(sentinel.name)
            if name not in names:
                names.append(name)
    return names


def _redact_content(
    content: Any,
    scrub: ScrubText,
    *,
    capability_class: str | None,
    max_chars: int | None,
) -> Any:
    """Scrub, then bound, a message content field.

    Scrub first. ``truncate_output`` keeps a head and a tail, so a bound
    applied first could cut through a secret and leave both halves.
    """
    if isinstance(content, str):
        scrubbed = scrub(content, capability_class)
        return truncate_output(scrubbed, max_chars) if max_chars else scrubbed
    if isinstance(content, list):
        return [
            _redact_block(
                block, scrub, capability_class=capability_class, max_chars=max_chars
            )
            for block in cast(list[Any], content)
        ]
    return content


def _redact_block(
    block: Any,
    scrub: ScrubText,
    *,
    capability_class: str | None,
    max_chars: int | None,
) -> Any:
    if not isinstance(block, Mapping):
        return block
    updated = dict(cast(Mapping[str, Any], block))
    text = updated.get("text")
    if isinstance(text, str):
        updated["text"] = _redact_content(
            text, scrub, capability_class=capability_class, max_chars=max_chars
        )
    return updated


def _redact_tool_calls(tool_calls: Any, scrub: ScrubText) -> Any:
    """Scrub serialized tool arguments.

    ``sensitive_params`` masks arguments by NAME. A credential can still ride
    inside a value the tool never declared sensitive, such as the resolved
    command in ``mysql -p<password>``.

    The arguments get a value-by-value scrub whatever the tool's class is. A
    ``credential.access`` tool takes a secret id as an argument and returns
    the value, so the argument is not the credential and must stay readable.
    """
    if not isinstance(tool_calls, list):
        return tool_calls
    redacted: list[Any] = []
    for call in cast(list[Any], tool_calls):
        if not isinstance(call, Mapping):
            redacted.append(call)
            continue
        call_copy = dict(cast(Mapping[str, Any], call))
        function = call_copy.get("function")
        if isinstance(function, Mapping):
            function_copy = dict(cast(Mapping[str, Any], function))
            arguments = function_copy.get("arguments")
            if isinstance(arguments, str):
                function_copy["arguments"] = scrub(arguments, None)
            call_copy["function"] = function_copy
        redacted.append(call_copy)
    return redacted


def redact_message(
    message: Mapping[str, Any],
    scrub: ScrubText,
    *,
    capability_of: Callable[[str], str | None] | None = None,
    max_tool_result_chars: int | None = TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
) -> dict[str, Any]:
    """Return a persist-safe copy of one message.

    The input is never mutated. The live turn keeps the real values, because
    the model still needs them to finish its work.

    *capability_of* maps a tool name to its capability class. A caller that
    holds the tool registry should pass one, so a ``credential.access``
    result is dropped whole rather than scrubbed value by value. Without it
    this function still scrubs known values.

    The class travels with each text (#41). This side decides the shape of
    the record, and the scrubber decides the text, so one class is read in
    one place.
    """
    redacted = dict(message)
    role = redacted.get("role")
    tool_name = redacted.get("name")
    is_tool_result = role == "tool"
    capability_class: str | None = None
    if is_tool_result and capability_of is not None and isinstance(tool_name, str):
        capability_class = capability_of(tool_name)

    if capability_class == CREDENTIAL_ACCESS:
        content = redacted.get("content")
        text = content if isinstance(content, str) else str(content)
        redacted["content"] = scrub(text, CREDENTIAL_ACCESS)
        return redacted

    if "content" in redacted:
        redacted["content"] = _redact_content(
            redacted.get("content"),
            scrub,
            capability_class=capability_class,
            # Only tool output is bounded. An answer or a user message is
            # authored content, and a bound there loses the record itself.
            max_chars=max_tool_result_chars if is_tool_result else None,
        )
    if "tool_calls" in redacted:
        redacted["tool_calls"] = _redact_tool_calls(redacted.get("tool_calls"), scrub)
    return redacted


def _credential_reference(text: str, sentinels: Sequence[SecretSentinel]) -> str:
    """Replace a whole credential.access result with a name-only reference."""
    names = _matched_secret_names(text, sentinels)
    return _CREDENTIAL_RESULT_PLACEHOLDER.format(
        name=", ".join(names) if names else _UNKNOWN_SECRET_NAME
    )


def redact_messages(
    messages: Iterable[Mapping[str, Any]],
    scrub: ScrubText,
    *,
    capability_of: Callable[[str], str | None] | None = None,
    max_tool_result_chars: int | None = TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Return persist-safe copies of *messages*."""
    return [
        redact_message(
            message,
            scrub,
            capability_of=capability_of,
            max_tool_result_chars=max_tool_result_chars,
        )
        for message in messages
    ]


# -- what a record holds when nobody scrubbed it ----------------------------


def withheld_message(message: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Return a copy of one message with its text withheld.

    The record keeps its shape. ``role``, ``name``, ``tool_call_id``, and each
    tool call id survive, because persistence and provider replay both read
    them, and none of them can hold a credential value.

    The fields this touches are exactly the fields the scrub touches. So a
    withheld record covers what a scrubbed record would have covered.
    """
    marker = withheld_text(reason)
    out = dict(message)
    if "content" in out:
        out["content"] = _withheld_content(out.get("content"), marker)
    if "tool_calls" in out:
        out["tool_calls"] = _withheld_tool_calls(out.get("tool_calls"), marker)
    return out


def withheld_mapping(values: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Return a copy of one mapping with every string value withheld.

    The keys stay, so a reader still sees the shape of the record. An empty
    string stays as well, because it holds nothing.
    """
    marker = withheld_text(reason)
    return {key: _withheld_any(value, marker) for key, value in values.items()}


def _withheld_any(value: Any, marker: str) -> Any:
    if isinstance(value, str):
        return marker if value else value
    if isinstance(value, Mapping):
        return {
            key: _withheld_any(item, marker)
            for key, item in cast(Mapping[str, Any], value).items()
        }
    if isinstance(value, list):
        return [_withheld_any(item, marker) for item in cast(list[Any], value)]
    return value


def _withheld_content(content: Any, marker: str) -> Any:
    """Withhold a content field, and keep a block list a block list.

    An image block survives with its data. Only the text of a block goes, the
    same as on the scrub path, so a withheld turn still renders its media.
    """
    if isinstance(content, str):
        return marker if content else content
    if isinstance(content, list):
        return [_withheld_block(block, marker) for block in cast(list[Any], content)]
    return content


def _withheld_block(block: Any, marker: str) -> Any:
    if not isinstance(block, Mapping):
        return block
    updated = dict(cast(Mapping[str, Any], block))
    text = updated.get("text")
    if isinstance(text, str) and text:
        updated["text"] = marker
    return updated


def _withheld_tool_calls(tool_calls: Any, marker: str) -> Any:
    """Withhold serialized arguments, and keep them parseable.

    Session history replays to a provider, so a bare marker in place of the
    arguments would leave a tool call whose arguments are not JSON.
    """
    if not isinstance(tool_calls, list):
        return tool_calls
    withheld: list[Any] = []
    for call in cast(list[Any], tool_calls):
        if not isinstance(call, Mapping):
            withheld.append(call)
            continue
        call_copy = dict(cast(Mapping[str, Any], call))
        function = call_copy.get("function")
        if isinstance(function, Mapping):
            function_copy = dict(cast(Mapping[str, Any], function))
            if isinstance(function_copy.get("arguments"), str):
                function_copy["arguments"] = json.dumps(
                    {_WITHHELD_ARGUMENT_KEY: marker}, ensure_ascii=False
                )
            call_copy["function"] = function_copy
        withheld.append(call_copy)
    return withheld


# -- the agent's redaction path ---------------------------------------------


def _scrub_with_no_sentinels(text: str, capability_class: str | None) -> str:
    """The scrub for a workspace that holds no secret.

    No sentinel can match, so no value changes. A ``credential.access`` result
    still drops whole, because such a result is credential material by its
    class rather than by a match (#17). The bound on a persisted tool result
    applies as well, and neither answer needs a round trip.
    """
    return scrub_one_text(text, capability_class, ())


class TranscriptRedactor:
    """What one caller needs to persist a record safely (#41).

    It holds two decisions. Whether this workspace needs the executor at all,
    and what a record holds when the executor answers nothing.

    One instance serves one persist operation. It asks the executor once per
    text, which costs one connection per text on a local socket. A workspace
    with no secret asks nothing at all, and that is the common case.
    """

    def __init__(self, scrub: ScrubText, *, asks_executor: bool = True) -> None:
        self._scrub = scrub
        self._asks_executor = asks_executor

    @classmethod
    def for_workspace(
        cls, workspace: Path | str | None, *, scrub: ScrubText | None = None
    ) -> TranscriptRedactor:
        """Build the redactor for one workspace.

        A workspace with no stored secret scrubs locally against no sentinel,
        so it performs no round trip and it withholds nothing.

        ``workspace=None`` does the same. Such a caller sits outside a
        workspace scope, so nothing could resolve a sentinel for it. That is a
        stated limit, and every caller that holds a workspace passes it.

        *scrub* replaces the socket client. Tests use it, and so does a caller
        that already holds a scrubber.
        """
        if scrub is not None:
            return cls(scrub)
        if workspace is None or not workspace_may_hold_a_secret(workspace):
            return cls(_scrub_with_no_sentinels, asks_executor=False)
        from nanoinfra.gates.executor.scrub_client import default_scrub_client

        return cls(default_scrub_client().scrub)

    @property
    def asks_the_executor(self) -> bool:
        """Whether this redactor sends a text over a socket at all."""
        return self._asks_executor

    def text(self, value: str) -> str:
        """Scrub one text, or return the marker."""
        try:
            return self._scrub(value, None)
        except Exception as exc:  # noqa: BLE001 -- no scrub means no raw text persists
            return withheld_text(self._reason(exc))

    def mapping(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Scrub the strings of one mapping, or withhold every one of them."""
        try:
            return redact_mapping(values, self._scrub)
        except Exception as exc:  # noqa: BLE001 -- no scrub means no raw text persists
            return withheld_mapping(values, self._reason(exc))

    def message(
        self,
        message: Mapping[str, Any],
        *,
        capability_of: Callable[[str], str | None] | None = None,
        max_tool_result_chars: int | None = TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
    ) -> dict[str, Any]:
        """Scrub one message, or withhold its text."""
        try:
            return redact_message(
                message,
                self._scrub,
                capability_of=capability_of,
                max_tool_result_chars=max_tool_result_chars,
            )
        except Exception as exc:  # noqa: BLE001 -- no scrub means no raw text persists
            return withheld_message(message, self._reason(exc))

    def messages(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        capability_of: Callable[[str], str | None] | None = None,
        max_tool_result_chars: int | None = TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
    ) -> list[dict[str, Any]]:
        """Scrub a list of messages, or withhold the text of every one of them.

        One failure withholds the whole list. A scrubber that answered nothing
        for the first text will answer nothing for the rest, so a partial
        result would mix a scrubbed record with an unscrubbed one.
        """
        listed = list(messages)
        try:
            return redact_messages(
                listed,
                self._scrub,
                capability_of=capability_of,
                max_tool_result_chars=max_tool_result_chars,
            )
        except Exception as exc:  # noqa: BLE001 -- no scrub means no raw text persists
            reason = self._reason(exc)
            return [withheld_message(message, reason) for message in listed]

    @staticmethod
    def _reason(exc: BaseException) -> str:
        """The sentence a marker carries, and one log line for the operator."""
        reason = str(exc) or type(exc).__name__
        logger.warning("Withheld transcript text, because no scrub ran: {}", reason)
        return reason


__all__ = [
    "CREDENTIAL_ACCESS",
    "MIN_REDACTABLE_SECRET_CHARS",
    "SCRUB_UNAVAILABLE_MARKER",
    "TRANSCRIPT_TOOL_RESULT_MAX_CHARS",
    "ScrubText",
    "SecretSentinel",
    "TranscriptRedactor",
    "redact_mapping",
    "redact_message",
    "redact_messages",
    "redact_text",
    "scrub_one_text",
    "usable_sentinels",
    "withheld_mapping",
    "withheld_message",
    "withheld_text",
    "workspace_may_hold_a_secret",
]
