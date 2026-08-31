"""A failure has to be retractable (found on the demo).

`merged()` ignores an empty value on purpose -- a write that does not know who a connector acts as
must not delete the answer a previous one found. That rule is right for a fact and wrong for a
failure, and it made `last_error` permanent: a connector fixed two releases ago kept showing the
error that no longer happens, in red, under a row that works.
"""

from __future__ import annotations

from pathlib import Path

from nanoinfra.connectors import state as connector_state


def test_an_empty_value_still_does_not_erase_a_fact(tmp_path: Path) -> None:
    connector_state.record(tmp_path, "google-calendar", acts_as="ops@example.test")

    connector_state.record(tmp_path, "google-calendar", tested_at="2026-08-31T00:00:00Z")

    assert connector_state.read_one(tmp_path, "google-calendar").acts_as == "ops@example.test"


def test_a_success_retracts_the_last_failure(tmp_path: Path) -> None:
    connector_state.record(
        tmp_path, "google-calendar", last_error="Permission denied", last_error_at="then"
    )

    connector_state.record(
        tmp_path,
        "google-calendar",
        tested_at="2026-08-31T00:00:00Z",
        test_summary="list_events returned 3 items",
        clear=("last_error", "last_error_at"),
    )

    recorded = connector_state.read_one(tmp_path, "google-calendar")
    assert recorded.last_error == ""
    assert recorded.last_error_at == ""
    assert recorded.test_summary == "list_events returned 3 items"


def test_passing_an_empty_string_does_not_clear_it(tmp_path: Path) -> None:
    """The trap: every caller already passed `last_error=""` and every one was ignored, so the
    field looked like it was being cleared and never was. Naming the field is the difference
    between "I have nothing to say about this" and "this no longer applies"."""
    connector_state.record(tmp_path, "google-calendar", last_error="Permission denied")

    connector_state.record(tmp_path, "google-calendar", last_error="")

    assert connector_state.read_one(tmp_path, "google-calendar").last_error == "Permission denied"


def test_clearing_a_field_that_is_not_recorded_is_harmless(tmp_path: Path) -> None:
    connector_state.record(tmp_path, "google-calendar", clear=("last_error",))

    assert connector_state.read_one(tmp_path, "google-calendar").last_error == ""
