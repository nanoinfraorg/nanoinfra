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

**``scrub_many`` carries a whole walk in one frame (nanoinfraorg/nanoinfra#54).** ``scrub`` costs
one connection per text, and a Responses payload holds one item per message plus one per tool
call. So a walk over such a payload would open one connection per item on every save.
``scrub_many`` sends the texts of the whole walk together and reads the answers back in the same
order.

**A count that does not match refuses the whole batch.** The answers pair with the fields the
caller collected them from by position only. A client that returned what arrived would let the
caller write one field's scrub over another field's text, so a mismatch raises the same error a
dead socket raises, and the caller persists nothing.
"""

from __future__ import annotations

import socket
from collections.abc import Sequence
from pathlib import Path

from nanoinfra.gates.executor.protocol import ProtocolError, read_frame, write_frame
from nanoinfra.gates.executor.scrub_protocol import (
    ScrubBatchRequest,
    ScrubRequest,
    decode_scrub_batch_response,
    decode_scrub_response,
    default_scrub_socket_path,
    encode_scrub_batch_request,
    encode_scrub_request,
    split_scrub_batch,
)

# Connect, write, and read, inside this budget. A local Unix socket answers a string replace in
# microseconds, so seconds here are already an anomaly.
DEFAULT_SCRUB_TIMEOUT_S = 5.0


class ScrubUnavailableError(RuntimeError):
    """No scrub ran, so the caller must not persist the text."""


class ScrubClient:
    """One request per connection. The executor answers one at a time per connection."""

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
            with self._connected() as conn:
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

    def scrub_many(self, texts: Sequence[tuple[str, str | None]]) -> list[str]:
        """Scrub many texts in one round trip, or raise ``ScrubUnavailableError`` (#54).

        Each element is a text and the class of the tool that produced it, or None when the
        caller knows none. The answer holds one text per element, in the same order.

        An empty sequence opens no connection. A payload with no text carrier is the common case
        for a provider that stores identifiers only, and it must cost nothing.

        The wire caps one frame, so a very long walk goes as a few frames rather than one. That
        is a property of the wire and not of this method: the caller still sees one call and one
        list, and ``split_scrub_batch`` states what a split costs.
        """
        if not texts:
            return []
        items = [
            ScrubRequest(text=text, capability_class=capability_class or "")
            for text, capability_class in texts
        ]
        answers: list[str] = []
        for batch in split_scrub_batch(items):
            answers.extend(self._one_batch(batch))
        if len(answers) != len(items):
            # This cannot happen while every batch checks its own count, and it is checked again
            # because the alternative is silence. A caller that paired the wrong answer with the
            # wrong field would write one field's scrub over another field's text.
            raise ScrubUnavailableError(
                f"The executor answered {len(answers)} texts for {len(items)} sent."
            )
        return answers

    def _one_batch(self, items: list[ScrubRequest]) -> list[str]:
        try:
            with self._connected() as conn:
                write_frame(conn, encode_scrub_batch_request(ScrubBatchRequest(items=items)))
                response = decode_scrub_batch_response(read_frame(conn))
        except (OSError, ProtocolError) as exc:
            raise ScrubUnavailableError(
                f"Could not reach the scrub socket at {self._socket_path}: {exc}"
            ) from exc
        if not response.ok:
            raise ScrubUnavailableError(
                f"The executor refused the scrub: {response.error or 'no reason given'}"
            )
        if len(response.texts) != len(items):
            raise ScrubUnavailableError(
                f"The executor answered {len(response.texts)} texts for {len(items)} sent. "
                "A batch answers by position, so a different count pairs the wrong answer with "
                "the wrong field."
            )
        return response.texts

    def _connected(self) -> socket.socket:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(self._timeout_s)
            conn.connect(str(self._socket_path))
        except BaseException:
            conn.close()
            raise
        return conn


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
