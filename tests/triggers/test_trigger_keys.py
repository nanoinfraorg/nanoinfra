"""A trigger's own key: what makes HTTP ingress possible without a shared credential.

The gateway's tokens cannot serve here -- GatewayTokenStore holds them in in-memory dicts keyed on
time.monotonic() and they do not survive a restart, so a monitor that fires once a night has
nothing to hold (nanoinfraorg/nanoinfra#161).

Only the digest is stored. A stolen triggers.json yields no working key, and there is deliberately
no way to read an existing key back -- issuing again is what rotation is.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanoinfra.triggers.local_store import LocalTriggerStore


def _trigger(store: LocalTriggerStore, name: str = "CI review"):
    return store.create(
        name=name,
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )


def test_a_new_trigger_has_no_key(tmp_path: Path) -> None:
    """Closed until configured: an unissued trigger cannot be fired remotely."""
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)

    assert trigger.key_hash == ""
    assert store.verify_key(trigger.id, "anything") is False


def test_an_issued_key_verifies(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)

    key = store.issue_key(trigger.id)

    assert key is not None
    assert key.startswith("ntk_")
    assert store.verify_key(trigger.id, key) is True


def test_a_wrong_key_does_not_verify(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)
    store.issue_key(trigger.id)

    assert store.verify_key(trigger.id, "ntk_wrong") is False
    assert store.verify_key(trigger.id, "") is False


def test_the_plaintext_is_never_stored(tmp_path: Path) -> None:
    """The property this whole design exists for."""
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)

    key = store.issue_key(trigger.id)
    assert key is not None

    on_disk = store.store_path.read_text(encoding="utf-8")
    assert key not in on_disk
    assert key.removeprefix("ntk_") not in on_disk


def test_only_a_digest_reaches_the_record(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)
    store.issue_key(trigger.id)

    raw = json.loads(store.store_path.read_text(encoding="utf-8"))
    entries = raw["triggers"] if isinstance(raw, dict) and "triggers" in raw else raw
    entry = next(item for item in entries if item["id"] == trigger.id)

    assert len(entry["keyHash"]) == 64
    assert entry["keyCreatedAtMs"] > 0
    assert "key" not in entry


def test_issuing_again_rotates_and_invalidates_the_old_key(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)
    first = store.issue_key(trigger.id)
    assert first is not None

    second = store.issue_key(trigger.id)

    assert second is not None
    assert second != first
    assert store.verify_key(trigger.id, first) is False
    assert store.verify_key(trigger.id, second) is True


def test_revoking_closes_the_trigger(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)
    key = store.issue_key(trigger.id)
    assert key is not None

    assert store.revoke_key(trigger.id) is True
    assert store.verify_key(trigger.id, key) is False
    # Reports whether there was anything to revoke.
    assert store.revoke_key(trigger.id) is False


def test_a_key_survives_a_restart(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)
    key = store.issue_key(trigger.id)
    assert key is not None

    assert LocalTriggerStore(tmp_path).verify_key(trigger.id, key) is True


def test_a_key_authorises_exactly_one_trigger(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    first = _trigger(store, "CI review")
    second = _trigger(store, "Backup check")
    key = store.issue_key(first.id)
    assert key is not None

    assert store.verify_key(first.id, key) is True
    assert store.verify_key(second.id, key) is False


def test_issuing_for_an_unknown_trigger_reports_nothing(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)

    assert store.issue_key("nope") is None
    assert store.revoke_key("nope") is False
    assert store.verify_key("nope", "ntk_whatever") is False


def test_a_trigger_written_before_keys_existed_has_none(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = _trigger(store)
    raw = json.loads(store.store_path.read_text(encoding="utf-8"))
    entries = raw["triggers"] if isinstance(raw, dict) and "triggers" in raw else raw
    for entry in entries:
        entry.pop("keyHash", None)
        entry.pop("keyCreatedAtMs", None)
    store.store_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    reloaded = LocalTriggerStore(tmp_path).get(trigger.id)

    assert reloaded is not None
    assert reloaded.key_hash == ""


def test_two_triggers_get_different_keys(tmp_path: Path) -> None:
    store = LocalTriggerStore(tmp_path)
    first = store.issue_key(_trigger(store, "one").id)
    second = store.issue_key(_trigger(store, "two").id)

    assert first is not None and second is not None
    assert first != second
