"""The fetcher: the only process with broad egress -- nanoinfraorg/nanoinfra#19.

``web_fetch`` and ``web_search`` run here. Untrusted content enters here, and this process holds
nothing an attacker wants: no host credential, no transport to a host, and no way to run a program.
The one credential it holds is a search provider key, because an egress call to a search API needs
one, and that key authorizes nothing on any host.

The shape copies the executor (#18) on purpose. One socket, one request per connection, a refusal
for a frame this side cannot read, and a socket file removed on exit. A second process model would
drift from the first, and two models mean two sets of mistakes.

Two properties this module must keep, and ``tests/gates/test_fetcher_isolation.py`` asserts both:

- It imports neither ``nanoinfra.secrets.store`` nor an execution backend. A compromise here must
  yield no path to a transport and no path to a host credential.
- It cannot exec. No ``subprocess`` import, no ``os.system``, and no ``os.exec*``. #22 adds stdio
  MCP servers, which are subprocesses, so #22 has to answer this property rather than delete it.

The settings reload on every request. An operator changes the search provider or the proxy in the
WebUI, and a long-lived fetcher must not answer with the provider it started with.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from loguru import logger

from nanoinfra.gates.fetcher.egress import DEFAULT_USER_AGENT, Payload
from nanoinfra.gates.fetcher.fetch import DEFAULT_MAX_CHARS, WebFetch
from nanoinfra.gates.fetcher.protocol import (
    MAX_FRAME_BYTES,
    FetchRequest,
    FetchResponse,
    ProtocolError,
    SearchRequest,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)
from nanoinfra.gates.fetcher.search import WebSearch

# The socket's own mode is not honoured on every platform, so the directory carries the control.
# 0o700 keeps another local account out of the fetcher's door.
_SOCKET_DIR_MODE = 0o700


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Everything the fetcher needs to answer one request.

    The fetcher takes a flat set of values rather than the tool's config objects. So this process
    holds no import from the tool layer, and a test can build one settings object in a line.
    """

    provider: str = "duckduckgo"
    api_key: str = ""
    base_url: str = ""
    max_results: int = 5
    timeout: float = 30
    proxy: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    use_jina_reader: bool = True
    jina_api_key: str = ""
    max_chars: int = DEFAULT_MAX_CHARS


def load_web_settings() -> WebSettings:
    """Read the current web settings from the app config and the environment.

    The import stays local. The config module pulls in a large part of the package, and the
    fetcher must not pay for that at import time.
    """
    from nanoinfra.config.loader import load_config, resolve_config_env_vars

    web = resolve_config_env_vars(load_config()).tools.web
    return WebSettings(
        provider=web.search.provider,
        api_key=web.search.api_key,
        base_url=web.search.base_url,
        max_results=web.search.max_results,
        timeout=web.search.timeout,
        proxy=web.proxy,
        user_agent=web.user_agent or DEFAULT_USER_AGENT,
        use_jina_reader=web.fetch.use_jina_reader,
        jina_api_key=os.environ.get("JINA_API_KEY", ""),
    )


@dataclass(slots=True)
class Fetcher:
    """Answers one fetch or one search at a time."""

    # None means the app's own settings. The lookup happens per request rather than at class
    # definition, so the loader a deployment installs is the loader this process calls.
    settings_loader: Callable[[], WebSettings] | None = None
    # The settings the last successful load produced. A load can fail, and a fetcher that then
    # served defaults would silently drop an operator's proxy or provider choice. No caller sets
    # this: a caller that could inject "the last settings" could choose the provider.
    _last: WebSettings = field(default_factory=WebSettings, init=False)

    async def handle(self, request: FetchRequest | SearchRequest) -> FetchResponse:
        """Answer one request. Never raises for a failed fetch: a failure is a response."""
        settings = self._settings()
        if isinstance(request, FetchRequest):
            payload = await self._fetch(settings).run(
                request.url,
                extract_mode=request.extract_mode,
                max_chars=request.max_chars,
            )
        else:
            payload = await self._search(settings).run(
                request.query,
                request.count,
                time_range=request.time_range,
                auth_level=request.auth_level,
                query_rewrite=request.query_rewrite,
                freshness=request.freshness,
            )
        return _answer(payload)

    def _settings(self) -> WebSettings:
        """Reload the settings, or keep the last ones that loaded.

        An operator changes the provider or the proxy while this process runs, and the next
        request must use the new value. A broken config file must not stop the fetcher, because
        the last known settings still describe what the operator asked for.
        """
        loader = self.settings_loader or load_web_settings
        try:
            self._last = loader()
        except Exception:
            logger.exception("gates: the fetcher kept its last settings, because a reload failed")
        return self._last

    def _fetch(self, settings: WebSettings) -> WebFetch:
        return WebFetch(
            use_jina_reader=settings.use_jina_reader,
            proxy=settings.proxy,
            user_agent=settings.user_agent,
            max_chars=settings.max_chars,
            jina_api_key=settings.jina_api_key,
        )

    def _search(self, settings: WebSettings) -> WebSearch:
        return WebSearch(
            provider=settings.provider,
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_results=settings.max_results,
            timeout=settings.timeout,
            proxy=settings.proxy,
            user_agent=settings.user_agent,
        )


def _answer(payload: Payload) -> FetchResponse:
    """Wrap one payload for the wire. ``ok`` says the fetcher completed the operation."""
    return FetchResponse(
        ok=True,
        body=payload.text,
        blocks=payload.blocks,
        is_error=payload.is_error,
        error=None,
    )


def _error(message: str) -> FetchResponse:
    """A frame the fetcher could not act on. ``ok`` False keeps this apart from a failed fetch."""
    return FetchResponse(ok=False, body="", blocks=None, is_error=True, error=message)


def serve_forever(
    socket_path: Path | str, *, workspace: Path | str, max_requests: int | None = None
) -> None:
    """Bind the Unix socket and serve until terminated.

    ``max_requests`` exists for tests. Production passes nothing and the loop runs until the
    supervisor stops the process.

    ``workspace`` keeps one entry-point shape with the executor, so one supervisor pattern starts
    either process. The fetcher reads no credential and no server record from it: a fetch needs a
    URL and a search needs a query, and neither needs a file.

    One connection at a time is deliberate. ``ddgs`` is not safe to call concurrently, and the old
    tool asked the agent's runner for that serialization. The process supplies it here instead.

    The socket file is removed on exit. A stale file blocks the next bind, and a supervisor that
    restarts the fetcher must not need a human to delete one.
    """
    path = Path(socket_path)
    # A private mode only on a directory this process creates. A two-uid deployment owns that
    # decision: with separate accounts the directory is owned by the fetcher and carries setgid
    # plus group traversal (2710), so the agent account can reach a known socket name without
    # listing the directory. A blanket chmod here would lock the agent out, and a split the agent
    # cannot talk to is worse than the mode it replaced.
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, _SOCKET_DIR_MODE)
    if path.exists():
        path.unlink()

    fetcher = Fetcher()
    served = 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(8)
        logger.info("gates: fetcher listening on {} (workspace {})", path, workspace)
        try:
            while max_requests is None or served < max_requests:
                conn, _ = server.accept()
                with conn:
                    _serve_one(conn, fetcher)
                served += 1
        finally:
            with contextlib.suppress(OSError):
                path.unlink()


def _serve_one(conn: socket.socket, fetcher: Fetcher) -> None:
    """Answer one connection. A bad frame gets a refusal, and never a crash.

    A peer that speaks nonsense must not take the fetcher down. Untrusted content reaches this
    process, so its availability is part of what the split has to keep.
    """
    try:
        payload = read_frame(conn)
        request = decode_request(payload)
    except ProtocolError as exc:
        logger.warning("gates: fetcher refused a frame: {}", exc)
        _write(conn, _error(f"Malformed request: {exc}"))
        return

    try:
        response = asyncio.run(fetcher.handle(request))
    except Exception as exc:  # noqa: BLE001 -- one bad request must not end the process
        logger.exception("gates: fetcher failed a request")
        response = _error(f"The fetcher failed this request: {exc}")

    _write(conn, response)


def _write(conn: socket.socket, response: FetchResponse) -> None:
    """Send one reply, and answer rather than hang up when the reply is too large.

    A page or an image can exceed the wire limit. A frame that write_frame refuses would close the
    connection with nothing on it, and a silent close reads to the caller as "the fetcher is not
    running". That sends an operator to a deployment problem they do not have.
    """
    payload = encode_response(response)
    if len(payload) > MAX_FRAME_BYTES:
        logger.warning("gates: fetcher reply of {} bytes is above the wire limit", len(payload))
        payload = encode_response(
            _error(
                f"The fetched content came to {len(payload)} bytes, above the "
                f"{MAX_FRAME_BYTES} byte limit of the fetcher wire. Ask for fewer characters, or "
                "fetch a smaller resource."
            )
        )
    with contextlib.suppress(OSError, ProtocolError):
        write_frame(conn, payload)


__all__ = ["Fetcher", "WebSettings", "load_web_settings", "serve_forever"]
