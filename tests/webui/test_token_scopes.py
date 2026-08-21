"""One scope model for API tokens.

``api_tokens`` was a bare ``dict[str, float]`` and ``check_api_token`` a boolean, so any API token
was authority over every route the gateway serves. Survivable while the WebUI was the only holder;
not survivable once a second, narrower one exists -- which the TUI adoption needs, and one model
beats two (nanoinfraorg/nanoinfra#164).

The default is ``operate``. A route that says nothing requires the widest scope, so a route added
without thinking about scopes is operator-only rather than reachable by whatever narrow token
happens to exist.
"""

from __future__ import annotations

from typing import Any

from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest

from nanoinfra.webui.gateway_tokens import (
    ALL_SCOPES,
    OPERATE_SCOPE,
    TOKEN_PREFIX,
    GatewayTokenStore,
)


def _request(token: str) -> Any:
    return WsRequest(
        path="/api/webui/secrets",
        headers=Headers([("Authorization", f"Bearer {token}")]),
    )


def test_an_unscoped_token_holds_everything() -> None:
    """What every token had before, and what the WebUI bootstrap still gets."""
    store = GatewayTokenStore()
    token = store.issue_api_token(300)

    assert store.check_api_token(_request(token)) is True
    for scope in ALL_SCOPES:
        assert store.check_api_token(_request(token), scope=scope) is True


def test_a_narrow_token_is_refused_where_it_has_no_scope() -> None:
    store = GatewayTokenStore()
    token = store.issue_api_token(300, scopes={"read", "chat"})

    assert store.check_api_token(_request(token), scope="read") is True
    assert store.check_api_token(_request(token), scope="chat") is True
    assert store.check_api_token(_request(token), scope="approve") is False
    assert store.check_api_token(_request(token), scope="secrets") is False


def test_a_narrow_token_does_not_pass_the_default_check() -> None:
    """The fail-closed direction: an unaudited route stays operator-only."""
    store = GatewayTokenStore()
    token = store.issue_api_token(300, scopes={"read"})

    assert store.check_api_token(_request(token)) is False


def test_operate_implies_every_scope() -> None:
    store = GatewayTokenStore()
    token = store.issue_api_token(300, scopes={OPERATE_SCOPE})

    for scope in ALL_SCOPES:
        assert store.check_api_token(_request(token), scope=scope) is True


def test_an_unknown_token_is_refused() -> None:
    store = GatewayTokenStore()
    store.issue_api_token(300)

    assert store.check_api_token(_request("nwt_nope")) is False


def test_no_token_is_refused() -> None:
    store = GatewayTokenStore()
    request = WsRequest(path="/api/webui/secrets", headers=Headers([]))

    assert store.check_api_token(request) is False


def test_an_expired_token_forgets_its_scopes() -> None:
    """Otherwise the scope map grows for the life of the process."""
    store = GatewayTokenStore()
    token = store.issue_api_token(-1, scopes={"read"})

    assert store.check_api_token(_request(token), scope="read") is False
    assert token not in store.api_tokens
    assert token not in store.api_token_scopes


def test_clear_drops_the_scope_map_too() -> None:
    store = GatewayTokenStore()
    token = store.issue_api_token(300, scopes={"read"})

    store.clear()

    assert store.api_token_scopes == {}
    assert store.check_api_token(_request(token), scope="read") is False


def test_the_token_prefix_is_not_the_old_project_name() -> None:
    """It was nbwt_ -- nanobot web token -- baked into a credential this project hands out."""
    store = GatewayTokenStore()

    assert TOKEN_PREFIX == "nwt_"
    assert store.issue_api_token(300).startswith("nwt_")
    assert store.issue_token(300).startswith("nwt_")
