"""SQLite for one row per provider attempt, and the aggregates the WebUI already reads (#176).

Why a database at all, when `webui/token_usage.py` already keeps day rows in JSON: a day row cannot
answer *how many calls failed and retried, and what did the retry cost*. It cannot answer it in
principle, not for want of fields -- the information is destroyed when the day is summed. One row
per attempt keeps it, and every aggregate the WebUI shows is still a `SUM` away.

Three properties are load-bearing:

- **Bounded.** 400 days and 100,000 rows, pruned on write. The JSON file it replaces was capped at
  512KB and the same 400 days, so this is not a new promise -- it is the same promise with room for
  granularity.
- **Content-free by construction.** The schema has no column a prompt could be written to. See
  `models.py` for the exclusions and why they are the reason to keep this for 400 days.
- **Read-only readers.** The query path opens its own connection with `query_only`, so a payload
  request cannot write, and a corrupt read cannot take the writer down with it.

The payload keys are the ones `webui/token_usage.py` already served -- `prompt_tokens`,
`provider_tokens`, `sources` -- because the WebUI reads them today and a rename would be a second
change wearing the first one's clothes. The new keys (`failed_requests_30d`, `providers_30d`) are
additions, and the shape is otherwise the same one `SettingsPayload["usage"]` describes.

**A second table, `tool_calls` (#232).** This database is here because it is the one place in the
tree that already answers a question relationally, and "who ran `execute_on_server` yesterday, did
it succeed, how long did it take, and who approved it" was four reads and a correlation by
timestamp across a JSONL transcript, this store, a gate log and a run record. So the tool row
joins this family rather than starting a fifth store. It keeps the three properties above with one
difference: it holds `session_key`, `turn_id` and `seq`, which are the *address* of the arguments
in the session history rather than the arguments, and its window is 180 days rather than 400
because one row per call grows faster than one row per attempt. See `models.ToolCallRecord`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from nanoinfra.llm_usage.models import (
    TOOL_CALL_ERROR_KINDS,
    LLMCallRecord,
    ToolCallRecord,
)

#: 2 adds `tool_calls` and its purge ledger (#232). Both arrive through the same
#: `CREATE TABLE IF NOT EXISTS` script every connection runs, so an existing database with months
#: of usage rows in it gains the table when it is next opened and loses nothing.
SCHEMA_VERSION = 2
#: The provider name a migrated day carries. Named for what it is rather than for a
#: real provider, so a per-model chart cannot claim precision the data never had.
MIGRATED_PROVIDER = "migrated"
#: The same 400 days the JSON store kept, so nothing that was visible stops being visible.
MAX_DAYS_RETAINED = 400
#: A second bound, because 400 days of a busy deployment is a lot of rows and the point of the
#: store is to answer questions quickly rather than to keep everything forever.
MAX_CALLS_RETAINED = 100_000
#: How often a write bothers to prune. Pruning on every row would make the common path pay for the
#: rare one.
_PRUNE_EVERY = 500

#: 180 days for a tool-call row, against 400 for a usage row (#234). One row per call grows far
#: faster than one row per provider attempt, and the window was decided now rather than when
#: somebody's database is 4 GB.
MAX_TOOL_CALL_DAYS_RETAINED = 180
#: How many purge entries the ledger keeps. A purge writes one entry, so this is years of them,
#: and a ledger that grew forever would be the leak the window exists to prevent.
MAX_TOOL_CALL_PURGES_RETAINED = 365

#: Coarse error kinds. A provider's own error *text* is not stored -- it is the one field most
#: likely to quote a prompt back -- so an unrecognised kind becomes `other` rather than being kept
#: verbatim.
_ERROR_KINDS = frozenset({
    "authentication",
    "cancelled",
    "configuration",
    "connection",
    "content_filter",
    "context_length",
    "empty",
    "http",
    "invalid_request",
    "other",
    "overloaded",
    "permission",
    "rate_limit",
    "refusal",
    "server_error",
    "timeout",
})
_FINISH_REASONS = frozenset({
    "cancelled",
    "content_filter",
    "error",
    "function_call",
    "length",
    "refusal",
    "stop",
    "tool_calls",
})

_USAGE_COLUMNS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "total_tokens",
    "provider_tokens",
    "estimated_tokens",
    "generation_ms",
    "measured_completion_tokens",
    "ttft_ms",
    "timed_requests",
)
_REQUEST_COLUMNS = (
    "requests",
    "failed_requests",
    "provider_requests",
    "estimated_requests",
)

# One expression list, used for the day rows, the per-source rows and the per-model rows, so the
# three can never disagree about what a column means.
_AGGREGATE_SQL = """
    COALESCE(SUM(input_tokens), 0) AS prompt_tokens,
    COALESCE(SUM(output_tokens), 0) AS completion_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cached_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(total_tokens), 0) AS total_tokens,
    COALESCE(SUM(reported_tokens), 0) AS provider_tokens,
    COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
    COALESCE(SUM(generation_ms), 0) AS generation_ms,
    COALESCE(SUM(measured_output_tokens), 0) AS measured_completion_tokens,
    COALESCE(SUM(ttft_ms), 0) AS ttft_ms,
    COALESCE(SUM(timed_requests), 0) AS timed_requests,
    COUNT(*) AS requests,
    COALESCE(SUM(
        CASE WHEN finish_reason IN ('error', 'cancelled') THEN 1 ELSE 0 END
    ), 0) AS failed_requests,
    COALESCE(SUM(CASE WHEN reported_tokens > 0 THEN 1 ELSE 0 END), 0) AS provider_requests,
    COALESCE(SUM(
        CASE WHEN estimated_tokens > 0 AND reported_tokens = 0 THEN 1 ELSE 0 END
    ), 0) AS estimated_requests
"""


def _zone(timezone_name: str | None) -> timezone | ZoneInfo:
    if not timezone_name:
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _clean_error_kind(value: str | None) -> str | None:
    """Normalise to a known kind, or `other`. Never the provider's own words."""
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    return cleaned if cleaned in _ERROR_KINDS else "other"


def _clean_finish_reason(value: str) -> str:
    cleaned = value.strip().lower()
    return cleaned if cleaned in _FINISH_REASONS else "other"


def _clean_tool_call_error_kind(value: str | None) -> str | None:
    """The same rule as `_clean_error_kind`, over the tool vocabulary (#232)."""
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    return cleaned if cleaned in TOOL_CALL_ERROR_KINDS else "other"


def _clipped(value: str | None, limit: int) -> str | None:
    """Bound an identifier, and never store blank text where nothing was known."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _clean_status_code(value: int | None) -> int | None:
    """Only a plausible HTTP status. A provider that returned something else says nothing here."""
    if value is None or isinstance(value, bool):
        return None
    return value if 100 <= value <= 599 else None


def _row_ints(row: sqlite3.Row) -> dict[str, int]:
    return {key: int(row[key] or 0) for key in (*_USAGE_COLUMNS, *_REQUEST_COLUMNS)}


def _empty_totals() -> dict[str, int]:
    return {key: 0 for key in (*_USAGE_COLUMNS, *_REQUEST_COLUMNS)}


def _sum_rows(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals = _empty_totals()
    for row in rows:
        for key in totals:
            value = row.get(key)
            if isinstance(value, int):
                totals[key] += value
    return totals


class LLMUsageStore:
    """The attempts, and the aggregates over them. One instance per database path."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        # Recorded so a forked worker does not inherit and share a connection: SQLite handles do
        # not survive a fork, and the gateway's supervisors fork.
        self._connection_pid: int | None = None
        self._writes_since_prune = 0
        # Counted separately from the usage rows (#234): the two tables have different windows and
        # very different write rates, so one counter would prune the busy table on the quiet one's
        # schedule.
        self._tool_call_writes_since_prune = 0

    # --- connections -----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        pid = os.getpid()
        existing = self._connection
        if existing is not None and self._connection_pid == pid:
            return existing
        if existing is not None:
            # Inherited across a fork: closing would corrupt the parent's handle, so it is
            # abandoned rather than closed.
            self._connection = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 250")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY,
                started_at_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                source TEXT NOT NULL,
                stream INTEGER NOT NULL,
                finish_reason TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reported_tokens INTEGER,
                estimated_tokens INTEGER,
                generation_ms INTEGER,
                measured_output_tokens INTEGER,
                ttft_ms INTEGER,
                timed_requests INTEGER,
                error_status_code INTEGER,
                error_kind TEXT
            );
            CREATE INDEX IF NOT EXISTS llm_calls_started_at_idx
                ON llm_calls(started_at_ms);
            CREATE INDEX IF NOT EXISTS llm_calls_provider_model_time_idx
                ON llm_calls(provider, model, started_at_ms);

            CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY,
                ts_ms INTEGER NOT NULL,
                session_key TEXT,
                turn_id TEXT,
                seq INTEGER,
                tool TEXT NOT NULL,
                source TEXT NOT NULL,
                actor TEXT,
                capability_class TEXT,
                gate_decision TEXT,
                gate_reason TEXT,
                outcome TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                error_kind TEXT
            );
            CREATE INDEX IF NOT EXISTS tool_calls_ts_idx
                ON tool_calls(ts_ms);
            -- The two questions the page asks: what did this tool do lately, and what happened in
            -- this turn. `session_key, turn_id, seq` is also the address a reader expands, so the
            -- index that answers "which calls were in this turn" is the same one.
            CREATE INDEX IF NOT EXISTS tool_calls_tool_ts_idx
                ON tool_calls(tool, ts_ms);
            CREATE INDEX IF NOT EXISTS tool_calls_turn_idx
                ON tool_calls(session_key, turn_id, seq);

            CREATE TABLE IF NOT EXISTS tool_call_purges (
                id INTEGER PRIMARY KEY,
                ts_ms INTEGER NOT NULL,
                rows_purged INTEGER NOT NULL,
                cutoff_ms INTEGER NOT NULL
            );
            """
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._connection = connection
        self._connection_pid = pid
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        """A separate, read-only handle. A payload request must not be able to write."""
        self._connect()  # ensure the schema exists before opening read-only
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 250")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        return connection

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            self._connection_pid = None
        if connection is not None:
            with suppress_sqlite():
                connection.close()

    # --- writes ----------------------------------------------------------------------

    def record(self, call: LLMCallRecord) -> None:
        """Append one attempt."""
        self.record_many((call,))

    def record_many(self, calls: Iterable[LLMCallRecord]) -> None:
        rows = [self._as_row(call) for call in calls]
        if not rows:
            return
        with self._lock:
            connection = self._connect()
            connection.executemany(
                """
                INSERT INTO llm_calls (
                    started_at_ms, duration_ms, provider, model, source, stream, finish_reason,
                    input_tokens, output_tokens, total_tokens, cache_read_tokens,
                    cache_write_tokens, reported_tokens, estimated_tokens, generation_ms,
                    measured_output_tokens, ttft_ms, timed_requests, error_status_code, error_kind
                ) VALUES (
                    :started_at_ms, :duration_ms, :provider, :model, :source, :stream,
                    :finish_reason, :input_tokens, :output_tokens, :total_tokens,
                    :cache_read_tokens, :cache_write_tokens, :reported_tokens, :estimated_tokens,
                    :generation_ms, :measured_output_tokens, :ttft_ms, :timed_requests,
                    :error_status_code, :error_kind
                )
                """,
                rows,
            )
            self._writes_since_prune += len(rows)
            if self._writes_since_prune >= _PRUNE_EVERY:
                self._writes_since_prune = 0
                self._prune(connection)

    @staticmethod
    def _as_row(call: LLMCallRecord) -> dict[str, Any]:
        usage = call.usage
        return {
            "started_at_ms": int(call.started_at_ms),
            "duration_ms": int(call.duration_ms),
            "provider": call.provider.strip()[:128],
            "model": call.model.strip()[:256],
            "source": call.source,
            "stream": 1 if call.stream else 0,
            "finish_reason": _clean_finish_reason(call.finish_reason),
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "cache_read_tokens": usage.cache_read_tokens if usage else None,
            "cache_write_tokens": usage.cache_write_tokens if usage else None,
            "reported_tokens": usage.reported_tokens if usage else None,
            "estimated_tokens": usage.estimated_tokens if usage else None,
            "generation_ms": usage.generation_ms if usage else None,
            "measured_output_tokens": usage.measured_output_tokens if usage else None,
            "ttft_ms": usage.ttft_ms if usage else None,
            "timed_requests": usage.timed_requests if usage else None,
            "error_status_code": _clean_status_code(call.error_status_code),
            "error_kind": _clean_error_kind(call.error_kind),
        }

    def _prune(self, connection: sqlite3.Connection) -> None:
        cutoff_ms = int((time.time() - MAX_DAYS_RETAINED * 86_400) * 1000)
        connection.execute("DELETE FROM llm_calls WHERE started_at_ms < ?", (cutoff_ms,))
        connection.execute(
            """
            DELETE FROM llm_calls WHERE id NOT IN (
                SELECT id FROM llm_calls ORDER BY started_at_ms DESC, id DESC LIMIT ?
            )
            """,
            (MAX_CALLS_RETAINED,),
        )

    # --- tool calls ------------------------------------------------------------------

    def record_tool_call(self, call: ToolCallRecord) -> None:
        """Append one tool call. The address of its arguments, never the arguments (#232)."""
        self.record_tool_calls((call,))

    def record_tool_calls(self, calls: Iterable[ToolCallRecord]) -> None:
        rows = [self._as_tool_call_row(call) for call in calls]
        if not rows:
            return
        with self._lock:
            connection = self._connect()
            connection.executemany(
                """
                INSERT INTO tool_calls (
                    ts_ms, session_key, turn_id, seq, tool, source, actor, capability_class,
                    gate_decision, gate_reason, outcome, duration_ms, error_kind
                ) VALUES (
                    :ts_ms, :session_key, :turn_id, :seq, :tool, :source, :actor,
                    :capability_class, :gate_decision, :gate_reason, :outcome, :duration_ms,
                    :error_kind
                )
                """,
                rows,
            )
            self._tool_call_writes_since_prune += len(rows)
            if self._tool_call_writes_since_prune >= _PRUNE_EVERY:
                self._tool_call_writes_since_prune = 0
                self._prune_tool_calls(connection)

    @staticmethod
    def _as_tool_call_row(call: ToolCallRecord) -> dict[str, Any]:
        return {
            "ts_ms": int(call.ts_ms),
            # The address, and the only identifiers in this database. `llm_calls` keeps none,
            # because a day's cost is answerable without them and a call's history is not.
            "session_key": _clipped(call.session_key, 256),
            "turn_id": _clipped(call.turn_id, 128),
            "seq": None if call.seq is None else int(call.seq),
            "tool": call.tool.strip()[:128],
            "source": call.source,
            "actor": _clipped(call.actor, 128),
            "capability_class": _clipped(call.capability_class, 64),
            "gate_decision": _clipped(call.gate_decision, 32),
            "gate_reason": _clipped(call.gate_reason, 240),
            "outcome": call.outcome,
            "duration_ms": int(call.duration_ms),
            "error_kind": _clean_tool_call_error_kind(call.error_kind),
        }

    def _prune_tool_calls(self, connection: sqlite3.Connection, *, now: float | None = None) -> int:
        """Drop rows outside the window and **record how many went** (#234).

        The count is the point. A window that silently deletes makes a gap in the history look
        like a quiet week, and an operator asking why last spring shows no tool calls at all
        deserves an answer from the database rather than from the release notes.
        """
        moment = now if now is not None else time.time()
        cutoff_ms = int((moment - MAX_TOOL_CALL_DAYS_RETAINED * 86_400) * 1000)
        cursor = connection.execute("DELETE FROM tool_calls WHERE ts_ms < ?", (cutoff_ms,))
        purged = int(cursor.rowcount or 0)
        if purged <= 0:
            return 0
        connection.execute(
            "INSERT INTO tool_call_purges (ts_ms, rows_purged, cutoff_ms) VALUES (?, ?, ?)",
            (int(moment * 1000), purged, cutoff_ms),
        )
        connection.execute(
            """
            DELETE FROM tool_call_purges WHERE id NOT IN (
                SELECT id FROM tool_call_purges ORDER BY id DESC LIMIT ?
            )
            """,
            (MAX_TOOL_CALL_PURGES_RETAINED,),
        )
        logger.info(
            "pruned {} tool-call row(s) older than {} days",
            purged,
            MAX_TOOL_CALL_DAYS_RETAINED,
        )
        return purged

    def tool_calls(
        self,
        *,
        limit: int = 200,
        since_ms: int | None = None,
        tool: str | None = None,
        outcome: str | None = None,
        source: str | None = None,
        actor: str | None = None,
        session_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """The rows, newest first. Filters are the ones a reader asks for by name.

        No filter takes free text, and the row carries none either, so there is nothing here to
        search *within* a call -- that answer lives in the transcript the row addresses.
        """
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("ts_ms >= ?", since_ms),
            ("tool = ?", tool),
            ("outcome = ?", outcome),
            ("source = ?", source),
            ("actor = ?", actor),
            ("session_key = ?", session_key),
        ):
            if value is not None:
                clauses.append(column)
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._read_connection()
        try:
            rows = connection.execute(
                f"""
                SELECT ts_ms, session_key, turn_id, seq, tool, source, actor, capability_class,
                       gate_decision, gate_reason, outcome, duration_ms, error_kind
                FROM tool_calls
                {where}
                ORDER BY ts_ms DESC, id DESC
                LIMIT ?
                """,
                (*params, max(0, min(int(limit), 5_000))),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def tool_call_count(self) -> int:
        connection = self._read_connection()
        try:
            row = connection.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()
            return int(row["n"] or 0)
        finally:
            connection.close()

    def tool_call_purges(self) -> dict[str, Any]:
        """How many rows the window has taken, and when it last took any (#234)."""
        connection = self._read_connection()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS purges,
                       COALESCE(SUM(rows_purged), 0) AS rows_purged,
                       MAX(ts_ms) AS last_purge_ms
                FROM tool_call_purges
                """
            ).fetchone()
        finally:
            connection.close()
        last = row["last_purge_ms"]
        return {
            "purges": int(row["purges"] or 0),
            "rows_purged": int(row["rows_purged"] or 0),
            "last_purge_ms": int(last) if last is not None else None,
            "retention_days": MAX_TOOL_CALL_DAYS_RETAINED,
        }

    # --- reads -----------------------------------------------------------------------

    def count(self) -> int:
        connection = self._read_connection()
        try:
            row = connection.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()
            return int(row["n"] or 0)
        finally:
            connection.close()

    def usage_payload(
        self,
        *,
        days: int = 371,
        timezone_name: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """The shape `SettingsPayload["usage"]` already describes, served from the attempts.

        Day boundaries are local, because a heatmap of days is read by somebody in a timezone.
        """
        zone = _zone(timezone_name)
        today = (now or datetime.now(tz=zone)).astimezone(zone).date()
        connection = self._read_connection()
        try:
            day_rows = self._daily_rows(connection, zone=zone, days=max(1, days), today=today)
            all_rows = self._daily_rows(
                connection, zone=zone, days=MAX_DAYS_RETAINED, today=today
            )
            providers = self._provider_rows(connection, zone=zone, days=30, today=today)
        finally:
            connection.close()

        by_date = {row["date"]: row for row in all_rows}
        last_30_start = (today - timedelta(days=29)).isoformat()
        last_365_start = (today - timedelta(days=364)).isoformat()
        last_30 = [row for date_key, row in by_date.items() if date_key >= last_30_start]
        last_365 = [row for date_key, row in by_date.items() if date_key >= last_365_start]
        totals = _sum_rows(all_rows)
        totals_30 = _sum_rows(last_30)
        totals_365 = _sum_rows(last_365)

        active_dates = {
            date.fromisoformat(row["date"])
            for row in all_rows
            if int(row.get("total_tokens") or 0) > 0
        }
        current_streak = 0
        cursor = today
        while cursor in active_dates:
            current_streak += 1
            cursor -= timedelta(days=1)
        longest_streak = 0
        running = 0
        for cursor in sorted(active_dates):
            running = running + 1 if cursor - timedelta(days=1) in active_dates else 1
            longest_streak = max(longest_streak, running)

        latest = max((row["date"] for row in all_rows), default=None)
        return {
            "days": day_rows,
            "total_tokens": totals["total_tokens"],
            "total_tokens_30d": totals_30["total_tokens"],
            "total_tokens_365d": totals_365["total_tokens"],
            "peak_day_tokens": max(
                [int(row.get("total_tokens") or 0) for row in all_rows] or [0]
            ),
            "current_streak_days": current_streak,
            "longest_streak_days": longest_streak,
            "active_days_30d": sum(
                1 for row in last_30 if int(row.get("total_tokens") or 0) > 0
            ),
            "requests_30d": totals_30["requests"],
            # What a day row could not answer, which is the reason this store exists.
            "failed_requests_30d": totals_30["failed_requests"],
            "providers_30d": providers,
            "updated_at": latest,
        }

    def _daily_rows(
        self,
        connection: sqlite3.Connection,
        *,
        zone: timezone | ZoneInfo,
        days: int,
        today: date,
    ) -> list[dict[str, Any]]:
        start_ms = self._midnight_ms(today - timedelta(days=days - 1), zone)
        end_ms = self._midnight_ms(today + timedelta(days=1), zone)
        rows = connection.execute(
            f"""
            SELECT started_at_ms, source, {_AGGREGATE_SQL.replace("COUNT(*)", "COUNT(*)")}
            FROM llm_calls
            WHERE started_at_ms >= ? AND started_at_ms < ?
            GROUP BY {self._day_expression()}, source
            """,
            (start_ms, end_ms),
        ).fetchall()
        by_day: dict[str, dict[str, Any]] = {}
        for row in rows:
            day = self._local_day(int(row["started_at_ms"]), zone)
            aggregate = _row_ints(row)
            day_row = by_day.setdefault(
                day, {"date": day, **_empty_totals(), "sources": {}}
            )
            for key, value in aggregate.items():
                day_row[key] = int(day_row.get(key) or 0) + value
            sources = cast(dict[str, dict[str, int]], day_row["sources"])
            source_key = str(row["source"])
            existing = sources.setdefault(source_key, _empty_totals())
            for key, value in aggregate.items():
                existing[key] += value
        return [by_day[key] for key in sorted(by_day)]

    def _provider_rows(
        self,
        connection: sqlite3.Connection,
        *,
        zone: timezone | ZoneInfo,
        days: int,
        today: date,
    ) -> list[dict[str, Any]]:
        """Per provider and model, which no day row could carry at all.

        Migrated rows are excluded. A migrated row is a whole day of a deployment's
        history folded into one record -- it has no model and its request count is a
        day's worth -- so leaving it in produced a first place of `migrated /
        migrated` with 17.9M tokens against 19 "calls", which invites a reader to
        divide and get nonsense. The day totals still count it; only the per-model
        breakdown, which it cannot answer, drops it.
        """
        start_ms = self._midnight_ms(today - timedelta(days=days - 1), zone)
        rows = connection.execute(
            f"""
            SELECT provider, model, {_AGGREGATE_SQL}
            FROM llm_calls
            WHERE started_at_ms >= ? AND provider != '{MIGRATED_PROVIDER}'
            GROUP BY provider, model
            ORDER BY total_tokens DESC
            LIMIT 24
            """,
            (start_ms,),
        ).fetchall()
        return [
            {"provider": str(row["provider"]), "model": str(row["model"]), **_row_ints(row)}
            for row in rows
        ]

    @staticmethod
    def _day_expression() -> str:
        # Grouped in Python by local day rather than in SQL, because SQLite has no timezone
        # database: the GROUP BY here only has to be finer than a day, and the row's own
        # timestamp is re-read to place it.
        return "started_at_ms / 3600000"

    @staticmethod
    def _local_day(started_at_ms: int, zone: timezone | ZoneInfo) -> str:
        return (
            datetime.fromtimestamp(started_at_ms / 1000, tz=timezone.utc)
            .astimezone(zone)
            .date()
            .isoformat()
        )

    @staticmethod
    def _midnight_ms(value: date, zone: timezone | ZoneInfo) -> int:
        return int(
            datetime(value.year, value.month, value.day, tzinfo=zone).timestamp() * 1000
        )


class suppress_sqlite:  # noqa: N801 - a context manager used as a statement, not a class
    """Swallow a close-time SQLite error. A telemetry store must not break a shutdown."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is None:
            return False
        if isinstance(exc, sqlite3.Error):
            logger.debug("llm usage store close failed: {}", exc)
            return True
        return False


__all__ = [
    "LLMUsageStore",
    "MAX_CALLS_RETAINED",
    "MAX_DAYS_RETAINED",
    "MAX_TOOL_CALL_DAYS_RETAINED",
    "MAX_TOOL_CALL_PURGES_RETAINED",
    "SCHEMA_VERSION",
]
