# tests/gates/test_fetcher_fetch.py
"""Item 16 (#19): web_fetch inside the fetcher, and the SSRF properties that move with it.

Every property in this file came from ``tests/tools/test_web_fetch_security.py`` and
``tests/tools/test_web_fetch_url_sanitization.py``. The split moved the code into the process with
broad egress. It did not make a request safe to send without a check, so every check still runs and
every one of them is still asserted here.

Why each group matters:

- A private address is the whole SSRF class. Cloud metadata at 169.254.169.254 hands out
  credentials to anybody who can make the fetcher ask for them.
- A redirect is a second URL the caller never chose. So the guard runs again on every hop, before
  the request rather than after it.
- DNS can change between the check and the connect. The pinned transport dials the address the
  guard validated, and a rebind after that must end the fetch rather than fall through to a reader
  that would ask again.
- Fetched text carries an untrusted banner. Content from a page is data, and a model that reads it
  as instruction is the reason this process exists.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from nanoinfra.gates.fetcher import egress as egress_module
from nanoinfra.gates.fetcher.egress import get_with_safe_redirects, validate_url
from nanoinfra.gates.fetcher.fetch import WebFetch
from nanoinfra.security.network import PinnedDNSAsyncTransport
from nanoinfra.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)

_REAL_GETADDRINFO = socket.getaddrinfo
_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_PROXY_ENV_VARS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def _fake_resolve_private(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]


def _fake_resolve_public(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


def _patch_fetch_fake_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    client_kwargs: list[dict[str, Any]] = []

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        url = "https://example.com/page"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeJinaResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "title": "Example",
                    "content": "Hello",
                    "url": "https://example.com/page",
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            client_kwargs.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, **kwargs):
            return FakeStreamResponse()

        async def get(self, url, headers=None, **kwargs):
            return FakeJinaResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())
    monkeypatch.setattr(
        "nanoinfra.security.network.httpx.AsyncHTTPTransport",
        lambda **_kwargs: object(),
    )
    return client_kwargs


# ------------------------------------------------------------------ private targets


@pytest.mark.asyncio
async def test_fetch_blocks_private_ip():
    """Cloud metadata is the payoff of an SSRF, so the address family is refused outright."""
    fetcher = WebFetch()
    with patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_private):
        result = await fetcher.run("http://169.254.169.254/computeMetadata/v1/")
    data = json.loads(result.text)
    assert "error" in data
    assert "private" in data["error"].lower() or "blocked" in data["error"].lower()


@pytest.mark.asyncio
async def test_fetch_blocks_localhost():
    fetcher = WebFetch()

    def _resolve_localhost(hostname, port, family=0, type_=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    with patch("nanoinfra.security.network.socket.getaddrinfo", _resolve_localhost):
        result = await fetcher.run("http://localhost/admin")
    data = json.loads(result.text)
    assert "error" in data


@pytest.mark.asyncio
async def test_fetch_blocks_localhost_even_in_full_workspace_scope(tmp_path):
    """A wide filesystem scope must not widen the network.

    The two are separate grants. An operator who opens the workspace has said nothing about the
    loopback services on the same host.
    """
    fetcher = WebFetch()
    scope = build_workspace_scope(tmp_path, "full")

    def _resolve_localhost(hostname, port, family=0, type_=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    token = bind_workspace_scope(scope)
    try:
        with patch("nanoinfra.security.network.socket.getaddrinfo", _resolve_localhost):
            result = await fetcher.run("http://localhost/admin")
    finally:
        reset_workspace_scope(token)
    data = json.loads(result.text)
    assert "error" in data


# ---------------------------------------------------------------- untrusted content


@pytest.mark.asyncio
async def test_fetch_result_contains_untrusted_flag(monkeypatch: pytest.MonkeyPatch):
    """When fetch succeeds, result JSON must include untrusted=True and the banner."""
    fetcher = WebFetch()
    _patch_fetch_fake_client(monkeypatch)

    with patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await fetcher.run("https://example.com/page")

    data = json.loads(result.text)
    assert data.get("untrusted") is True
    assert "[External content" in data.get("text", "")


# --------------------------------------------------------------------- pinned DNS


@pytest.mark.asyncio
async def test_safe_redirect_requests_use_independent_pinned_dns_concurrently(monkeypatch):
    """Two fetches at once must not share one pinned resolver.

    A shared resolver would let one fetch dial the address validated for the other.
    """
    public_ips = {
        "a.example": "93.184.216.34",
        "b.example": "93.184.216.35",
    }
    calls: dict[str, int] = {host: 0 for host in public_ips}
    seen: dict[str, str] = {}

    def _rebinding_resolver(hostname, port, family=0, type_=0, proto=0, flags=0):
        host = str(hostname).rstrip(".").lower()
        calls[host] += 1
        ip = public_ips[host] if calls[host] <= 2 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

    class ResolvingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0)
            infos = socket.getaddrinfo(
                request.url.host,
                request.url.port or 443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            seen[str(request.url)] = infos[0][4][0]
            return httpx.Response(200, request=request)

    async def _fetch(url: str) -> tuple[httpx.Response | None, str | None]:
        async with httpx.AsyncClient(
            transport=PinnedDNSAsyncTransport(inner=ResolvingTransport())
        ) as client:
            return await get_with_safe_redirects(client, url)

    monkeypatch.setattr("nanoinfra.security.network.socket.getaddrinfo", _rebinding_resolver)

    results = await asyncio.gather(
        _fetch("https://a.example/"),
        _fetch("https://b.example/"),
    )

    assert all(error is None and response is not None for response, error in results)
    assert seen == {
        "https://a.example/": "93.184.216.34",
        "https://b.example/": "93.184.216.35",
    }
    assert calls == {"a.example": 2, "b.example": 2}


@pytest.mark.asyncio
async def test_fetch_does_not_fallback_after_pinned_dns_rebind_rejection(monkeypatch):
    """A rebind between the check and the socket must end the fetch.

    A fallback reader would ask again, and the second ask could win the race the guard refused.
    """
    calls = {"evil.example": 0}

    def _rebinding_resolver(hostname, port, family=0, type_=0, proto=0, flags=0):
        host = str(hostname).rstrip(".").lower()
        calls[host] += 1
        ip = "93.184.216.34" if calls[host] <= 2 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

    fetcher = WebFetch()

    async def _unexpected_jina(*args, **kwargs):
        raise AssertionError("Jina fallback should not run after an SSRF rejection")

    async def _unexpected_readability(*args, **kwargs):
        raise AssertionError("Readability fallback should not run after an SSRF rejection")

    monkeypatch.setattr(WebFetch, "_fetch_jina", _unexpected_jina)
    monkeypatch.setattr(WebFetch, "_fetch_readability", _unexpected_readability)

    class FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("rebound target must be rejected before transport")

    monkeypatch.setattr(
        egress_module,
        "pinned_dns_transport",
        lambda: PinnedDNSAsyncTransport(inner=FailTransport()),
    )

    with patch("nanoinfra.security.network.socket.getaddrinfo", _rebinding_resolver):
        result = await fetcher.run("http://evil.example/page")

    data = json.loads(result.text)
    assert "error" in data
    assert "blocked" in data["error"].lower()
    assert calls["evil.example"] == 3


# -------------------------------------------------------------------------- proxies


@pytest.mark.asyncio
async def test_fetch_proxy_remains_supported(monkeypatch):
    """A configured proxy owns resolution, so the pinned transport steps aside for it."""
    fetcher = WebFetch(proxy="http://config-proxy.example:7890")
    client_kwargs = _patch_fetch_fake_client(monkeypatch)

    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "example.com")

    with patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await fetcher.run("https://example.com/page")

    data = json.loads(result.text)
    assert data["extractor"] == "jina"
    assert all(kwargs["proxy"] == "http://config-proxy.example:7890" for kwargs in client_kwargs)
    assert all("mounts" not in kwargs for kwargs in client_kwargs)
    assert all("transport" not in kwargs for kwargs in client_kwargs)


@pytest.mark.asyncio
async def test_fetch_env_proxy_adds_proxy_mounts_and_keeps_pinned_transport(monkeypatch):
    fetcher = WebFetch()
    client_kwargs = _patch_fetch_fake_client(monkeypatch)

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")

    with patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await fetcher.run("https://example.com/page")

    data = json.loads(result.text)
    assert data["extractor"] == "jina"
    fetch_kwargs = [kwargs for kwargs in client_kwargs if kwargs.get("timeout") == 15.0]
    assert fetch_kwargs
    assert all("transport" in kwargs for kwargs in fetch_kwargs)
    assert all("mounts" in kwargs for kwargs in fetch_kwargs)


def test_fetch_no_proxy_env_keeps_pinned_direct_route(monkeypatch):
    """A no_proxy entry must keep the direct route pinned rather than drop the transport."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())
    monkeypatch.setattr(
        "nanoinfra.security.network.httpx.AsyncHTTPTransport",
        lambda **_kwargs: object(),
    )

    kwargs = egress_module.fetch_client_kwargs(None, 15.0)

    assert "transport" in kwargs
    assert any(transport is None for transport in kwargs["mounts"].values())


# ------------------------------------------------------------------- the readers


@pytest.mark.asyncio
async def test_fetch_can_skip_jina_and_use_custom_user_agent(monkeypatch):
    fetcher = WebFetch(use_jina_reader=False, user_agent="nanoinfra-test-agent")
    seen_headers: list[dict[str, str]] = []

    async def _fail_jina(*args, **kwargs):
        raise AssertionError("Jina Reader should be skipped when disabled")

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        url = "https://example.com/page"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aread(self):
            raise AssertionError("non-image prefetch body should not be read")

    class FakeResponse:
        status_code = 200
        url = "https://example.com/page"
        text = "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>"
        headers = {"content-type": "text/html"}
        is_redirect = False

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, **kwargs):
            seen_headers.append(headers or {})
            return FakeStreamResponse()

        async def get(self, url, headers=None, **kwargs):
            seen_headers.append(headers or {})
            return FakeResponse()

    monkeypatch.setattr(WebFetch, "_fetch_jina", _fail_jina)
    monkeypatch.setattr(WebFetch, "_extract_readable_html", lambda self, html, mode: "Hello world")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())

    with patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await fetcher.run("https://example.com/page")

    data = json.loads(result.text)
    assert data["extractor"] == "readability"
    assert [headers["User-Agent"] for headers in seen_headers] == [
        "nanoinfra-test-agent",
        "nanoinfra-test-agent",
    ]


@pytest.mark.asyncio
async def test_fetch_falls_back_when_readability_dependency_is_missing(monkeypatch):
    """A missing optional library must degrade to raw text rather than fail the fetch."""
    fetcher = WebFetch(use_jina_reader=False)

    class FakeResponse:
        status_code = 200
        url = "https://example.com/page"
        text = "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, follow_redirects=False, **kwargs):
            return FakeResponse()

    def _missing_readability(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'lxml_html_clean'")

    monkeypatch.setattr(WebFetch, "_extract_readable_html", _missing_readability)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())

    with patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await fetcher._fetch_readability("https://example.com/page", "markdown", 5000)

    data = json.loads(result.text)
    assert data["extractor"] == "html"
    assert data["untrusted"] is True
    assert "Hello world" in data["text"]


# ------------------------------------------------------------------- redirects


@pytest.mark.asyncio
async def test_fetch_blocks_private_redirect_before_readability_request(monkeypatch):
    """The hop is checked before the request, so the private target is never asked."""
    fetcher = WebFetch(use_jina_reader=False)
    requested: list[str] = []

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        url = "https://attacker.example/start"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aread(self):
            raise AssertionError("non-image prefetch body should not be read")

    class FakeRedirectResponse:
        status_code = 302
        headers = {"location": "http://127.0.0.1:8765/metadata"}
        url = "https://attacker.example/start"

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, **kwargs):
            return FakeStreamResponse()

        async def get(self, url, headers=None, **kwargs):
            requested.append(url)
            if url == "http://127.0.0.1:8765/metadata":
                raise AssertionError("private redirect target should not be requested")
            return FakeRedirectResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())

    def resolve_public_start_only(hostname, port, family=0, type_=0):
        if hostname == "attacker.example":
            return _fake_resolve_public(hostname, port, family, type_)
        return _REAL_GETADDRINFO(hostname, port, family, type_)

    with patch("nanoinfra.security.network.socket.getaddrinfo", resolve_public_start_only):
        result = await fetcher.run("https://attacker.example/start")

    data = json.loads(result.text)
    assert "error" in data
    assert "redirect blocked" in data["error"].lower()
    assert requested == ["https://attacker.example/start"]


@pytest.mark.asyncio
async def test_fetch_blocks_private_redirect_before_returning_image(monkeypatch):
    """An image on the far side of a private redirect is still a read of a private service."""
    fetcher = WebFetch(use_jina_reader=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://example.com/image.png":
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1/secret.png"},
                request=request,
            )
        if str(request.url) == "http://127.0.0.1/secret.png":
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"\x89PNG\r\n\x1a\n",
                request=request,
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    class TransportAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("proxy", None)
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", TransportAsyncClient)
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())

    def resolve_public_start_only(hostname, port, family=0, type_=0):
        if hostname == "example.com":
            return _fake_resolve_public(hostname, port, family, type_)
        return _REAL_GETADDRINFO(hostname, port, family, type_)

    with patch("nanoinfra.security.network.socket.getaddrinfo", resolve_public_start_only):
        result = await fetcher.run("https://example.com/image.png")

    data = json.loads(result.text)
    assert "error" in data
    assert "redirect blocked" in data["error"].lower()


@pytest.mark.asyncio
async def test_fetch_does_not_request_private_redirect_target(monkeypatch):
    fetcher = WebFetch(use_jina_reader=False)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == "https://attacker.example/start":
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1:8765/metadata"},
                request=request,
            )
        if str(request.url) == "http://127.0.0.1:8765/metadata":
            return httpx.Response(200, content=b"internal secret", request=request)
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    class TransportAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", TransportAsyncClient)
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())

    def resolve_public_start_only(hostname, port, family=0, type_=0):
        if hostname == "attacker.example":
            return _fake_resolve_public(hostname, port, family, type_)
        return _REAL_GETADDRINFO(hostname, port, family, type_)

    with patch("nanoinfra.security.network.socket.getaddrinfo", resolve_public_start_only):
        result = await fetcher.run("https://attacker.example/start")

    data = json.loads(result.text)
    assert "error" in data
    assert "redirect blocked" in data["error"].lower()
    assert requested == ["https://attacker.example/start"]


# --------------------------------------------------------------------- images


@pytest.mark.asyncio
async def test_an_image_comes_back_as_content_blocks(monkeypatch):
    """An image must not reach a text reader, and the blocks must survive the process boundary.

    A textual reader would caption the image instead of returning it, so the fetcher detects the
    content type with a streamed request and returns the bytes as blocks.
    """
    fetcher = WebFetch()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"\x89PNG\r\n\x1a\n",
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    class TransportAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("proxy", None)
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", TransportAsyncClient)
    monkeypatch.setattr(egress_module, "pinned_dns_transport", lambda: object())

    with patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await fetcher.run("https://example.com/image.png")

    assert result.blocks is not None
    assert result.blocks[0]["type"] == "image_url"
    assert "data:image/png;base64," in result.blocks[0]["image_url"]["url"]


# ------------------------------------------------------------- URL sanitization


@contextmanager
def _patched_fetch():
    class FakeResponse:
        status_code = 200
        url = "https://example.com/page"
        text = "<html><head><title>T</title></head><body><p>ok</p></body></html>"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class FakeStreamResponse:
        headers = {"content-type": "text/html"}
        url = "https://example.com/page"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kw):
            return FakeStreamResponse()

        async def get(self, url, **kw):
            return FakeResponse()

    with (
        patch("nanoinfra.security.network.socket.getaddrinfo", _fake_resolve_public),
        patch.object(httpx, "AsyncClient", FakeClient),
        patch.object(egress_module, "pinned_dns_transport", lambda: object()),
    ):
        yield


@pytest.mark.parametrize("dirty_url", [
    "`https://example.com/page`",
    " `https://example.com/page` ",
    '"https://example.com/page"',
    "'https://example.com/page'",
    '  "https://example.com/page"  ',
])
def test_dirty_urls_fail_validation(dirty_url):
    """The wrapper characters are not part of a URL, so validation must not accept them."""
    is_valid, _ = validate_url(dirty_url)
    assert not is_valid


def test_clean_url_passes_validation():
    is_valid, _ = validate_url("https://example.com/page")
    assert is_valid


def test_backtick_url_produces_empty_scheme_in_urlparse():
    """The reason the strip exists: a wrapped URL parses as a URL with no scheme at all."""
    from urllib.parse import urlparse

    p = urlparse("`https://example.com/page`")
    assert p.scheme == ""
    assert p.netloc == ""


@pytest.mark.parametrize("wrapped_url", [
    "`https://example.com/page`",
    '"https://example.com/page"',
    "'https://example.com/page'",
    "  `https://example.com/page`  ",
    '"`https://example.com/page`"',
    "HTTPS://example.com/page",
])
@pytest.mark.asyncio
async def test_the_fetcher_strips_what_the_model_wrapped_the_url_in(wrapped_url):
    """A model quotes a URL often enough that a strip is cheaper than a refusal.

    The strip lives with the check rather than with the caller, because the check is what has to
    see the cleaned URL.
    """
    with _patched_fetch():
        result = await WebFetch().run(wrapped_url)
    data = json.loads(result.text)
    assert "error" not in data, f"unexpected error: {data}"


@pytest.mark.parametrize("bad_url", [
    "ftp://example.com/file",
    "`not a url at all`",
    "`example.com/page`",
])
@pytest.mark.asyncio
async def test_the_fetcher_refuses_what_is_not_an_http_url(bad_url):
    """A strip must not turn a non-URL into a fetch. Only http and https pass."""
    result = await WebFetch().run(bad_url)
    data = json.loads(result.text)
    assert "error" in data
    assert "URL validation failed" in data["error"]
