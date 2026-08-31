"""A connector against a public API activates and mints nothing (#195).

The gap this closes was found writing the `hello-world` example: `_credential_for` required a
`clientId` and a `secretRef`, so the simplest connector anybody would write first -- one read
against a public API -- was the only kind that could not be enabled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.connectors.engine import call
from nanoinfra.connectors.package import load_connector_package

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "connectors" / "hello-world"


class _RefusingTokens:
    """A token source that fails the test if it is ever asked."""

    async def access_token(
        self, connector: str, capability_class: str, *, force_refresh: bool = False
    ) -> str:
        raise AssertionError("a credential-free connector must not ask for a token")


def test_it_activates_with_no_credential_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    from nanoinfra.connectors import setup as setup_module

    plugin = load_connector_package(_EXAMPLE)
    monkeypatch.setattr(
        setup_module, "discover_connectors", lambda workspace_path=None: {plugin.name: plugin}
    )
    cfg = ConnectorRuntimeConfig.model_validate({"active": ["hello-world"], "connectors": {}})

    active, problems = setup_module.resolve_active(cfg)

    assert problems == [], [str(p) for p in problems]
    assert [entry.name for entry in active] == ["hello-world"]
    # Nothing to resolve, so nothing is named.
    assert active[0].credential.secret_ref == ""


def test_a_credential_named_anyway_is_still_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who put one there meant it: a package declaring `none` does not get to decide
    that a deployment sends no token."""
    from nanoinfra.connectors import setup as setup_module

    plugin = load_connector_package(_EXAMPLE)
    monkeypatch.setattr(
        setup_module, "discover_connectors", lambda workspace_path=None: {plugin.name: plugin}
    )
    cfg = ConnectorRuntimeConfig.model_validate({
        "active": ["hello-world"],
        "connectors": {"hello-world": {"credential": "some_credential"}},
        "credentials": {
            "some_credential": {"clientId": "cid", "secretRef": "ref", "scopes": []}
        },
    })

    active, problems = setup_module.resolve_active(cfg)

    assert problems == []
    assert active[0].credential.secret_ref == "ref"


def test_no_authorization_header_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_connector_package(_EXAMPLE)
    operation = plugin.operation("current_weather")
    assert operation is not None
    seen: dict[str, Any] = {}

    async def _request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        seen["headers"] = dict(kwargs.get("headers") or {})
        return httpx.Response(200, json={"latitude": 1.0, "current": {"temperature_2m": 20.3}})

    monkeypatch.setattr(httpx.AsyncClient, "request", _request)

    payload = asyncio.run(
        call(plugin, operation, {"latitude": "1", "longitude": "2"}, tokens=_RefusingTokens())
    )

    assert "Authorization" not in seen["headers"]
    assert seen["headers"]["Accept"] == "application/json"
    assert payload["current"]["temperature_2m"] == 20.3
