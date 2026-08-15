# tests/agent/tools/test_gate_owned_dry_run.py
"""Item 7 (#10): the gate owns the preview-versus-execute decision.

``dry_run`` used to be an argument with a strongly worded description. A description is
advice, and the model still picked the value. Now the argument only asks. A request to see
an effect reaches no host, so it needs no permission and gets no policy question. A request
to execute stays a request, and the gate answers it.

Two previews therefore exist, and they must never read the same. One says a caller asked to
look. The other says policy would not permit the action. One message for both would teach
an operator that a preview means nothing.

The interactive half stays ungated here. #8 enforces the unattended half only, and #13 and
#27 build the approval path. tests/agent/tools/test_unattended_enforcement.py's
test_an_interactive_call_is_not_gated_yet holds that line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_AUTOMATION,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import (
    PREVIEW_ON_REQUEST_NOTE,
    PREVIEW_WITHHELD_NOTE,
    ExecuteOnServerTool,
)
from nanoinfra.config.gates import GatesConfig
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _tool(tmp_path: Path) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(
        servers=ServerStore(tmp_path), secrets=SecretStore(tmp_path), jobs=JobStore(tmp_path)
    )


def _server(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
    }
    ServerStore(tmp_path).create(raw)


def _unattended() -> RequestContext:
    return RequestContext(
        channel="telegram",
        chat_id="c1",
        session_key="s1",
        execution_context=EXECUTION_CONTEXT_AUTOMATION,
    )


def _policy(**over: object) -> GatesConfig:
    return GatesConfig.model_validate(over) if over else GatesConfig()


@pytest.mark.asyncio
async def test_a_requested_preview_says_the_caller_asked_for_it(tmp_path: Path) -> None:
    """Case one. A request to look is not a privileged act, so it is never refused."""
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        request_context(_unattended()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=True
        )

    assert PREVIEW_ON_REQUEST_NOTE in str(result)
    assert PREVIEW_WITHHELD_NOTE not in str(result)
    assert not getattr(result, "is_error", False)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_withheld_preview_says_policy_would_not_permit_execution(tmp_path: Path) -> None:
    """Case two. The caller asked to execute, and the gate answered with a preview."""
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        request_context(_unattended()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert PREVIEW_WITHHELD_NOTE in str(result)
    assert PREVIEW_ON_REQUEST_NOTE not in str(result)
    assert result.is_error
    run.assert_not_called()


@pytest.mark.asyncio
async def test_the_two_preview_cases_never_share_a_message(tmp_path: Path) -> None:
    """The difference is the point of this item, so it gets its own test.

    A merged message would tell an operator that a preview means nothing, because they
    could no longer tell a look from a stopped action.
    """
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_unattended()),
    ):
        asked = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=True
        )
        withheld = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert PREVIEW_ON_REQUEST_NOTE not in PREVIEW_WITHHELD_NOTE
    assert PREVIEW_WITHHELD_NOTE not in PREVIEW_ON_REQUEST_NOTE
    assert str(asked) != str(withheld)
    assert not getattr(asked, "is_error", False)
    assert withheld.is_error


@pytest.mark.asyncio
async def test_a_request_to_execute_at_an_approve_scope_does_not_execute(tmp_path: Path) -> None:
    """The acceptance case: ``dry_run=false`` at a scope that needs approval runs nothing."""
    _server(tmp_path)
    needs_approval = _policy(unattended={"mutate.remote": {"host": "approve"}})

    with (
        patch(
            "nanoinfra.agent.tools.server_execution.load_policy", return_value=needs_approval
        ),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        request_context(_unattended()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    run.assert_not_called()
    assert PREVIEW_WITHHELD_NOTE in str(result)
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_withheld_preview_still_shows_what_would_have_run(tmp_path: Path) -> None:
    """An operator who reads the refusal needs the resolved action, not only the verdict."""
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_unattended()),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    text = str(result)
    assert "prod-web-01" in text
    assert "ssh" in text
    assert "uptime" in text
    assert "10.0.1.5" in text


@pytest.mark.asyncio
async def test_a_preview_request_asks_no_policy_question(tmp_path: Path) -> None:
    """A look reaches no host, so the tool reads no policy for one (requirement 1)."""
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy") as policy,
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_unattended()),
    ):
        await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=True
        )

    policy.assert_not_called()


def test_the_schema_keeps_dry_run_and_stops_calling_it_a_confirmation(tmp_path: Path) -> None:
    """Old transcripts still carry the argument, so the schema keeps it (requirement 3).

    The description must no longer frame ``dry_run=false`` as the thing that authorizes a
    call, because the argument authorizes nothing.
    """
    schema = _tool(tmp_path).parameters
    dry_run = schema["properties"]["dry_run"]
    description = dry_run["description"].lower()

    assert "gate" in description
    assert "not a grant" in description
    assert "confirm" not in description
