"""The read side of the gate audit log -- nanoinfraorg/nanoinfra#29.

This module reads. It exposes no write, no delete, and no prune, because #16 makes the log
append-only and an append-only log with a delete endpoint would not be one. #16 owns retention,
and it trims whole expired segments without rewriting a record it keeps.

Two fields drive an operator's judgement rather than the layout. ``same_path`` marks a decision
whose request and approval arrived on one channel, and #13 records it even when policy allowed the
action, so the viewer must keep it visible. ``command_digest`` is what the log holds by default,
because a resolved command routinely embeds a secret.

The log holds two kinds of record after #46. A decision record says what the gate decided. A
completion record says what happened next, and it carries the exit code and the duration. The
``decision`` field names the kind, so one filter isolates the completions. ``follows`` names the
decision record a completion follows, so a reader pairs the two rows by an id.

A record names two people after #79, and the viewer keeps them apart. ``actor`` is the person who
answered, and a path this deployment trusts authenticated them. ``origin_actor`` is the person the
request came from, and it is the agent's claim about itself. The filter accepts the second one, so
a reviewer can ask "every action this person raised" here rather than with a shell.

Filters fail closed. A value the log never writes matches nothing, so a typo narrows the answer to
nothing rather than widening it to every record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_INVENTORY,
    MUTATE_LOCAL,
    MUTATE_REMOTE,
    READ,
)
from nanoinfra.gates.audit import DECISION_COMPLETION, DECISION_GRANT_WRITTEN

if TYPE_CHECKING:
    from nanoinfra.gates.audit import AuditStore

# The route the viewer reads. One path, and no sibling that writes.
AUDIT_READ_PATH = "/api/webui/gates/audit"

# The values the log writes today, for the viewer's selects. The server owns this list, so a new
# decision name needs no UI edit. `refused` is #15's latched attempt. `expired` is #38's deadline.
# `completion` is #46's outcome record, and `grant_written` is #219's derived grant. Both come from
# the store rather than from a copy of the string here, so one name cannot drift into two.
DECISION_CHOICES = (
    "allow",
    "grant",
    "approve",
    "deny",
    "refused",
    "expired",
    "denied",
    "cleared",
    "preview",
    "would_gate",
    DECISION_COMPLETION,
    DECISION_GRANT_WRITTEN,
)

CAPABILITY_CLASS_CHOICES = (
    READ,
    MUTATE_LOCAL,
    MUTATE_INVENTORY,
    MUTATE_REMOTE,
    CREDENTIAL_ACCESS,
)

EXECUTION_CONTEXT_CHOICES = ("interactive", "automation", "subagent")

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
    origin_actor: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of decisions, newest first.

    Newest first because an operator opens this after something happened, so the last decision
    leads. ``total`` counts every record the filters matched, not the page, so the viewer can
    show how much it is not displaying.

    ``origin_actor`` answers "every action this person raised" (#79). It is free text and not a
    choice, because the value is a person and no list of people exists here. The match is exact,
    like every other filter, so a record that names nobody never matches a name.
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
            origin_actor=origin_actor,
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
    origin_actor: str | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    for key, wanted in (
        ("decision", decision),
        ("capability_class", capability_class),
        ("execution_context", execution_context),
        ("session_id", session_id),
        ("origin_actor", origin_actor),
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


class AuditReadSurface:
    """The operator's read of the gate audit log (#29).

    The class holds one method on purpose. #16 makes the log append-only, so a surface that
    could prune or edit a record would make that false. Retention belongs to #16, which drops
    whole expired segments and never rewrites a record it keeps.

    The gateway attaches this after boot. A route with no surface answers 503, because a viewer
    that cannot reach the log must not render an empty log.
    """

    def __init__(self, store: AuditStore) -> None:
        self._store = store

    def page(self, query: Mapping[str, Sequence[str]]) -> dict[str, Any]:
        """One page of decisions for the viewer, newest first.

        Query values arrive as the parsed query string, so each one is a list. An absent filter
        and a blank filter mean the same thing: no filter.
        """
        raw = audit_page(
            self._store,
            decision=_first(query, "decision"),
            capability_class=_first(query, "capabilityClass"),
            execution_context=_first(query, "executionContext"),
            session_id=_first(query, "sessionId"),
            origin_actor=_first(query, "originActor"),
            since=_moment(_first(query, "since")),
            until=_moment(_first(query, "until")),
            limit=_positive_int(_first(query, "limit"), DEFAULT_LIMIT),
            offset=_positive_int(_first(query, "offset"), 0),
        )
        holds_text = bool(raw["records_command_text"])
        return {
            "records": [_for_viewer(record, holds_text=holds_text) for record in raw["records"]],
            "total": raw["total"],
            "limit": raw["limit"],
            "offset": raw["offset"],
            "recordsCommandText": holds_text,
            "choices": {
                "decision": list(DECISION_CHOICES),
                "capabilityClass": list(CAPABILITY_CLASS_CHOICES),
                "executionContext": list(EXECUTION_CONTEXT_CHOICES),
            },
        }


def _for_viewer(record: Mapping[str, Any], *, holds_text: bool) -> dict[str, Any]:
    """One record in the WebUI's spelling, with every field the detail view shows.

    `holdsCommandText` marks a record whose text the log kept. A reader must know that the text
    may carry a secret, and the mark travels with the record rather than with the page, because a
    reader may read one record and never the page header.
    """
    text = record.get("command_text")
    return {
        "ts": record.get("ts"),
        # The two halves of #46's join. `recordId` names this record. `follows` names the decision
        # record a completion follows, and it is null on a decision record.
        "recordId": record.get("record_id"),
        "follows": record.get("follows"),
        "sessionId": record.get("session_id"),
        "executionContext": record.get("execution_context"),
        "originPath": record.get("origin_path"),
        "approvalPath": record.get("approval_path"),
        # #13 records this even when policy allowed the action, so the viewer keeps it visible.
        "samePath": bool(record.get("same_path")),
        # The two identities of #79. `actor` is the person who answered, and a trusted path
        # authenticated them. `originActor` is the person the request came from, and it is the
        # agent's claim about itself. The viewer says which is which, because a reader treats a
        # name in a log as authenticated unless something says otherwise.
        "originActor": record.get("origin_actor"),
        "actor": record.get("actor"),
        "capabilityClass": record.get("capability_class"),
        "scope": record.get("scope"),
        "hosts": list(record.get("hosts") or ()),
        "hostCount": record.get("host_count"),
        "commandDigest": record.get("command_digest"),
        "commandText": text if isinstance(text, str) else None,
        "holdsCommandText": holds_text and isinstance(text, str),
        "decision": record.get("decision"),
        "reason": record.get("reason"),
        "grantId": record.get("grant_id"),
        "tokenNonce": record.get("token_nonce"),
        "exitCode": record.get("exit_code"),
        "durationMs": record.get("duration_ms"),
        "tool": record.get("tool"),
    }


def _first(query: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    value = str(values[0]).strip()
    return value or None


def _moment(raw: str | None) -> datetime | None:
    """An ISO-8601 filter bound, or None when the caller sent nothing usable.

    An unparsable bound drops the bound rather than the query. The other filters still narrow
    the answer, and a viewer that answered 400 for a half-typed date would fight its own user.
    """
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        logger.debug("audit viewer ignored an unparsable time bound: {!r}", raw)
        return None


def _positive_int(raw: str | None, fallback: int) -> int:
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value >= 0 else fallback
