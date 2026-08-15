"""The read side of the gate audit log -- nanoinfraorg/nanoinfra#29.

This module reads. It exposes no write, no delete, and no prune, because #16 makes the log
append-only and an append-only log with a delete endpoint would not be one. #16 owns retention,
and it trims whole expired segments without rewriting a record it keeps.

Two fields drive an operator's judgement rather than the layout. ``same_path`` marks a decision
whose request and approval arrived on one channel, and #13 records it even when policy allowed the
action, so the viewer must keep it visible. ``command_digest`` is what the log holds by default,
because a resolved command routinely embeds a secret.

Filters fail closed. A value the log never writes matches nothing, so a typo narrows the answer to
nothing rather than widening it to every record.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanoinfra.gates.audit import AuditStore

# The caller controls the page size, so the server bounds it. A log with a year of decisions must
# not arrive in one response.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def audit_page(
    store: AuditStore,
    *,
    decision: str | None = None,
    capability_class: str | None = None,
    execution_context: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of decisions, newest first.

    Newest first because an operator opens this after something happened, so the last decision
    leads. ``total`` counts every record the filters matched, not the page, so the viewer can
    show how much it is not displaying.
    """
    records = _all_records(store)
    matched = [
        record
        for record in records
        if _matches(
            record,
            decision=decision,
            capability_class=capability_class,
            execution_context=execution_context,
            session_id=session_id,
            since=since,
            until=until,
        )
    ]
    matched.sort(key=_sort_key, reverse=True)

    start = max(0, offset)
    size = min(max(1, limit), MAX_LIMIT)
    return {
        "records": matched[start : start + size],
        "total": len(matched),
        # Whether the log is configured to hold full command text. The viewer marks those
        # records, so a reader knows the text may carry a secret.
        "records_command_text": _records_command_text(store),
        "limit": size,
        "offset": start,
    }


def _all_records(store: AuditStore) -> list[dict[str, Any]]:
    """Every readable record, or an empty list.

    A missing audit root is a fresh install, and the viewer must not read that as an error. A
    corrupt line costs its own record: #16 documents that only a segment's last line can tear,
    and one torn tail must not empty the viewer.
    """
    try:
        return store.read_all()
    except OSError as exc:
        logger.warning("audit viewer could not read the log: {}", exc)
        return []


def _records_command_text(store: AuditStore) -> bool:
    config = getattr(store, "config", None)
    return bool(getattr(config, "record_command_text", False))


def _matches(
    record: dict[str, Any],
    *,
    decision: str | None,
    capability_class: str | None,
    execution_context: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    for key, wanted in (
        ("decision", decision),
        ("capability_class", capability_class),
        ("execution_context", execution_context),
        ("session_id", session_id),
    ):
        if wanted is not None and record.get(key) != wanted:
            return False

    if since is None and until is None:
        return True
    moment = _timestamp(record)
    if moment is None:
        # A record with no readable timestamp cannot be placed in a range. Excluding it from a
        # ranged query is the honest answer, and an unfiltered query still shows it.
        return False
    if since is not None and moment < since:
        return False
    return not (until is not None and moment > until)


def _timestamp(record: dict[str, Any]) -> datetime | None:
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _sort_key(record: dict[str, Any]) -> str:
    """Sort on the stored ISO string.

    #16 writes a UTC ISO timestamp, and those sort correctly as text. A record with no timestamp
    sorts last rather than crashing the page.
    """
    raw = record.get("ts")
    return raw if isinstance(raw, str) else ""


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "audit_page"]
