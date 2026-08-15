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
"""

from __future__ import annotations

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

# Characters a name may not contribute to a placeholder. A name that carries
# a bracket or a newline could otherwise fake a second placeholder, or split
# one record into two lines of a JSONL transcript.
_NAME_FORBIDDEN = str.maketrans({"[": "", "]": "", "\n": " ", "\r": " ", "\t": " "})


@dataclass(frozen=True)
class SecretSentinel:
    """One decrypted secret value, plus the name that replaces it."""

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


def workspace_secret_sentinels(workspace: Path | str) -> list[SecretSentinel]:
    """Return the secrets this workspace can decrypt, as sentinels.

    The SecretStore import stays local. Persistence modules must not pull the
    crypto and Postgres import graph in just to be importable.

    Every failure returns an empty list. Redaction is best-effort, so a
    broken or unconfigured secret store must never cost the caller its
    transcript. An unset ``NANOINFRA_SECRETS_KEY`` also means no turn could
    have resolved a secret, so there is nothing to scrub.
    """
    try:
        from nanoinfra.secrets import crypto
        from nanoinfra.secrets.store import SecretStore

        if not crypto.is_configured():
            return []
        store = SecretStore(Path(workspace))
        sentinels: list[SecretSentinel] = []
        for secret in store.list_secrets():
            try:
                value = store.resolve_plaintext(secret.id)
            except Exception:  # noqa: BLE001 -- one bad secret must not stop the rest
                logger.warning("Could not decrypt secret {} for redaction", secret.id)
                continue
            if value:
                sentinels.append(SecretSentinel(name=secret.name, value=value))
        return usable_sentinels(sentinels)
    except Exception:  # noqa: BLE001 -- redaction never breaks persistence
        logger.warning("Secret lookup for redaction failed. Text persists unredacted.")
        return []


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


def redact_mapping(
    values: Mapping[str, Any], sentinels: Sequence[SecretSentinel]
) -> dict[str, Any]:
    """Scrub the string values of a flat metadata mapping.

    Transcript metadata carries a subagent's error string, and a failure
    message can quote the credential that failed. This also walks nested
    containers, so the mapping stays safe when a caller adds one.
    """
    return {key: _redact_any(value, sentinels) for key, value in values.items()}


def _redact_any(value: Any, sentinels: Sequence[SecretSentinel]) -> Any:
    if isinstance(value, str):
        return redact_text(value, sentinels)
    if isinstance(value, Mapping):
        return redact_mapping(cast(Mapping[str, Any], value), sentinels)
    if isinstance(value, list):
        return [_redact_any(item, sentinels) for item in cast(list[Any], value)]
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
    sentinels: Sequence[SecretSentinel],
    *,
    max_chars: int | None,
) -> Any:
    """Scrub, then bound, a message content field.

    Scrub first. ``truncate_output`` keeps a head and a tail, so a bound
    applied first could cut through a secret and leave both halves.
    """
    if isinstance(content, str):
        scrubbed = redact_text(content, sentinels)
        return truncate_output(scrubbed, max_chars) if max_chars else scrubbed
    if isinstance(content, list):
        return [
            _redact_block(block, sentinels, max_chars=max_chars)
            for block in cast(list[Any], content)
        ]
    return content


def _redact_block(
    block: Any, sentinels: Sequence[SecretSentinel], *, max_chars: int | None
) -> Any:
    if not isinstance(block, Mapping):
        return block
    updated = dict(cast(Mapping[str, Any], block))
    text = updated.get("text")
    if isinstance(text, str):
        updated["text"] = _redact_content(text, sentinels, max_chars=max_chars)
    return updated


def _redact_tool_calls(
    tool_calls: Any, sentinels: Sequence[SecretSentinel]
) -> Any:
    """Scrub serialized tool arguments.

    ``sensitive_params`` masks arguments by NAME. A credential can still ride
    inside a value the tool never declared sensitive, such as the resolved
    command in ``mysql -p<password>``.
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
                function_copy["arguments"] = redact_text(arguments, sentinels)
            call_copy["function"] = function_copy
        redacted.append(call_copy)
    return redacted


def redact_message(
    message: Mapping[str, Any],
    sentinels: Sequence[SecretSentinel],
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
    """
    redacted = dict(message)
    role = redacted.get("role")
    tool_name = redacted.get("name")
    is_tool_result = role == "tool"

    if is_tool_result and capability_of is not None and isinstance(tool_name, str):
        if capability_of(tool_name) == CREDENTIAL_ACCESS:
            redacted["content"] = _credential_reference(
                redacted.get("content"), sentinels
            )
            return redacted

    if "content" in redacted:
        redacted["content"] = _redact_content(
            redacted.get("content"),
            sentinels,
            # Only tool output is bounded. An answer or a user message is
            # authored content, and a bound there loses the record itself.
            max_chars=max_tool_result_chars if is_tool_result else None,
        )
    if "tool_calls" in redacted:
        redacted["tool_calls"] = _redact_tool_calls(redacted.get("tool_calls"), sentinels)
    return redacted


def _credential_reference(content: Any, sentinels: Sequence[SecretSentinel]) -> str:
    """Replace a whole credential.access result with a name-only reference."""
    text = content if isinstance(content, str) else str(content)
    names = _matched_secret_names(text, sentinels)
    return _CREDENTIAL_RESULT_PLACEHOLDER.format(
        name=", ".join(names) if names else _UNKNOWN_SECRET_NAME
    )


def redact_messages(
    messages: Iterable[Mapping[str, Any]],
    sentinels: Sequence[SecretSentinel],
    *,
    capability_of: Callable[[str], str | None] | None = None,
    max_tool_result_chars: int | None = TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Return persist-safe copies of *messages*."""
    return [
        redact_message(
            message,
            sentinels,
            capability_of=capability_of,
            max_tool_result_chars=max_tool_result_chars,
        )
        for message in messages
    ]


__all__ = [
    "CREDENTIAL_ACCESS",
    "MIN_REDACTABLE_SECRET_CHARS",
    "TRANSCRIPT_TOOL_RESULT_MAX_CHARS",
    "SecretSentinel",
    "redact_mapping",
    "redact_message",
    "redact_messages",
    "redact_text",
    "usable_sentinels",
    "workspace_secret_sentinels",
]
