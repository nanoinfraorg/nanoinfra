"""web_fetch inside the fetcher process -- nanoinfraorg/nanoinfra#19.

This is the code that reads a page a stranger wrote. It runs in the process with broad egress and
no host credential, so a page that takes this code over gets network reach and nothing else.

The order of the work is the order the tool used before the split, and for the same reasons. The
URL is checked first, because a refusal must cost no request. An image is detected with a streamed
request, so a captioning reader never sees an image. Then the reader runs, and the extracted text
carries the untrusted banner.

The banner is not decoration. Text from a page is data, and the one thing a compromised page wants
is for the model to read it as instruction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qsl, urlparse

import httpx
from loguru import logger

from nanoinfra.gates.fetcher.egress import (
    DEFAULT_USER_AGENT,
    UNTRUSTED_BANNER,
    Payload,
    fetch_client_kwargs,
    get_with_safe_redirects,
    image_content_blocks,
    normalize,
    stream_with_safe_redirects,
    strip_tags,
    unsafe_url_request_error,
    validate_url_safe,
)

DEFAULT_MAX_CHARS = 50000

# The model puts a URL in backticks or quotes often enough that a strip is cheaper than a refusal.
# The strip happens here rather than in the caller, because the check that follows it lives here.
_URL_WRAPPERS = " \t\r\n`\"'"


# Forwarding a URL to the remote Jina reader discloses it to a third party, so a URL that embeds
# credential material must never leave the machine. This fetcher holds no host credential, and that
# is beside the point here: the credential is in the URL the caller handed us, so the guard belongs
# at the URL.
#
# Matching is by parameter *name*, never by value. A value that looks secret is usually a search
# term ("?q=token"); a name that says secret usually is one. The asymmetry sets the error to prefer:
# over-matching costs a fall back to the local readability reader, under-matching leaks a secret.
_CREDENTIAL_QUERY_PARAMS = frozenset({
    "access_token", "api-key", "api-token", "apikey", "api_key", "api_token",
    "auth", "authorization", "client_assertion", "client_secret", "code",
    "credential", "credentials", "id_token", "jwt", "key", "password",
    "passwd", "private_key", "pwd", "refresh_token", "samlresponse", "secret",
    "session_id", "session_token", "sessionid", "sig", "signature", "sso_token",
    "ticket", "token",
})
_CREDENTIAL_QUERY_PREFIXES = ("x-amz-", "x-goog-")


def _url_carries_credentials(url: str) -> bool:
    """True when this URL must not be disclosed to a third-party reader."""
    try:
        parsed = urlparse(url)
    except ValueError:
        # An unparseable URL cannot be cleared, so it is refused. Fail closed.
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    # Some frameworks still accept semicolons as query separators. Splitting on them here can
    # over-match a value, and the cost of that is only using the local extractor instead.
    query = parsed.query.replace(";", "&")
    for name, _value in parse_qsl(query, keep_blank_values=True):
        lowered = name.strip().lower()
        if lowered in _CREDENTIAL_QUERY_PARAMS or lowered.startswith(_CREDENTIAL_QUERY_PREFIXES):
            return True
    return False


def _redact_url_for_log(url: str) -> str:
    """Return only a URL's origin, excluding userinfo, path, query, and fragment."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not parsed.scheme or hostname is None:
            return "<redacted URL>"
        if ":" in hostname:
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        authority = f"{hostname}:{port}" if port is not None else hostname
        return f"{parsed.scheme}://{authority}"
    except ValueError:
        return "<redacted URL>"


@dataclass(slots=True)
class WebFetch:
    """One URL reader. Holds no credential except an optional Jina key from the environment."""

    use_jina_reader: bool = True
    proxy: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    max_chars: int = DEFAULT_MAX_CHARS
    jina_api_key: str = ""

    async def run(
        self, url: str, extract_mode: str = "markdown", max_chars: int | None = None
    ) -> Payload:
        """Read one URL, or say why the fetcher refused it."""
        url = url.strip(_URL_WRAPPERS)
        max_chars = max_chars or self.max_chars
        is_valid, error_msg = validate_url_safe(url)
        if not is_valid:
            return _document({"error": f"URL validation failed: {error_msg}", "url": url})

        # Detect and fetch images directly to avoid Jina's textual image captioning
        try:
            async with httpx.AsyncClient(
                **fetch_client_kwargs(self.proxy, 15.0),
            ) as client:
                r, stream, redirect_error = await stream_with_safe_redirects(
                    client,
                    url,
                    headers={"User-Agent": self.user_agent},
                )
                if redirect_error:
                    return _document({"error": redirect_error, "url": url})
                if r is None:
                    return _document({"error": "Fetch failed", "url": url})

                try:
                    ctype = r.headers.get("content-type", "")
                    if ctype.startswith("image/"):
                        r.raise_for_status()
                        raw = await r.aread()
                        return Payload(text="", blocks=image_content_blocks(raw, ctype, url))
                finally:
                    if stream is not None:
                        await stream.__aexit__(None, None, None)
        except Exception as e:
            unsafe_error = unsafe_url_request_error(e)
            if unsafe_error is not None:
                # A rebind between the check and the socket. The reader must not run after this:
                # a fallback would ask again and could win the race the guard just refused.
                return _document({"error": f"URL validation failed: {unsafe_error}", "url": url})
            logger.debug("Pre-fetch image detection failed for {}: {}", url, e)

        result: Payload | None = None
        if self.use_jina_reader:
            result = await self._fetch_jina(url, max_chars)
        if result is None:
            result = await self._fetch_readability(url, extract_mode, max_chars)
        return result

    async def _fetch_jina(self, url: str, max_chars: int) -> Payload | None:
        """Try fetching via Jina Reader API. Returns None on failure."""
        if _url_carries_credentials(url):
            # Origin only. The query is the thing being protected, so it cannot go in the log
            # that records the refusal.
            logger.debug(
                "Skipping Jina Reader for {}: URL carries credential material",
                _redact_url_for_log(url),
            )
            return None
        # httpx drops the fragment when it builds the request, so this strip changes nothing
        # today. It is here because an OAuth implicit flow puts a token in the fragment, and
        # that must not start travelling the day the transport changes.
        forwarded_url = url.split("#", 1)[0]
        try:
            headers = {"Accept": "application/json", "User-Agent": self.user_agent}
            if self.jina_api_key:
                headers["Authorization"] = f"Bearer {self.jina_api_key}"
            async with httpx.AsyncClient(proxy=self.proxy, timeout=20.0) as client:
                r = await client.get(f"https://r.jina.ai/{forwarded_url}", headers=headers)
                if r.status_code == 429:
                    logger.debug("Jina Reader rate limited, falling back to readability")
                    return None
                r.raise_for_status()

            data = r.json().get("data", {})
            title = data.get("title", "")
            text = data.get("content", "")
            if not text:
                return None

            if title:
                text = f"# {title}\n\n{text}"
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            text = f"{UNTRUSTED_BANNER}\n\n{text}"

            return _document({
                "url": url, "finalUrl": data.get("url", url), "status": r.status_code,
                "extractor": "jina", "truncated": truncated, "length": len(text),
                "untrusted": True, "text": text,
            })
        except Exception as e:
            logger.debug("Jina Reader failed for {}, falling back to readability: {}", url, e)
            return None

    async def _fetch_readability(self, url: str, extract_mode: str, max_chars: int) -> Payload:
        """Local fallback using readability-lxml."""
        try:
            async with httpx.AsyncClient(
                **fetch_client_kwargs(self.proxy, 30.0),
            ) as client:
                r, redirect_error = await get_with_safe_redirects(
                    client,
                    url,
                    headers={"User-Agent": self.user_agent},
                )
                if redirect_error:
                    return _document({"error": redirect_error, "url": url})
                if r is None:
                    return _document({"error": "Fetch failed", "url": url})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")
            if ctype.startswith("image/"):
                return Payload(text="", blocks=image_content_blocks(r.content, ctype, url))

            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                try:
                    text = self._extract_readable_html(r.text, extract_mode)
                    extractor = "readability"
                except Exception as e:
                    logger.warning("Readability failed for {}, using raw HTML fallback: {}", url, e)
                    text, extractor = normalize(strip_tags(r.text)), "html"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            text = f"{UNTRUSTED_BANNER}\n\n{text}"

            return _document({
                "url": url, "finalUrl": str(r.url), "status": r.status_code,
                "extractor": extractor, "truncated": truncated, "length": len(text),
                "untrusted": True, "text": text,
            })
        except httpx.ProxyError as e:
            logger.exception("WebFetch proxy error for {}", url)
            return _document({"error": f"Proxy error: {e}", "url": url})
        except Exception as e:
            logger.exception("WebFetch error for {}", url)
            return _document({"error": str(e), "url": url})

    def _extract_readable_html(self, html_content: str, extract_mode: str) -> str:
        from readability import Document  # pyright: ignore[reportMissingTypeStubs]

        doc = Document(html_content)
        summary = cast(str, doc.summary())
        content = self._to_markdown(summary) if extract_mode == "markdown" else strip_tags(summary)
        return f"# {doc.title()}\n\n{content}" if doc.title() else content

    def _to_markdown(self, html_content: str) -> str:
        """Convert HTML to markdown."""
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{strip_tags(m[2])}]({m[1]})', html_content, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {strip_tags(m[1])}', text,
                      flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return normalize(strip_tags(text))


def _document(fields: dict[str, Any]) -> Payload:
    """Wrap one web_fetch document.

    A failed fetch is a document with an ``error`` member rather than a tool error. That shape is
    the tool's contract with the model, and #19 must not change what the model reads.
    """
    return Payload(text=json.dumps(fields, ensure_ascii=False))


__all__ = ["DEFAULT_MAX_CHARS", "WebFetch"]
