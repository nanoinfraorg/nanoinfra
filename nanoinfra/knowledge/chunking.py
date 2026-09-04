"""Split a document into citable fragments (#239).

A citation is ``path#section``, so a chunk boundary has to be something a reader can find again
by opening the file. Markdown gives that for free: the heading. Everything else gets a line
range, which is the only anchor a plain text file honestly has.

The alternative -- a fixed window over the whole tree -- retrieves the same text but cites
``doc.md`` and leaves the reader to search a 900-line runbook for the paragraph the model used.
That is the failure this module exists to prevent: an unverifiable citation is decoration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A section longer than this is split into parts that share its citation anchor. The number is a
#: retrieval decision, not a storage one: BM25 scores a whole document, so a 20 KB section scores
#: on words that may be nowhere near each other.
MAX_CHUNK_CHARS = 4000

#: Line windows for a file with no headings. Small enough that ``L1-L80`` points somewhere.
LINES_PER_WINDOW = 80

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdx"})

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_SLUG_STRIP = re.compile(r"[^a-z0-9\s-]")
_SLUG_SPACES = re.compile(r"[\s-]+")


@dataclass(frozen=True)
class Chunk:
    """One fragment of one document, with the anchor its citation will carry."""

    #: The ``#section`` half of the citation. Never empty -- a hit with no source is a bug.
    section: str
    #: The heading text as written, or "" for a line-range window. Indexed as its own field so
    #: BM25F can weight a title match above a body match.
    heading: str
    text: str
    #: Position in the file, so a chunk id stays stable when a heading repeats.
    ordinal: int


def slugify(heading: str) -> str:
    """Turn a heading into an anchor.

    Deliberately the GitHub anchor rule (lowercase, punctuation dropped, spaces to hyphens), so
    ``runbook.md#restart-the-pod`` is a link a reader can paste rather than a token only this
    index understands.
    """
    lowered = heading.strip().lower()
    lowered = _SLUG_STRIP.sub("", lowered)
    slug = _SLUG_SPACES.sub("-", lowered).strip("-")
    return slug


def chunk_document(text: str, *, suffix: str) -> list[Chunk]:
    """Split *text* into citable chunks, by heading for markdown and by line window otherwise."""
    if suffix.lower() in MARKDOWN_SUFFIXES:
        return _chunk_markdown(text)
    return _chunk_by_lines(text, start_line=1)


def _chunk_markdown(text: str) -> list[Chunk]:
    lines = text.splitlines()
    sections: list[tuple[str, int, list[str]]] = []
    preamble: list[str] = []
    in_fence = False
    fence_marker = ""

    for index, line in enumerate(lines):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker[:3]
            elif marker.startswith(fence_marker):
                in_fence, fence_marker = False, ""
        # A ``#`` inside a fenced block is shell, not a heading. Without this the first comment
        # in a code sample becomes a section and takes the rest of the document with it.
        heading = None if in_fence else _ATX_HEADING.match(line)
        if heading is None:
            (sections[-1][2] if sections else preamble).append(line)
            continue
        sections.append((heading.group(2).strip(), index + 1, [line]))

    chunks: list[Chunk] = []
    used: dict[str, int] = {}
    if preamble and "".join(preamble).strip():
        chunks.extend(_windows("", _anchor_for_lines(1, len(preamble)), preamble, len(chunks)))

    for heading, line_no, body in sections:
        anchor = slugify(heading) or _anchor_for_lines(line_no, line_no + len(body) - 1)
        seen = used.get(anchor, 0) + 1
        used[anchor] = seen
        # Two ``## Notes`` headings in one file are common. The second gets ``notes-2``, which is
        # again the anchor a markdown renderer would mint, so the citation still resolves.
        if seen > 1:
            anchor = f"{anchor}-{seen}"
        chunks.extend(_windows(heading, anchor, body, len(chunks)))
    return chunks


def _chunk_by_lines(text: str, *, start_line: int) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    for offset in range(0, max(len(lines), 1), LINES_PER_WINDOW):
        window = lines[offset : offset + LINES_PER_WINDOW]
        if not "".join(window).strip():
            continue
        first = start_line + offset
        anchor = _anchor_for_lines(first, first + len(window) - 1)
        chunks.extend(_windows("", anchor, window, len(chunks)))
    return chunks


def _windows(heading: str, anchor: str, lines: list[str], ordinal_base: int) -> list[Chunk]:
    """Emit one chunk per ``MAX_CHUNK_CHARS`` of *lines*, all citing *anchor*.

    The parts share the anchor on purpose: the citation names a section of a document, and a
    reader does not care that retrieval scored the second half of it.
    """
    chunks: list[Chunk] = []
    buffer: list[str] = []
    size = 0
    for line in lines:
        if buffer and size + len(line) + 1 > MAX_CHUNK_CHARS:
            chunks.append(
                Chunk(
                    section=anchor,
                    heading=heading,
                    text="\n".join(buffer),
                    ordinal=ordinal_base + len(chunks),
                )
            )
            buffer, size = [], 0
        buffer.append(line)
        size += len(line) + 1
    if buffer and "\n".join(buffer).strip():
        chunks.append(
            Chunk(
                section=anchor,
                heading=heading,
                text="\n".join(buffer),
                ordinal=ordinal_base + len(chunks),
            )
        )
    return chunks


def _anchor_for_lines(first: int, last: int) -> str:
    return f"L{first}-L{last}" if last > first else f"L{first}"
