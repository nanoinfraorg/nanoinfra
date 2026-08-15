"""web_search inside the fetcher process -- nanoinfraorg/nanoinfra#19.

Eleven providers, one shape of result. Every provider call is an egress call, and most need an API
key, so this is the one credential the fetcher holds. That key buys search results. It authorizes
nothing on any host, it opens no transport, and the credential store that holds host credentials
lives in the executor (#18) where this process cannot reach it.

A provider without its key falls back to DuckDuckGo rather than fails. The fallback is old
behaviour and it stays: a missing key is a configuration gap, and a search that still answers is
more useful than a refusal the model cannot fix.

DuckDuckGo runs through ``ddgs``, which is not safe to call concurrently. The old tool declared
itself exclusive to get that serialization from the agent's runner. The fetcher gets it from the
process: the server answers one connection at a time, so two searches never overlap in here.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx
from loguru import logger

from nanoinfra.gates.fetcher.egress import (
    DEFAULT_USER_AGENT,
    Payload,
    error,
    format_results,
    validate_url,
)

BOCHA_SEARCH_API_URL = "https://api.bochaai.com/v1/web-search"
KEENABLE_SEARCH_API_URL = "https://api.keenable.ai/v1/search"
VOLCENGINE_SEARCH_API_URL = "https://open.feedcoopapi.com/search_api/web_search"

_VOLCENGINE_TRAFFIC_TAG = "nanoinfra"
_VOLCENGINE_TIME_RANGES = {"OneDay", "OneWeek", "OneMonth", "OneYear"}
_VOLCENGINE_DATE_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$")


def _normalize_volcengine_time_range(value: Any) -> str | None:
    if value is None:
        return None
    time_range = str(value).strip()
    if not time_range:
        return None
    if time_range in _VOLCENGINE_TIME_RANGES or _VOLCENGINE_DATE_RANGE_RE.fullmatch(time_range):
        return time_range
    raise ValueError(
        "timeRange must be OneDay, OneWeek, OneMonth, OneYear, "
        "or YYYY-MM-DD..YYYY-MM-DD"
    )


def _normalize_volcengine_auth_level(value: Any) -> int | None:
    if value is None:
        return None
    try:
        auth_level = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("authLevel must be 0 or 1") from exc
    if auth_level not in {0, 1}:
        raise ValueError("authLevel must be 0 or 1")
    return auth_level


@dataclass(slots=True)
class WebSearch:
    """One search backend. Holds the provider choice, its key, and the egress settings."""

    provider: str = "duckduckgo"
    api_key: str = ""
    base_url: str = ""
    max_results: int = 5
    timeout: float = 30
    proxy: str | None = None
    user_agent: str = DEFAULT_USER_AGENT

    async def run(
        self,
        query: str,
        count: int | None = None,
        *,
        time_range: str | None = None,
        auth_level: int | None = None,
        query_rewrite: bool | None = None,
        freshness: str | None = None,
    ) -> Payload:
        """Search with the configured provider, or say why the fetcher could not."""
        provider = self.provider.strip().lower() or "brave"
        n = min(max(count or self.max_results, 1), 10)

        if provider == "olostep":
            return await self._search_olostep(query, n)
        if provider == "volcengine":
            return await self._search_volcengine(
                query,
                n,
                time_range=time_range,
                auth_level=auth_level,
                query_rewrite=query_rewrite,
            )
        if provider == "duckduckgo":
            return await self._search_duckduckgo(query, n)
        elif provider == "tavily":
            return await self._search_tavily(query, n)
        elif provider == "searxng":
            return await self._search_searxng(query, n)
        elif provider == "jina":
            return await self._search_jina(query, n)
        elif provider == "brave":
            return await self._search_brave(query, n)
        elif provider == "kagi":
            return await self._search_kagi(query, n)
        elif provider == "exa":
            return await self._search_exa(query, n)
        elif provider == "bocha":
            return await self._search_bocha(query, n, freshness=freshness or "noLimit")
        elif provider == "keenable":
            return await self._search_keenable(query, n)
        elif provider == "serper":
            return await self._search_serper(query, n)
        else:
            return error(f"Error: unknown search provider '{provider}'")

    def _key(self, *env_names: str) -> str:
        """The configured key, or the first environment variable that carries one."""
        if self.api_key:
            return self.api_key
        for name in env_names:
            value = os.environ.get(name, "")
            if value:
                return value
        return ""

    async def _search_olostep(self, query: str, n: int) -> Payload:
        try:
            from olostep import (  # pyright: ignore[reportMissingImports, reportMissingTypeStubs]
                AsyncOlostep,  # pyright: ignore[reportUnknownVariableType]
                Olostep_BaseError,  # pyright: ignore[reportUnknownVariableType, reportAttributeAccessIssue]
            )
        except ImportError:
            return error("Error: olostep package not installed. Run: pip install olostep")
        async_olostep = cast(Any, AsyncOlostep)
        olostep_base_error = cast(type[Exception], Olostep_BaseError)
        api_key = self._key("OLOSTEP_API_KEY")
        if not api_key:
            logger.warning("OLOSTEP_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            async with async_olostep(api_key=api_key) as client:
                if self.proxy:
                    transport = getattr(client, "_transport", None)
                    http_client = getattr(transport, "_client", None)
                    if transport is not None and isinstance(http_client, httpx.AsyncClient):
                        await http_client.aclose()
                        transport._client = httpx.AsyncClient(  # type: ignore[attr-defined]
                            proxy=self.proxy,
                            headers=dict(http_client.headers),
                            timeout=http_client.timeout,
                            limits=httpx.Limits(
                                max_keepalive_connections=100,
                                max_connections=200,
                            ),
                            http2=True,
                        )
                result: Any = await client.answers.create(task=query)

            sources = cast(list[Any], getattr(result, "sources", None) or [])
            source_lines: list[str] = []
            for i, source_value in enumerate(sources[:n], 1):
                source: Any = source_value
                if isinstance(source, dict):
                    source_dict = cast(dict[str, Any], source)
                    title = source_dict.get("title", "")
                    url = source_dict.get("url", "")
                else:
                    title = getattr(source, "title", "")
                    url = getattr(source, "url", "")
                if title and url:
                    source_lines.append(f"{i}. {title} — {url}")
                elif url:
                    source_lines.append(f"{i}. {url}")
                elif title:
                    source_lines.append(f"{i}. {title}")

            answer_text = getattr(result, "answer", "") or ""
            items = [{
                "title": answer_text or "Olostep answer",
                "url": "",
                "content": "\n".join(source_lines),
            }]
            return Payload(format_results(query, items, n))
        except olostep_base_error as e:
            return error(f"Error: Olostep search error: {type(e).__name__}: {e}")
        except Exception as e:
            return error(f"Error: Olostep search error: {type(e).__name__}: {e}")

    async def _search_brave(self, query: str, n: int) -> Payload:
        api_key = self._key("BRAVE_API_KEY")
        if not api_key:
            logger.warning("BRAVE_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            headers = {
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
                "User-Agent": self.user_agent,
            }
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r: httpx.Response | None = None
                for attempt in range(2):
                    r = await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": n},
                        headers=headers,
                        timeout=10.0,
                    )
                    if r.status_code != 429:
                        break
                    if attempt == 0:
                        logger.warning("Brave search rate limited; retrying once in 1.0s")
                        await asyncio.sleep(1.0)
                assert r is not None
                r.raise_for_status()
            items = [
                {
                    "title": x.get("title", ""),
                    "url": x.get("url", ""),
                    "content": x.get("description", ""),
                }
                for x in r.json().get("web", {}).get("results", [])
            ]
            return Payload(format_results(query, items, n))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return error(
                    "Error: Brave search rate limited after retry. "
                    "Retry later or reduce consecutive web_search calls."
                )
            return error(f"Error: {e}")
        except Exception as e:
            return error(f"Error: {e}")

    async def _search_tavily(self, query: str, n: int) -> Payload:
        api_key = self._key("TAVILY_API_KEY")
        if not api_key:
            logger.warning("TAVILY_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {api_key}", "User-Agent": self.user_agent},
                    json={"query": query, "max_results": n},
                    timeout=15.0,
                )
                r.raise_for_status()
            return Payload(format_results(query, r.json().get("results", []), n))
        except Exception as e:
            return error(f"Error: {e}")

    async def _search_keenable(self, query: str, n: int) -> Payload:
        api_key = self._key("KEENABLE_API_KEY")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "X-Keenable-Title": "nanoinfra",
        }
        # Without a key, the token-less /public endpoint serves the free tier.
        url = KEENABLE_SEARCH_API_URL
        if api_key:
            headers["X-API-Key"] = api_key
        else:
            url += "/public"
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    url,
                    headers=headers,
                    json={"query": query},
                    timeout=float(self.timeout),
                )
                r.raise_for_status()
            items = [
                {
                    "title": x.get("title", ""),
                    "url": x.get("url", ""),
                    "content": x.get("snippet") or x.get("description", ""),
                }
                for x in r.json().get("results", [])
            ]
            return Payload(format_results(query, items, n))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return error(
                    "Error: Keenable search rate limited. "
                    "Try again later or reduce search frequency."
                )
            return error(f"Error: Keenable search failed ({e.response.status_code}): {e}")
        except Exception as e:
            return error(f"Error: Keenable search failed: {e}")

    async def _search_searxng(self, query: str, n: int) -> Payload:
        base_url = (self.base_url or os.environ.get("SEARXNG_BASE_URL", "")).strip()
        if not base_url:
            logger.warning("SEARXNG_BASE_URL not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        endpoint = f"{base_url.rstrip('/')}/search"
        is_valid, error_msg = validate_url(endpoint)
        if not is_valid:
            return error(f"Error: invalid SearXNG URL: {error_msg}")
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    endpoint,
                    params={"q": query, "format": "json"},
                    headers={"User-Agent": self.user_agent},
                    timeout=10.0,
                )
                r.raise_for_status()
            return Payload(format_results(query, r.json().get("results", []), n))
        except Exception as e:
            return error(f"Error: {e}")

    async def _search_jina(self, query: str, n: int) -> Payload:
        api_key = self._key("JINA_API_KEY")
        if not api_key:
            logger.warning("JINA_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": self.user_agent,
            }
            encoded_query = quote(query, safe="")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    f"https://s.jina.ai/{encoded_query}",
                    headers=headers,
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json().get("data", [])[:n]
            items = [
                {
                    "title": d.get("title", ""),
                    "url": d.get("url", ""),
                    "content": d.get("content", "")[:500],
                }
                for d in data
            ]
            return Payload(format_results(query, items, n))
        except Exception as e:
            logger.warning("Jina search failed ({}), falling back to DuckDuckGo", e)
            return await self._search_duckduckgo(query, n)

    async def _search_kagi(self, query: str, n: int) -> Payload:
        api_key = self._key("KAGI_API_KEY")
        if not api_key:
            logger.warning("KAGI_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    "https://kagi.com/api/v1/search",
                    json={"query": query, "limit": n},
                    headers={"Authorization": f"Bearer {api_key}", "User-Agent": self.user_agent},
                    timeout=10.0,
                )
                r.raise_for_status()
            items = [
                {"title": d.get("title", ""), "url": d.get("url", ""), "content": d.get("snippet", "")}
                for d in r.json().get("data", {}).get("search", [])
            ]
            return Payload(format_results(query, items, n))
        except Exception as e:
            return error(f"Error: {e}")

    async def _search_exa(self, query: str, n: int) -> Payload:
        api_key = self._key("EXA_API_KEY")
        if not api_key:
            logger.warning("EXA_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "User-Agent": self.user_agent,
            }
            body = {
                "query": query,
                "numResults": n,
                "contents": {"highlights": True},
            }
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.exa.ai/search",
                    headers=headers,
                    json=body,
                    timeout=float(self.timeout),
                )
                r.raise_for_status()
            data = cast(dict[str, Any], r.json())
            items: list[dict[str, Any]] = []
            for result_value in cast(list[object], data.get("results", [])):
                if not isinstance(result_value, dict):
                    continue
                result = cast(dict[str, Any], result_value)
                highlights: Any = result.get("highlights") or []
                if isinstance(highlights, list):
                    content = "\n".join(
                        str(highlight)
                        for highlight in cast(list[object], highlights)
                        if highlight
                    )
                else:
                    content = str(highlights)
                if not content:
                    content = str(result.get("summary") or result.get("text") or "")[:500]
                items.append(
                    {
                        "title": result.get("title", ""),
                        "url": result.get("url", ""),
                        "content": content,
                    }
                )
            return Payload(format_results(query, items, n))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return error(
                    "Error: Exa search rate limited. Try again later or reduce search frequency."
                )
            return error(f"Error: Exa search failed ({e.response.status_code}): {e}")
        except Exception as e:
            return error(f"Error: Exa search failed: {e}")

    async def _search_serper(self, query: str, n: int) -> Payload:
        """Search via Serper.dev (Google Search API)."""
        api_key = self._key("SERPER_API_KEY")
        if not api_key:
            logger.warning("SERPER_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            headers = {
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            }
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    "https://google.serper.dev/search",
                    headers=headers,
                    json={"q": query, "num": n},
                    timeout=float(self.timeout),
                )
                r.raise_for_status()
            data = cast(dict[str, Any], r.json())
            organic = cast(list[object], data.get("organic", []))
            items: list[dict[str, Any]] = [
                {
                    "title": result.get("title", ""),
                    "url": result.get("link", ""),
                    "content": result.get("snippet", ""),
                }
                for result_value in organic
                if isinstance(result_value, dict)
                for result in (cast(dict[str, Any], result_value),)
            ]
            return Payload(format_results(query, items, n))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return error(
                    "Error: Serper search rate limited. Try again later or reduce search frequency."
                )
            return error(f"Error: Serper search failed ({e.response.status_code}): {e}")
        except Exception as e:
            return error(f"Error: Serper search failed: {e}")

    async def _search_volcengine(
        self,
        query: str,
        n: int,
        *,
        time_range: str | None = None,
        auth_level: int | None = None,
        query_rewrite: bool | None = None,
    ) -> Payload:
        api_key = self._key("VOLCENGINE_SEARCH_API_KEY", "WEB_SEARCH_API_KEY")
        if not api_key:
            logger.warning(
                "VOLCENGINE_SEARCH_API_KEY/WEB_SEARCH_API_KEY not set, falling back to DuckDuckGo"
            )
            return await self._search_duckduckgo(query, n)

        try:
            normalized_time_range = (
                _normalize_volcengine_time_range(time_range) if time_range else None
            )
            normalized_auth_level = (
                _normalize_volcengine_auth_level(auth_level) if auth_level is not None else None
            )
        except ValueError as e:
            return error(f"Error: {e}")

        body: dict[str, Any] = {
            "Query": query,
            "SearchType": "web",
            "Count": n,
            "NeedSummary": True,
        }
        if normalized_time_range:
            body["TimeRange"] = normalized_time_range
        if normalized_auth_level is not None:
            body["Filter"] = {"AuthInfoLevel": normalized_auth_level}
        if query_rewrite:
            body["QueryControl"] = {"QueryRewrite": True}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "X-Traffic-Tag": _VOLCENGINE_TRAFFIC_TAG,
        }
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    VOLCENGINE_SEARCH_API_URL,
                    headers=headers,
                    json=body,
                    timeout=float(self.timeout),
                )
                r.raise_for_status()
            data = cast(dict[str, Any], r.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return error(
                    "Error: Volcengine search rate limited. "
                    "Try again later or reduce search frequency."
                )
            return error(f"Error: Volcengine search failed ({e.response.status_code}): {e}")
        except Exception as e:
            return error(f"Error: Volcengine search failed: {e}")

        response_metadata = cast(
            dict[str, Any],
            data.get("ResponseMetadata") or {},
        )
        provider_error = (
            response_metadata.get("Error")
            or data.get("Error")
            or data.get("error")
        )
        if provider_error:
            if isinstance(provider_error, dict):
                provider_error = cast(dict[str, Any], provider_error)
                code = provider_error.get("Code") or provider_error.get("code") or "unknown"
                message = (
                    provider_error.get("Message")
                    or provider_error.get("message")
                    or provider_error
                )
                return error(f"Error: Volcengine search error {code}: {message}")
            return error(f"Error: Volcengine search error: {provider_error}")

        result = cast(dict[str, Any], data.get("Result") or data)
        web_results = cast(
            list[object],
            result.get("WebResults")
            or result.get("webResults")
            or result.get("results")
            or [],
        )
        items: list[dict[str, Any]] = []
        for item_value in web_results:
            if not isinstance(item_value, dict):
                continue
            item = cast(dict[str, Any], item_value)
            meta_parts = [
                str(part)
                for part in (
                    item.get("SiteName") or item.get("siteName") or item.get("Site"),
                    item.get("AuthInfoDes") or item.get("authInfoDes"),
                    item.get("PublishTime") or item.get("publishTime"),
                )
                if part
            ]
            summary = cast(str, (
                item.get("Summary")
                or item.get("summary")
                or item.get("Snippet")
                or item.get("snippet")
                or item.get("Content")
                or item.get("content")
                or ""
            ))
            content = "\n".join(part for part in (" | ".join(meta_parts), summary) if part)
            items.append(
                {
                    "title": item.get("Title") or item.get("title") or "",
                    "url": item.get("Url") or item.get("URL") or item.get("url") or "",
                    "content": content,
                }
            )

        return Payload(format_results(query, items, n))

    async def _search_duckduckgo(self, query: str, n: int) -> Payload:
        try:
            # Note: duckduckgo_search is synchronous and does its own requests
            # We run it in a thread to avoid blocking the loop
            from ddgs import DDGS  # pyright: ignore[reportUnknownVariableType]

            ddgs_type = cast(Any, DDGS)
            ddgs = ddgs_type(timeout=10, proxy=self.proxy)
            raw = await asyncio.wait_for(
                asyncio.to_thread(ddgs.text, query, max_results=n),
                timeout=self.timeout,
            )
            if not raw:
                return Payload(f"No results for: {query}")
            raw_items = cast(list[dict[str, Any]], raw)
            items: list[dict[str, Any]] = [
                {"title": r.get("title", ""), "url": r.get("href", ""), "content": r.get("body", "")}
                for r in raw_items
            ]
            return Payload(format_results(query, items, n))
        except Exception as e:
            logger.warning("DuckDuckGo search failed: {}", e)
            return error(f"Error: DuckDuckGo search failed ({e})")

    async def _search_bocha(self, query: str, n: int, freshness: str = "noLimit") -> Payload:
        api_key = self._key("BOCHA_API_KEY")
        if not api_key:
            logger.warning("BOCHA_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            if self.user_agent:
                headers["User-Agent"] = self.user_agent
            payload = {
                "query": query,
                "freshness": freshness,
                "summary": True,
                "count": n,
            }
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    BOCHA_SEARCH_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                if r.status_code == 429:
                    return error("Error: Bocha search rate-limited (HTTP 429). Wait and retry.")
                r.raise_for_status()
            data = cast(dict[str, Any], r.json())
            wrapped_data = data.get("data")
            result_data = (
                cast(dict[str, Any], wrapped_data)
                if isinstance(wrapped_data, dict)
                else data
            )
            web_pages_data = cast(
                dict[str, Any],
                result_data.get("webPages", {}),
            )
            web_pages = cast(list[dict[str, Any]], web_pages_data.get("value", []))
            items: list[dict[str, Any]] = [
                {
                    "title": x.get("name", ""),
                    "url": x.get("url", ""),
                    "content": x.get("summary", "") or x.get("snippet", ""),
                }
                for x in web_pages
            ]
            return Payload(format_results(query, items, n))
        except httpx.HTTPStatusError as e:
            return error(
                f"Error: Bocha search HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except Exception as e:
            return error(f"Error: {e}")


__all__ = [
    "BOCHA_SEARCH_API_URL",
    "KEENABLE_SEARCH_API_URL",
    "VOLCENGINE_SEARCH_API_URL",
    "WebSearch",
]
