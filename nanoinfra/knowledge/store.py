"""The semlix index behind the knowledge base (#238).

Everything that touches semlix is in this file, and nothing outside it imports semlix. That is
the point of the module: semlix ships no type information, so the strict type checker sees an
untyped surface for everything it exports. Confining that to one file keeps the rest of the
package typed, and keeps the choice of engine replaceable.

Only the zero-dependency classic API is used -- ``index``, ``fields``, ``qparser``, ``scoring``,
``highlight``. ``semlix.unified`` and ``semlix.bm25`` raise ImportError demanding ``bm25s`` and
``PyStemmer``, which this project does not declare, so a deployment that installed nothing extra
would find the knowledge base broken at its first search rather than at install time.

The imports sit inside the functions rather than at module scope. The tool module that reaches
this one is imported by the ``pkgutil`` tool scan on every gateway start, including on
deployments with knowledge switched off, and semlix costs ~35ms to import.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanoinfra.knowledge.chunking import Chunk

#: How long a writer waits for the index lock. The cron pass and a search-time freshness pass can
#: collide, and the loser should wait a moment rather than report the knowledge base as broken.
WRITER_LOCK_TIMEOUT_S = 10.0

#: A heading match is a stronger signal than a body match: an operator who titles a section
#: "Restart the pod" has already told us what the section is about.
_FIELD_BOOSTS = {"heading": 2.0, "body": 1.0}

#: Enough of the matching text to judge the hit, not enough to become a second prompt.
_SNIPPET_CHARS = 320


class KnowledgeIndexBusyError(RuntimeError):
    """Another pass holds the index write lock."""


@dataclass(frozen=True)
class SearchHit:
    """One fragment, with the citation the answer must carry (#239)."""

    path: str
    section: str
    snippet: str
    score: float

    @property
    def citation(self) -> str:
        return f"{self.path}#{self.section}"


def _semlix_schema() -> Any:
    from semlix.fields import ID, STORED, TEXT, Schema

    # ``body`` is stored because the highlighter reads the stored text to build a snippet, and a
    # hit without a snippet is a citation the reader has to go and verify by hand.
    return Schema(
        chunk_id=ID(stored=True, unique=True),
        path=ID(stored=True),
        section=STORED(),
        heading=TEXT(stored=True),
        body=TEXT(stored=True),
    )


class LexicalIndex:
    """A BM25F index over the workspace's knowledge folder."""

    def __init__(self, index_dir: Path, handle: Any) -> None:
        self._dir = index_dir
        self._ix = handle

    @classmethod
    def open(cls, index_dir: Path, *, recreate: bool = False) -> LexicalIndex:
        """Open the index, creating it when absent or when *recreate* is asked for.

        A corrupt or older-format index is recreated rather than raised: the documents are the
        source of truth and the index is derived from them, so rebuilding is always a cheaper
        answer than refusing to search.
        """
        from semlix import index as semlix_index

        # Bound as Any deliberately: semlix is untyped, and one alias here is a smaller price
        # than a file-wide suppression that would also hide an unknown in our own code.
        api: Any = semlix_index
        index_dir.mkdir(parents=True, exist_ok=True)
        if not recreate and api.exists_in(str(index_dir)):
            try:
                return cls(index_dir, api.open_dir(str(index_dir)))
            except Exception:
                pass
        return cls(index_dir, api.create_in(str(index_dir), _semlix_schema()))

    @contextmanager
    def batch(self) -> Generator[IndexBatch]:
        """Hold one writer for a whole pass, committing once at the end."""
        from semlix.index import LockError

        try:
            writer: Any = self._ix.writer(timeout=WRITER_LOCK_TIMEOUT_S)
        except LockError as exc:
            raise KnowledgeIndexBusyError(
                "the knowledge index is being written by another pass"
            ) from exc
        try:
            yield IndexBatch(writer)
        except BaseException:
            writer.cancel()
            raise
        writer.commit()

    def document_count(self) -> int:
        """How many fragments are indexed. Zero is a legitimate answer."""
        with self._ix.searcher() as searcher:
            return int(searcher.doc_count())

    def search(self, query: str, limit: int) -> list[SearchHit]:
        """Return at most *limit* fragments, best first.

        An unparsable query returns nothing rather than raising: the model wrote it, and a
        traceback teaches it less than an empty result it can rephrase.
        """
        from semlix import qparser
        from semlix.highlight import ContextFragmenter, NullFormatter
        from semlix.qparser import OrGroup
        from semlix.scoring import BM25F

        text = query.strip()
        if not text:
            return []
        # Reached through the module rather than imported: ``MultifieldParser`` is an untyped
        # factory, and the alias is what keeps this file free of a blanket suppression.
        parser_api: Any = qparser
        parser: Any = parser_api.MultifieldParser(
            ["heading", "body"],
            schema=self._ix.schema,
            fieldboosts=_FIELD_BOOSTS,
            # A Whoosh-shaped parser defaults to AND, which answers nothing for a question
            # phrased as a sentence, so this is an OR: a document matching every term still
            # outscores one matching a single term, because BM25 sums the term scores.
            #
            # Plain `OrGroup`, never `OrGroup.factory(scale)`. The scaled variant wraps the
            # query in a CoordMatcher whose factor is `(termcount - 1) / termcount` over the
            # *surviving* term matchers, so a one-word query -- which matches `body` and not
            # `heading` -- scores every hit 0.0 and ranks by docnum (#260).
            group=OrGroup,
        )
        try:
            parsed: Any = parser.parse(text)
        except Exception:
            return []

        hits: list[SearchHit] = []
        with self._ix.searcher(weighting=BM25F()) as searcher:
            results: Any = searcher.search(parsed, limit=limit)
            results.fragmenter = ContextFragmenter(maxchars=_SNIPPET_CHARS, surround=60)
            # Plain text, not HTML: this snippet goes to a model and to a terminal.
            results.formatter = NullFormatter()
            for hit in list(results):
                fields: dict[str, Any] = hit.fields()
                hits.append(
                    SearchHit(
                        path=str(fields.get("path") or ""),
                        section=str(fields.get("section") or ""),
                        snippet=_snippet(hit, fields),
                        score=float(hit.score or 0.0),
                    )
                )
        return hits


class IndexBatch:
    """The writer half of one pass. Created by :meth:`LexicalIndex.batch`."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def remove(self, rel: str) -> None:
        """Drop every fragment of one document.

        By term on the unanalysed ``path`` field, so a document whose file is gone leaves no hit
        behind -- the deletion case the cron pass exists to collect.
        """
        self._writer.delete_by_term("path", rel)

    def replace(self, rel: str, chunks: list[Chunk]) -> None:
        """Reindex one document: delete what was there, add what is there now.

        The file's mtime is not written here. The manifest is the single authority on freshness,
        and two copies of one fact is how a stale index starts claiming to be current.
        """
        self.remove(rel)
        for chunk in chunks:
            self._writer.add_document(
                chunk_id=f"{rel} {chunk.ordinal}",
                path=rel,
                section=chunk.section,
                heading=chunk.heading,
                body=chunk.text,
            )


def _snippet(hit: Any, fields: dict[str, Any]) -> str:
    """A snippet for one hit, falling back to the head of the fragment.

    The highlighter returns "" when the match is in ``heading`` only, and a result with no
    snippet is the thing #239 forbids -- so the fallback is not an optimisation, it is the
    contract.
    """
    try:
        highlighted = str(hit.highlights("body") or "")
    except Exception:
        highlighted = ""
    if highlighted.strip():
        return " ".join(highlighted.split())
    collapsed = " ".join(str(fields.get("body") or "").split())
    if len(collapsed) <= _SNIPPET_CHARS:
        return collapsed
    return collapsed[:_SNIPPET_CHARS].rstrip() + "..."
