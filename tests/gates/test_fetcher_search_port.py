# tests/gates/test_fetcher_search_port.py
"""The fetcher must reach the search backend its operator configured.

A self-hosted provider answers where its operator put it. A SearXNG on `http://searxng:8080/` is
the case that found this: the socket, the account and the filesystem policy were all correct, and
search failed with "All connection attempts failed" because 8080 is in no default list. The port
allowlist already grew for a proxy on the same reasoning; the search backend was missing.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.gates import confinement


def _ports(monkeypatch: pytest.MonkeyPatch, base_url: str | None) -> tuple[int, ...]:
    for name in confinement._PROXY_ENV_VARS:  # pyright: ignore[reportPrivateUsage]
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        confinement, "_configured_proxy", lambda: None
    )
    monkeypatch.setattr(
        confinement, "_configured_search_base_url", lambda: base_url
    )
    return confinement._fetcher_ports()  # pyright: ignore[reportPrivateUsage]


def test_a_self_hosted_search_backend_joins_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert 8080 in _ports(monkeypatch, "http://searxng:8080/")


def test_the_defaults_stay_when_no_backend_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = _ports(monkeypatch, None)

    assert 53 in ports and 80 in ports and 443 in ports
    assert 8080 not in ports


def test_a_backend_on_a_default_port_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _ports(monkeypatch, "https://api.search.brave.com/") == _ports(monkeypatch, None)


def test_an_unparseable_base_url_leaves_the_list_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config fault is the fetcher's to report, not this module's, and it must not widen."""
    assert _ports(monkeypatch, "not a url at all") == _ports(monkeypatch, None)


def test_a_config_that_cannot_be_read_leaves_the_list_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode() -> Any:
        raise RuntimeError("no config here")

    monkeypatch.setattr("nanoinfra.config.loader.load_config", explode)
    # Reads through the real helper this time, to prove the try/except covers it.
    assert confinement._configured_search_base_url() is None  # pyright: ignore[reportPrivateUsage]
