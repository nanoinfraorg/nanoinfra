# tests/agent/tools/test_all_scope_never_executes.py
"""`all` scope has no path to execution, in any context.

#7 types that field as `Literal["deny"]` so a config cannot even ask for another value, and #8
states the rule as absolute. #9 then made group execution reachable, which exposed a hole: the
interactive short-circuit in `_decide` returns EXECUTE before policy runs, so an interactive
turn could run `group: "all"`.

The short-circuit exists because the interactive `approve` decision has no approval surface
before #13 and #27. That reason does not extend to `all`, because `all` has no approval path by
design. So the scope refusal runs before the short-circuit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

# Inventory host names are addresses on purpose. The guard validates the names the resolver
# returns, and a name like `web-01` would send every test through a DNS lookup that fails.
_INVENTORY = """
[web]
10.0.2.11
10.0.2.12
"""


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "inventory").write_text(_INVENTORY, encoding="utf-8")
    return project


def _tool(tmp_path: Path, pattern: str) -> ExecuteOnServerTool:
    store = ServerStore(tmp_path)
    store.create(
        {
            "name": "fleet",
            "providerId": "ansible-runner",
            "config": {"group": pattern, "projectPath": str(_project(tmp_path))},
        }
    )
    return ExecuteOnServerTool(
        servers=store, secrets=SecretStore(tmp_path), jobs=JobStore(tmp_path)
    )


def _ctx(execution_context: str) -> RequestContext:
    return RequestContext(
        channel="telegram", chat_id="c1", session_key="s1", execution_context=execution_context
    )


@pytest.mark.parametrize("pattern", ["all", "*"])
@pytest.mark.parametrize(
    "execution_context", [EXECUTION_CONTEXT_INTERACTIVE, EXECUTION_CONTEXT_AUTOMATION]
)
@pytest.mark.asyncio
async def test_an_unbounded_pattern_never_executes(
    tmp_path: Path, pattern: str, execution_context: str
) -> None:
    tool = _tool(tmp_path, pattern)

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run",
            new=AsyncMock(),
        ) as run,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
        request_context(_ctx(execution_context)),
    ):
        result = await tool.execute(
            server_id_or_name="fleet", command="uptime", dry_run=False
        )

    run.assert_not_called()
    resolve.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []
    assert "all" in str(result).lower()


@pytest.mark.asyncio
async def test_a_bounded_group_still_executes_interactively(tmp_path: Path) -> None:
    """The refusal must be about unbounded scope, and not about groups."""
    tool = _tool(tmp_path, "web")
    from nanoinfra.servers.execution.base import ExecutionResult

    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run",
            new=AsyncMock(return_value=fake),
        ) as run,
        request_context(_ctx(EXECUTION_CONTEXT_INTERACTIVE)),
    ):
        result = await tool.execute(
            server_id_or_name="fleet", command="uptime", dry_run=False
        )

    run.assert_called_once()
    assert not getattr(result, "is_error", False)


@pytest.mark.asyncio
async def test_a_preview_of_an_unbounded_pattern_is_still_a_preview(tmp_path: Path) -> None:
    """A preview reaches no host, so the scope rule does not turn it into an error."""
    tool = _tool(tmp_path, "all")

    with (
        patch(
            "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run",
            new=AsyncMock(),
        ) as run,
        request_context(_ctx(EXECUTION_CONTEXT_INTERACTIVE)),
    ):
        result = await tool.execute(server_id_or_name="fleet", command="uptime")

    run.assert_not_called()
    assert "Preview (not executed)" in str(result)


