# tests/servers/execution/test_ssm_backend.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from nanoinfra.servers.execution.ssm_backend import SSMBackend
from nanoinfra.servers.types import Server


def _server() -> Server:
    return Server(
        id="a" * 32,
        name="test-server",
        provider_id="ssm",
        config={"instanceId": "i-0123456789abcdef0", "region": "us-east-1"},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


def _client_error(code: str, message: str = "x", operation_name: str = "GetCommandInvocation") -> ClientError:
    """Builds a real botocore.exceptions.ClientError the way botocore itself
    constructs one -- ClientError.__init__ takes (error_response, operation_name)
    and formats its str() from error_response["Error"]["Code"/"Message"]."""
    return ClientError({"Error": {"Code": code, "Message": message}, "ResponseMetadata": {}}, operation_name)


@pytest.mark.asyncio
async def test_run_sends_command_and_polls_until_success():
    fake_client = MagicMock()
    fake_client.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
    fake_client.get_command_invocation.side_effect = [
        {"Status": "InProgress"},
        {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": "up 3 days", "StandardErrorContent": ""},
    ]
    on_activity = MagicMock()

    with patch("boto3.client", return_value=fake_client):
        backend = SSMBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=on_activity, poll_interval_s=0.01)

    assert result.exit_code == 0
    assert result.output == "up 3 days"
    fake_client.send_command.assert_called_once()
    _, kwargs = fake_client.send_command.call_args
    assert kwargs["InstanceIds"] == ["i-0123456789abcdef0"]
    assert kwargs["Parameters"] == {"commands": ["uptime"]}
    # on_activity fires exactly once, and only once the invocation reaches a
    # terminal status -- SSM has no partial-output signal, so it must not be
    # called during the "InProgress" poll.
    on_activity.assert_called_once_with("Success")


@pytest.mark.asyncio
async def test_run_reports_failed_status():
    fake_client = MagicMock()
    fake_client.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
    fake_client.get_command_invocation.return_value = {
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": "",
        "StandardErrorContent": "no such command",
    }

    with patch("boto3.client", return_value=fake_client):
        backend = SSMBackend()
        result = await backend.run(_server(), "badcmd", None, on_activity=lambda _c: None, poll_interval_s=0.01)

    assert result.exit_code == 1
    assert "no such command" in result.output


@pytest.mark.asyncio
async def test_exception_is_reported_not_raised():
    with patch("boto3.client", side_effect=RuntimeError("no credentials")):
        backend = SSMBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None)

    assert result.exit_code is None
    assert "no credentials" in result.error


@pytest.mark.asyncio
async def test_run_retries_past_invocation_does_not_exist_then_succeeds():
    """Regression test for the real API's eventual-consistency behavior:
    GetCommandInvocation's own docstring states the Run Command API is
    eventually consistent, and in practice the very first poll right after
    send_command frequently raises a ClientError with code
    "InvocationDoesNotExist" rather than returning a "Pending" status dict --
    the invocation record hasn't propagated to the read path yet. A backend
    that only handles the happy-path dict response (as the naive
    implementation would) crashes out of the polling loop on the very first
    poll and reports a spurious failure instead of retrying. This test
    fails against that naive version and passes only if InvocationDoesNotExist
    is treated as "not ready yet, keep polling" rather than a fatal error.
    """
    fake_client = MagicMock()
    fake_client.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
    fake_client.get_command_invocation.side_effect = [
        _client_error("InvocationDoesNotExist"),
        _client_error("InvocationDoesNotExist"),
        {"Status": "InProgress"},
        {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": "ok", "StandardErrorContent": ""},
    ]

    with patch("boto3.client", return_value=fake_client):
        backend = SSMBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None, poll_interval_s=0.01)

    assert result.exit_code == 0
    assert result.output == "ok"
    assert fake_client.get_command_invocation.call_count == 4


@pytest.mark.asyncio
async def test_other_client_errors_during_polling_are_reported_not_swallowed():
    # Only InvocationDoesNotExist is a "keep polling" signal -- any other
    # ClientError (e.g. permission problems discovered mid-poll) must still
    # surface as a failed ExecutionResult, not be silently retried forever.
    fake_client = MagicMock()
    fake_client.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
    fake_client.get_command_invocation.side_effect = _client_error(
        "AccessDeniedException", message="not authorized"
    )

    with patch("boto3.client", return_value=fake_client):
        backend = SSMBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None, poll_interval_s=0.01)

    assert result.exit_code is None
    assert "AccessDeniedException" in result.error or "not authorized" in result.error
