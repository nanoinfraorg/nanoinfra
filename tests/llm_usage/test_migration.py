"""The history comes across (#177).

Upstream shipped the same store with no migration, which drops every day row a deployment had.
The acceptance criterion from the issue: *a day present in the JSON is present after the migration,
with its token totals intact and its per-attempt fields at zero.*
"""

from __future__ import annotations

import json
from pathlib import Path

from nanoinfra.llm_usage.migration import MIGRATION_MARKER, migrate_token_usage_json
from nanoinfra.llm_usage.store import LLMUsageStore

_OLD_STATE = {
    "schema_version": 1,
    "updated_at": "2026-06-03T00:00:00+00:00",
    "days": {
        "2026-06-02": {
            "date": "2026-06-02",
            "prompt_tokens": 1_000,
            "completion_tokens": 200,
            "cached_tokens": 400,
            "total_tokens": 1_200,
            "provider_tokens": 1_200,
            "estimated_tokens": 0,
            "requests": 4,
            "provider_requests": 4,
            "estimated_requests": 0,
            "sources": {
                "user": {
                    "prompt_tokens": 900,
                    "completion_tokens": 180,
                    "cached_tokens": 400,
                    "total_tokens": 1_080,
                    "provider_tokens": 1_080,
                    "estimated_tokens": 0,
                    "requests": 3,
                },
                "cron": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cached_tokens": 0,
                    "total_tokens": 120,
                    "provider_tokens": 120,
                    "estimated_tokens": 0,
                    "requests": 1,
                },
            },
        },
        "2026-06-03": {
            "date": "2026-06-03",
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "total_tokens": 55,
            "estimated_tokens": 55,
            "requests": 1,
            "estimated_requests": 1,
        },
    },
}


def _write_old(tmp_path: Path, state: object = _OLD_STATE) -> Path:
    path = tmp_path / "token-usage.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_a_day_in_the_json_is_a_day_in_the_store(tmp_path: Path) -> None:
    """The acceptance criterion, as written in the issue."""
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")

    outcome = migrate_token_usage_json(_write_old(tmp_path), store)

    assert outcome == {"days": 2, "records": 3}
    payload = store.usage_payload(timezone_name="UTC")
    by_date = {row["date"]: row for row in payload["days"]}
    assert by_date["2026-06-02"]["total_tokens"] == 1_200
    assert by_date["2026-06-02"]["prompt_tokens"] == 1_000
    assert by_date["2026-06-02"]["cached_tokens"] == 400
    assert by_date["2026-06-03"]["total_tokens"] == 55


def test_what_was_never_measured_is_zero_rather_than_invented(tmp_path: Path) -> None:
    """A zero says nothing was recorded, which is what happened. Attempts cannot be
    reconstructed from a daily sum, and a fresh database says the same thing by deleting the
    evidence."""
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")

    migrate_token_usage_json(_write_old(tmp_path), store)

    day = {row["date"]: row for row in store.usage_payload(timezone_name="UTC")["days"]}[
        "2026-06-02"
    ]
    assert day["ttft_ms"] == 0
    assert day["generation_ms"] == 0
    assert day["measured_completion_tokens"] == 0
    assert day["timed_requests"] == 0
    assert day["cache_write_tokens"] == 0


def test_the_per_source_breakdown_survives(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")

    migrate_token_usage_json(_write_old(tmp_path), store)

    sources = {row["date"]: row for row in store.usage_payload(timezone_name="UTC")["days"]}[
        "2026-06-02"
    ]["sources"]
    assert sources["user"]["total_tokens"] == 1_080
    assert sources["cron"]["total_tokens"] == 120


def test_an_estimated_day_stays_estimated(tmp_path: Path) -> None:
    """The partition is the thing #174 made real, so a migration that lost it would undo that."""
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")

    migrate_token_usage_json(_write_old(tmp_path), store)

    day = {row["date"]: row for row in store.usage_payload(timezone_name="UTC")["days"]}[
        "2026-06-03"
    ]
    assert day["estimated_tokens"] == 55
    assert day["provider_tokens"] == 0


def test_a_day_with_no_source_breakdown_becomes_a_user_row(tmp_path: Path) -> None:
    """The first version of the old file had no sources, and it only recorded person-started
    turns."""
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    path = _write_old(tmp_path, {"days": {"2026-05-01": {"total_tokens": 10, "requests": 1}}})

    migrate_token_usage_json(path, store)

    day = store.usage_payload(timezone_name="UTC")["days"][0]
    assert set(day["sources"]) == {"user"}


def test_the_json_is_set_aside_rather_than_left_to_disagree(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    path = _write_old(tmp_path)

    migrate_token_usage_json(path, store)

    assert not path.exists()
    # Renamed, not unlinked: the numbers were the only copy, and a migration that got the
    # arithmetic wrong should be recoverable by somebody who notices next week.
    assert (tmp_path / MIGRATION_MARKER).exists()


def test_running_it_twice_does_not_double_the_history(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    path = _write_old(tmp_path)

    migrate_token_usage_json(path, store)
    second = migrate_token_usage_json(path, store)

    assert second == {"days": 0, "records": 0}
    assert store.count() == 3


def test_a_malformed_day_key_is_dropped_rather_than_carried(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    path = _write_old(
        tmp_path,
        {"days": {"not-a-dat3": {"total_tokens": 7, "requests": 1}}},
    )

    migrate_token_usage_json(path, store)

    assert store.count() == 0


def test_an_unreadable_file_is_left_alone(tmp_path: Path) -> None:
    """A migration that cannot read the file must not delete it."""
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    path = tmp_path / "token-usage.json"
    path.write_text("{not json", encoding="utf-8")

    outcome = migrate_token_usage_json(path, store)

    assert outcome == {"days": 0, "records": 0}
    assert path.exists()


def test_no_file_is_not_an_error(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")

    assert migrate_token_usage_json(tmp_path / "absent.json", store) == {
        "days": 0,
        "records": 0,
    }
