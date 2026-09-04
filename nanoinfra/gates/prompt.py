"""Render the approval prompt from resolver output -- nanoinfraorg/nanoinfra#14.

A human approves two facts: the command that runs, and the hosts it runs on. Both facts come
from the #4 resolver. Neither fact comes from the model.

A model-written summary inside the security path is the unfaithful-summarization problem. The
human authorizes a sentence, the executor runs a command, and nothing compares the two. So
this module accepts no description, no summary, and no reason. The signature makes that text
unrepresentable, instead of a rule somebody has to remember: every input is either the resolved
command string, a ``ScopeResolution`` from the resolver, or a tuple of resolved host names.

A group renders as every resolved name plus a count. ``group: webservers`` renders as fourteen
names. It never renders as ``webservers``, and never as "the web tier". A count with no names
hides the one host an operator would have refused.

The rendered text is the thing the digest covers. Every variable byte is the command or a
resolved host name. Every other byte is fixed, or derives from those two, such as the count and
the line numbers. ``compute_target_digest`` (#12) binds the command and the host set, so
``digest_rendered_prompt`` re-derives the same value from the bytes alone. An approval therefore
covers what the human read, and no field beside the payload has to be trusted.

``scope`` and ``pattern`` stay beside the payload and out of the rendered text on purpose. The
digest binds the command and the host set only. A pattern name inside the text would be a byte
the digest does not cover, and "the digest covers these bytes" has to stay true with no
exception. A caller that shows provenance (#13, #16, #27) reads those fields the same way it
reads the session id, which the digest does not cover either.

The module already sits on the executor side of the #18 boundary. It is a pure function: no
import of nanoinfra.agent, no read of request context, no read of config, no clock, and no
transport. #18 moves the file into the executor process and changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from nanoinfra.gates.tokens import compute_target_digest
from nanoinfra.servers.scope import ScopeResolution

# The format version. A change of any fixed byte below changes the payload a human reads, so
# the version names which renderer produced a given text. #18 pins it across the process split.
PROMPT_VERSION = 1

_HEADER = f"nanoinfra approval request v{PROMPT_VERSION}"

# The provenance line is a constant, and it names the executor rather than the agent. A line
# built from a variable would be a byte the digest cannot bind.
_PROVENANCE = "The executor resolved this request. No part of it comes from the agent."

_COMMAND_HEADER = "Command, exactly as the executor will run it:"
_HOSTS_HEADER = "Hosts: "
_DIGEST_HEADER = "Binding digest: "

# Every command line carries this prefix. A command that imitates a host line or a header then
# stays inside the command block, and one strip recovers it exactly.
_COMMAND_PREFIX = "  | "

_HOST_INDENT = "  "

# One rendered host line: the indent, a right-aligned index, ". ", then the whole name. The
# name runs to the end of the line, because a host name may hold a dot and a digit.
_HOST_LINE_RE = re.compile(r"^ {2,}(\d+)\. (.+)$")

# Unicode line separators, written as escapes so a reader of this file can see them. Some
# readers break a line on these and some do not, so a name that holds one displays
# differently to two people who approve the same bytes.
_LINE_SEPARATORS = ("\u2028", "\u2029")


class PromptRenderError(Exception):
    """The renderer cannot build the payload, or the text is not one it produced.

    Never substitute a partial payload for this error. A payload that drops a host, or a text
    nobody rendered, would buy an approval for something the human never read.
    """


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    """The bytes a human approves, plus the resolver facts that produced them.

    ``text`` is the payload, and ``target_digest`` binds it. ``command`` and ``hosts`` are the
    fields that digest covers, after the sort and the deduplication the digest applies.

    ``scope`` and ``pattern`` are provenance, and the digest does not cover them. They read
    ``None`` when the caller passed hosts directly, because no resolver named a pattern then.
    """

    command: str
    hosts: tuple[str, ...]
    text: str
    target_digest: str
    scope: str | None = None
    pattern: str | None = None

    @property
    def host_count(self) -> int:
        """How many hosts the payload names. Always the number of rendered host lines."""
        return len(self.hosts)


def render_approval_prompt(*, command: str, resolution: ScopeResolution) -> ApprovalPrompt:
    """Render the payload for one resolved action, from #4's resolver output.

    ``resolution`` carries the named hosts, so a group arrives here already expanded. The
    resolver raises rather than return an empty set, which is why this function never has to
    read "no host" as "no target".
    """
    resolved = _checked_resolution(resolution)
    return _build(
        command=command,
        hosts=resolved.hosts,
        scope=resolved.scope,
        pattern=resolved.pattern,
    )


def render_approval_prompt_for_hosts(*, command: str, hosts: tuple[str, ...]) -> ApprovalPrompt:
    """Render the payload from an explicit tuple of resolved host names.

    The parameter is a tuple and not a ``Sequence[str]``. A ``str`` is itself a sequence of
    ``str``, so a sentence would pass the annotation and render one host per character.
    """
    return _build(command=command, hosts=hosts, scope=None, pattern=None)


def action_from_rendered_prompt(text: str) -> ApprovalPrompt:
    """Recover the command and the host set from a rendered payload's own bytes.

    The function reads the command and the host list back out of ``text``, and then renders
    those values again. A text whose count, order, digest line, or fixed wording differs from
    that render raises: exactly one text exists for one command and one host set.

    "Approve and add" (#219) derives its grant through this function, and not from the request
    body. The bytes are the ones the executor rendered and the digest covers, so a grant built
    from them cannot be wider than the action a human read. A grant built from a browser-side
    string would be a way to widen authority by editing a request.
    """
    command, hosts = _extract(text)
    payload = _build(command=command, hosts=hosts, scope=None, pattern=None)
    if payload.text != text:
        raise PromptRenderError(
            "This text is not a payload this renderer produced. A re-render of its command "
            "and its host list gives other bytes, so no digest describes it."
        )
    return payload


def digest_rendered_prompt(text: str) -> str:
    """Re-derive the binding digest from a rendered payload's own bytes.

    This is how a caller proves the approval covers the display. The executor in #18 holds the
    digest and the text, and needs no third field to compare them.
    """
    return action_from_rendered_prompt(text).target_digest


def _build(
    *, command: object, hosts: object, scope: str | None, pattern: str | None
) -> ApprovalPrompt:
    """The one path that validates, digests, and renders. Both entry points share it.

    A second path would let one caller render bytes another caller cannot digest.
    """
    resolved_command = _checked_command(command)
    resolved_hosts = _checked_hosts(hosts)
    digest = compute_target_digest(command=resolved_command, hosts=resolved_hosts)
    return ApprovalPrompt(
        command=resolved_command,
        hosts=resolved_hosts,
        text=_render_text(resolved_command, resolved_hosts, digest),
        target_digest=digest,
        scope=scope,
        pattern=pattern,
    )


def _render_text(command: str, hosts: tuple[str, ...], digest: str) -> str:
    """Lay out the payload. Same inputs, same bytes, every time.

    The index column is as wide as the count, so the names line up and a reader counts lines
    without effort.
    """
    width = len(str(len(hosts)))
    lines = [_HEADER, _PROVENANCE, "", _COMMAND_HEADER]
    lines += [_COMMAND_PREFIX + line for line in command.split("\n")]
    lines += ["", f"{_HOSTS_HEADER}{len(hosts)}"]
    lines += [
        f"{_HOST_INDENT}{index:>{width}}. {host}" for index, host in enumerate(hosts, start=1)
    ]
    lines += ["", f"{_DIGEST_HEADER}{digest}", ""]
    return "\n".join(lines)


def _extract(text: str) -> tuple[str, tuple[str, ...]]:
    """Read the command and the host names back out of a rendered payload.

    The reader stays simple, because ``digest_rendered_prompt`` re-renders the result and
    compares every byte. A line this function reads wrong therefore fails there.
    """
    command_lines: list[str] = []
    hosts: list[str] = []
    for line in text.split("\n"):
        if line.startswith(_COMMAND_PREFIX):
            command_lines.append(line[len(_COMMAND_PREFIX) :])
            continue
        match = _HOST_LINE_RE.match(line)
        if match is not None:
            hosts.append(match.group(2))
    if not command_lines or not hosts:
        raise PromptRenderError(
            "This text holds no command block, or no host list, so it is not an approval "
            "payload. Free-form text never gets a digest."
        )
    return "\n".join(command_lines), tuple(hosts)


def _checked_resolution(resolution: object) -> ScopeResolution:
    """Resolver output, or an error.

    The parameter is ``object`` so the check does real work at run time and not only in a type
    checker. A dynamic call site can pass anything, and a stand-in that merely holds a ``hosts``
    attribute would render names nobody resolved.
    """
    if not isinstance(resolution, ScopeResolution):
        raise PromptRenderError(
            "The target must be a ScopeResolution from nanoinfra.servers.scope. Only the "
            "resolver names the hosts an action touches."
        )
    return resolution


def _checked_command(command: object) -> str:
    """The resolved command, or an error. The text is never repaired.

    A newline is the one control character a command may hold, because a resolved command can
    be more than one line. Every other control character can repaint a terminal line or split
    it, and then the bytes a human reads differ from the bytes the digest covers.
    """
    if not isinstance(command, str):
        raise PromptRenderError("The command must be the resolved command string.")
    if not command.strip():
        raise PromptRenderError(
            "The payload needs a resolved command. An empty command approves nothing."
        )
    for line in command.split("\n"):
        if not _displayable(line):
            raise PromptRenderError(
                "The command holds a control character. Such a command displays as bytes the "
                "digest does not describe."
            )
    return command


def _checked_hosts(hosts: object) -> tuple[str, ...]:
    """The resolved host names, sorted and deduplicated exactly as the digest treats them.

    The order and the deduplication match ``compute_target_digest`` on purpose. A display of
    fifteen lines under a digest of fourteen hosts lets a human count one thing and authorize
    another.
    """
    if not isinstance(hosts, tuple):
        raise PromptRenderError(
            "The host list must be a tuple of resolved host names. A str is itself a sequence "
            "of str, so a sentence would render one host per character."
        )
    # The cast rests on the isinstance check above, and every entry gets a type check below.
    entries = cast("tuple[object, ...]", hosts)
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise PromptRenderError("Every host must be a resolved host name.")
        if not entry:
            raise PromptRenderError("A host name is empty. An unnamed host names no target.")
        if not _displayable(entry) or any(char.isspace() for char in entry):
            # A name with whitespace in it reads as two hosts, because the payload separates
            # host names by whitespace. A control character repaints or splits the line.
            raise PromptRenderError(
                f"Host name {entry!r} holds whitespace or a control character. The name a human "
                "reads would then differ from the name the digest covers."
            )
        names.add(entry)
    if not names:
        raise PromptRenderError(
            "The payload needs at least one resolved host. An approval bound to no host would "
            "verify against anything."
        )
    return tuple(sorted(names))


def _displayable(value: str) -> bool:
    """True when every character renders as itself, on every path that shows the payload."""
    for char in value:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F or char in _LINE_SEPARATORS:
            return False
    return True


__all__ = [
    "PROMPT_VERSION",
    "ApprovalPrompt",
    "PromptRenderError",
    "action_from_rendered_prompt",
    "digest_rendered_prompt",
    "render_approval_prompt",
    "render_approval_prompt_for_hosts",
]
