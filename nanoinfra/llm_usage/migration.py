"""Bring the day rows across instead of starting again (#177).

Upstream shipped this store with no migration, so every day row a deployment had accumulated
stopped being visible the moment it upgraded. We will not do that. The old store is
`~/.nanoinfra/webui/token-usage.json`, its fields map onto this schema almost one to one, and the
ones that were never measured get a **zero**.

A zero is the honest value there. It says *nothing was recorded*, which is what happened: per-attempt
rows, cache writes, `ttft_ms` and `generation_ms` cannot be reconstructed after the fact from a
daily sum. The alternative -- a fresh database -- makes exactly the same statement by deleting the
evidence, and takes the token totals with it.

One row per (day, source), written at local midday of that day. Midday rather than midnight because
the store groups rows into local days when it reads them, and a timestamp at a boundary lands in a
different day for any reader whose timezone moved since. Midday survives a change of up to eleven
hours in either direction.

The JSON is deleted in the same step. Two stores that can disagree is worse than either of them.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanoinfra.llm_usage.models import LLM_USAGE_SOURCES, LLMCallRecord
from nanoinfra.llm_usage.store import LLMUsageStore
from nanoinfra.providers.base import LLMUsage

#: The marker file. Written beside the database so a second start does not migrate a JSON file that
#: a previous start already consumed -- and so an operator can see that it happened.
MIGRATION_MARKER = "token-usage-migrated.json"

#: How the old file spelled things. Kept here rather than imported, because the module that owns
#: those names is the one this migration exists to retire.
_OLD_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "total_tokens",
    "provider_tokens",
    "estimated_tokens",
)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _records_for_day(day: str, row: dict[str, Any]) -> list[LLMCallRecord]:
    """One record per source on one day, or one for the day when it named no sources."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        # A malformed day key. The old store pruned these on rewrite; this one does not inherit
        # them, because a row that cannot be placed on a calendar cannot be charted either.
        return []
    started_at_ms = int(
        datetime.combine(parsed, time(12, 0), tzinfo=timezone.utc).timestamp() * 1000
    )

    sources = row.get("sources")
    per_source: dict[str, dict[str, Any]] = {}
    if isinstance(sources, dict):
        for key, value in cast(dict[str, Any], sources).items():
            if key in LLM_USAGE_SOURCES and isinstance(value, dict):
                per_source[key] = cast(dict[str, Any], value)
    if not per_source:
        # The rows the very first version wrote had no per-source breakdown. `user` rather than
        # `system`, because that version only recorded turns a person had started.
        per_source = {"user": row}

    records: list[LLMCallRecord] = []
    for source, values in sorted(per_source.items()):
        total = _int(values.get("total_tokens"))
        prompt = _int(values.get("prompt_tokens"))
        completion = _int(values.get("completion_tokens"))
        if total <= 0:
            total = prompt + completion
        if total <= 0:
            continue
        estimated = min(_int(values.get("estimated_tokens")), total)
        reported = _int(values.get("provider_tokens"))
        # The partition has to exhaust the total or the type refuses the value. The old file
        # reconstructed it heuristically on read, and this is the same reconciliation done once,
        # here, at the point the numbers stop being heuristic.
        if reported + estimated != total:
            reported = max(0, total - estimated)
        cached = _int(values.get("cached_tokens"))
        usage = LLMUsage(
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=total,
            # Present rather than `None`: the old store did record a cache read, so zero here
            # means it recorded zero. `cache_write_tokens` stays `None` -- that metric never
            # existed in the old file, and a zero would claim it had been measured.
            cache_read_tokens=min(cached, prompt),
            cache_write_tokens=None,
            reported_tokens=reported,
            estimated_tokens=estimated,
            # Never measured, and unreconstructable from a daily sum.
            generation_ms=0,
            measured_output_tokens=0,
            ttft_ms=0,
            timed_requests=0,
            context_tokens=None,
            request_count=max(1, _int(values.get("requests"))),
        )
        records.append(
            LLMCallRecord(
                started_at_ms=started_at_ms,
                duration_ms=0,
                # Named for what it is. A migrated day is not one attempt against one model, and
                # labelling it with a real provider would let a per-model chart claim precision
                # this data never had.
                provider="migrated",
                model="migrated",
                source=cast(Any, source),
                stream=False,
                finish_reason="stop",
                usage=usage,
            )
        )
    return records


def migrate_token_usage_json(
    json_path: Path,
    store: LLMUsageStore,
    *,
    delete_after: bool = True,
) -> dict[str, int]:
    """Read the old day rows into *store* and remove the file. Idempotent by deletion.

    Returns what happened, for the log line: a migration nobody can see is one nobody trusts.
    """
    if not json_path.exists():
        return {"days": 0, "records": 0}
    try:
        raw = cast(dict[str, Any], json.loads(json_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("could not read {} to migrate it: {}", json_path, exc)
        return {"days": 0, "records": 0}

    days = raw.get("days")
    if not isinstance(days, dict):
        days = {}
    records: list[LLMCallRecord] = []
    for day, row in sorted(cast(dict[str, Any], days).items()):
        if isinstance(row, dict):
            records.extend(_records_for_day(day, cast(dict[str, Any], row)))

    if records:
        store.record_many(records)
    if delete_after:
        try:
            # Renamed rather than unlinked: the numbers are the only copy, and a migration that
            # got the arithmetic wrong should be recoverable by somebody who notices next week.
            json_path.replace(json_path.with_name(MIGRATION_MARKER))
        except OSError as exc:
            logger.warning("migrated {} but could not set it aside: {}", json_path, exc)
    outcome = {"days": len(cast(dict[str, Any], days)), "records": len(records)}
    if records:
        logger.info(
            "migrated {} day(s) of token usage into the call store as {} row(s)",
            outcome["days"],
            outcome["records"],
        )
    return outcome


__all__ = ["MIGRATION_MARKER", "migrate_token_usage_json"]
