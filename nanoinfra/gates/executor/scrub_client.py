"""The agent side of the scrub wire -- nanoinfraorg/nanoinfra#41.

This module is agent-side code, so it holds no sentinel, no credential store, and no scrub
logic. It writes one frame and reads one frame. It must not import
``nanoinfra.gates.executor.scrub``, because that module builds the sentinels, and
``tests/agent/test_redaction_isolation.py`` asserts the whole package respects that direction.

``ScrubUnavailableError`` covers three cases on purpose: no socket, a peer that disappears, and
an executor that answers a refusal. The caller treats all three the same way, because all three
mean the same thing for a transcript. Nobody scrubbed this text, so nobody may persist it. The
message differs per case, so an operator can still tell a stopped executor from a broken store.

**The deadline is short, and that is deliberate.** A persist sits on the hot path of a turn, and
the scrub is a string replace inside a local process. So a slow answer is a fault rather than
patience, and a turn must not hang on one. A timeout costs the turn its transcript text, and the
caller writes a marker that names the cause.
"""

from __future__ import annotations

import socket
from pathlib import Path

from nanoinfra.gates.executor.protocol import ProtocolError, read_frame, write_frame
from nanoinfra.gates.executor.scrub_protocol import (
    ScrubRequest,
    decode_scrub_response,
    default_scrub_socket_path,
    encode_scrub_request,
)

# Connect, write, and read, inside this budget. A local Unix socket answers a string replace in
# microseconds, so seconds here are already an anomaly.
DEFAULT_SCRUB_TIMEOUT_S = 5.0


class ScrubUnavailableError(RuntimeError):
    """No scrub ran, so the caller must not persist the text."""


class ScrubClient:
    """One text per connection. The executor answers one at a time per connection."""

    def __init__(
        self, socket_path: Path | str, *, timeout_s: float = DEFAULT_SCRUB_TIMEOUT_S
    ) -> None:
        self._socket_path = Path(socket_path)
        self._timeout_s = timeout_s

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def scrub(self, text: str, capability_class: str | None) -> str:
        """Return the scrubbed text, or raise ``ScrubUnavailableError``.

        *capability_class* is the class of the tool that produced *text*, or None when the
        caller knows none. #17 drops a ``credential.access`` result whole, and only the side
        with the sentinels can name which secret matched.
        """
        request = ScrubRequest(text=text, capability_class=capability_class or "")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self._timeout_s)
                conn.connect(str(self._socket_path))
                write_frame(conn, encode_scrub_request(request))
                response = decode_scrub_response(read_frame(conn))
        except (OSError, ProtocolError) as exc:
            raise ScrubUnavailableError(
                f"Could not reach the scrub socket at {self._socket_path}: {exc}"
            ) from exc
        if not response.ok:
            raise ScrubUnavailableError(
                f"The executor refused the scrub: {response.error or 'no reason given'}"
            )
        return response.text


def default_scrub_client(*, timeout_s: float = DEFAULT_SCRUB_TIMEOUT_S) -> ScrubClient:
    """The scrub client for this deployment.

    The path derives from the execute socket, so one deployment variable places both. The
    import of the tool module stays inside the function: it reads the variable and the data
    directory in one place, and a module level import would tie the redaction path to the tool
    registry's import graph.
    """
    from nanoinfra.agent.tools.server_execution import default_socket_path

    return ScrubClient(default_scrub_socket_path(default_socket_path()), timeout_s=timeout_s)


__all__ = [
    "DEFAULT_SCRUB_TIMEOUT_S",
    "ScrubClient",
    "ScrubUnavailableError",
    "default_scrub_client",
]
