# tests/agent/tools/test_group_execution.py
"""Item 18 (#9): a group config executes, and every resolved host passes the guard.

Group execution used to be unreachable. ``_target_host()`` read ``inventoryHost`` alone, so
an ansible-runner server that named only ``group`` hit the "Cannot validate network target"
refusal before anything decrypted a credential. The ``group`` scope tier therefore described
an action the system could not perform.

A group is an inventory label and not an address, so the guard expands it with #4's resolver
and checks every host the label names. Two rules carry the security property. One blocked
host refuses the whole group, because ansible cannot run on the rest only and a partial run
on hosts nobody cleared is worse than no run. An inventory the resolver cannot read still
refuses, because an unexpandable pattern is not an empty one.

The gate stays in front (#8). This item turns an impossible action into a possible one, and
an unattended group action still needs a standing grant that covers every resolved host.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import PREVIEW_WITHHELD_NOTE, ExecuteOnServerTool
from nanoinfra.config.gates import GatesConfig
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

# Addresses rather than names, so the guard parses them and no test touches DNS.
# ansible accepts an address as a host entry, so this is a real inventory.
_WEB_HOSTS = ("10.0.2.11", "10.0.2.12", "10.0.2.13")

_INVENTORY = """\
[web]
10.0.2.11
10.0.2.12
10.0.2.13

[db]
10.0.3.21
"""

# The acceptance case: one cloud-metadata host inside an otherwise routine group.
_METADATA_INVENTORY = """\
[web]
10.0.2.11
169.254.169.254
10.0.2.13
"""

# A blocked host outside the group an operator would name, for the unbounded pattern.
_WIDE_INVENTORY = """\
[web]
10.0.2.11
10.0.2.12

[db]
169.254.169.254
"""

_COMMAND = "systemctl reload nginx"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _project(tmp_path: Path, text: str = _INVENTORY) -> str:
    """An ansible-runner private_data_dir holding the inventory the backend reads.

    ansible-runner passes ``-i <private_data_dir>/inventory`` when that path exists, so the
    resolver and the backend read the same file.
    """
    project = tmp_path / "ansible-project"
    project.mkdir(exist_ok=True)
    (project / "inventory").write_text(text, encoding="utf-8")
    return str(project)


def _tool(tmp_path: Path) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(
        servers=ServerStore(tmp_path), secrets=SecretStore(tmp_path), jobs=JobStore(tmp_path)
    )


def _group_server(
    tmp_path: Path,
    *,
    project_path: str,
    group: str = "web",
    secret_ref: str | None = None,
    extra: dict[str, str] | None = None,
) -> None:
    config: dict[str, str] = {"group": group, "projectPath": project_path}
    config.update(extra or {})
    raw: dict[str, Any] = {
        "name": "ansible-web-group",
        "providerId": "ansible-runner",
        "config": config,
    }
    if secret_ref:
        raw["secretRef"] = secret_ref
    ServerStore(tmp_path).create(raw)


def _ctx(execution_context: str) -> RequestContext:
    return RequestContext(
        channel="telegram",
        chat_id="c1",
        session_key="s1",
        execution_context=execution_context,
    )


def _interactive() -> RequestContext:
    return _ctx(EXECUTION_CONTEXT_INTERACTIVE)


def _policy(**over: object) -> GatesConfig:
    return GatesConfig.model_validate(over) if over else GatesConfig()


@pytest.mark.asyncio
async def test_a_group_config_runs_when_every_resolved_host_passes_the_guard(
    tmp_path: Path,
) -> None:
    """The gap this item closes: a group-only config used to be refused outright."""
    _group_server(tmp_path, project_path=_project(tmp_path))
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run",
            new=AsyncMock(return_value=fake),
        ) as run,
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert not getattr(result, "is_error", False)
    assert "ok" in str(result)
    run.assert_called_once()
    jobs = JobStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "completed"


@pytest.mark.asyncio
async def test_every_resolved_host_reaches_the_guard_not_only_the_first(tmp_path: Path) -> None:
    """One validated address plus a backend that dials three is the bypass class."""
    _group_server(tmp_path, project_path=_project(tmp_path))
    guard = MagicMock(return_value=(True, ""))

    with (
        patch("nanoinfra.agent.tools.server_execution.validate_server_target", new=guard),
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run",
            new=AsyncMock(return_value=ExecutionResult(exit_code=0, output="ok", error=None)),
        ),
        request_context(_interactive()),
    ):
        await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    checked = [call.args[0] for call in guard.call_args_list]
    assert sorted(checked) == sorted(_WEB_HOSTS)


@pytest.mark.asyncio
async def test_the_guard_never_checks_the_pattern_string_itself(tmp_path: Path) -> None:
    """A pattern is a label. Checking it would validate a name nothing ever dials."""
    _group_server(tmp_path, project_path=_project(tmp_path))
    guard = MagicMock(return_value=(True, ""))

    with (
        patch("nanoinfra.agent.tools.server_execution.validate_server_target", new=guard),
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run",
            new=AsyncMock(return_value=ExecutionResult(exit_code=0, output="ok", error=None)),
        ) as run,
        request_context(_interactive()),
    ):
        await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    # The run itself proves the guard ran and passed. Without it this test would also
    # hold for a tool that checked nothing at all.
    run.assert_called_once()
    assert "web" not in [call.args[0] for call in guard.call_args_list]


@pytest.mark.asyncio
async def test_one_metadata_host_refuses_the_whole_group(tmp_path: Path) -> None:
    """The acceptance case. Partial execution must not happen."""
    secret = SecretStore(tmp_path).create(
        {"name": "ansible-key", "kind": "ssh_key", "providerId": "local", "value": "s3cr3t"}
    )
    _group_server(
        tmp_path,
        project_path=_project(tmp_path, _METADATA_INVENTORY),
        secret_ref=secret.id,
    )

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert result.is_error
    # The blocked address proves the expansion happened. A refusal that only named the
    # missing config field would mean the group was never expanded at all.
    assert "169.254.169.254" in str(result)
    run.assert_not_called()
    resolve.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_the_refusal_names_the_blocked_host_and_the_whole_group(tmp_path: Path) -> None:
    """An operator must learn which host stopped the run, and that none of them ran."""
    _group_server(tmp_path, project_path=_project(tmp_path, _METADATA_INVENTORY))

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ),
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    text = str(result)
    assert "169.254.169.254" in text
    assert "'web'" in text
    assert "3 hosts" in text
    assert "No host ran." in text


@pytest.mark.asyncio
async def test_an_unreadable_inventory_still_refuses(tmp_path: Path) -> None:
    """Requirement 3. The refusal survives where the expansion cannot happen."""
    empty = tmp_path / "no-inventory-here"
    empty.mkdir()
    _group_server(tmp_path, project_path=str(empty))

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert result.is_error
    text = str(result)
    assert "Cannot validate network target" in text
    # The resolver's own reason, so the refusal says WHY the host set is unknown. ansible
    # would fall back to ansible.cfg here, which the resolver cannot read.
    assert "cannot expand 'web'" in text
    assert "No inventory at" in text
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_pattern_that_matches_nothing_refuses_rather_than_reads_as_empty(
    tmp_path: Path,
) -> None:
    """An unexpandable pattern is not an empty one, so a typo is never a safe no-op."""
    _group_server(tmp_path, project_path=_project(tmp_path), group="staging")

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert result.is_error
    assert "Cannot validate network target" in str(result)
    assert "staging" in str(result)
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_host_shaped_extra_key_cannot_stand_in_for_the_group_hosts(
    tmp_path: Path,
) -> None:
    """AnsibleRunnerBackend never reads ``host``, so that key must satisfy nothing.

    A config that pairs a group with a host-shaped extra key used to be the way to make the
    guard validate a safe address while the backend targeted the group.
    """
    _group_server(
        tmp_path,
        project_path=_project(tmp_path, _METADATA_INVENTORY),
        extra={"host": "8.8.8.8"},
    )
    checked: list[str] = []

    def recording_guard(host: str) -> tuple[bool, str]:
        from nanoinfra.servers.network_guard import validate_server_target

        checked.append(host)
        return validate_server_target(host)

    with (
        patch(
            "nanoinfra.agent.tools.server_execution.validate_server_target", new=recording_guard
        ),
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert result.is_error
    assert "8.8.8.8" not in checked
    assert "169.254.169.254" in checked
    run.assert_not_called()


@pytest.mark.asyncio
async def test_the_widest_pattern_also_gets_every_host_checked(tmp_path: Path) -> None:
    """An unbounded pattern reaches the whole inventory, so the guard reads the whole set.

    ``all`` covers every host an operator adds tomorrow, and #4 keeps its scope at ``all``
    for that reason. The check is the same one a named group gets: one blocked host anywhere
    in the inventory refuses the action.
    """
    _group_server(tmp_path, project_path=_project(tmp_path, _WIDE_INVENTORY), group="all")

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert result.is_error
    assert "169.254.169.254" in str(result)
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_preview_of_a_group_names_the_hosts_the_guard_validated(tmp_path: Path) -> None:
    """A pattern alone hides the blast radius, so the preview shows the resolved hosts."""
    _group_server(tmp_path, project_path=_project(tmp_path))

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        request_context(_interactive()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=True
        )

    text = str(result)
    assert "Preview (not executed)" in text
    assert "web" in text
    for host in _WEB_HOSTS:
        assert host in text
    run.assert_not_called()


@pytest.mark.asyncio
async def test_an_unattended_group_call_without_a_grant_is_still_withheld(tmp_path: Path) -> None:
    """#8 stays in front. A possible action is not a permitted action."""
    _group_server(tmp_path, project_path=_project(tmp_path))

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert result.is_error
    assert PREVIEW_WITHHELD_NOTE in str(result)
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_grant_covering_every_group_host_lets_an_unattended_group_run(
    tmp_path: Path,
) -> None:
    _group_server(tmp_path, project_path=_project(tmp_path))
    granted = _policy(
        unattended={"mutate.remote": {"group": "grant"}},
        standingGrants=[
            {
                "id": "reload-web",
                "contexts": ["unattended"],
                "hosts": list(_WEB_HOSTS),
                "commands": [_COMMAND],
            }
        ],
    )
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=granted),
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run",
            new=AsyncMock(return_value=fake),
        ) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert not getattr(result, "is_error", False)
    run.assert_called_once()


@pytest.mark.asyncio
async def test_a_grant_that_misses_one_group_host_refuses_the_whole_group(
    tmp_path: Path,
) -> None:
    """Partial coverage is no coverage. A group never runs on hosts nobody granted."""
    _group_server(tmp_path, project_path=_project(tmp_path))
    partial = _policy(
        unattended={"mutate.remote": {"group": "grant"}},
        standingGrants=[
            {
                "id": "reload-two-of-three",
                "contexts": ["unattended"],
                "hosts": list(_WEB_HOSTS[:2]),
                "commands": [_COMMAND],
            }
        ],
    )

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=partial),
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
        ) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="ansible-web-group", command=_COMMAND, dry_run=False
        )

    assert result.is_error
    assert PREVIEW_WITHHELD_NOTE in str(result)
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []
