"""Web tools: web_search and web_fetch -- nanoinfraorg/nanoinfra#19.

Item 16 moved the implementation into the fetcher process. ``nanoinfra/gates/fetcher/`` holds it
now: ``egress.py`` for the URL checks and the redirect walk, ``fetch.py`` for web_fetch, and
``search.py`` for the eleven search providers. That process has broad egress, no host credential,
and no way to run a program. Untrusted content enters there and stops there.

This module is the agent side of that wire, and it copies ``server_execution.py`` (#18). It writes
one request to a Unix domain socket and it renders the reply.

The import list is the security property, not a style choice. This file must import no HTTP client,
no provider package, and not ``nanoinfra.security.network``. A module that imports one of those can
send a request without the fetcher, so the split it claims would be false.
``tests/agent/tools/test_web_client.py`` walks this file's whole syntax tree to assert the absence.
A lazy import inside a function would satisfy a grep and fail that test.

There is no fallback. An unreachable fetcher produces a deployment fault, and never an in-process
fetch. A fallback fetch would put the egress back in the agent, next to the credential store.

The config classes and ``SEARCH_PROVIDER_OPTIONS`` stay in this module. The config schema, the
WebUI settings API, and the CLI wizard all import them from here. The fetcher reads the same
settings through ``nanoinfra.gates.fetcher.server.load_web_settings`` on every request, so an
operator who changes the provider reaches the next search.
"""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.context import ToolContext
from nanoinfra.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanoinfra.config_base import Base
from nanoinfra.gates.fetcher.client import FetcherClient, FetcherUnavailableError

if TYPE_CHECKING:
    from nanoinfra.gates.fetcher.protocol import FetchResponse

# How a deployment tells the tool where the fetcher listens. The container start and the gateway
# start both export it, so the tool reads a path rather than a guess. A guess reads to an operator
# as a fetcher that does not run.
FETCHER_SOCKET_ENV = "NANOINFRA_FETCHER_SOCKET"

# Where the fetcher listens when no deployment names a path. The gateway binds the same place.
DEFAULT_SOCKET_NAME = "fetcher.sock"

# The words for a fetcher nobody started. They must differ from the words for a page that failed.
# An operator who reads one as the other looks for the fault in the wrong place.
FETCHER_UNAVAILABLE_NOTE = (
    "This is a deployment fault rather than a web error. Check that the fetcher process runs. "
    "Nothing reached the network, and no other tool answers this request."
)


# Single source of truth for selectable search providers (CLI wizard + WebUI).
# "credential" describes what each provider needs: none / api_key / base_url /
# optional_api_key.
SEARCH_PROVIDER_OPTIONS: tuple[dict[str, str], ...] = (
    {"name": "duckduckgo", "label": "DuckDuckGo", "credential": "none"},
    {"name": "brave", "label": "Brave Search", "credential": "api_key"},
    {"name": "tavily", "label": "Tavily", "credential": "api_key"},
    {"name": "searxng", "label": "SearXNG", "credential": "base_url"},
    {"name": "jina", "label": "Jina", "credential": "api_key"},
    {"name": "kagi", "label": "Kagi", "credential": "api_key"},
    {"name": "exa", "label": "Exa", "credential": "api_key"},
    {"name": "olostep", "label": "Olostep", "credential": "api_key"},
    {"name": "bocha", "label": "Bocha", "credential": "api_key"},
    {"name": "volcengine", "label": "Volcengine Search", "credential": "api_key"},
    {"name": "keenable", "label": "Keenable", "credential": "optional_api_key"},
)


class WebSearchConfig(Base):
    """Web search configuration."""
    provider: str = "duckduckgo"
    api_key: str = ""
    base_url: str = ""
    max_results: int = 5
    timeout: int = 30


class WebFetchConfig(Base):
    """Web fetch tool configuration."""
    use_jina_reader: bool = True


class WebToolsConfig(Base):
    """Web tools configuration."""
    enable: bool = True
    proxy: str | None = None
    user_agent: str | None = None
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)


def default_socket_path() -> Path:
    """Return the fetcher socket path this install uses.

    The environment wins, because only the deployment knows where it put the socket. The container
    puts it under /run on a root-owned path, and a pip install puts it in the data directory.
    """
    from nanoinfra.config.paths import get_data_dir

    named = os.environ.get(FETCHER_SOCKET_ENV, "").strip()
    if named:
        return Path(named)
    return get_data_dir() / "run" / DEFAULT_SOCKET_NAME


def _client(ctx: ToolContext) -> FetcherClient:
    """Build the client for one tool. A caller may name the socket, or the deployment does."""
    socket_path = getattr(ctx, "fetcher_socket", None) or default_socket_path()
    return FetcherClient(socket_path)


def _render(response: FetchResponse) -> Any:
    """Turn one reply into what the model reads.

    The order matters. ``error`` marks a frame the fetcher refused, so it answers first.
    ``is_error`` marks a tool-level failure such as a rate-limited provider. ``blocks`` carries the
    native image blocks, and the model needs those rather than a JSON copy of the same bytes.
    """
    if response.error:
        return ToolResult.error(response.error)
    if response.is_error:
        return ToolResult.error(response.body)
    if response.blocks is not None:
        return response.blocks
    return response.body


def _unavailable(exc: FetcherUnavailableError) -> ToolResult:
    """Report a fetcher nobody started, and never a page that failed."""
    return ToolResult.error(f"The fetcher is not reachable: {exc} {FETCHER_UNAVAILABLE_NOTE}")


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Search query"),
        count=IntegerSchema(description="Results (1-10)", minimum=1, maximum=10),
        timeRange=StringSchema(
            "Optional time filter for providers that support it: "
            "OneDay, OneWeek, OneMonth, OneYear, or YYYY-MM-DD..YYYY-MM-DD",
        ),
        authLevel=IntegerSchema(
            description="Optional authority filter for providers that support it: 0=all, 1=authoritative",
            minimum=0,
            maximum=1,
        ),
        queryRewrite=BooleanSchema(
            description="Optional provider-side query rewrite for conversational or ambiguous searches",
        ),
        required=["query"],
    )
)
class WebSearchTool(Tool):
    """Ask the fetcher to search the web.

    The tool names no provider and holds no key. The fetcher owns both, and the reply has no field
    that could carry a key back to the agent.
    """

    capability_class = "read"

    _scopes = {"core", "subagent"}

    name = "web_search"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "Search the web. Returns titles, URLs, and snippets. "
        "count defaults to 5 (max 10). "
        "Some providers support timeRange, authLevel, and queryRewrite. "
        "Use web_fetch to read a specific page in full."
    )

    config_key = "web"

    @classmethod
    def config_cls(cls) -> type[WebToolsConfig]:
        return WebToolsConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.config.web.enable

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(client=_client(ctx))

    def __init__(self, *, client: FetcherClient | None = None) -> None:
        self.client = client if client is not None else FetcherClient(default_socket_path())

    @property
    def read_only(self) -> bool:
        return True

    # The tool declares no `exclusive`. The old tool asked the runner to serialize a DuckDuckGo
    # search, because ``ddgs`` is not safe to call concurrently. The fetcher answers one connection
    # at a time, so the process supplies that serialization now.

    async def execute(
        self,
        query: str,
        count: int | None = None,
        time_range: str | None = None,
        auth_level: int | None = None,
        query_rewrite: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        # The model writes the camelCase names the schema declares. The wire fields are snake_case,
        # and this is the one place that translates between the two.
        time_range = kwargs.pop("timeRange", time_range)
        auth_level = kwargs.pop("authLevel", auth_level)
        query_rewrite = kwargs.pop("queryRewrite", query_rewrite)
        # Bocha reads a freshness filter. The schema declares no such parameter, and a caller that
        # passes one still reaches the provider.
        freshness = kwargs.pop("freshness", None)

        try:
            # The client blocks on a socket, so it runs off the event loop. The fetcher owns the
            # per-request timeouts, and a search can take seconds.
            response = await asyncio.to_thread(
                self.client.search,
                query=query,
                count=count,
                time_range=time_range,
                auth_level=auth_level,
                query_rewrite=query_rewrite,
                freshness=freshness,
            )
        except FetcherUnavailableError as exc:
            return _unavailable(exc)

        return _render(response)


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to fetch"),
        extractMode={
            "type": "string",
            "enum": ["markdown", "text"],
            "default": "markdown",
        },
        maxChars=IntegerSchema(minimum=100),
        required=["url"],
    )
)
class WebFetchTool(Tool):
    """Ask the fetcher to read one URL.

    The URL check, the redirect walk, and the reader all live in the fetcher. This tool validates
    nothing, because a second copy of the check would drift from the one that runs.
    """

    capability_class = "read"

    _scopes = {"core", "subagent"}

    name = "web_fetch"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "Fetch a URL and extract readable content (HTML → markdown/text). "
        "Output is capped at maxChars (default 50 000). "
        "Works for most web pages and docs; may fail on login-walled or JS-heavy sites."
    )

    config_key = "web"

    @classmethod
    def config_cls(cls) -> type[WebToolsConfig]:
        return WebToolsConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.config.web.enable

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(client=_client(ctx))

    def __init__(self, *, client: FetcherClient | None = None) -> None:
        self.client = client if client is not None else FetcherClient(default_socket_path())

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        url: str,
        extract_mode: str = "markdown",
        max_chars: int | None = None,
        **kwargs: Any,
    ) -> Any:
        extract_mode = kwargs.pop("extractMode", extract_mode)
        # A None cap tells the fetcher to apply its own default. A default here would hide the
        # fetcher's value and give two answers to one question.
        max_chars = kwargs.pop("maxChars", max_chars)

        try:
            response = await asyncio.to_thread(
                self.client.fetch, url=url, extract_mode=extract_mode, max_chars=max_chars
            )
        except FetcherUnavailableError as exc:
            return _unavailable(exc)

        return _render(response)


__all__ = [
    "DEFAULT_SOCKET_NAME",
    "FETCHER_SOCKET_ENV",
    "FETCHER_UNAVAILABLE_NOTE",
    "SEARCH_PROVIDER_OPTIONS",
    "WebFetchConfig",
    "WebFetchTool",
    "WebSearchConfig",
    "WebSearchTool",
    "WebToolsConfig",
    "default_socket_path",
]
