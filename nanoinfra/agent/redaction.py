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
   prints. The persisted copy is therefore bounded, with ``truncate_output``
   from nanoinfra/servers/execution/base.py, so one function shortens a string
   whoever asks for it.

   The bound is asked for, and it is no longer a default (#56). A caller either
   holds a budget of its own or names ``TRANSCRIPT_TOOL_RESULT_MAX_CHARS``. A
   default made this module look like the owner of the transcript budget, and
   the owner is ``AgentLoop``: it applies ``AgentDefaults.max_tool_result_chars``
   to a session record, which is four times this value.

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

**The reasoning of a turn scrubs too, and a scrubbed block loses its
signature (nanoinfraorg/nanoinfra#48).** ``reasoning_content`` and
``thinking_blocks`` persisted to ``sessions/*.jsonl`` with no scrub. A model
that plans a remote action writes the resolved command in its reasoning, and
a resolved command embeds a credential. #17 covered the transcript and the
reasoning pane, and neither one covered the session file.

#48 weighed three answers. Answer 1 persists no reasoning at all for a
workspace that holds a secret, and it costs an operator the review of a turn.
Answer 3 persists a signed copy beside a scrubbed copy, and it doubles the
place a plaintext can leak. This module implements answer 2. It scrubs the
reasoning that reaches disk. It also drops the signature of every block whose
text the scrub changed, and it marks that block.

Four reasons carry that decision, and this file states them rather than only
obeys them:

1. A provider needs a signature that matches the text of its thinking block.
   A scrubbed block plus the original signature is a mismatched pair. A turn
   that sends such a pair is worse off than a turn that sends no block.
2. A signature matters while a turn is still in flight, because the provider
   needs the prior thinking blocks to continue a tool-use turn. The live turn
   keeps its own message list, and this module copies rather than mutates. The
   persisted copy serves later turns, and a later turn needs no prior thinking
   block. So the scrub costs the stored text and never the live turn.
3. A turn that held no secret changes in no way. Its blocks keep their
   signatures, and they replay exactly as they do today.
4. A marker tells a reader months later why a block is short. The replay path
   in ``nanoinfra/session/manager.py`` reads the same marker, and it sends no
   marked block to a provider.

The release note for this change must say one thing plainly. A turn whose
reasoning held a stored secret value replays with no thinking block for that
turn.

**A runtime checkpoint scrubs on the same terms
(nanoinfraorg/nanoinfra#51).** ``AgentRunner`` emits a checkpoint on every turn
that runs a tool, and ``AgentLoop`` writes it into ``session.metadata``. That
reaches the same ``sessions/*.jsonl`` file, one line before the message
records, and no redactor sat on that second path. The payload holds the
reasoning of the assistant message, the output of a completed tool, and the
resolved arguments of a tool call the turn had not finished. So a turn the
message path scrubbed correctly persisted the same plaintext one line earlier
in the same file.

``redact_checkpoint`` reuses the message functions rather than repeating them.
One field then scrubs by one rule, and a shape #17 or #48 covers stays covered
here on the day it changes there.

**A signed tool call follows the #48 rule as well
(nanoinfraorg/nanoinfra#53).** ``_redact_tool_calls`` copied every key of a call
and then replaced the arguments, so a sibling key survived a change to the text
it describes. No provider in this tree emits a signed tool call today. Gemini's
function call carries a ``thought_signature``, and an OpenAI-compatible surface
passes an unknown key through, so the key arrives the day somebody adds that
provider or a provider adds the field. This file therefore states a rule rather
than closes a live leak.

Two things differ from #48 here, and both point the same way:

1. The drop is narrower. A named set of signature keys goes, and every other
   key stays. A rule that dropped every unknown key would break a provider that
   needs one.
2. The other half matters more. A tool call replays on the very next iteration
   of the same turn, so an unnecessary drop breaks a live conversation rather
   than an old transcript. A call the scrub did not change is identical field
   for field.

The replay path needs no change for a tool call. A marked thinking block goes
unsent, because a provider needs the block and the signature together. An
unsigned tool call is an ordinary tool call, and dropping it would orphan the
tool result that names its id.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar, cast

from loguru import logger

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS
from nanoinfra.servers.execution.base import truncate_output

#: The budget a subagent transcript applies to one tool result, and the only
#: budget this module holds (#56). Far below the 50 000-char in-flight budget
#: (``MAX_OUTPUT_CHARS``): a bounded head and tail is enough for an operator to
#: see what a command did, and the rest is mostly what makes an accidental
#: credential dump durable.
#:
#: **This is not the budget of the main transcript.** ``AgentLoop`` applies
#: ``AgentDefaults.max_tool_result_chars`` (16 000) to a session record itself,
#: through ``_bounded_tool_result``, and it therefore asks this module for no
#: bound at all. Two bounds on one string would truncate it twice.
#:
#: So one store takes this value, ``subagent_transcript.py``, and it names the
#: value at the call. Nothing takes it by default. A default here read as "the
#: persisted budget", and a reader who believed that applied 4 000 where 16 000
#: applies, or applied a second bound where one already ran.
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

#: The key a thinking block carries after this module changed its text (#48).
#: A reader months later must tell a scrubbed block from a short one. The
#: replay path reads the same key, and it drops the block.
REASONING_SCRUB_MARKER_KEY = "nanoinfra_scrubbed"

#: What that key says when a scrub removed a value from the block.
REASONING_SCRUBBED_MARKER = (
    "nanoinfra removed a stored credential value from this thinking block. The block lost its "
    "signature, because a provider needs a signature that matches the text. A later turn "
    "replays no thinking block from this record."
)

#: What that key says when no scrub ran and the text went instead.
REASONING_WITHHELD_MARKER = (
    "nanoinfra withheld the text of this thinking block. The block lost its signature, because "
    "the signature no longer matches the text."
)

#: The key a provider issues to bind a signature to one thinking text.
_THINKING_SIGNATURE_KEY = "signature"

#: The keys that bind a signature to the arguments of one tool call (#53).
#: ``thought_signature`` is the Gemini function call, and ``signature`` is the
#: name every other signed field in this repository uses.
#:
#: The set is named rather than open. A rule that dropped every unknown key
#: would break a provider that needs one, and no provider in this tree emits a
#: signed tool call today, so the rule exists for the day one does.
_TOOL_CALL_SIGNATURE_KEYS = frozenset({"signature", "thought_signature"})

#: What a tool call says after this module changed its arguments (#53). A tool
#: call replays on the next iteration of the same turn, so a reader needs to
#: tell a dropped signature from a call that never carried one.
TOOL_CALL_SCRUBBED_MARKER = (
    "nanoinfra changed the arguments of this tool call, so it dropped the signature the provider "
    "issued for them. A signature has to match the text it signed, and a mismatched pair is "
    "worse than none."
)

#: The same, for the case no scrub ran and the arguments went instead.
TOOL_CALL_WITHHELD_MARKER = (
    "nanoinfra withheld the arguments of this tool call, so it dropped the signature the provider "
    "issued for them. The signature no longer matches the arguments."
)

#: The keys of a thinking block that this module copies as they are. ``type``
#: selects the provider's own block shape, and a signature is opaque provider
#: bytes. A placeholder in either one breaks the record and protects nothing.
#: The marker is this module's own text, so it needs no scrub either.
_THINKING_BLOCK_LITERAL_KEYS = frozenset(
    {"type", _THINKING_SIGNATURE_KEY, REASONING_SCRUB_MARKER_KEY}
)

#: The key a withheld tool call keeps its marker under. Session history
#: replays to a provider, so the arguments must stay parseable JSON.
_WITHHELD_ARGUMENT_KEY = "withheld"

#: The three keys of a runtime checkpoint that hold text a turn produced (#51).
#: The rest of the payload is a phase name, an iteration number, and a model
#: id, and none of those three can carry a credential value.
_CHECKPOINT_MESSAGE_KEY = "assistant_message"
_CHECKPOINT_TOOL_RESULTS_KEY = "completed_tool_results"
_CHECKPOINT_TOOL_CALLS_KEY = "pending_tool_calls"

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

#: The same, for many texts in one round trip (nanoinfraorg/nanoinfra#54). The
#: answer holds one text per element, in the order the elements arrived.
ScrubTexts = Callable[[Sequence[tuple[str, str | None]]], list[str]]

#: What one redaction pass returns. ``in_one_batch`` runs the pass and hands
#: back its own answer, so the batch stays invisible to the caller's shape.
_T = TypeVar("_T")


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

    A call whose arguments changed loses the signature the provider issued for
    them (#53). #48 settled that rule for a thinking block, and this is the same
    rule on the field it did not cover.
    """
    if not isinstance(tool_calls, list):
        return tool_calls
    redacted: list[Any] = []
    for call in cast(list[Any], tool_calls):
        if not isinstance(call, Mapping):
            redacted.append(call)
            continue
        call_copy = dict(cast(Mapping[str, Any], call))
        changed = False
        function = call_copy.get("function")
        if isinstance(function, Mapping):
            function_copy = dict(cast(Mapping[str, Any], function))
            arguments = function_copy.get("arguments")
            if isinstance(arguments, str):
                scrubbed = scrub(arguments, None)
                changed = scrubbed != arguments
                function_copy["arguments"] = scrubbed
            call_copy["function"] = function_copy
        redacted.append(
            _unsigned_tool_call(call_copy, TOOL_CALL_SCRUBBED_MARKER) if changed else call_copy
        )
    return redacted


def _unsigned_tool_call(call: dict[str, Any], marker: str) -> dict[str, Any]:
    """Drop the signature of a tool call whose arguments changed, and say why (#53).

    The drop is narrow on purpose. Only a named signature key goes, and every
    other key stays, because a provider needs the id, the type, and the index of
    the call it replays.

    A call that carried no signature gains no marker. A marker there would state
    something that never happened.

    A provider can map its own shape onto this one with the signature beside the
    function or inside it, so both levels are checked.
    """
    dropped = False
    for key in _TOOL_CALL_SIGNATURE_KEYS:
        if call.pop(key, None) is not None:
            dropped = True
    function = call.get("function")
    if isinstance(function, dict):
        function_copy = cast("dict[str, Any]", function)
        for key in _TOOL_CALL_SIGNATURE_KEYS:
            if function_copy.pop(key, None) is not None:
                dropped = True
    if dropped:
        call[REASONING_SCRUB_MARKER_KEY] = marker
    return call


def _redact_reasoning_content(value: Any, scrub: ScrubText) -> Any:
    """Scrub the reasoning text of one turn (#48).

    The class is None, so the text scrubs value by value. A ``credential.access``
    result drops whole because that whole result IS the credential (#17).
    Reasoning is not a tool result. It is the plan of the turn, and an operator
    reads it to see what the turn did. A whole drop would cost the record that
    #48 exists to keep. So one value goes, and the words around it stay.

    An empty string stays empty. DeepSeek needs the key on a tool-call turn, and
    an empty value holds nothing to scrub.
    """
    if not isinstance(value, str) or not value:
        return value
    return scrub(value, None)


def _redact_thinking_blocks(blocks: Any, scrub: ScrubText) -> Any:
    if not isinstance(blocks, list):
        return blocks
    return [_redact_thinking_block(block, scrub) for block in cast(list[Any], blocks)]


def _redact_thinking_block(block: Any, scrub: ScrubText) -> Any:
    """Scrub one thinking block, and unsign it when the text changed (#48).

    A provider needs a signature that matches the text of the block. A scrubbed
    block plus its old signature is a mismatched pair. A turn that sends such a
    pair is worse off than a turn that sends no block. So a changed block loses
    its signature, and it says why.

    A block the scrub did not change keeps its signature and its marker-free
    shape. Such a turn replays exactly as it does today.
    """
    if not isinstance(block, Mapping):
        return block
    updated: dict[str, Any] = {}
    changed = False
    for key, value in cast(Mapping[str, Any], block).items():
        if key in _THINKING_BLOCK_LITERAL_KEYS or not isinstance(value, str) or not value:
            updated[key] = value
            continue
        scrubbed = scrub(value, None)
        changed = changed or scrubbed != value
        updated[key] = scrubbed
    if not changed:
        return updated
    return _unsigned_thinking_block(updated, REASONING_SCRUBBED_MARKER)


def _unsigned_thinking_block(block: dict[str, Any], marker: str) -> dict[str, Any]:
    """Drop the signature of a block whose text changed, and record the cause."""
    block.pop(_THINKING_SIGNATURE_KEY, None)
    block[REASONING_SCRUB_MARKER_KEY] = marker
    return block


def redact_message(
    message: Mapping[str, Any],
    scrub: ScrubText,
    *,
    capability_of: Callable[[str], str | None] | None = None,
    max_tool_result_chars: int | None = None,
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
    # The reasoning of the turn (#48). A model that plans a remote action writes
    # the resolved command here, and a resolved command embeds a credential.
    #
    # Both fields scrub value by value. A ``credential.access`` result drops
    # whole above, because the whole result IS the credential (#17). Reasoning is
    # not a tool result, and an operator reads it to review what the turn did. So
    # the value goes and the plan stays.
    if "reasoning_content" in redacted:
        redacted["reasoning_content"] = _redact_reasoning_content(
            redacted.get("reasoning_content"), scrub
        )
    if "thinking_blocks" in redacted:
        redacted["thinking_blocks"] = _redact_thinking_blocks(
            redacted.get("thinking_blocks"), scrub
        )
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
    max_tool_result_chars: int | None = None,
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


def redact_checkpoint(
    checkpoint: Mapping[str, Any],
    scrub: ScrubText,
    *,
    capability_of: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Return a persist-safe copy of one runtime checkpoint (#51).

    Three keys hold text that a turn produced. The assistant message and each
    completed tool result take ``redact_message``, and the pending tool calls
    take the same argument scrub a message record uses. So the metadata line of
    a session file and its message lines scrub by one rule.

    The bound stays off (``max_tool_result_chars=None``). A bound belongs to the
    message record, which the turn writes when it ends. A bound here would also
    shorten a checkpoint that holds no secret, and #51 requires that such a
    checkpoint keeps the bytes it has today.

    The input is never mutated. The runner holds these same dicts in the message
    list of a turn that is still running.
    """
    out = dict(checkpoint)
    message = out.get(_CHECKPOINT_MESSAGE_KEY)
    if isinstance(message, Mapping):
        out[_CHECKPOINT_MESSAGE_KEY] = redact_message(
            cast(Mapping[str, Any], message),
            scrub,
            capability_of=capability_of,
            max_tool_result_chars=None,
        )
    results = out.get(_CHECKPOINT_TOOL_RESULTS_KEY)
    if isinstance(results, list):
        out[_CHECKPOINT_TOOL_RESULTS_KEY] = [
            (
                redact_message(
                    cast(Mapping[str, Any], result),
                    scrub,
                    capability_of=capability_of,
                    max_tool_result_chars=None,
                )
                if isinstance(result, Mapping)
                else result
            )
            for result in cast(list[Any], results)
        ]
    if _CHECKPOINT_TOOL_CALLS_KEY in out:
        out[_CHECKPOINT_TOOL_CALLS_KEY] = _redact_tool_calls(
            out.get(_CHECKPOINT_TOOL_CALLS_KEY), scrub
        )
    return out


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
    if "reasoning_content" in out:
        out["reasoning_content"] = _withheld_any(out.get("reasoning_content"), marker)
    if "thinking_blocks" in out:
        out["thinking_blocks"] = _withheld_thinking_blocks(out.get("thinking_blocks"), marker)
    return out


def withheld_mapping(values: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Return a copy of one mapping with every string value withheld.

    The keys stay, so a reader still sees the shape of the record. An empty
    string stays as well, because it holds nothing.
    """
    marker = withheld_text(reason)
    return {key: _withheld_any(value, marker) for key, value in values.items()}


def withheld_checkpoint(checkpoint: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Return a copy of one runtime checkpoint with its text withheld (#51).

    The record keeps the keys the restore path reads: the phase, each tool call
    id, and each tool name. The restore path needs every one of them to close an
    interrupted turn, and none of them can hold a credential value.

    The fields this touches are the fields ``redact_checkpoint`` touches. So a
    withheld checkpoint covers what a scrubbed checkpoint would have covered.
    """
    out = dict(checkpoint)
    message = out.get(_CHECKPOINT_MESSAGE_KEY)
    if isinstance(message, Mapping):
        out[_CHECKPOINT_MESSAGE_KEY] = withheld_message(
            cast(Mapping[str, Any], message), reason
        )
    results = out.get(_CHECKPOINT_TOOL_RESULTS_KEY)
    if isinstance(results, list):
        out[_CHECKPOINT_TOOL_RESULTS_KEY] = [
            (
                withheld_message(cast(Mapping[str, Any], result), reason)
                if isinstance(result, Mapping)
                else result
            )
            for result in cast(list[Any], results)
        ]
    if _CHECKPOINT_TOOL_CALLS_KEY in out:
        out[_CHECKPOINT_TOOL_CALLS_KEY] = _withheld_tool_calls(
            out.get(_CHECKPOINT_TOOL_CALLS_KEY), withheld_text(reason)
        )
    return out


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


def _withheld_thinking_blocks(blocks: Any, marker: str) -> Any:
    if not isinstance(blocks, list):
        return blocks
    return [_withheld_thinking_block(block, marker) for block in cast(list[Any], blocks)]


def _withheld_thinking_block(block: Any, marker: str) -> Any:
    """Withhold the text of one thinking block, and unsign it (#48).

    A scrub that cannot run must persist no raw reasoning. #41 set that rule for
    the rest of the transcript, and the same answer applies here. The marker
    replaces the text, so the signature no longer matches it, and the signature
    goes as well.
    """
    if not isinstance(block, Mapping):
        return block
    updated: dict[str, Any] = {}
    withheld = False
    for key, value in cast(Mapping[str, Any], block).items():
        if key in _THINKING_BLOCK_LITERAL_KEYS or not isinstance(value, str) or not value:
            updated[key] = value
            continue
        updated[key] = marker
        withheld = True
    if not withheld:
        return updated
    return _unsigned_thinking_block(updated, REASONING_WITHHELD_MARKER)


def _withheld_tool_calls(tool_calls: Any, marker: str) -> Any:
    """Withhold serialized arguments, and keep them parseable.

    Session history replays to a provider, so a bare marker in place of the
    arguments would leave a tool call whose arguments are not JSON.

    The arguments are replaced whole here, so a signature over them is stale for
    certain, and it goes (#53). A withheld thinking block follows the same rule.
    """
    if not isinstance(tool_calls, list):
        return tool_calls
    withheld: list[Any] = []
    for call in cast(list[Any], tool_calls):
        if not isinstance(call, Mapping):
            withheld.append(call)
            continue
        call_copy = dict(cast(Mapping[str, Any], call))
        replaced = False
        function = call_copy.get("function")
        if isinstance(function, Mapping):
            function_copy = dict(cast(Mapping[str, Any], function))
            if isinstance(function_copy.get("arguments"), str):
                function_copy["arguments"] = json.dumps(
                    {_WITHHELD_ARGUMENT_KEY: marker}, ensure_ascii=False
                )
                replaced = True
            call_copy["function"] = function_copy
        withheld.append(
            _unsigned_tool_call(call_copy, TOOL_CALL_WITHHELD_MARKER) if replaced else call_copy
        )
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


class ScrubBatchError(RuntimeError):
    """A batch answered a shape its caller cannot pair, so no text may persist."""


class TranscriptRedactor:
    """What one caller needs to persist a record safely (#41).

    It holds two decisions. Whether this workspace needs the executor at all,
    and what a record holds when the executor answers nothing.

    One instance serves one persist operation. It asks the executor once per
    text, which costs one connection per text on a local socket. A workspace
    with no secret asks nothing at all, and that is the common case.

    ``in_one_batch`` pays one round trip for a whole record instead
    (nanoinfraorg/nanoinfra#54). A caller with many fields uses it, and a caller
    with a handful keeps the per-text path.
    """

    def __init__(
        self,
        scrub: ScrubText,
        *,
        asks_executor: bool = True,
        scrub_many: ScrubTexts | None = None,
    ) -> None:
        self._scrub = scrub
        self._asks_executor = asks_executor
        self._scrub_many = scrub_many

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
        that already holds a scrubber. Such a redactor gets no batch verb, so
        ``in_one_batch`` runs it once per text, which is the answer a stand-in
        expects.
        """
        if scrub is not None:
            return cls(scrub)
        if workspace is None or not workspace_may_hold_a_secret(workspace):
            return cls(_scrub_with_no_sentinels, asks_executor=False)
        from nanoinfra.gates.executor.scrub_client import default_scrub_client

        client = default_scrub_client()
        return cls(client.scrub, scrub_many=client.scrub_many)

    @property
    def asks_the_executor(self) -> bool:
        """Whether this redactor sends a text over a socket at all."""
        return self._asks_executor

    def in_one_batch(self, run: Callable[[ScrubText], _T]) -> _T:
        """Run *run* and pay one round trip for every text it asks about (#54).

        *run* is any redaction that takes a ``ScrubText``. It runs twice. The
        first pass collects the texts and changes nothing. One batch then
        answers them all. The second pass runs on the same input again and
        returns the answers in place.

        **The requirement on *run* is that it be pure**, and that it ask for the
        same texts in the same order for one input. Every redaction function in
        this module satisfies that, and so does a provider's own field walk. The
        rule is not merely trusted: the second pass checks that the text it is
        handed at each position is the text the first pass recorded there, and it
        raises when it is not. So a *run* that branches on the answer of a scrub
        fails loudly rather than pairing the wrong answer with the wrong field.

        A redactor with no batch verb runs *run* once, with the per-text scrub.
        A workspace with no secret therefore pays nothing, and a test that
        injected its own scrubber keeps the behaviour it asked for.

        A failure raises rather than answering markers. The caller decides what a
        record holds when no scrub ran, and for a provider state that answer is
        no record at all rather than a marker.
        """
        if self._scrub_many is None:
            return run(self._scrub)

        asked: list[tuple[str, str | None]] = []

        def _collect(text: str, capability_class: str | None) -> str:
            asked.append((text, capability_class))
            return text

        run(_collect)
        answers = self._scrub_many(asked)
        if len(answers) != len(asked):
            raise ScrubBatchError(
                f"The scrubber answered {len(answers)} texts for {len(asked)} asked."
            )

        position = 0

        def _fill(text: str, capability_class: str | None) -> str:
            nonlocal position
            if position >= len(asked):
                raise ScrubBatchError(
                    "The second pass asked for more texts than the first pass collected."
                )
            if asked[position] != (text, capability_class):
                raise ScrubBatchError(
                    f"The two passes disagree at position {position}, so no answer can be paired."
                )
            answer = answers[position]
            position += 1
            return answer

        filled = run(_fill)
        if position != len(asked):
            raise ScrubBatchError(
                f"The second pass used {position} of {len(asked)} answers, so a field went unscrubbed."
            )
        return filled

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
        max_tool_result_chars: int | None = None,
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
        max_tool_result_chars: int | None = None,
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

    def checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        *,
        capability_of: Callable[[str], str | None] | None = None,
    ) -> dict[str, Any]:
        """Scrub one runtime checkpoint, or withhold every text it holds (#51).

        One failure withholds the whole payload, which is the rule ``messages``
        follows. A scrubber that answered nothing for one field will answer
        nothing for the next, so a partial result would put a scrubbed field
        beside an unscrubbed one inside one record.
        """
        try:
            return redact_checkpoint(
                checkpoint, self._scrub, capability_of=capability_of
            )
        except Exception as exc:  # noqa: BLE001 -- no scrub means no raw text persists
            return withheld_checkpoint(checkpoint, self._reason(exc))

    @staticmethod
    def _reason(exc: BaseException) -> str:
        """The sentence a marker carries, and one log line for the operator."""
        reason = str(exc) or type(exc).__name__
        logger.warning("Withheld transcript text, because no scrub ran: {}", reason)
        return reason


__all__ = [
    "CREDENTIAL_ACCESS",
    "MIN_REDACTABLE_SECRET_CHARS",
    "REASONING_SCRUBBED_MARKER",
    "REASONING_SCRUB_MARKER_KEY",
    "REASONING_WITHHELD_MARKER",
    "SCRUB_UNAVAILABLE_MARKER",
    "TOOL_CALL_SCRUBBED_MARKER",
    "TOOL_CALL_WITHHELD_MARKER",
    "TRANSCRIPT_TOOL_RESULT_MAX_CHARS",
    "ScrubBatchError",
    "ScrubText",
    "ScrubTexts",
    "SecretSentinel",
    "TranscriptRedactor",
    "redact_checkpoint",
    "redact_mapping",
    "redact_message",
    "redact_messages",
    "redact_text",
    "scrub_one_text",
    "usable_sentinels",
    "withheld_checkpoint",
    "withheld_mapping",
    "withheld_message",
    "withheld_text",
    "workspace_may_hold_a_secret",
]
