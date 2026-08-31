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

from nanoinfra.llm_usage.models import LLMCallRecord

SCHEMA_VERSION = 1
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
    "SCHEMA_VERSION",
]
