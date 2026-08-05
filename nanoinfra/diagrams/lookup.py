"""Shared id-or-name lookup for saved diagrams.

Used by the ``/infradiagrams`` command (attaches a diagram as read-only
context) and by the agent tools in ``nanoinfra/agent/tools/diagrams.py``
(which read and update a diagram directly) -- one lookup, two callers.
"""

from __future__ import annotations

from nanoinfra.diagrams.store import DiagramStore
from nanoinfra.diagrams.types import Diagram, DiagramSummary


def resolve_diagram(store: DiagramStore, query: str) -> Diagram | None:
    """Look up a saved diagram by exact id, else case-insensitive name match.

    ``query`` is often "<name-or-id> <trailing free text>" -- e.g.
    ``/infradiagrams vLLM deployment basic what do you think of this`` -- so a
    diagram name (which can itself contain spaces) is also matched as a
    whole-word *prefix* of ``query``, picking the longest such match rather
    than requiring the whole remainder to equal a name/id exactly.

    Always re-fetches from the store and returns only its data -- never trusts
    anything about the diagram beyond using ``query`` as a lookup key, the
    same discipline ``session_access.py::normalize_mentions()`` uses for
    WebUI session mentions.
    """
    diagram = store.get(query)
    if diagram is not None:
        return diagram
    query_lower = query.lower()
    summaries = store.list_diagrams()
    for summary in summaries:
        if summary.name.lower() == query_lower:
            return store.get(summary.id)

    best_summary: DiagramSummary | None = None
    best_len = 0
    for summary in summaries:
        for candidate in (summary.id, summary.name):
            if len(candidate) <= best_len:
                continue
            candidate_lower = candidate.lower()
            if not query_lower.startswith(candidate_lower):
                continue
            remainder = query_lower[len(candidate) :]
            if remainder and not remainder[0].isspace():
                continue  # e.g. "vLLMx" must not match a "vLLM" prefix
            best_summary = summary
            best_len = len(candidate)
    return store.get(best_summary.id) if best_summary is not None else None


__all__ = ["resolve_diagram"]
