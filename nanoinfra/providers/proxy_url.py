"""Normalise an explicitly configured proxy URL to a scheme httpx implements."""

from __future__ import annotations

#: Proxy scheme aliases httpx does not know, mapped to the scheme it implements for the same wire
#: protocol.
#:
#: httpx 0.28.1 accepts exactly `http`, `https`, `socks5` and `socks5h`
#: (`httpx._transports.default.AsyncHTTPTransport.__init__` branches on those four) and raises
#: `ValueError: Unknown scheme for proxy URL` for anything else -- at client construction, so on a
#: host configured this way *every* LLM call fails before a request is sent, not just the ones
#: that would have gone through the proxy. `socks://` is the alias many desktop proxy clients
#: print and export in `ALL_PROXY`, and it is what an operator then copies into
#: `providers.<name>.proxy`.
#:
#: The target is `socks5h` rather than `socks5` because that is what httpx does either way: both
#: schemes construct the same `httpcore.AsyncSOCKSProxy`, which sends the target *hostname* to the
#: proxy (`socksio.socks5.SOCKS5CommandRequest.from_address`) instead of resolving it locally. The
#: `h` spelling describes the behaviour rather than contradicting it.
#:
#: `socks4://` is deliberately absent. httpx implements no SOCKS4 transport, so mapping it onto
#: SOCKS5 would silently speak the wrong protocol to the proxy; letting httpx reject it keeps a
#: real incompatibility visible.
_PROXY_SCHEME_ALIASES = {"socks": "socks5h"}


def normalize_proxy_url(proxy: str | None) -> str | None:
    """Return *proxy* with an httpx-known scheme, or unchanged when nothing is aliased.

    Only the scheme is rewritten. The rest of the URL is carried over byte-for-byte, because a
    proxy URL routinely holds percent-encoded credentials and a parse/unparse round trip is free
    to re-encode them into something the proxy no longer accepts.
    """
    if not proxy:
        return proxy
    scheme, separator, rest = proxy.partition("://")
    if not separator:
        return proxy
    replacement = _PROXY_SCHEME_ALIASES.get(scheme.strip().lower())
    if replacement is None:
        return proxy
    return f"{replacement}://{rest}"
