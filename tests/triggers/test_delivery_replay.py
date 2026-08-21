"""A dead-lettered delivery can be re-run, with lineage.

Both subsystems wrote run records and neither could re-run one. A delivery in ``failed/`` is a JSON
file describing exactly what to do, and the only way to act on it was to read it and retype the
command (nanoinfraorg/nanoinfra#163).

This is also the eval/replay infrastructure the self-improving-harness research item identified as
its only missing pillar, which is the argument for doing it now rather than treating it as a nicety.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.triggers.local_store import (
    LocalTriggerStore,
    TriggerDisabledError,
)
from nanoinfra.utils.backoff import BackoffPolicy


def _store(tmp_path: Path) -> LocalTriggerStore:
    return LocalTriggerStore(tmp_path, backoff=BackoffPolicy(base_delay_ms=0, max_delay_ms=0))


def _dead_letter(store: LocalTriggerStore, trigger_id: str, content: str) -> str:
    """Burn a delivery's attempts until it dead-letters, and return its id."""
    delivery = store.enqueue(trigger_id, content)
    for _ in range(20):
        claimed = store.claim_deliveries()
        if not claimed:
            break
        if not store.retry_delivery(claimed[0], "downstream unavailable"):
            break
    return delivery.id


def _trigger(store: LocalTriggerStore, name: str = "CI review"):
    return store.create(
        name=name,
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )


def test_a_dead_lettered_delivery_is_listed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trigger = _trigger(store)
    delivery_id = _dead_letter(store, trigger.id, "CI failed")

    failed = store.list_failed_deliveries()

    assert [item.id for item in failed] == [delivery_id]
    assert failed[0].content == "CI failed"
    assert failed[0].last_error == "downstream unavailable"


def test_listing_can_be_scoped_to_one_trigger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _trigger(store, "CI review")
    second = _trigger(store, "Backup check")
    _dead_letter(store, first.id, "CI failed")
    _dead_letter(store, second.id, "Backup failed")

    assert len(store.list_failed_deliveries()) == 2
    assert len(store.list_failed_deliveries(trigger_id=first.id)) == 1


def test_a_replay_is_queued_with_a_new_id_and_lineage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")

    replay = store.replay_failed_delivery(original)

    assert replay is not None
    assert replay.id != original
    assert replay.replay_of == original
    assert replay.content == "CI failed"
    # Attempts reset: the operator replayed because they believe the cause is fixed.
    assert replay.attempts == 0


def test_a_replay_is_claimable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")
    store.replay_failed_delivery(original)

    claimed = store.claim_deliveries()

    assert len(claimed) == 1
    assert claimed[0].replay_of == original


def test_the_original_stays_in_failed(tmp_path: Path) -> None:
    """Deleting it would erase the reason someone chose to replay."""
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")

    store.replay_failed_delivery(original)

    assert [item.id for item in store.list_failed_deliveries()] == [original]


def test_the_lineage_reaches_the_run_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")
    replay = store.replay_failed_delivery(original)
    assert replay is not None

    records = sorted(store.runs_dir.glob("*.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in records]
    replay_records = [item for item in payloads if item.get("delivery_id") == replay.id]

    assert replay_records
    assert replay_records[0]["replay_of"] == original


def test_a_replay_survives_a_new_store_instance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")

    replay = LocalTriggerStore(tmp_path).replay_failed_delivery(original)

    assert replay is not None
    assert replay.replay_of == original


def test_replaying_an_unknown_delivery_reports_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _trigger(store)

    assert store.replay_failed_delivery("tdl_nope") is None


def test_replaying_into_a_disabled_trigger_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")
    store.enable(trigger.id, enabled=False)

    with pytest.raises(TriggerDisabledError):
        store.replay_failed_delivery(original)


def test_deleting_a_trigger_takes_its_dead_letters_with_it(tmp_path: Path) -> None:
    """So there is nothing left to replay into a trigger that no longer exists.

    _delete_delivery_files_for_trigger_unlocked sweeps failed/ along with inbox/ and processing/,
    which is why the deleted-trigger branch inside replay_failed_delivery is defence rather than a
    reachable path. Asserted rather than skipped, because a skip reads as coverage.
    """
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")
    assert store.list_failed_deliveries()

    assert store.delete(trigger.id) is True

    assert store.list_failed_deliveries() == []
    assert store.replay_failed_delivery(original) is None


def test_a_replay_can_itself_be_replayed(tmp_path: Path) -> None:
    """Lineage points at the immediate parent, which is what a history reader needs."""
    store = _store(tmp_path)
    trigger = _trigger(store)
    original = _dead_letter(store, trigger.id, "CI failed")
    first_replay = store.replay_failed_delivery(original)
    assert first_replay is not None

    claimed = store.claim_deliveries()
    for _ in range(20):
        if not claimed or not store.retry_delivery(claimed[0], "still unavailable"):
            break
        claimed = store.claim_deliveries()

    second = store.replay_failed_delivery(first_replay.id)

    assert second is not None
    assert second.replay_of == first_replay.id
