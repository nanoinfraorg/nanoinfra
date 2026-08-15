"""Tests for the append-only gate audit store -- nanoinfraorg/nanoinfra#16."""

from __future__ import annotations

import ast
import multiprocessing
import stat
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import nanoinfra.gates.audit as audit_module
from nanoinfra.agent.tools.capabilities import command_digest
from nanoinfra.config.gates import AuditConfig
from nanoinfra.gates.audit import AuditStore

# One fixed instant, so every concurrent writer targets the same segment file. One shared
# file is the point of the test.
_FIXED_TS = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

# Every line carries the same keys, so a reader can load the log into a table.
# `record_id` names the record, and `follows` names the record a completion follows (#46).
_EXPECTED_KEYS = {
    "ts",
    "record_id",
    "follows",
    "session_id",
    "execution_context",
    "origin_path",
    "approval_path",
    "same_path",
    "actor",
    "capability_class",
    "scope",
    "hosts",
    "host_count",
    "secret_ref",
    "command_digest",
    "decision",
    "reason",
    "grant_id",
    "approval_id",
    "token_nonce",
    "exit_code",
    "duration_ms",
    "tool",
}


def _store(
    root: Path,
    *,
    retention_days: int = 90,
    record_command_text: bool = False,
) -> AuditStore:
    return AuditStore(
        root / "gates" / "audit",
        config=AuditConfig(
            retention_days=retention_days,
            record_command_text=record_command_text,
        ),
    )


def test_record_writes_one_json_line_per_decision(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record(decision="deny", capability_class="mutate.remote", execution_context="automation")
    store.record(decision="allow", capability_class="read", execution_context="interactive")

    segments = store.segments()
    assert len(segments) == 1
    lines = segments[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [r["decision"] for r in store.read_all()] == ["deny", "allow"]


def test_record_carries_every_field_of_the_spec(tmp_path: Path) -> None:
    store = _store(tmp_path)

    written = store.record(
        decision="approve",
        capability_class="credential.access",
        execution_context="interactive",
        tool="execute_on_server",
        session_id="telegram:42",
        origin_path="telegram:42",
        approval_path="telegram:42",
        actor="telegram:7",
        scope="host",
        hosts=["db-1"],
        secret_ref="6f1c1d3e9b8a4f52",
        command="psql -h db-1",
        reason="policy=approve",
        grant_id="nightly-backup",
        approval_id="4d1f8c2b",
        token_nonce="abc123",
        exit_code=0,
        duration_ms=1234,
    )

    assert set(written) == _EXPECTED_KEYS
    assert store.read_all() == [written]


def test_absent_fields_stay_present_as_null(tmp_path: Path) -> None:
    store = _store(tmp_path)

    written = store.record(
        decision="deny", capability_class="mutate.remote", execution_context="subagent"
    )

    assert set(written) == _EXPECTED_KEYS
    assert written["grant_id"] is None
    assert written["exit_code"] is None
    assert written["hosts"] == []
    assert written["host_count"] == 0


def test_ts_is_utc_and_sortable(tmp_path: Path) -> None:
    store = _store(tmp_path)

    written = store.record(
        decision="deny",
        capability_class="mutate.remote",
        execution_context="automation",
        ts=datetime(2026, 8, 14, 9, 30, tzinfo=UTC),
    )

    assert written["ts"] == "2026-08-14T09:30:00+00:00"


def test_host_count_is_derived_and_cannot_disagree_with_hosts(tmp_path: Path) -> None:
    store = _store(tmp_path)

    written = store.record(
        decision="deny",
        capability_class="mutate.remote",
        execution_context="automation",
        hosts=("web-1", "web-2", "web-3"),
    )

    assert written["hosts"] == ["web-1", "web-2", "web-3"]
    assert written["host_count"] == 3


def test_same_path_is_derived_from_the_two_paths(tmp_path: Path) -> None:
    store = _store(tmp_path)

    same = store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="interactive",
        origin_path="telegram:42",
        approval_path="telegram:42",
    )
    other = store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="interactive",
        origin_path="telegram:42",
        approval_path="slack:ops",
    )
    unapproved = store.record(
        decision="deny",
        capability_class="mutate.remote",
        execution_context="automation",
        origin_path="cron:nightly",
    )

    assert same["same_path"] is True
    assert other["same_path"] is False
    # No approval arrived, so the record must not claim an out-of-band path.
    assert unapproved["same_path"] is None


def test_a_denial_appears_in_the_log(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record(
        decision="deny",
        capability_class="mutate.remote",
        execution_context="automation",
        reason="unattended policy denies scope=group",
        scope="group",
    )

    records = store.read_all()
    assert len(records) == 1
    assert records[0]["decision"] == "deny"
    assert records[0]["reason"] == "unattended policy denies scope=group"


def test_an_expiry_and_a_latched_refusal_appear_in_the_log(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record(
        decision="expired",
        capability_class="mutate.remote",
        execution_context="interactive",
        reason="approval token expired",
        token_nonce="nonce-1",
    )
    store.record(
        decision="latched_refusal",
        capability_class="mutate.remote",
        execution_context="interactive",
        reason="the resolved action changed after approval",
        token_nonce="nonce-1",
    )

    assert [r["decision"] for r in store.read_all()] == ["expired", "latched_refusal"]


def test_a_later_record_never_rewrites_an_earlier_one(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record(decision="deny", capability_class="read", execution_context="automation")
    segment = store.segments()[0]
    first_bytes = segment.read_bytes()

    for index in range(20):
        store.record(
            decision="allow",
            capability_class="read",
            execution_context="automation",
            reason=f"call {index}",
        )

    # The first record must stay byte-identical at the head of the file. A
    # read-modify-write store cannot promise this.
    assert segment.read_bytes().startswith(first_bytes)
    assert len(store.read_all()) == 21


def test_the_command_text_stays_out_of_the_log_by_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret_command = "mysql -h db-1 -u root -pPLACEHOLDER_SECRET_VALUE"

    written = store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="automation",
        command=secret_command,
    )

    assert "command_text" not in written
    assert written["command_digest"] == command_digest(secret_command)
    # The bytes on disk are the real test. A record that never held the text still
    # leaks it if some other field carries it.
    raw = store.segments()[0].read_bytes()
    assert b"PLACEHOLDER_SECRET_VALUE" not in raw
    assert b"mysql" not in raw


def test_the_command_text_is_recorded_only_under_the_opt_in(tmp_path: Path) -> None:
    store = _store(tmp_path, record_command_text=True)

    written = store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="interactive",
        command="systemctl restart nginx",
    )

    assert written["command_text"] == "systemctl restart nginx"
    assert written["command_digest"] == command_digest("systemctl restart nginx")
    assert store.read_all()[0]["command_text"] == "systemctl restart nginx"


def test_a_precomputed_digest_is_kept_and_adds_no_text(tmp_path: Path) -> None:
    store = _store(tmp_path, record_command_text=True)

    written = store.record(
        decision="deny",
        capability_class="mutate.remote",
        execution_context="automation",
        command_digest="sha256:deadbeef",
    )

    assert written["command_digest"] == "sha256:deadbeef"
    assert "command_text" not in written


def test_the_resolved_command_wins_over_a_supplied_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)

    written = store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="automation",
        command="uptime",
        command_digest="sha256:wrong",
    )

    assert written["command_digest"] == command_digest("uptime")


def test_prune_deletes_only_the_segments_outside_retention(tmp_path: Path) -> None:
    # Seed through a store whose retention outlives every segment below, so they survive
    # until the explicit pass. A live store trims on rotation instead. Retention cannot be
    # zero any more: the schema refuses a value that disables retention silently.
    seed = _store(tmp_path, retention_days=36500)
    store = _store(tmp_path, retention_days=30)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    for age_days in (200, 31, 29, 0):
        seed.record(
            decision="deny",
            capability_class="read",
            execution_context="automation",
            reason=f"age {age_days}",
            ts=now - timedelta(days=age_days),
        )
    assert len(store.segments()) == 4

    removed = store.prune(now=now)

    assert [path.name for path in removed] == ["gate-2026-01-26.jsonl", "gate-2026-07-14.jsonl"]
    assert [r["reason"] for r in store.read_all()] == ["age 29", "age 0"]


def test_prune_leaves_the_surviving_records_untouched(tmp_path: Path) -> None:
    seed = _store(tmp_path, retention_days=36500)
    store = _store(tmp_path, retention_days=30)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    seed.record(
        decision="deny",
        capability_class="read",
        execution_context="automation",
        ts=now - timedelta(days=200),
    )
    seed.record(
        decision="allow",
        capability_class="read",
        execution_context="automation",
        ts=now,
    )
    survivor = store.root / f"gate-{now.date().isoformat()}.jsonl"
    before_bytes = survivor.read_bytes()
    before_mtime = survivor.stat().st_mtime_ns

    assert len(store.prune(now=now)) == 1

    # Retention must not rewrite a record it keeps. An auditor may hold a hash of it.
    assert survivor.read_bytes() == before_bytes
    assert survivor.stat().st_mtime_ns == before_mtime


def test_prune_keeps_everything_when_retention_is_not_positive(tmp_path: Path) -> None:
    """The fail-safe branch, which config can no longer reach.

    The schema now refuses a retention below one day, because a hand-edited zero turned
    retention off with no message. The branch stays as defence: a caller that builds the config
    without validation must still not empty an audit log by accident. So the test skips
    validation on purpose.
    """
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    for retention_days in (0, -1):
        store = AuditStore(
            tmp_path / str(retention_days) / "gates" / "audit",
            config=AuditConfig.model_construct(
                retention_days=retention_days, record_command_text=False
            ),
        )
        store.record(
            decision="deny",
            capability_class="read",
            execution_context="automation",
            ts=now - timedelta(days=5000),
        )

        assert store.prune(now=now) == []
        assert len(store.read_all()) == 1


def test_prune_never_deletes_a_file_it_cannot_date(tmp_path: Path) -> None:
    store = _store(tmp_path, retention_days=1)
    store.root.mkdir(parents=True, exist_ok=True)
    strays = [store.root / "gate-not-a-date.jsonl", store.root / "notes.txt"]
    for stray in strays:
        stray.write_text("keep me\n", encoding="utf-8")

    assert store.prune(now=datetime(2026, 8, 14, tzinfo=UTC)) == []
    assert all(stray.exists() for stray in strays)


def test_a_new_segment_prunes_the_expired_ones(tmp_path: Path) -> None:
    store = _store(tmp_path, retention_days=7)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    store.record(
        decision="deny",
        capability_class="read",
        execution_context="automation",
        ts=now - timedelta(days=90),
    )
    assert len(store.segments()) == 1

    # Rotation is the natural trim point: it happens once a UTC day, and it needs no
    # scheduler.
    store.record(
        decision="allow",
        capability_class="read",
        execution_context="automation",
        ts=now,
    )

    assert [path.name for path in store.segments()] == ["gate-2026-08-14.jsonl"]


def test_a_newline_inside_a_field_stays_on_one_line(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record(
        decision="deny",
        capability_class="mutate.remote",
        execution_context="automation",
        reason="line one\nline two\r\nline three",
    )

    segment = store.segments()[0]
    assert len(segment.read_text(encoding="utf-8").splitlines()) == 1
    assert store.read_all()[0]["reason"] == "line one\nline two\r\nline three"


def test_read_all_skips_a_malformed_line_and_keeps_the_rest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(decision="deny", capability_class="read", execution_context="automation")
    with open(store.segments()[0], "ab") as handle:
        handle.write(b"not json at all\n")
    store.record(decision="allow", capability_class="read", execution_context="automation")

    records = store.read_all()

    assert [r["decision"] for r in records] == ["deny", "allow"]


def test_a_torn_tail_costs_the_tail_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(decision="deny", capability_class="read", execution_context="automation")
    store.record(decision="approve", capability_class="read", execution_context="interactive")
    # A power loss can cut the last line short. The writer probes nothing before it
    # appends, because a probe cannot be race free while other writers append. So the
    # record after a tear joins the stump, and both of those lines go. Every earlier
    # record survives, which is the property that matters.
    with open(store.segments()[0], "ab") as handle:
        handle.write(b'{"ts": "2026-08-14T00:00:00+00:00", "decision": "de')
    store.record(decision="allow", capability_class="read", execution_context="automation")

    records = store.read_all()

    assert [r["decision"] for r in records] == ["deny", "approve"]


def test_the_segment_is_writable_by_the_owner_and_readable_by_the_group(
    tmp_path: Path,
) -> None:
    """This asserted 0o600 until #18 split the processes, and owner-only broke the split.

    The executor writes this log and the agent process reads it: #32 rebuilds denial latches
    there and #29's viewer serves it. Under 0o600 the agent could not open a segment, `Path.glob`
    swallowed the PermissionError, and every latch cleared on every boot.

    The protective half of the original intent stands and is asserted here: the group cannot
    write, and another user gets nothing. Only the read bit moved.
    """
    store = _store(tmp_path)

    store.record(decision="deny", capability_class="read", execution_context="automation")

    assert stat.S_IMODE(store.segments()[0].stat().st_mode) == 0o640


def _write_many(root: str, writer: str, count: int, barrier: Any) -> None:
    """Append *count* records from one process. Used by the concurrency test."""
    store = AuditStore(Path(root), config=AuditConfig())
    barrier.wait()
    for index in range(count):
        store.record(
            decision="deny",
            capability_class="mutate.remote",
            execution_context="automation",
            actor=writer,
            reason=f"{writer}:{index}",
            ts=_FIXED_TS,
        )


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="needs fork to share the test module with the child processes",
)
def test_concurrent_processes_all_produce_intact_records(tmp_path: Path) -> None:
    root = tmp_path / "gates" / "audit"
    writers, per_writer = 6, 40
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(writers)
    processes = [
        ctx.Process(target=_write_many, args=(str(root), f"pid-{index}", per_writer, barrier))
        for index in range(writers)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)

    assert [p.exitcode for p in processes] == [0] * writers
    store = AuditStore(root, config=AuditConfig())
    segment = store.segments()[0]
    raw_lines = segment.read_bytes().splitlines()
    records = store.read_all()
    # Every line must be one whole record. A dropped line means a lost decision, and a
    # spliced line means two writers interleaved inside one record.
    assert len(raw_lines) == writers * per_writer
    assert len(records) == writers * per_writer
    counts = Counter(str(record["actor"]) for record in records)
    assert counts == Counter({f"pid-{index}": per_writer for index in range(writers)})


def test_concurrent_threads_all_produce_intact_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    writers, per_writer = 8, 40
    barrier = threading.Barrier(writers)

    def run(writer: str) -> None:
        barrier.wait()
        for index in range(per_writer):
            store.record(
                decision="allow",
                capability_class="mutate.remote",
                execution_context="automation",
                actor=writer,
                reason=f"{writer}:{index}",
                ts=_FIXED_TS,
            )

    threads = [threading.Thread(target=run, args=(f"thread-{i}",)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    records = store.read_all()
    assert len(store.segments()[0].read_bytes().splitlines()) == writers * per_writer
    assert len(records) == writers * per_writer
    assert len({str(record["reason"]) for record in records}) == writers * per_writer


def test_the_store_stays_independent_of_transcripts_and_the_event_bus() -> None:
    tree = ast.parse(Path(str(audit_module.__file__)).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # #16 keeps the audit trail out of the session history, and apart from the in-memory
    # WebUI bus. A future import from either one would quietly undo that.
    forbidden = ("runtime_events", "nanoinfra.session", "nanoinfra.agent.memory", "nanoinfra.bus")
    assert not [name for name in imported if name.startswith(forbidden)]
