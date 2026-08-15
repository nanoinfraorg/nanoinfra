# tests/gates/test_scope_resolution_budget.py
"""Item 33 (#35): one action reads the inventory once, and never on the event loop.

`Executor.handle` resolved the same action four times over. The guard resolved it, the
observation record resolved it for its scope label, the gate resolved it again, and the preview
line resolved it a fourth time. Each resolve runs `ansible-inventory` or parses the inventory
file, so the cost multiplied for one answer that cannot change inside one action.

The read also ran on the event loop, which stalls every other session for the duration.

The cache lifetime is one action, and no longer. #24 re-resolves a grant host on purpose,
because an inventory write between two turns must invalidate a match. A cache that outlived one
action would restore the redirect that #23 and #24 close.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.servers import scope as scope_module
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run"

_SERVER = "ansible-web-group"
_MIRROR = "ansible-web-mirror"
_COMMAND = "systemctl reload nginx"

_WEB_HOSTS = ("10.0.2.11", "10.0.2.12")

_INVENTORY = """\
[web]
10.0.2.11
10.0.2.12
"""

_WIDER_INVENTORY = """\
[web]
10.0.2.11
10.0.2.12
10.0.2.13
"""


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


class _ReadCounter:
    """Count the inventory reads, and say whether each one ran on the event loop.

    The counter wraps the uncached read, so a cache above it shows up as a lower count.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_event_loop: list[bool] = []
        self._real = scope_module._read_inventory_uncached

    def __call__(self, project_path: str) -> Any:
        self.calls.append(project_path)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.on_event_loop.append(False)
        else:
            self.on_event_loop.append(True)
        return self._real(project_path)

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture
def reads() -> Any:
    counter = _ReadCounter()
    with patch.object(scope_module, "_read_inventory_uncached", new=counter):
        yield counter


def _project(tmp_path: Path, text: str = _INVENTORY) -> str:
    project = tmp_path / "ansible-project"
    project.mkdir(exist_ok=True)
    (project / "inventory").write_text(text, encoding="utf-8")
    return str(project)


def _servers(tmp_path: Path, *, project_path: str) -> None:
    """Two records against one project, so a grant host resolves the same inventory."""
    store = ServerStore(tmp_path)
    for name in (_SERVER, _MIRROR):
        store.create(
            {
                "name": name,
                "providerId": "ansible-runner",
                "config": {"group": "web", "projectPath": project_path},
            }
        )


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": _SERVER,
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _executor(tmp_path: Path, gates: GatesConfig | None = None) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=lambda: gates or GatesConfig())


def _ok() -> ExecutionResult:
    return ExecutionResult(exit_code=0, output="ok", error=None)


@pytest.mark.asyncio
async def test_one_action_reads_the_inventory_once(tmp_path: Path, reads: Any) -> None:
    """The guard, the record, the gate, and the preview share one answer."""
    _servers(tmp_path, project_path=_project(tmp_path))

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())):
        await _executor(tmp_path).handle(_request())

    assert reads.count == 1


@pytest.mark.asyncio
async def test_a_second_action_reads_the_inventory_again(tmp_path: Path, reads: Any) -> None:
    """The cache dies with the action. A cache that outlived it would restore #23's redirect."""
    _servers(tmp_path, project_path=_project(tmp_path))
    executor = _executor(tmp_path)

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())):
        await executor.handle(_request())
        await executor.handle(_request())

    assert reads.count == 2


@pytest.mark.asyncio
async def test_an_inventory_edit_between_two_actions_changes_the_second_result(
    tmp_path: Path,
) -> None:
    """The rule #24 states, as a test: an inventory write must reach the next action."""
    project = _project(tmp_path)
    _servers(tmp_path, project_path=project)
    executor = _executor(tmp_path)

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())):
        first = await executor.handle(_request(preview_requested=True))
        (Path(project) / "inventory").write_text(_WIDER_INVENTORY, encoding="utf-8")
        second = await executor.handle(_request(preview_requested=True))

    assert "10.0.2.13" not in str(first.output)
    assert "10.0.2.13" in str(second.output)


@pytest.mark.asyncio
async def test_no_inventory_read_runs_on_the_event_loop(tmp_path: Path, reads: Any) -> None:
    """A quarter-second subprocess on the loop stalls every other session for that long."""
    _servers(tmp_path, project_path=_project(tmp_path))

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())):
        await _executor(tmp_path).handle(_request())

    assert reads.on_event_loop == [False]


@pytest.mark.asyncio
async def test_a_grant_host_in_the_same_project_adds_no_read(tmp_path: Path, reads: Any) -> None:
    """A grant that names a record resolves it, and the action already read that inventory."""
    _servers(tmp_path, project_path=_project(tmp_path))
    granted = GatesConfig.model_validate(
        {
            "interactive": {"mutate.remote": {"group": "grant"}},
            "standingGrants": [
                {
                    "id": "reload-web",
                    "contexts": ["interactive"],
                    "hosts": [_MIRROR, *_WEB_HOSTS],
                    "commands": [_COMMAND],
                }
            ],
        }
    )

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await _executor(tmp_path, granted).handle(_request())

    assert response.ok
    run.assert_called_once()
    assert reads.count == 1


@pytest.mark.asyncio
async def test_an_unresolvable_inventory_reads_once_too(tmp_path: Path, reads: Any) -> None:
    """The failed read is also the same answer four times over, so it caches the same way."""
    ServerStore(tmp_path).create(
        {
            "name": _SERVER,
            "providerId": "ansible-runner",
            "config": {"group": "web", "projectPath": str(tmp_path / "absent")},
        }
    )

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    run.assert_not_called()
    assert reads.count == 1
