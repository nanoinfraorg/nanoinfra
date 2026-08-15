# tests/gates/test_fetcher_search.py
"""Item 16 (#19): web_search inside the fetcher, provider by provider.

Every property here came from ``tests/tools/test_web_search_tool.py``. The code moved into the
process with broad egress, and the behaviour a caller sees must not change with it: the same
headers, the same request bodies, the same fallbacks, and the same words on a failure.

Two properties changed home rather than disappeared, and both are named where they now live:

- **Serialization.** ``ddgs`` is not safe to call concurrently, so the old tool declared itself
  exclusive and asked the agent's runner for that. The fetcher serves one connection at a time, so
  the process supplies it. ``test_fetcher_server.py`` holds that test.
- **The key stays in the fetcher.** The provider key is the one credential this process holds,
  because an egress call to a search API needs one. A test below asserts a reply never carries it.
"""

from __future__ import annotations

import httpx
import pytest

from nanoinfra.gates.fetcher.search import WebSearch


def _search(
    provider: str = "brave",
    api_key: str = "",
    base_url: str = "",
    user_agent: str | None = None,
    **over: object,
) -> WebSearch:
    kwargs: dict[str, object] = {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
    }
    if user_agent is not None:
        kwargs["user_agent"] = user_agent
    kwargs.update(over)
    return WebSearch(**kwargs)  # pyright: ignore[reportArgumentType]


def _response(status: int = 200, json: dict | None = None) -> httpx.Response:
    """Build a mock httpx.Response with a dummy request attached."""
    r = httpx.Response(status, json=json)
    r._request = httpx.Request("GET", "https://mock")
    return r


class _MockDDGS:
    def __init__(self, **kw):
        pass

    def text(self, query, max_results=5):
        return [{"title": "Fallback", "href": "https://ddg.example", "body": "DuckDuckGo fallback"}]


# ------------------------------------------------------------------------- brave


@pytest.mark.asyncio
async def test_brave_search(monkeypatch):
    async def mock_get(self, url, **kw):
        assert "brave" in url
        assert kw["headers"]["X-Subscription-Token"] == "brave-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        return _response(json={
            "web": {
                "results": [
                    {
                        "title": "NanoInfra",
                        "url": "https://example.com",
                        "description": "AI assistant",
                    }
                ]
            }
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    result = await _search(
        provider="brave", api_key="brave-key", user_agent="nanoinfra-search-test"
    ).run("nanoinfra", 1)
    assert "NanoInfra" in result.text
    assert "https://example.com" in result.text


@pytest.mark.asyncio
async def test_brave_search_retries_rate_limit_once(monkeypatch):
    """One retry, one second apart. A provider that rate limits once must not fail the search."""
    calls = {"n": 0}
    sleeps: list[float] = []

    async def mock_sleep(delay: float):
        sleeps.append(delay)

    async def mock_get(self, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response(status=429, json={"error": "rate limit"})
        return _response(json={
            "web": {
                "results": [
                    {"title": "Recovered", "url": "https://example.com", "description": "ok"}
                ]
            }
        })

    monkeypatch.setattr("nanoinfra.gates.fetcher.search.asyncio.sleep", mock_sleep)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    result = await _search(provider="brave", api_key="brave-key").run("nanoinfra", 1)

    assert calls["n"] == 2
    assert "Recovered" in result.text
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_brave_search_returns_clear_rate_limit_after_retries(monkeypatch):
    """The message names the cause and what to do, because the model cannot see the status code."""
    calls = {"n": 0}

    async def mock_sleep(delay: float):
        return None

    async def mock_get(self, url, **kw):
        calls["n"] += 1
        return _response(status=429, json={"error": "rate limit"})

    monkeypatch.setattr("nanoinfra.gates.fetcher.search.asyncio.sleep", mock_sleep)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    result = await _search(provider="brave", api_key="brave-key").run("nanoinfra", 1)

    assert calls["n"] == 2
    assert "Brave search rate limited" in result.text
    assert "consecutive web_search" in result.text
    assert result.is_error


@pytest.mark.asyncio
async def test_brave_fallback_to_duckduckgo_when_no_key(monkeypatch):
    """A missing key is a configuration gap, and an answer beats a refusal the model cannot fix."""
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    result = await _search(provider="brave", api_key="").run("test")
    assert "Fallback" in result.text


# ------------------------------------------------------------------------ tavily


@pytest.mark.asyncio
async def test_tavily_search(monkeypatch):
    async def mock_post(self, url, **kw):
        assert "tavily" in url
        assert kw["headers"]["Authorization"] == "Bearer tavily-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        return _response(json={
            "results": [{"title": "OpenClaw", "url": "https://openclaw.io", "content": "Framework"}]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(
        provider="tavily", api_key="tavily-key", user_agent="nanoinfra-search-test"
    ).run("openclaw")
    assert "OpenClaw" in result.text
    assert "https://openclaw.io" in result.text


# ---------------------------------------------------------------------- keenable


@pytest.mark.asyncio
async def test_keenable_search(monkeypatch):
    async def mock_post(self, url, **kw):
        assert "keenable" in url
        assert kw["headers"]["X-API-Key"] == "keen-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        assert kw["headers"]["X-Keenable-Title"] == "nanoinfra"
        return _response(json={
            "results": [
                {
                    "title": "Keen",
                    "url": "https://keenable.ai",
                    "description": "short",
                    "snippet": "longer excerpt",
                }
            ]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(
        provider="keenable", api_key="keen-key", user_agent="nanoinfra-search-test"
    ).run("keenable", 1)
    assert "Keen" in result.text
    assert "https://keenable.ai" in result.text
    assert "longer excerpt" in result.text


@pytest.mark.asyncio
async def test_keenable_without_api_key_uses_public_endpoint(monkeypatch):
    """Keenable serves a free tier without a token, so no key means the public path."""
    async def mock_post(self, url, **kw):
        assert url == "https://api.keenable.ai/v1/search/public"
        assert "X-API-Key" not in kw["headers"]
        assert kw["headers"]["X-Keenable-Title"] == "nanoinfra"
        return _response(json={
            "results": [{"title": "Public", "url": "https://keenable.ai/pub", "description": "ok"}]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
    result = await _search(provider="keenable", api_key="").run("keenable", 1)
    assert "Public" in result.text
    assert "https://keenable.ai/pub" in result.text


@pytest.mark.asyncio
async def test_keenable_search_uses_env_api_key(monkeypatch):
    async def mock_post(self, url, **kw):
        assert kw["headers"]["X-API-Key"] == "env-keen-key"
        return _response(json={
            "results": [{"title": "Env", "url": "https://keenable.ai/env", "description": "ok"}]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setenv("KEENABLE_API_KEY", "env-keen-key")
    result = await _search(provider="keenable", api_key="").run("keenable", 1)
    assert "Env" in result.text


@pytest.mark.asyncio
async def test_keenable_search_http_error(monkeypatch):
    async def mock_post(self, url, **kw):
        return _response(status=401, json={"error": "invalid key"})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(provider="keenable", api_key="bad-keen-key").run("keenable")
    assert "Error: Keenable search failed (401)" in result.text
    assert result.is_error


# ------------------------------------------------------------------------ serper


@pytest.mark.asyncio
async def test_serper_search(monkeypatch):
    async def mock_post(self, url, **kw):
        assert url == "https://google.serper.dev/search"
        assert kw["headers"]["X-API-KEY"] == "serper-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        assert kw["json"] == {"q": "serper", "num": 1}
        return _response(json={
            "organic": [
                {"title": "Serper", "link": "https://serper.dev", "snippet": "Google Search API"}
            ]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(
        provider="serper", api_key="serper-key", user_agent="nanoinfra-search-test"
    ).run("serper", 1)
    assert "Serper" in result.text
    assert "https://serper.dev" in result.text
    assert "Google Search API" in result.text


@pytest.mark.asyncio
async def test_serper_search_uses_env_api_key(monkeypatch):
    async def mock_post(self, url, **kw):
        assert kw["headers"]["X-API-KEY"] == "env-serper-key"
        return _response(json={
            "organic": [{"title": "Env", "link": "https://serper.dev/env", "snippet": "ok"}]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setenv("SERPER_API_KEY", "env-serper-key")
    result = await _search(provider="serper", api_key="").run("serper", 1)
    assert "Env" in result.text


@pytest.mark.asyncio
async def test_serper_fallback_to_duckduckgo_when_no_key(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    result = await _search(provider="serper", api_key="").run("serper", 1)
    assert "DuckDuckGo fallback" in result.text


@pytest.mark.asyncio
async def test_serper_search_http_error(monkeypatch):
    async def mock_post(self, url, **kw):
        return _response(status=403, json={"message": "Unauthorized"})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(provider="serper", api_key="bad-serper-key").run("serper")
    assert "Error: Serper search failed (403)" in result.text
    assert result.is_error


@pytest.mark.asyncio
async def test_serper_search_rate_limited(monkeypatch):
    async def mock_post(self, url, **kw):
        return _response(status=429, json={"message": "rate limited"})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(provider="serper", api_key="serper-key").run("serper")
    assert "Serper search rate limited" in result.text
    assert result.is_error


# ------------------------------------------------------------------------- bocha


@pytest.mark.asyncio
async def test_bocha_search(monkeypatch):
    async def mock_post(self, url, **kw):
        assert url == "https://api.bochaai.com/v1/web-search"
        assert kw["headers"]["Authorization"] == "Bearer bocha-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        assert kw["json"] == {
            "query": "MAI-THINKING-1 model",
            "freshness": "noLimit",
            "summary": True,
            "count": 2,
        }
        return _response(json={
            "webPages": {
                "value": [
                    {
                        "name": "MAI-THINKING-1 - Microsoft Research",
                        "url": "https://www.microsoft.com/research/maithinking-1",
                        "summary": (
                            "MAI-THINKING-1 is a 35B-active MoE model with strong reasoning "
                            "capabilities."
                        ),
                        "snippet": (
                            "MAI-THINKING-1 achieves 97.0% on AIME 2025 and 52.8% on SWE-Bench "
                            "Pro."
                        ),
                    }
                ]
            }
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(
        provider="bocha", api_key="bocha-key", user_agent="nanoinfra-search-test"
    ).run("MAI-THINKING-1 model", 2)

    assert "MAI-THINKING-1" in result.text
    assert "https://www.microsoft.com/research/maithinking-1" in result.text
    assert "35B-active MoE" in result.text


@pytest.mark.asyncio
async def test_bocha_missing_key_falls_back_to_duckduckgo(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)

    result = await _search(provider="bocha").run("test")

    assert "DuckDuckGo fallback" in result.text


@pytest.mark.asyncio
async def test_bocha_rate_limited(monkeypatch):
    async def mock_post(self, url, **kw):
        return _response(status=429, json={"error": "rate limit"})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(provider="bocha", api_key="bocha-key").run("test")

    assert "429" in result.text
    assert result.is_error


@pytest.mark.asyncio
async def test_bocha_honours_a_freshness_filter(monkeypatch):
    """The freshness filter reaches the provider, so the wire has to carry it.

    The old tool read this out of loose keyword arguments. The wire has a named field instead, and
    a fixed field set is what keeps a free-form member off the request.
    """
    seen: dict[str, object] = {}

    async def mock_post(self, url, **kw):
        seen.update(kw["json"])
        return _response(json={"webPages": {"value": []}})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    await _search(provider="bocha", api_key="bocha-key").run("test", freshness="oneWeek")

    assert seen["freshness"] == "oneWeek"


# -------------------------------------------------------------------- volcengine


@pytest.mark.asyncio
async def test_volcengine_search(monkeypatch):
    async def mock_post(self, url, **kw):
        assert url == "https://open.feedcoopapi.com/search_api/web_search"
        assert kw["headers"]["Authorization"] == "Bearer volc-key"
        assert kw["headers"]["X-Traffic-Tag"] == "nanoinfra"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        assert kw["json"] == {
            "Query": "北京周边游",
            "SearchType": "web",
            "Count": 2,
            "NeedSummary": True,
            "TimeRange": "OneWeek",
            "Filter": {"AuthInfoLevel": 1},
            "QueryControl": {"QueryRewrite": True},
        }
        return _response(json={
            "Result": {
                "WebResults": [
                    {
                        "Title": "北京周边游攻略",
                        "Url": "https://example.cn/travel",
                        "Summary": "适合周末出行的路线。",
                        "AuthInfoDes": "非常权威",
                    }
                ]
            }
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(
        provider="volcengine", api_key="volc-key", user_agent="nanoinfra-search-test"
    ).run("北京周边游", 2, time_range="OneWeek", auth_level=1, query_rewrite=True)

    assert "北京周边游攻略" in result.text
    assert "https://example.cn/travel" in result.text
    assert "非常权威" in result.text


@pytest.mark.asyncio
async def test_volcengine_missing_key_falls_back_to_duckduckgo(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)
    monkeypatch.delenv("VOLCENGINE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)

    result = await _search(provider="volcengine").run("test")

    assert "DuckDuckGo fallback" in result.text


@pytest.mark.asyncio
async def test_volcengine_invalid_time_range_returns_error():
    """A bad filter is refused before the request, and the message lists what is accepted."""
    result = await _search(provider="volcengine", api_key="volc-key").run(
        "test", time_range="Yesterday"
    )

    assert "timeRange must be" in result.text
    assert result.is_error


# ----------------------------------------------------------------------- searxng


@pytest.mark.asyncio
async def test_searxng_search(monkeypatch):
    async def mock_get(self, url, **kw):
        assert "searx.example" in url
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        return _response(json={
            "results": [
                {"title": "Result", "url": "https://example.com", "content": "SearXNG result"}
            ]
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    result = await _search(
        provider="searxng", base_url="https://searx.example", user_agent="nanoinfra-search-test"
    ).run("test")
    assert "Result" in result.text


@pytest.mark.asyncio
async def test_searxng_no_base_url_falls_back(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)

    result = await _search(provider="searxng", base_url="").run("test")
    assert "Fallback" in result.text


@pytest.mark.asyncio
async def test_searxng_invalid_url():
    """A self-hosted endpoint is operator input, and a bad one must not become a request."""
    result = await _search(provider="searxng", base_url="not-a-url").run("test")
    assert "Error" in result.text
    assert result.is_error


# -------------------------------------------------------------------- duckduckgo


@pytest.mark.asyncio
async def test_duckduckgo_search(monkeypatch):
    class MockDDGS:
        def __init__(self, **kw):
            pass

        def text(self, query, max_results=5):
            return [{"title": "DDG Result", "href": "https://ddg.example", "body": "From DuckDuckGo"}]

    monkeypatch.setattr("ddgs.DDGS", MockDDGS)

    result = await _search(provider="duckduckgo").run("hello")
    assert "DDG Result" in result.text


@pytest.mark.asyncio
async def test_duckduckgo_search_passes_proxy(monkeypatch):
    """DDGS client must receive the configured proxy so search works behind a proxy."""
    captured: dict[str, object] = {}
    proxy_url = "http://proxy.example:8080"

    class ProxyCaptorDDGS:
        def __init__(self, **kw):
            captured.update(kw)

        def text(self, query, max_results=5):
            return [{"title": "Result", "href": "https://example.com", "body": "OK"}]

    monkeypatch.setattr("ddgs.DDGS", ProxyCaptorDDGS)

    result = await WebSearch(provider="duckduckgo", proxy=proxy_url).run("test")
    assert captured["proxy"] == proxy_url
    assert captured["timeout"] == 10
    assert "Result" in result.text


@pytest.mark.asyncio
async def test_duckduckgo_timeout_returns_error(monkeypatch):
    """asyncio.wait_for guard should fire when DDG search hangs.

    ``ddgs`` is synchronous and runs in a thread, so without this guard one hung search would hold
    the fetcher's single request slot open.
    """
    import threading

    gate = threading.Event()

    class HangingDDGS:
        def __init__(self, **kw):
            pass

        def text(self, query, max_results=5):
            gate.wait(timeout=10)
            return []

    monkeypatch.setattr("ddgs.DDGS", HangingDDGS)
    result = await _search(provider="duckduckgo", timeout=0.2).run("test")
    gate.set()
    assert "Error" in result.text
    assert result.is_error


# -------------------------------------------------------------------------- jina


@pytest.mark.asyncio
async def test_jina_search(monkeypatch):
    async def mock_get(self, url, **kw):
        assert "s.jina.ai" in str(url)
        assert kw["headers"]["Authorization"] == "Bearer jina-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        return _response(json={
            "data": [{"title": "Jina Result", "url": "https://jina.ai", "content": "AI search"}]
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    result = await _search(
        provider="jina", api_key="jina-key", user_agent="nanoinfra-search-test"
    ).run("test")
    assert "Jina Result" in result.text
    assert "https://jina.ai" in result.text


@pytest.mark.asyncio
async def test_jina_search_uses_path_encoded_query(monkeypatch):
    """The query rides in the path, so it must be percent-encoded rather than sent raw."""
    calls: dict[str, object] = {}

    async def mock_get(self, url, **kw):
        calls["url"] = str(url)
        calls["params"] = kw.get("params")
        return _response(json={
            "data": [{"title": "Jina Result", "url": "https://jina.ai", "content": "AI search"}]
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    await _search(provider="jina", api_key="jina-key").run("hello world")
    assert str(calls["url"]).rstrip("/") == "https://s.jina.ai/hello%20world"
    assert calls["params"] in (None, {})


@pytest.mark.asyncio
async def test_jina_422_falls_back_to_duckduckgo(monkeypatch):
    async def mock_get(self, url, **kw):
        assert "s.jina.ai" in str(url)
        raise httpx.HTTPStatusError(
            "422 Unprocessable Entity",
            request=httpx.Request("GET", str(url)),
            response=httpx.Response(422, request=httpx.Request("GET", str(url))),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)

    result = await _search(provider="jina", api_key="jina-key").run("test")
    assert "DuckDuckGo fallback" in result.text


# -------------------------------------------------------------------------- kagi


@pytest.mark.asyncio
async def test_kagi_search(monkeypatch):
    async def mock_post(self, url, **kw):
        assert "kagi.com/api/v1/search" in url
        assert kw["headers"]["Authorization"] == "Bearer kagi-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        assert kw["json"] == {"query": "test", "limit": 2}
        return _response(json={
            "data": {
                "search": [
                    {"title": "Kagi Result", "url": "https://kagi.com", "snippet": "Premium search"},
                ],
                "related_search": [
                    {"title": "ignored related search", "url": "", "snippet": ""},
                ],
            }
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(
        provider="kagi", api_key="kagi-key", user_agent="nanoinfra-search-test"
    ).run("test", 2)
    assert "Kagi Result" in result.text
    assert "https://kagi.com" in result.text
    assert "ignored related search" not in result.text


@pytest.mark.asyncio
async def test_kagi_fallback_to_duckduckgo_when_no_key(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)
    monkeypatch.delenv("KAGI_API_KEY", raising=False)

    result = await _search(provider="kagi", api_key="").run("test")
    assert "Fallback" in result.text


# --------------------------------------------------------------------------- exa


@pytest.mark.asyncio
async def test_exa_search(monkeypatch):
    async def mock_post(self, url, **kw):
        assert url == "https://api.exa.ai/search"
        assert kw["headers"]["x-api-key"] == "exa-key"
        assert kw["headers"]["User-Agent"] == "nanoinfra-search-test"
        assert kw["json"] == {
            "query": "test",
            "numResults": 2,
            "contents": {"highlights": True},
        }
        return _response(json={
            "results": [
                {
                    "title": "Exa Result",
                    "url": "https://exa.ai",
                    "highlights": ["Relevant Exa highlight"],
                }
            ]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(
        provider="exa", api_key="exa-key", user_agent="nanoinfra-search-test"
    ).run("test", 2)

    assert "Exa Result" in result.text
    assert "https://exa.ai" in result.text
    assert "Relevant Exa highlight" in result.text


@pytest.mark.asyncio
async def test_exa_search_uses_env_api_key(monkeypatch):
    async def mock_post(self, url, **kw):
        assert kw["headers"]["x-api-key"] == "env-exa-key"
        return _response(json={
            "results": [
                {
                    "title": "Env Exa Result",
                    "url": "https://exa.ai/env",
                    "summary": "Summary fallback",
                }
            ]
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setenv("EXA_API_KEY", "env-exa-key")
    result = await _search(provider="exa", api_key="").run("test", 1)

    assert "Env Exa Result" in result.text
    assert "Summary fallback" in result.text


@pytest.mark.asyncio
async def test_exa_search_http_error(monkeypatch):
    async def mock_post(self, url, **kw):
        return _response(status=401, json={"error": "invalid key"})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    result = await _search(provider="exa", api_key="bad-exa-key").run("test")

    assert "Error: Exa search failed (401)" in result.text
    assert result.is_error


@pytest.mark.asyncio
async def test_exa_fallback_to_duckduckgo_when_no_key(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _MockDDGS)
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    result = await _search(provider="exa", api_key="").run("test")
    assert "Fallback" in result.text


# ----------------------------------------------------------------------- olostep


@pytest.mark.asyncio
async def test_olostep_search_formats_answer_and_sources(monkeypatch):
    from types import SimpleNamespace

    calls: dict[str, str] = {}

    class MockAsyncOlostep:
        def __init__(self, api_key: str):
            calls["api_key"] = api_key
            self.answers = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def create(self, task: str):
            calls["task"] = task
            return SimpleNamespace(
                answer="Mocked Olostep answer",
                sources=[SimpleNamespace(title="Example Source", url="https://example.com")],
            )

    import sys
    import types

    fake_mod = types.ModuleType("olostep")
    fake_mod.AsyncOlostep = MockAsyncOlostep
    fake_mod.Olostep_BaseError = Exception
    monkeypatch.setitem(sys.modules, "olostep", fake_mod)

    result = await _search(provider="olostep", api_key="olostep-key").run("test query")

    assert calls["api_key"] == "olostep-key"
    assert calls["task"] == "test query"
    assert "Mocked Olostep answer" in result.text
    assert "Example Source" in result.text
    assert "https://example.com" in result.text


@pytest.mark.asyncio
async def test_olostep_missing_key_falls_back_to_duckduckgo(monkeypatch):
    import sys
    import types
    from unittest.mock import patch

    fake_mod = types.ModuleType("olostep")
    fake_mod.AsyncOlostep = object
    fake_mod.Olostep_BaseError = Exception
    monkeypatch.setitem(sys.modules, "olostep", fake_mod)

    monkeypatch.delenv("OLOSTEP_API_KEY", raising=False)
    with patch("ddgs.DDGS", _MockDDGS):
        result = await _search(provider="olostep", api_key="").run("test query")

    assert "Fallback" in result.text


@pytest.mark.asyncio
async def test_olostep_package_missing_returns_install_hint(monkeypatch):
    """An optional library that is absent gets a message that says how to fix it."""
    import sys

    monkeypatch.delitem(sys.modules, "olostep", raising=False)
    monkeypatch.setitem(sys.modules, "olostep", None)
    result = await _search(provider="olostep", api_key="olostep-key").run("test query")

    assert result.text == "Error: olostep package not installed. Run: pip install olostep"
    assert result.is_error


# ------------------------------------------------------------ provider selection


@pytest.mark.asyncio
async def test_unknown_provider():
    result = await _search(provider="unknown").run("test")
    assert "unknown" in result.text
    assert "Error" in result.text
    assert result.is_error


@pytest.mark.asyncio
async def test_default_provider_is_brave(monkeypatch):
    """An empty provider name means brave, so a blank config still searches."""
    async def mock_get(self, url, **kw):
        assert "brave" in url
        return _response(json={"web": {"results": []}})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    result = await _search(provider="", api_key="test-key").run("test")
    assert "No results" in result.text


@pytest.mark.asyncio
async def test_the_count_is_clamped_to_ten(monkeypatch):
    """A provider request must not carry a count the caller invented."""
    seen: dict[str, object] = {}

    async def mock_get(self, url, **kw):
        seen.update(kw["params"])
        return _response(json={"web": {"results": []}})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    await _search(provider="brave", api_key="brave-key").run("test", 500)

    assert seen["count"] == 10


# ------------------------------------------------------------- the provider key


@pytest.mark.asyncio
async def test_the_reply_never_carries_the_provider_key(monkeypatch):
    """The fetcher holds the key. The agent, and so the model, must never read it.

    A compromise of the fetcher yields this key. A reply that carried it would extend that to a
    compromise of the agent, and the model reads whatever the agent holds.
    """
    async def mock_get(self, url, **kw):
        return _response(json={
            "web": {
                "results": [
                    {"title": "NanoInfra", "url": "https://example.com", "description": "ok"}
                ]
            }
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    result = await _search(provider="brave", api_key="super-secret-brave-key").run("test")

    assert "super-secret-brave-key" not in result.text
