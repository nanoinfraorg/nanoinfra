"""AWS SSM Run Command execution backend, via boto3 (synchronous, wrapped
in asyncio.to_thread -- boto3 has no native async client).

Poll-based, no partial output: get_command_invocation only ever returns
the full StandardOutputContent/StandardErrorContent captured so far,
there's no separate "tail the output" call. on_activity fires once when
a terminal status is reached.

Verified against the real installed boto3==1.43.66 / botocore SSM service
model:

- ``Parameters`` for ``send_command`` is ``dict[str, list[str]]``, so
  ``{"commands": [command]}`` is correct (not a bare string).
- ``send_command``'s response nests the id at ``response["Command"]["CommandId"]``.
- ``get_command_invocation``'s ``Status`` is one of ``Pending`` | ``InProgress``
  | ``Delayed`` | ``Success`` | ``Cancelled`` | ``TimedOut`` | ``Failed`` |
  ``Cancelling`` -- ``Success``/``Failed``/``Cancelled``/``TimedOut`` are the
  terminal ones (``Delayed``/``Cancelling`` are still in flight, unlike a
  naive read of the AWS console's "Delivery Timed Out" wording might suggest).
- Critically, ``get_command_invocation``'s own docstring documents that the
  Run Command API follows an *eventual consistency* model. In practice this
  means the very first poll right after ``send_command`` returns commonly
  raises ``botocore.exceptions.ClientError`` with error code
  ``InvocationDoesNotExist`` rather than handing back a `Pending` status
  dict -- the invocation record hasn't propagated to the read path yet.
  That is not a rare edge case to shrug off: it's this API's documented
  normal behavior, so the polling loop treats it as "not ready yet, keep
  polling" rather than letting it fall through to the catch-all
  except-and-report handler as a fatal error. Any other ``ClientError``
  (bad permissions, throttling, etc.) is not given this treatment and is
  still reported as a failure.
"""

# pyright: reportMissingTypeStubs=false
from __future__ import annotations

import asyncio
from typing import Any, Callable, cast

import boto3
from botocore.exceptions import ClientError

from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.types import Server

boto3_module = cast(Any, boto3)

DEFAULT_IDLE_TIMEOUT_S = 180
_TERMINAL_STATUSES = {"Success", "Failed", "Cancelled", "TimedOut"}
_DEFAULT_POLL_INTERVAL_S = 2.0


class SSMBackend:
    async def run(
        self,
        server: Server,
        command: str,
        secret_value: str | None,  # unused: SSM auth is IAM-role-based, not secretRef-based
        *,
        on_activity: Callable[[str], None],
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> ExecutionResult:
        instance_id = server.config.get("instanceId", "")
        region = server.config.get("region") or None

        try:
            client = await asyncio.to_thread(boto3_module.client, "ssm", region_name=region)
            sent = await asyncio.to_thread(
                client.send_command,
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [command]},
            )
            command_id = sent["Command"]["CommandId"]
            # TODO(servers): this CommandId is the recovery handle for a timed-out
            # run. Once send_command returns, the command is running on the
            # instance and nothing here can un-send it -- cancelling the awaiting
            # coroutine only stops the polling (see timeout.py's module
            # docstring). Persisting it on the ServerJob would let a retry poll
            # the original invocation (or aws ssm cancel-command it) instead of
            # blindly starting a second copy. Out of scope for now; the timeout
            # message says plainly that the command may still be running.

            while True:
                try:
                    invocation = await asyncio.to_thread(
                        client.get_command_invocation, CommandId=command_id, InstanceId=instance_id
                    )
                except ClientError as exc:
                    error_response = cast(dict[str, Any], exc.response)
                    if error_response.get("Error", {}).get("Code") == "InvocationDoesNotExist":
                        # Eventual consistency -- the invocation hasn't propagated to
                        # the read path yet. Not a failure, just not ready: keep polling.
                        await asyncio.sleep(poll_interval_s)
                        continue
                    raise

                status = invocation.get("Status", "")
                if status in _TERMINAL_STATUSES:
                    on_activity(status)
                    output = invocation.get("StandardOutputContent", "")
                    stderr = invocation.get("StandardErrorContent", "")
                    if stderr:
                        output += ("\nSTDERR:\n" + stderr) if output else stderr
                    return ExecutionResult(exit_code=invocation.get("ResponseCode"), output=output, error=None)
                await asyncio.sleep(poll_interval_s)
        except Exception as exc:  # noqa: BLE001 -- must report, not raise
            return ExecutionResult(exit_code=None, output="", error=str(exc))


__all__ = ["DEFAULT_IDLE_TIMEOUT_S", "SSMBackend"]
