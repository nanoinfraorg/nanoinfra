"""HTTP API execution backend -- for servers managed by their own API,
not a generic shell. ``command`` is "<METHOD> <path>" (method optional,
defaults to GET), joined onto the Server's configured baseUrl.

Reuses validate_server_target (Task 1) on the parsed hostname before
ever making a request -- the same "block metadata/loopback, allow
RFC1918" policy every other provider gets, not the general SSRF guard.

The lenient guard is only defensible because the request can never leave
the operator-configured baseUrl's origin: run() pins scheme/host/port to
baseUrl's and refuses anything else. Without that pin the *effective*
URL is per-request agent input (urljoin happily accepts an absolute URL
in ``command``), so "baseUrl is operator-set" would say nothing about
where the decrypted credential actually goes.

Note on a directive checked and rejected during implementation: an
earlier instruction for this task said to validate the full URL via
nanoinfra.security.network.validate_url_target instead, since this
backend already deals in full URLs. That guard blocks all of RFC1918
by default (it's built for arbitrary, possibly attacker-influenced
URLs), which is exactly wrong for Server targets -- most real
infrastructure lives in RFC1918 space, and the plan document itself
(docs/superpowers/plans/2026-08-06-servers-execution.md, both its
general SSRF-policy section and this task's own brief text) explicitly
says not to use that guard here for that reason. Empirically confirmed:
swapping in validate_url_target broke 4 of this file's 6 tests because
the test fixtures' baseUrl (10.0.1.5) is RFC1918. validate_server_target
on the parsed hostname is the correct, plan-consistent choice.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.network_guard import validate_server_target
from nanoinfra.servers.types import Server

DEFAULT_IDLE_TIMEOUT_S = 30
_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _parse_command(command: str) -> tuple[str, str]:
    parts = command.strip().split(None, 1)
    if len(parts) == 2 and parts[0].upper() in _VALID_METHODS:
        return parts[0].upper(), parts[1]
    return "GET", command.strip()


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) with the scheme's default port filled in, so
    ``http://h`` and ``http://h:80`` compare equal."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:  # malformed port in the URL -- not this operator's baseUrl
        port = None
    return scheme, host, port or _DEFAULT_PORTS.get(scheme)


class ApiBackend:
    async def run(
        self,
        server: Server,
        command: str,
        secret_value: str | None,
        *,
        on_activity: Callable[[str], None],
    ) -> ExecutionResult:
        base_url = server.config.get("baseUrl", "")
        method, path = _parse_command(command)
        url = urljoin(base_url if base_url.endswith("/") else base_url + "/", path.lstrip("/"))

        # urljoin() lets an *absolute* URL in the agent-supplied command replace
        # the operator's baseUrl outright (urljoin("http://10.0.1.5/",
        # "http://attacker.example/x") == "http://attacker.example/x"). Since the
        # next few lines attach the decrypted secretRef as a Bearer token, that
        # would be a one-call credential exfiltration primitive -- and the target
        # guard is no backstop, because it deliberately allows public addresses
        # for this provider. So: the effective request must stay on exactly the
        # origin the operator configured. Checked before the Authorization header
        # is even constructed, let alone sent.
        if _origin(url) != _origin(base_url):
            return ExecutionResult(
                exit_code=None,
                output="",
                error=(
                    "Refusing to request a different origin than the server's configured "
                    f"baseUrl ({base_url!r}): the command's path resolved to {url!r}. "
                    "Pass a path, not an absolute URL."
                ),
            )

        hostname = urlparse(url).hostname or ""
        ok, error = validate_server_target(hostname)
        if not ok:
            return ExecutionResult(exit_code=None, output="", error=f"Blocked target: {error}")

        headers = {"Authorization": f"Bearer {secret_value}"} if secret_value else {}
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_IDLE_TIMEOUT_S) as client:
                response = await client.request(method, url, headers=headers)
        except Exception as exc:  # noqa: BLE001 -- must report, not raise
            return ExecutionResult(exit_code=None, output="", error=str(exc))

        on_activity(f"{response.status_code}")
        exit_code = 0 if 200 <= response.status_code < 300 else 1
        return ExecutionResult(exit_code=exit_code, output=response.text, error=None)


__all__ = ["ApiBackend", "DEFAULT_IDLE_TIMEOUT_S"]
