"""Device memory: one ``NOTES.md`` beside each inventory record (#223).

A device accumulates knowledge that is true about *the device* -- this box's disk fills because of
one runaway log, this one needs ``sudo -n``, this one's package manager is held back on purpose.
Today that lives in whoever remembers the last incident, and every session rediscovers it.

Three properties this module exists to hold, each of which is a decision rather than an
implementation detail:

- **Keyed by id, not by name.** ``<uuid4hex>.NOTES.md``, a sibling of ``<uuid4hex>.json``. A server
  can be renamed, which is why cron stores ids and re-reads the name at run time; a name-keyed
  notes file would orphan itself the first time somebody renamed a box. A sibling rather than a
  subdirectory because the store enumerates with ``glob("*.json")`` (``store.py:60``), so a ``.md``
  beside it is invisible to the store and obvious to a human in the file browser.

- **Appended with ``O_APPEND``, never rewritten.** An entry is text that goes on the end, so there
  is no read-modify-write and two peers of one plan appending concurrently are as safe as they are
  in a JSONL, for the same reason. Only :meth:`revise_own` and rotation need the whole file, and
  both take the per-file lock the session store already uses
  (``nanoinfra/session/manager.py:64,759``).

- **Authorship is in the entry, not only known by the caller** (#228), because the model has to see
  the precedence rather than being told about it in the abstract. An operator's entry carries
  ``(operator)``; an agent's carries a bare name. An agent may revise only its own entries -- see
  :meth:`revise_own`, which refuses on both halves of that rule.

There is no delete. A human deletes by editing the file; an agent corrects by appending or by
revising its own entry.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

from nanoinfra.servers.store import ServerStore, valid_server_id
from nanoinfra.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    ensure_dir,
)

#: Marks a human author inside the entry itself, so precedence survives a copy of the file and is
#: visible to the model reading it (#228).
OPERATOR_SUFFIX = "(operator)"

AUTHOR_AGENT = "agent"
AUTHOR_OPERATOR = "operator"

#: ``## <when> · <author> · <title>``. The middle dot rather than a dash so a title may contain one,
#: and the fields are non-greedy so only the *first* two separators split the heading -- a title
#: with a dot in it stays whole.
_HEADING_RE = re.compile(r"^## (?P<when>[^·\n]+?) · (?P<author>[^·\n]+?) · (?P<title>.+?)\s*$")

#: One entry's ceiling. A note is a conclusion, not a transcript -- the transcript already exists in
#: the session -- so an entry that needs more than this is the wrong shape rather than a big fact.
MAX_ENTRY_CHARS = 4_000

#: The live file's ceiling, past which the oldest entries rotate to the archive (#227). A cap on
#: characters rather than on entries because characters are what a prompt pays for, and because a
#: ``stat`` answers it without reading the file on the append path.
MAX_LIVE_CHARS = 24_000

#: Bounds the title, which becomes the handle :meth:`revise_own` matches on.
MAX_TITLE_CHARS = 120

#: The whole-file ceiling a human edit may not exceed. Far past the live cap on purpose -- rotation
#: governs what an agent accumulates, and this only stops a client from writing a megabyte into the
#: workspace through a route meant for prose.
MAX_FILE_CHARS = 200_000

_LOCK_TIMEOUT_SECONDS = 30


class ServerNotesError(ValueError):
    """A notes write the caller must fix rather than retry."""


class CredentialInNoteError(ServerNotesError):
    """The note carried credential material and was refused (#224).

    Refused rather than silently redacted, on purpose: masking a value keeps the sentence honest,
    but masking a hostname turns a useful note into a riddle, and nobody notices until somebody
    acts on it. The message names the shape so the model can rewrite the note.
    """


#: Shapes that mean "this note is carrying a credential", each with the phrase the refusal uses.
#:
#: The long-hex rule fires on 32 characters, which also catches a uuid4 id and a git sha. That is
#: the trade taken deliberately: a refusal costs the model one rewrite and names the reason, and a
#: digest a future visitor does not need was never note material. A leaked token costs more, and
#: this file is read by the next agent and carried by the backup.
_CREDENTIAL_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN[- A-Z0-9]*-----"), "a PEM block header (-----BEGIN ...)"),
    (
        re.compile(
            r"(?i)\b(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|bearer)\b"
            r"\s*[:=]\s*\S"
        ),
        "an assignment such as password=, token= or api_key=",
    ),
    (re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])"), "a long hex run (32+ chars)"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "a GitHub token"),
    (re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,}\b"), "a Slack token"),
    (re.compile(r"\bssh-(?:rsa|dss|ed25519)\s+[A-Za-z0-9+/]{40,}"), "an SSH key blob"),
)

#: A base64 run, checked with a predicate rather than by the pattern alone. ``/`` is a base64
#: character and also a path separator, so the pattern on its own refuses
#: ``/var/lib/postgresql/data/base/16384`` -- and a note about a box is mostly paths. The predicate
#: below asks for the mixed case and the digit that an encoded secret has and a path does not.
_BASE64_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")


def _looks_encoded(run: str) -> bool:
    if run.endswith("="):
        return True
    return (
        any(character.isupper() for character in run)
        and any(character.islower() for character in run)
        and any(character.isdigit() for character in run)
    )


def screen_for_credentials(text: str) -> str | None:
    """Return the phrase naming the credential shape found in *text*, or ``None``."""
    for pattern, phrase in _CREDENTIAL_SHAPES:
        if pattern.search(text):
            return phrase
    for match in _BASE64_RUN_RE.finditer(text):
        if _looks_encoded(match.group(0)):
            return "a long base64-like run (40+ chars)"
    return None


def sanitize_author(author: str, *, kind: str) -> str:
    """Reduce a derived author to something that cannot forge a heading or a rank.

    The separator and newlines go because they are the heading's structure, and a trailing
    ``(operator)`` goes because that string *is* the precedence marker (#228). An agent whose
    derived name ended in it would otherwise outrank the person who wrote the note above.
    """
    cleaned = " ".join(author.replace("·", " ").split()).strip()
    while cleaned.endswith(OPERATOR_SUFFIX):
        cleaned = cleaned[: -len(OPERATOR_SUFFIX)].strip()
    cleaned = cleaned[:60].strip()
    if not cleaned:
        cleaned = "operator" if kind == AUTHOR_OPERATOR else "agent"
    return cleaned


def _now_heading_time() -> str:
    """``2026-09-03 14:22 UTC``.

    The date alone is the shape the proposal illustrates, and the minute is added because the same
    document asks a reader to be able to see that two agents looked at the same box in the same
    hour. Both halves are one ``when`` field, so the heading grammar is unchanged.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class NoteEntry:
    """One entry, as parsed back out of the markdown."""

    when: str
    author: str
    title: str
    body: str
    is_operator: bool

    def render(self) -> str:
        author = f"{self.author} {OPERATOR_SUFFIX}" if self.is_operator else self.author
        heading = f"## {self.when} · {author} · {self.title}\n"
        body = self.body.strip("\n")
        return f"{heading}{body}\n" if body else heading

    def to_dict(self) -> dict[str, object]:
        return {
            "when": self.when,
            "author": self.author,
            "title": self.title,
            "body": self.body,
            "isOperator": self.is_operator,
        }


@dataclass(frozen=True)
class ParsedNotes:
    """The file split into what a human typed at the top and the entries below it.

    ``preamble`` exists so rotation and :meth:`revise_own` cannot eat a human's own free text: a
    person editing this file writes above the entries as often as inside one.
    """

    preamble: str = ""
    entries: list[NoteEntry] = field(default_factory=list["NoteEntry"])

    def render(self) -> str:
        parts = [self.preamble.strip("\n")] if self.preamble.strip() else []
        parts.extend(entry.render().rstrip("\n") for entry in self.entries)
        return "\n\n".join(parts) + "\n" if parts else ""


def parse_notes(text: str) -> ParsedNotes:
    """Split *text* into its preamble and its entries, in file order (oldest first)."""
    preamble_lines: list[str] = []
    entries: list[NoteEntry] = []
    current: tuple[str, str, bool, str] | None = None
    body: list[str] = []

    def close() -> None:
        if current is None:
            return
        when, author, is_operator, title = current
        entries.append(
            NoteEntry(
                when=when,
                author=author,
                title=title,
                body="\n".join(body).strip("\n"),
                is_operator=is_operator,
            )
        )

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            if current is None:
                preamble_lines.append(line)
            else:
                body.append(line)
            continue
        close()
        raw_author = match.group("author").strip()
        is_operator = raw_author.endswith(OPERATOR_SUFFIX)
        author = raw_author[: -len(OPERATOR_SUFFIX)].strip() if is_operator else raw_author
        current = (match.group("when").strip(), author, is_operator, match.group("title").strip())
        body = []
    close()
    return ParsedNotes(preamble="\n".join(preamble_lines).strip("\n"), entries=entries)


class ServerNotesStore:
    """The ``NOTES.md`` of every server in one workspace."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / "servers"
        self._servers = ServerStore(self.workspace_path)

    # --- paths ---

    def path(self, server_id: str) -> Path | None:
        """The live notes file, or ``None`` for an id the store would refuse to write."""
        if not valid_server_id(server_id):
            return None
        return self.root / f"{server_id}.NOTES.md"

    def archive_path(self, server_id: str) -> Path | None:
        if not valid_server_id(server_id):
            return None
        return self.root / f"{server_id}.NOTES.archive.md"

    def _lock(self, server_id: str) -> FileLock:
        """Guards the whole-file operations only -- ``revise-own`` and rotation.

        An append does not take it, and that is the point: the lock is for a read-modify-write, and
        an ``O_APPEND`` write is not one.
        """
        return FileLock(
            str(self.root / f".{server_id}.NOTES.lock"),
            timeout=_LOCK_TIMEOUT_SECONDS,
        )

    # --- reads ---

    def read(self, server_id: str) -> str:
        return self._read_file(self.path(server_id))

    def read_archive(self, server_id: str) -> str:
        return self._read_file(self.archive_path(server_id))

    def has_notes(self, server_id: str) -> bool:
        path = self.path(server_id)
        return path is not None and path.is_file() and path.stat().st_size > 0

    def entries(self, server_id: str) -> list[NoteEntry]:
        return parse_notes(self.read(server_id)).entries

    @staticmethod
    def _read_file(path: Path | None) -> str:
        if path is None:
            return ""
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().lstrip("\n")
        except FileNotFoundError:
            return ""

    # --- writes ---

    def append(
        self,
        server_id: str,
        *,
        author: str,
        kind: str,
        title: str,
        body: str,
    ) -> NoteEntry:
        """Append one entry through ``O_APPEND`` and stamp the record.

        The credential screen runs for an agent and not for an operator (#224). The hazard the
        refusal exists for is an agent summarising what it just did on a box and pasting a token it
        read from a config; a person deliberately writing in their own file is not that hazard, and
        refusing their edit would be the WebUI telling an operator what may go in a file they own.
        """
        path = self.path(server_id)
        if path is None:
            raise ServerNotesError(f"invalid server id: {server_id!r}")

        entry = self._build_entry(author=author, kind=kind, title=title, body=body)
        if kind != AUTHOR_OPERATOR:
            found = screen_for_credentials(entry.render())
            if found is not None:
                raise CredentialInNoteError(
                    f"Refusing to write this note: it contains {found}. "
                    "A note is a conclusion, not a transcript -- restate the fact without the "
                    "value and append again."
                )

        ensure_dir(self.root)
        # A leading blank line rather than a header written on creation: detecting "this is the
        # first entry" is itself a read-modify-write, and two peers racing on it could order the
        # header after an entry. Reads strip the leading blank, so it costs nothing.
        payload = ("\n" + entry.render()).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
        finally:
            os.close(fd)

        self._servers.touch_notes(server_id)
        self._rotate_if_over_cap(server_id, path)
        return entry

    def revise_own(
        self,
        server_id: str,
        *,
        author: str,
        kind: str,
        title: str,
        body: str,
    ) -> NoteEntry:
        """Replace the newest entry this author wrote under *title*.

        Refuses on both halves of the precedence rule (#228): an operator's entry is never a
        candidate, and an author only ever matches its own entries. An agent that believes an
        operator's note is now wrong appends saying so, which keeps the operator's words and puts
        the disagreement where a person can settle it.
        """
        path = self.path(server_id)
        if path is None:
            raise ServerNotesError(f"invalid server id: {server_id!r}")
        replacement = self._build_entry(author=author, kind=kind, title=title, body=body)
        if kind != AUTHOR_OPERATOR:
            found = screen_for_credentials(replacement.render())
            if found is not None:
                raise CredentialInNoteError(
                    f"Refusing to write this note: it contains {found}. "
                    "A note is a conclusion, not a transcript -- restate the fact without the "
                    "value and try again."
                )

        wanted = replacement.title.casefold()
        with self._lock(server_id):
            parsed = parse_notes(self._read_file(path))
            index = next(
                (
                    position
                    for position, entry in reversed(list(enumerate(parsed.entries)))
                    if not entry.is_operator
                    and entry.author == replacement.author
                    and entry.title.casefold() == wanted
                ),
                None,
            )
            if index is None:
                mine = [
                    entry.title
                    for entry in parsed.entries
                    if not entry.is_operator and entry.author == replacement.author
                ]
                detail = f" Your own entries are titled: {', '.join(mine)}." if mine else ""
                raise ServerNotesError(
                    f"No entry titled {replacement.title!r} written by {replacement.author!r}. "
                    "An agent may revise only its own entries; append instead."
                    + detail
                )
            previous = parsed.entries[index]
            revised = NoteEntry(
                when=_revised_when(previous.when, replacement.when),
                author=previous.author,
                title=previous.title,
                body=replacement.body,
                is_operator=False,
            )
            parsed.entries[index] = revised
            _write_text_atomic(path, parsed.render())
        self._servers.touch_notes(server_id)
        self._rotate_if_over_cap(server_id, path)
        return revised

    def replace(self, server_id: str, text: str) -> None:
        """Write the whole live file, for a human editing it in the WebUI (#229).

        Whole-file and therefore locked, so it cannot land in the middle of a rotation. Not screened
        for credentials, for the reason :meth:`append` gives.
        """
        path = self.path(server_id)
        if path is None:
            raise ServerNotesError(f"invalid server id: {server_id!r}")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if len(normalized) > MAX_FILE_CHARS:
            raise ServerNotesError(
                f"these notes are {len(normalized)} characters and the cap is {MAX_FILE_CHARS}."
            )
        ensure_dir(self.root)
        with self._lock(server_id):
            _write_text_atomic(path, f"{normalized}\n" if normalized else "")
        self._servers.touch_notes(server_id)

    # --- internals ---

    def _build_entry(self, *, author: str, kind: str, title: str, body: str) -> NoteEntry:
        clean_title = " ".join(title.replace("·", " ").split()).strip()[:MAX_TITLE_CHARS]
        if not clean_title:
            raise ServerNotesError("a note needs a short title")
        clean_body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not clean_body.strip():
            raise ServerNotesError("a note needs a body")
        # Headings inside the body would parse back as separate entries, so they are demoted
        # rather than refused: the model writing markdown is not a mistake worth a round trip.
        clean_body = re.sub(r"^(#{1,2})(?= )", r"###", clean_body, flags=re.MULTILINE)
        if len(clean_body) > MAX_ENTRY_CHARS:
            raise ServerNotesError(
                f"this note is {len(clean_body)} characters and the cap is {MAX_ENTRY_CHARS}. "
                "A note records what changes what the next visitor would do, not the transcript."
            )
        return NoteEntry(
            when=_now_heading_time(),
            author=sanitize_author(author, kind=kind),
            title=clean_title,
            body=clean_body,
            is_operator=kind == AUTHOR_OPERATOR,
        )

    def _rotate_if_over_cap(self, server_id: str, path: Path) -> bool:
        """Move the oldest entries to ``<id>.NOTES.archive.md`` once the live file is over cap.

        Never a delete (#227): the point of the file is that it remembers, so the archive keeps the
        oldest entries retrievable.

        Every operator entry stays in the live file whatever its age, because an operator's note is
        what an agent is meant to read before it acts (#228) and ageing it out would quietly remove
        the thing that outranks. An agent's entries are what the cap exists for -- a human writes
        these by hand, an agent does not.
        """
        try:
            if path.stat().st_size <= MAX_LIVE_CHARS:
                return False
        except FileNotFoundError:
            return False

        archive = self.archive_path(server_id)
        if archive is None:
            return False

        with self._lock(server_id):
            parsed = parse_notes(self._read_file(path))
            if len(parsed.render()) <= MAX_LIVE_CHARS:
                # Another writer rotated between the stat and the lock.
                return False
            # ``+ 2`` per part because ``ParsedNotes.render`` joins with a blank line, so the cost
            # of keeping an entry is its own text plus the separator that precedes it.
            def cost(entry: NoteEntry) -> int:
                return len(entry.render().rstrip("\n")) + 2

            # The operator's share comes off the budget before the loop rather than during it. Take
            # it as it is met, walking newest first, and an operator entry that happens to be the
            # oldest is charged after the budget is already spent -- which leaves the live file over
            # cap by exactly that entry.
            keep_indexes = {
                position for position, entry in enumerate(parsed.entries) if entry.is_operator
            }
            budget = MAX_LIVE_CHARS
            if parsed.preamble.strip():
                budget -= len(parsed.preamble.strip("\n")) + 2
            budget -= sum(cost(parsed.entries[position]) for position in keep_indexes)
            for position in range(len(parsed.entries) - 1, -1, -1):
                if position in keep_indexes:
                    continue
                entry_cost = cost(parsed.entries[position])
                if entry_cost > budget:
                    continue
                keep_indexes.add(position)
                budget -= entry_cost
            rotated = [
                entry for position, entry in enumerate(parsed.entries)
                if position not in keep_indexes
            ]
            if not rotated:
                return False
            kept = ParsedNotes(
                preamble=parsed.preamble,
                entries=[
                    entry for position, entry in enumerate(parsed.entries)
                    if position in keep_indexes
                ],
            )
            body = "".join(f"\n{entry.render()}" for entry in rotated).encode("utf-8")
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = 0
                while written < len(body):
                    written += os.write(fd, body[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            _write_text_atomic(path, kept.render())
        return True


def _revised_when(previous: str, now: str) -> str:
    """Keep when the fact was first recorded and add when it was corrected."""
    original = previous.split(" (revised ", 1)[0].strip()
    return f"{original} (revised {now})"


__all__ = [
    "AUTHOR_AGENT",
    "AUTHOR_OPERATOR",
    "CredentialInNoteError",
    "MAX_ENTRY_CHARS",
    "MAX_FILE_CHARS",
    "MAX_LIVE_CHARS",
    "NoteEntry",
    "OPERATOR_SUFFIX",
    "ParsedNotes",
    "ServerNotesError",
    "ServerNotesStore",
    "parse_notes",
    "sanitize_author",
    "screen_for_credentials",
]
