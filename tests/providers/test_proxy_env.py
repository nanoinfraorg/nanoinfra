"""Tests for proxy environment variable handling in OpenAICompatProvider."""

from unittest.mock import MagicMock

import httpx
import pytest

import nanoinfra.providers.openai_compat_provider as openai_compat_provider
from nanoinfra.providers.openai_compat_provider import OpenAICompatProvider
from nanoinfra.providers.xai_grok_provider import XAIGrokProvider


def _make_spec(is_local: bool = False) -> MagicMock:
    spec = MagicMock()
    spec.is_local = is_local
    return spec


class TestLocalEndpointProxyDisabled:
    """Local endpoints must bypass proxy to avoid routing LAN traffic through it."""

    async def test_local_disables_proxy(self):
        spec = _make_spec(is_local=True)
        spec.env_key = ""
        spec.default_api_base = "http://localhost:11434/v1"
        provider = OpenAICompatProvider(
            api_key="test", api_base="http://localhost:11434/v1", spec=spec,
        )
        await provider._ensure_client()
        transport = provider._client._client._transport
        # The transport should be an AsyncHTTPTransport with proxy=None
        assert isinstance(transport, httpx.AsyncHTTPTransport)

    async def test_lan_ip_disables_proxy(self):
        spec = _make_spec(is_local=False)
        spec.env_key = ""
        spec.default_api_base = None
        provider = OpenAICompatProvider(
            api_key="test", api_base="http://192.168.8.188:1234/v1", spec=spec,
        )
        await provider._ensure_client()
        transport = provider._client._client._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)


class TestCloudEndpointProxyEnabled:
    """Cloud endpoints must respect proxy env vars for corporate/VPN proxies."""

    async def test_cloud_respects_trust_env(self):
        spec = _make_spec(is_local=False)
        spec.env_key = ""
        spec.default_api_base = "https://api.openai.com/v1"
        provider = OpenAICompatProvider(
            api_key="test", api_base=None, spec=spec,
        )
        await provider._ensure_client()
        client = provider._client._client
        # trust_env should be True so httpx reads HTTP_PROXY etc.
        assert client._trust_env is True

    async def test_explicit_provider_proxy_overrides_env(self, monkeypatch):
        spec = _make_spec(is_local=False)
        spec.env_key = ""
        spec.default_api_base = "https://api.openai.com/v1"
        proxy = "http://127.0.0.1:23458"
        monkeypatch.delenv("NANOINFRA_OPENAI_COMPAT_TIMEOUT_S", raising=False)

        http_client = MagicMock()
        async_client = MagicMock(return_value=http_client)
        openai_client = MagicMock(return_value=object())
        monkeypatch.setattr(httpx, "AsyncClient", async_client)
        monkeypatch.setattr(openai_compat_provider, "AsyncOpenAI", openai_client)

        provider = OpenAICompatProvider(
            api_key="test",
            api_base=None,
            spec=spec,
            proxy=proxy,
        )
        provider._build_client()

        async_client.assert_called_once_with(
            timeout=120.0,
            proxy=proxy,
            trust_env=False,
            follow_redirects=True,
        )
        assert openai_client.call_args.kwargs["http_client"] is http_client


class TestSocksProxyAliasNormalization:
    """`socks://` is the alias desktop proxy clients export and httpx refuses."""

    @staticmethod
    def _cloud_spec() -> MagicMock:
        spec = _make_spec(is_local=False)
        spec.env_key = ""
        spec.default_api_base = "https://api.openai.com/v1"
        return spec

    def test_httpx_rejects_the_raw_alias(self):
        # The defect this normalization exists for, asserted against the real httpx: the refusal
        # happens while the client is being built, so nothing gets as far as a request.
        with pytest.raises(ValueError, match="Unknown scheme for proxy URL"):
            httpx.AsyncClient(proxy="socks://127.0.0.1:1080")

    def test_compat_provider_rewrites_the_alias_httpx_accepts(self):
        provider = OpenAICompatProvider(
            api_key="test",
            api_base=None,
            spec=self._cloud_spec(),
            proxy="socks://proxy-user:p%40ss@127.0.0.1:1080",
        )

        # Credentials survive byte-for-byte: only the scheme is rewritten.
        assert provider._proxy == "socks5h://proxy-user:p%40ss@127.0.0.1:1080"
        httpx.AsyncClient(proxy=provider._proxy)

    def test_xai_provider_rewrites_the_alias_case_insensitively(self):
        provider = XAIGrokProvider(proxy="SOCKS://127.0.0.1:1080")

        assert provider.proxy == "socks5h://127.0.0.1:1080"
        httpx.AsyncClient(proxy=provider.proxy)

    @pytest.mark.parametrize(
        "proxy",
        [
            "http://127.0.0.1:8080",
            "https://127.0.0.1:8080",
            "socks5://127.0.0.1:1080",
            "socks5h://127.0.0.1:1080",
        ],
    )
    def test_schemes_httpx_implements_are_untouched(self, proxy: str):
        provider = OpenAICompatProvider(
            api_key="test",
            api_base=None,
            spec=self._cloud_spec(),
            proxy=proxy,
        )

        assert provider._proxy == proxy
        assert XAIGrokProvider(proxy=proxy).proxy == proxy
