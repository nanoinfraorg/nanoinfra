"""ansible-runner execution backend -- ad-hoc module invocation, not a
named playbook (there's no playbook file on disk for an arbitrary
agent-supplied command string to reference).

ansible_runner.run() is synchronous and blocks until the whole play
finishes, so it's wrapped in asyncio.to_thread. on_activity fires at
most once, after run() returns (see this file's plan-task notes for
why event_handler's cross-thread marshalling was scoped out) -- unlike
SSHBackend, this is not a real-time activity signal, only a
"something happened" signal at completion, which the idle timeout
still benefits from (the moment the run takes) but doesn't reset mid-run.

Verified against the real installed ansible-runner==2.4.3 package:
``Runner.stdout`` is a *property*, not a plain attribute -- each access
opens a fresh file handle onto the artifact dir's ``stdout`` file (and
raises ``AnsibleRunnerException`` if that file doesn't exist, it never
just returns a falsy value). Accessing it twice, as in
``runner.stdout.read() if runner.stdout else ""``, would open two
separate file handles and leak the first one, and the "else" branch
would be dead code for the real object. This backend instead reads the
property exactly once and always closes the handle, with the read
wrapped so a missing-stdout edge case is reported like any other
failure instead of raising out of this backend.
"""

from __future__ import annotations

import asyncio

import ansible_runner

from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.types import Server

DEFAULT_IDLE_TIMEOUT_S = 300


class AnsibleRunnerBackend:
    async def run(self, server: Server, command: str, secret_value, *, on_activity) -> ExecutionResult:  # noqa: ANN001
        host_pattern = server.config.get("inventoryHost") or server.config.get("group") or "all"
        private_data_dir = server.config.get("projectPath") or "."

        kwargs: dict[str, object] = {
            "private_data_dir": private_data_dir,
            "module": "command",
            "module_args": command,
            "host_pattern": host_pattern,
            "quiet": True,
        }
        if secret_value:
            kwargs["ssh_key"] = secret_value

        try:
            runner = await asyncio.to_thread(ansible_runner.run, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- must report, not raise
            return ExecutionResult(exit_code=None, output="", error=str(exc))

        on_activity(runner.status)

        try:
            stdout_file = runner.stdout
            try:
                output = stdout_file.read()
            finally:
                stdout_file.close()
        except Exception as exc:  # noqa: BLE001 -- must report, not raise
            return ExecutionResult(exit_code=runner.rc, output="", error=str(exc))

        return ExecutionResult(exit_code=runner.rc, output=output, error=None)


__all__ = ["AnsibleRunnerBackend", "DEFAULT_IDLE_TIMEOUT_S"]
