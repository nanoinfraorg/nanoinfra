"""The content-free LLM usage backend (#176).

One store per path, one observer callback, and both fail open: telemetry that can break a turn is
worse than telemetry that is missing a row. Every entry point here swallows its own exceptions and
logs, because the caller is a provider in the middle of answering somebody.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.config.paths import get_data_dir
from nanoinfra.llm_usage.models import LLMCallRecord
from nanoinfra.llm_usage.store import LLMUsageStore

_STORES_LOCK = threading.Lock()
_STORES: dict[Path, LLMUsageStore] = {}


def llm_usage_store_path() -> Path:
    return get_data_dir() / "llm-usage.sqlite3"


def get_llm_usage_store(path: Path | None = None) -> LLMUsageStore:
    """The store for one path, created once per process."""
    resolved = (path or llm_usage_store_path()).resolve(strict=False)
    with _STORES_LOCK:
        store = _STORES.get(resolved)
        if store is None:
            store = LLMUsageStore(resolved)
            _STORES[resolved] = store
        return store


def reset_llm_usage_stores() -> None:
    """Drop every cached store. For tests, and for a process that changed its data directory."""
    with _STORES_LOCK:
        stores = list(_STORES.values())
        _STORES.clear()
    for store in stores:
        store.close()


def migrate_legacy_token_usage() -> dict[str, int]:
    """Bring `~/.nanoinfra/webui/token-usage.json` into the call store, once (#177).

    Called at gateway start, before anything reads the payload, so an upgrade never shows an empty
    heatmap for a deployment that had months of history. Idempotent because the migration sets the
    old file aside: a second start finds nothing to do.
    """
    from nanoinfra.config.paths import get_webui_dir
    from nanoinfra.llm_usage.migration import migrate_token_usage_json

    try:
        return migrate_token_usage_json(
            get_webui_dir() / "token-usage.json",
            get_llm_usage_store(),
        )
    except Exception:
        logger.exception("failed to migrate the legacy token usage file")
        return {"days": 0, "records": 0}


def record_llm_call(call: LLMCallRecord) -> None:
    """The observer the gateway attaches to every provider. Fails open, by design."""
    try:
        get_llm_usage_store().record(call)
    except Exception:
        logger.exception("failed to record an LLM call")


def empty_usage_payload() -> dict[str, Any]:
    """What a caller gets when the store cannot be read: zeros, not an exception.

    A Settings page that fails to render because telemetry is unavailable would be a worse outcome
    than a page showing nothing was recorded.
    """
    return {
        "days": [],
        "total_tokens": 0,
        "total_tokens_30d": 0,
        "total_tokens_365d": 0,
        "peak_day_tokens": 0,
        "current_streak_days": 0,
        "longest_streak_days": 0,
        "active_days_30d": 0,
        "requests_30d": 0,
        "failed_requests_30d": 0,
        "providers_30d": [],
        "updated_at": None,
    }


def llm_usage_payload(
    *,
    days: int = 371,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    try:
        return get_llm_usage_store().usage_payload(days=days, timezone_name=timezone_name)
    except Exception:
        logger.exception("failed to query LLM usage")
        return empty_usage_payload()


__all__ = [
    "LLMCallRecord",
    "migrate_legacy_token_usage",
    "LLMUsageStore",
    "empty_usage_payload",
    "get_llm_usage_store",
    "llm_usage_payload",
    "llm_usage_store_path",
    "record_llm_call",
    "reset_llm_usage_stores",
]
