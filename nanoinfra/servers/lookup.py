"""Shared id-or-name lookup for inventoried servers.

Same shape as nanoinfra/diagrams/lookup.py's resolve_diagram, minus the
"longest prefix of trailing free text" case -- callers pass a server name
directly (e.g. execute_on_server's server_id_or_name argument), not a
free-text command that happens to start with one.
"""

from __future__ import annotations

from nanoinfra.servers.store import ServerStore
from nanoinfra.servers.types import Server


def resolve_server(store: ServerStore, query: str) -> Server | None:
    """Look up a server by exact id, else case-insensitive exact name match."""
    server = store.get(query)
    if server is not None:
        return server
    query_lower = query.lower()
    for summary in store.list_servers():
        if summary.name.lower() == query_lower:
            return store.get(summary.id)
    return None


__all__ = ["resolve_server"]
