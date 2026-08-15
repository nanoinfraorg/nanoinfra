"""The agent side of the fetcher wire -- nanoinfraorg/nanoinfra#19.

This module is agent-side code, so it holds no credential, no transport, and no fetch logic. It
writes one frame and reads one frame. A test walks its whole syntax tree and asserts it imports
neither the secret store nor a backend nor the fetcher server, because the import direction is
what keeps the split true.

``FetcherUnavailableError`` is separate from a failed fetch on purpose. "The fetcher is not
running" and "the page returned 404" need different words for an operator: the first is a
deployment fault and the second is a result. A caller that conflates them teaches an operator to
read a broken deployment as a broken web page.
"""

from __future__ import annotations

import socket
from pathlib import Path

from nanoinfra.gates.fetcher.protocol import (
    FetchRequest,
    FetchResponse,
    ProtocolError,
    SearchRequest,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)

# A page can take a long time, and the fetcher holds the per-request timeouts. So this is a
# connect guard, not a fetch timeout: it must not cut a slow page short.
DEFAULT_CONNECT_TIMEOUT_S = 10.0


class FetcherUnavailableError(RuntimeError):
    """The fetcher could not be reached, or it dropped the connection."""


class FetcherClient:
    """One request per connection. The fetcher serves one at a time."""

    def __init__(
        self, socket_path: Path | str, *, connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    ) -> None:
        self._socket_path = Path(socket_path)
        self._connect_timeout_s = connect_timeout_s

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def fetch(
        self, *, url: str, extract_mode: str = "markdown", max_chars: int | None = None
    ) -> FetchResponse:
        """Ask the fetcher to read one URL."""
        return self._exchange(
            FetchRequest(url=url, extract_mode=extract_mode, max_chars=max_chars)
        )

    def search(
        self,
        *,
        query: str,
        count: int | None = None,
        time_range: str | None = None,
        auth_level: int | None = None,
        query_rewrite: bool | None = None,
        freshness: str | None = None,
    ) -> FetchResponse:
        """Ask the fetcher to search the web."""
        return self._exchange(
            SearchRequest(
                query=query,
                count=count,
                time_range=time_range,
                auth_level=auth_level,
                query_rewrite=query_rewrite,
                freshness=freshness,
            )
        )

    def _exchange(self, request: FetchRequest | SearchRequest) -> FetchResponse:
        """Send one request and return the fetcher's answer.

        Raises ``FetcherUnavailableError`` when the socket is absent or the peer disappears. It
        never raises for a failed fetch, because a failed fetch is a response.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self._connect_timeout_s)
                conn.connect(str(self._socket_path))
                # No read deadline past the connect: the fetcher owns the per-request timeouts,
                # and a deadline here would report a slow page as an unavailable fetcher.
                conn.settimeout(None)
                write_frame(conn, encode_request(request))
                return decode_response(read_frame(conn))
        except (OSError, ProtocolError) as exc:
            raise FetcherUnavailableError(
                f"Could not reach the fetcher at {self._socket_path}: {exc}"
            ) from exc


__all__ = ["DEFAULT_CONNECT_TIMEOUT_S", "FetcherClient", "FetcherUnavailableError"]
