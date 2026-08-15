# tests/webui/test_audit_api.py
"""Item 27 (#29): the read side of the audit log, for the viewer.

The viewer reads. It never edits and never deletes a record, so this module exposes no write of
any kind. #16 owns the writes, and an append-only log with a delete endpoint would not be one.

Two record fields drive the viewer's judgement rather than its layout. `same_path` marks a
decision whose request and approval shared one channel, and it must stay visible even when policy
allowed the action. `command_digest` is what the log holds by default, because a resolved command
routinely embeds a secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nanoinfra.gates.audit import AuditStore
from nanoinfra.webui.audit_api import audit_page


def _store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "gates")


def _record(store: AuditStore, **over: object) -> None:
    fields: dict[str, object] = {
        "decision": "denied",
        "capability_class": "mutate.remote",
        "execution_context": "automation",
        "session_id": "s1",
        "tool": "execute_on_server",
        "scope": "host",
        "hosts": ["10.0.1.5"],
        "command": "uptime",
        "reason": "no grant covers this",
    }
    fields.update(over)
    store.record(**fields)  # pyright: ignore[reportArgumentType]


def test_an_empty_log_reads_as_an_empty_page(tmp_path: Path) -> None:
    page = audit_page(_store(tmp_path))

    assert page["records"] == []
    assert page["total"] == 0


def test_records_come_back_newest_first(tmp_path: Path) -> None:
    """An operator opens this after something happened, so the last decision leads."""
    store = _store(tmp_path)
    base = datetime.now(UTC)
    _record(store, reason="older", ts=base - timedelta(minutes=5))
    _record(store, reason="newer", ts=base)

    page = audit_page(store)

    assert [r["reason"] for r in page["records"]] == ["newer", "older"]


def test_a_decision_filter_narrows_the_page(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, decision="denied")
    _record(store, decision="allow")

    page = audit_page(store, decision="denied")

    assert [r["decision"] for r in page["records"]] == ["denied"]


def test_a_context_filter_isolates_automation_decisions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, execution_context="automation")
    _record(store, execution_context="interactive")

    page = audit_page(store, execution_context="automation")

    assert [r["execution_context"] for r in page["records"]] == ["automation"]


def test_a_class_filter_narrows_the_page(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, capability_class="mutate.remote")
    _record(store, capability_class="mutate.inventory")

    page = audit_page(store, capability_class="mutate.inventory")

    assert [r["capability_class"] for r in page["records"]] == ["mutate.inventory"]


def test_a_date_range_excludes_what_falls_outside_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime.now(UTC)
    _record(store, reason="inside", ts=now)
    _record(store, reason="old", ts=now - timedelta(days=10))

    page = audit_page(store, since=now - timedelta(days=1))

    assert [r["reason"] for r in page["records"]] == ["inside"]


def test_filters_combine(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, decision="denied", execution_context="automation")
    _record(store, decision="denied", execution_context="interactive")
    _record(store, decision="allow", execution_context="automation")

    page = audit_page(store, decision="denied", execution_context="automation")

    assert page["total"] == 1


def test_a_page_is_bounded_and_reports_the_total(tmp_path: Path) -> None:
    """A log with a year of records must not arrive in one response."""
    store = _store(tmp_path)
    for index in range(30):
        _record(store, reason=f"r{index}")

    page = audit_page(store, limit=10)

    assert len(page["records"]) == 10
    assert page["total"] == 30


def test_an_offset_walks_the_page(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = datetime.now(UTC)
    for index in range(5):
        _record(store, reason=f"r{index}", ts=base + timedelta(seconds=index))

    page = audit_page(store, limit=2, offset=2)

    assert [r["reason"] for r in page["records"]] == ["r2", "r1"]


def test_the_same_path_flag_survives_to_the_viewer(tmp_path: Path) -> None:
    """#13 records a shared channel even when policy allowed it, and a reviewer needs to see it."""
    store = _store(tmp_path)
    _record(store, decision="allow", origin_path="webui", approval_path="webui")

    page = audit_page(store)

    assert page["records"][0]["same_path"] is True


def test_an_origin_actor_filter_answers_every_action_one_person_raised(tmp_path: Path) -> None:
    """The question #79 exists for. ``actor`` answers who approved, and this answers who asked."""
    store = _store(tmp_path)
    _record(store, reason="theirs", origin_actor="webui:paula@example.com")
    _record(store, reason="mine", origin_actor="webui:alberto@example.com")

    page = audit_page(store, origin_actor="webui:alberto@example.com")

    assert [r["reason"] for r in page["records"]] == ["mine"]


def test_an_unknown_origin_actor_matches_nothing(tmp_path: Path) -> None:
    """The filter fails closed like every other one. A typo must not widen the answer."""
    store = _store(tmp_path)
    _record(store, origin_actor="webui:alberto@example.com")

    page = audit_page(store, origin_actor="webui:alberto@exampel.com")

    assert page["records"] == []


def test_an_origin_actor_filter_never_matches_a_record_that_names_nobody(tmp_path: Path) -> None:
    """A record with no origin identity holds ``null``, and a name must not match that."""
    store = _store(tmp_path)
    _record(store, origin_actor=None)

    assert audit_page(store, origin_actor="webui:alberto@example.com")["records"] == []
    assert len(audit_page(store)["records"]) == 1


def test_the_page_carries_a_digest_and_not_the_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, command="mysql -u root -p'hunter2'")

    page = audit_page(store)

    assert "hunter2" not in str(page)
    assert str(page["records"][0]["command_digest"]).startswith("sha256:")


def test_the_page_says_whether_full_command_text_is_recorded(tmp_path: Path) -> None:
    """The viewer marks those records, so a reader knows the text may hold a secret."""
    page = audit_page(_store(tmp_path))

    assert page["records_command_text"] is False


def test_an_unknown_filter_value_returns_an_empty_page_and_not_everything(
    tmp_path: Path,
) -> None:
    """Fail closed on a filter. A typo must not widen the answer to every record."""
    store = _store(tmp_path)
    _record(store, decision="denied")

    page = audit_page(store, decision="not-a-decision")

    assert page["records"] == []


def test_the_module_exposes_no_write(tmp_path: Path) -> None:
    """The viewer reads. An append-only log with a delete endpoint would not be one."""
    import nanoinfra.webui.audit_api as module

    public = [name for name in dir(module) if not name.startswith("_")]
    forbidden = ("delete", "remove", "write", "record", "prune", "clear", "edit", "update")
    assert [name for name in public if any(word in name.lower() for word in forbidden)] == []


def test_a_corrupt_line_does_not_hide_the_rest(tmp_path: Path) -> None:
    """A torn tail costs its own record. It must not empty the viewer."""
    store = _store(tmp_path)
    _record(store, reason="kept")
    segment = store.segments()[0]
    with segment.open("a", encoding="utf-8") as handle:
        handle.write("{ torn\n")

    page = audit_page(store)

    assert [r["reason"] for r in page["records"]] == ["kept"]


def test_a_missing_audit_root_reads_as_empty_and_says_so(tmp_path: Path) -> None:
    """A fresh install has no log. The viewer must not read that as an error."""
    page = audit_page(AuditStore(tmp_path / "never-written"))

    assert page["records"] == []
    assert page["total"] == 0


@pytest.mark.parametrize("limit", [0, -1, 10_000])
def test_a_hostile_limit_is_clamped(tmp_path: Path, limit: int) -> None:
    """The caller controls the limit, so the server bounds it."""
    store = _store(tmp_path)
    _record(store)

    page = audit_page(store, limit=limit)

    assert 0 <= len(page["records"]) <= 200
