# tests/gates/test_audit_root_identity.py
"""Item 34 (#36): the agent account can rename the audit directory.

#32 rebuilds the denial latches from the audit log, because a model cannot un-append a file.
That holds for the contents of the file. It does not hold for the directory entry. Write rights
on a parent allow a rename of any entry inside it, whatever the entry's own owner and mode are.
The agent account owns `$HOME/.nanoinfra`, so it could move `$HOME/.nanoinfra/gates` aside. The
log then read as empty, the restore reported healthy, and every latch was gone.

entrypoint.sh closes the rename itself, and a shell layout has no test here. These tests cover
the second half: the executor pins the device and inode of its audit root, and it refuses to
serve when that pair changes. A rename then costs availability instead of every latch, and that
is the safe direction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.config.gates import AuditConfig, GatesConfig
from nanoinfra.gates.audit import AuditRootChangedError, AuditStore, root_identity
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor, _audit_store
from nanoinfra.gates.latch_restore import restore_latches
from nanoinfra.secrets import crypto
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_GRANTED_COMMAND = "systemctl reload nginx"
SESSION = "session-a"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _pinned(root: Path) -> AuditStore:
    """A store that holds the identity of the root it opened, the way the executor opens it."""
    return AuditStore(root, config=AuditConfig(), pin_root=True)


def _record(store: AuditStore, *, decision: str = "denied") -> None:
    store.record(
        decision=decision,
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id=SESSION,
    )


def _server(tmp_path: Path) -> None:
    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _GRANTED_COMMAND,
        "session_id": SESSION,
        "execution_context": "automation",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _granted() -> GatesConfig:
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["unattended"],
                    "hosts": ["10.0.1.5"],
                    "commands": [_GRANTED_COMMAND],
                }
            ],
        }
    )


def test_root_identity_names_the_device_and_inode_of_the_directory(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    root.mkdir()

    info = root.stat()

    assert root_identity(root) == (info.st_dev, info.st_ino)


def test_root_identity_is_none_when_no_directory_is_there(tmp_path: Path) -> None:
    assert root_identity(tmp_path / "absent") is None


def test_root_identity_is_none_when_the_path_is_not_a_directory(tmp_path: Path) -> None:
    """A file at the audit root is not the audit root, so it must not read as one."""
    path = tmp_path / "gates"
    path.write_text("not a directory", encoding="utf-8")

    assert root_identity(path) is None


def test_a_pinned_store_holds_the_identity_of_the_root_it_opened(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    root.mkdir()

    assert _pinned(root).pinned_identity == root_identity(root)


def test_a_renamed_root_refuses_the_next_record(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    root.mkdir()
    store = _pinned(root)
    _record(store)

    root.rename(tmp_path / "gates-moved")

    with pytest.raises(AuditRootChangedError):
        _record(store)


def test_a_refused_record_does_not_start_a_fresh_log(tmp_path: Path) -> None:
    """The old store recreated the directory here, and the fresh log held no latch."""
    root = tmp_path / "gates"
    root.mkdir()
    store = _pinned(root)
    _record(store)
    root.rename(tmp_path / "gates-moved")

    with pytest.raises(AuditRootChangedError):
        _record(store)

    assert not root.exists()


def test_a_fresh_directory_at_the_same_path_is_refused(tmp_path: Path) -> None:
    """The bypass keeps the path and changes the directory, so the path proves nothing."""
    root = tmp_path / "gates"
    root.mkdir()
    store = _pinned(root)
    _record(store)

    root.rename(tmp_path / "gates-moved")
    root.mkdir()

    with pytest.raises(AuditRootChangedError):
        _record(store)


def test_a_moved_root_is_named_in_the_failure(tmp_path: Path) -> None:
    """An operator reads this line to learn why every gated action stopped."""
    root = tmp_path / "gates"
    root.mkdir()
    store = _pinned(root)
    root.rename(tmp_path / "gates-moved")

    with pytest.raises(AuditRootChangedError) as failure:
        _record(store)

    assert str(root) in str(failure.value)


def test_the_failure_is_an_oserror_so_every_caller_fails_closed() -> None:
    """The executor refuses on OSError, and the restore degrades on OSError. Both stay true."""
    assert issubclass(AuditRootChangedError, OSError)


def test_a_reader_refuses_a_moved_root_too(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    root.mkdir()
    store = _pinned(root)
    _record(store)
    root.rename(tmp_path / "gates-moved")

    with pytest.raises(AuditRootChangedError):
        store.segments()


def test_the_pin_is_taken_when_the_root_first_appears(tmp_path: Path) -> None:
    """A fresh install opens a store before the directory exists, and it still pins."""
    root = tmp_path / "gates"
    store = _pinned(root)

    _record(store)
    root.rename(tmp_path / "gates-moved")
    root.mkdir()

    with pytest.raises(AuditRootChangedError):
        _record(store)


def test_an_unpinned_store_keeps_its_behaviour(tmp_path: Path) -> None:
    """The pin is opt-in. It guards a long-lived process, and every other caller is unchanged."""
    root = tmp_path / "gates"
    root.mkdir()
    store = AuditStore(root, config=AuditConfig())
    _record(store)

    root.rename(tmp_path / "gates-moved")
    _record(store)

    assert store.segments()


def test_a_moved_root_degrades_the_latch_restore(tmp_path: Path) -> None:
    """The point of the item. A rename must not read as "no latched sessions"."""
    root = tmp_path / "gates"
    root.mkdir()
    store = _pinned(root)
    _record(store, decision="denied")

    root.rename(tmp_path / "gates-moved")
    restored = restore_latches(store)

    assert restored.degraded
    assert restored.is_latched(SESSION, MUTATE_REMOTE)


@pytest.mark.asyncio
async def test_the_executor_refuses_an_allowed_action_when_the_root_moves(tmp_path: Path) -> None:
    """A grant covers this command, so only the moved root can refuse it."""
    _server(tmp_path)
    root = tmp_path / "gates"
    root.mkdir()
    audit = _pinned(root)
    root.rename(tmp_path / "gates-moved")

    with (
        patch(_BACKEND, new=AsyncMock()) as run,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
    ):
        response = await Executor(
            workspace=tmp_path, gates_loader=_granted, audit=audit
        ).handle(_request())

    assert not response.ok
    assert response.error
    assert "audit" in response.error
    run.assert_not_called()
    resolve.assert_not_called()


def test_the_executor_pins_the_root_it_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The executor is the long-lived process that holds the log, so it takes the pin."""
    root = tmp_path / "gates"
    root.mkdir()
    monkeypatch.setattr("nanoinfra.config.paths.get_data_dir", lambda: tmp_path)

    with patch("nanoinfra.gates.executor.server.load_policy", return_value=GatesConfig()):
        store = _audit_store()

    assert store.root == root
    assert store.pinned_identity == root_identity(root)
