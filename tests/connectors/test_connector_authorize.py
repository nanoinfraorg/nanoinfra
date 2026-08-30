"""The consent step, minus the browser.

Two Google-specific parameters are the whole reason this has its own tests. Without
``access_type=offline`` there is no refresh token, and without ``prompt=consent`` Google
returns one only on the first consent a client ever receives -- so re-authorising a connector
would appear to work and produce a credential that stops working in an hour. Both are easy to
lose in a refactor and neither fails visibly.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from nanoinfra.connectors.authorize import (
    Authorization,
    AuthorizationError,
    authorize_url,
    exchange_code,
    run_consent_flow,
)
from nanoinfra.connectors.contracts import ConnectorPlugin
from nanoinfra.connectors.registry import discover_connectors

CALENDAR = "google-calendar"


@pytest.fixture
def calendar() -> ConnectorPlugin:
    return discover_connectors()[CALENDAR]


def _query(url: str) -> dict[str, str]:
    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
    }


def test_the_authorize_url_asks_for_a_refresh_token_and_a_fresh_consent(
    calendar: ConnectorPlugin,
) -> None:
    url = authorize_url(
        client_id="cid",
        scopes=calendar.credential.declared_scopes(),
        redirect_uri="http://127.0.0.1:8766/",
        challenge="chal",
        state="st",
    )
    params = _query(url)
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] == "chal"
    assert params["state"] == "st"
    assert "calendar.readonly" in params["scope"]
    assert "calendar.events" in params["scope"]


def test_the_url_pre_fills_an_account_only_when_one_was_given() -> None:
    with_account = _query(
        authorize_url(
            client_id="cid",
            scopes=("s",),
            redirect_uri="http://127.0.0.1:8766/",
            challenge="c",
            state="s",
            login_hint="person@example.test",
        )
    )
    assert with_account["login_hint"] == "person@example.test"
    without = _query(
        authorize_url(
            client_id="cid",
            scopes=("s",),
            redirect_uri="http://127.0.0.1:8766/",
            challenge="c",
            state="s",
        )
    )
    assert "login_hint" not in without


def test_the_exchange_sends_the_verifier_and_the_client_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(
            pair.split("=", 1)  # pyright: ignore[reportArgumentType]
            for pair in request.content.decode().split("&")
            if "=" in pair
        )
        return httpx.Response(
            200, json={"refresh_token": "rt-1", "scope": "a b", "access_token": "at"}
        )

    monkeypatch.setattr(
        httpx.Client, "post", lambda _self, url, data=None: handler(httpx.Request("POST", url, data=data))
    )
    result = exchange_code(
        token_url="https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="cs",
        code="code-1",
        verifier="ver-1",
        redirect_uri="http://127.0.0.1:8766/",
    )
    assert result == Authorization(refresh_token="rt-1", granted_scopes=("a", "b"))
    assert sent["grant_type"] == "authorization_code"
    assert sent["code_verifier"] == "ver-1"
    assert sent["client_secret"] == "cs"


def test_an_exchange_with_no_refresh_token_says_what_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure an operator will actually hit, so the message names both causes."""
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda _self, url, data=None: httpx.Response(200, json={"access_token": "at"}),
    )
    with pytest.raises(AuthorizationError) as raised:
        exchange_code(
            token_url="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="cs",
            code="c",
            verifier="v",
            redirect_uri="http://127.0.0.1:8766/",
        )
    assert "access_type=offline" in str(raised.value)
    assert "prompt=consent" in str(raised.value)


def test_a_failed_exchange_quotes_the_providers_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda _self, url, data=None: httpx.Response(400, text='{"error": "invalid_grant"}'),
    )
    with pytest.raises(AuthorizationError, match="invalid_grant"):
        exchange_code(
            token_url="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="cs",
            code="c",
            verifier="v",
            redirect_uri="http://127.0.0.1:8766/",
        )


def test_a_taken_port_says_how_to_move_it(calendar: ConnectorPlugin) -> None:
    """The redirect URI has to match the OAuth client, so the message says to change both."""
    import socket

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(AuthorizationError) as raised:
            run_consent_flow(
                calendar,
                client_id="cid",
                client_secret="cs",
                port=port,
                open_browser=False,
                print_fn=lambda _text: None,
            )
    finally:
        holder.close()
    assert "--port" in str(raised.value)
    assert "redirect URI" in str(raised.value)


def test_a_connector_with_no_scopes_cannot_be_authorised() -> None:
    from nanoinfra.connectors.contracts import operation

    plugin = ConnectorPlugin(
        name="scopeless",
        display_name="Scopeless",
        base_url="https://example.test",
        operations=(operation("read_it", "read", "GET", "/v1/thing"),),
    )
    with pytest.raises(AuthorizationError, match="no OAuth scopes"):
        run_consent_flow(plugin, client_id="cid", client_secret="cs", open_browser=False)
