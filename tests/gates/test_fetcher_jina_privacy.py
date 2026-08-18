"""A credential-bearing URL must never reach the remote Jina reader.

Forwarding a URL to r.jina.ai discloses it to a third party. The fetcher holding
no host credential of its own does not help here: the credential is *in the URL*,
so the guard belongs at the URL. See nanoinfraorg/nanoinfra#137.
"""

from __future__ import annotations

import pytest

from nanoinfra.gates.fetcher.fetch import _redact_url_for_log, _url_carries_credentials

CREDENTIAL_URLS = [
    # userinfo
    "https://user:pass@example.com/doc",
    "https://user@example.com/doc",
    # signed URLs
    "https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc&X-Amz-Expires=60",
    "https://storage.googleapis.com/b/o?X-Goog-Signature=abc",
    # named credential parameters, any case
    "https://example.com/a?access_token=abc",
    "https://example.com/a?API_KEY=abc",
    "https://example.com/a?token=abc",
    "https://example.com/a?sig=abc",
    "https://example.com/a?signature=abc",
    "https://example.com/a?password=hunter2",
    "https://example.com/a?client_secret=abc",
    "https://example.com/a?refresh_token=abc",
    "https://example.com/a?key=abc",
    # present but blank still counts: the name is the signal
    "https://example.com/a?token=",
    # hyphen and underscore spellings of the same thing
    "https://example.com/a?api-key=abc",
    "https://example.com/a?api_token=abc",
    # SSO / OAuth material
    "https://example.com/a?code=abc",
    "https://example.com/a?jwt=abc",
    "https://example.com/a?SAMLResponse=abc",
    "https://example.com/a?sessionid=abc",
    "https://example.com/a?private_key=abc",
    "https://example.com/a?client_assertion=abc",
    "https://example.com/a?ticket=abc",
    # semicolon used as a query separator
    "https://example.com/a?page=2;token=abc",
    # surrounding whitespace in the name
    "https://example.com/a?%20token%20=abc",
]

CLEAN_URLS = [
    "https://example.com/doc",
    "https://example.com/doc?page=2&sort=asc",
    "https://example.com/search?q=token",  # value mentions a secret; name does not
    "https://example.com/a?keyword=abc",  # not a bare 'key'
    "https://example.com/a?monkey=abc",  # does not end-match 'key'
]


@pytest.mark.parametrize("url", CREDENTIAL_URLS)
def test_credential_urls_are_detected(url: str) -> None:
    assert _url_carries_credentials(url) is True


@pytest.mark.parametrize("url", CLEAN_URLS)
def test_clean_urls_are_not_flagged(url: str) -> None:
    assert _url_carries_credentials(url) is False


def test_unparseable_url_fails_closed() -> None:
    """An unparseable URL is treated as credential-bearing, never as clean."""
    assert _url_carries_credentials("https://[oops") is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/secret/path?token=abc", "https://example.com"),
        ("https://user:pass@example.com/p", "https://example.com"),
        ("https://example.com:8443/p?k=v", "https://example.com:8443"),
        ("http://[2001:db8::1]/p", "http://[2001:db8::1]"),
        ("not a url", "<redacted URL>"),
        ("https://[oops", "<redacted URL>"),
    ],
)
def test_redact_url_for_log_keeps_only_the_origin(url: str, expected: str) -> None:
    """The refusal log must not restate the credential it is refusing to send."""
    assert _redact_url_for_log(url) == expected


async def test_fetch_jina_refuses_a_credential_url(monkeypatch) -> None:
    """The reader must return None without making any request."""
    from nanoinfra.gates.fetcher import fetch as fetch_mod

    tool = fetch_mod.WebFetch()

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("no HTTP client may be constructed for a credential URL")

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", explode)

    result = await tool._fetch_jina(
        "https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc", 1000
    )
    assert result is None


async def test_fetch_jina_strips_the_fragment(monkeypatch) -> None:
    """OAuth implicit flows put tokens in the fragment; it must not be forwarded."""
    from nanoinfra.gates.fetcher import fetch as fetch_mod

    tool = fetch_mod.WebFetch()
    seen: dict[str, str] = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": {"title": "t", "content": "body"}}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> _Response:
            seen["url"] = url
            return _Response()

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", _Client)

    await tool._fetch_jina("https://example.com/doc#access_token=abc", 1000)

    assert "access_token" not in seen["url"]
    assert seen["url"] == "https://r.jina.ai/https://example.com/doc"
