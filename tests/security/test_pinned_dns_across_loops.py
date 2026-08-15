# tests/security/test_pinned_dns_across_loops.py
"""The DNS pin must serialize across every event loop and every thread (found during #19).

`PinnedDNSAsyncTransport` held one class-level `asyncio.Lock`. That lock binds to the first event
loop that contends on it, so a second loop raised "bound to a different event loop" and the
request failed. The fetcher process runs `asyncio.run` per request, so it met this shape.

The crash is the smaller half. `pin_resolved_url_dns` patches `socket.getaddrinfo`, which is a
process global, and an `asyncio.Lock` guards one loop alone. Two threads with their own loops could
therefore patch it at the same time, and the second exit would restore the first thread's original
resolver while a request still ran under the pin. That is an unpinned request, which is the SSRF
hole the pin exists to close.

So the guard has to reach across loops and threads. Only a threading primitive does.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx
import pytest

from nanoinfra.security.network import PinnedDNSAsyncTransport

_URL = "http://127.0.0.1/"


class _CountingInner(httpx.AsyncBaseTransport):
    """An inner transport that records how many requests hold the pin at one time."""

    def __init__(self) -> None:
        self.held = 0
        self.max_held = 0
        self._guard = threading.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        with self._guard:
            self.held += 1
            self.max_held = max(self.max_held, self.held)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(200, text="ok")
        finally:
            with self._guard:
                self.held -= 1


class _RaisingInner(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)


def _transport(inner: httpx.AsyncBaseTransport) -> PinnedDNSAsyncTransport:
    return PinnedDNSAsyncTransport(allow_loopback=True, inner=inner)


async def _contended_pair(transport: PinnedDNSAsyncTransport) -> list[Any]:
    """Two requests at once, which is what binds the old lock to this loop."""

    def _one() -> Any:
        return transport.handle_async_request(httpx.Request("GET", _URL))

    return list(await asyncio.gather(_one(), _one()))


def test_a_second_event_loop_still_pins() -> None:
    """The reported crash. One process may run one loop after another."""
    transport = _transport(_CountingInner())

    first = asyncio.run(_contended_pair(transport))
    second = asyncio.run(_contended_pair(transport))

    assert [response.status_code for response in first] == [200, 200]
    assert [response.status_code for response in second] == [200, 200]


def test_two_threads_never_hold_the_pin_at_once() -> None:
    """The real defect. The pin patches a process global, so one holder at a time is the rule."""
    inner = _CountingInner()
    transport = _transport(inner)

    failures: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(_contended_pair(transport))
        except BaseException as exc:  # noqa: BLE001 - the test reports it below
            failures.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # A thread that crashed would never overlap, so the count alone could pass for the wrong
    # reason. Every thread must finish its two requests.
    assert failures == []
    assert inner.max_held == 1


def test_a_failed_request_releases_the_pin() -> None:
    """A guard a raise could keep would stop every later request in the process."""
    transport = _transport(_RaisingInner())

    async def _attempt() -> None:
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(httpx.Request("GET", _URL))

    asyncio.run(_attempt())

    # The pin is free again, so a second request answers rather than hangs.
    working = _transport(_CountingInner())
    assert asyncio.run(_contended_pair(working))[0].status_code == 200
