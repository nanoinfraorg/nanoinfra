"""The two WebUI metrics routes (#235).

`/api/webui/metrics/live` and `/api/webui/metrics/calls` are the only readers the Live and Calls
tabs have. Three properties matter more than the payload shapes:

* **Both carry the API token.** They publish model names, spend and every tool call this
  deployment ran, and the WebUI's port is the one a reverse proxy fronts.
* **"Not a gateway" is not "nothing is happening".** `nanoinfra webui` against a remote gateway has
  no bus and no channel manager, and the Live route must say so rather than render seven zeros.
* **A filter is a bound parameter.** Every one of them arrives from a query string.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.datastructures import Headers

from nanoinfra.channels.websocket.runtime import WebSocketConfig
from nanoinfra.llm_usage.gauges import GaugeSources, set_active_gauge_sources
from nanoinfra.llm_usage.models import ToolCallRecord
from nanoinfra.llm_usage.store import LLMUsageStore
from nanoinfra.webui.gateway_services import build_gateway_services
from nanoinfra.webui.ws_http import GatewayHTTPHandler

_LIVE = "/api/webui/metrics/live"
_CALLS = "/api/webui/metrics/calls"


def _handler(tmp_path: Path) -> GatewayHTTPHandler:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    services = build_gateway_services(
        config=WebSocketConfig.model_validate(
            {
                "enabled": True,
                "allowFrom": ["*"],
                "host": "127.0.0.1",
                "port": 8765,
                "path": "/",
            }
        ),
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path / "workspace",
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return services.http


def _request(path: str, *, token: str | None = None):
    headers: list[tuple[str, str]] = []
    if token is not None:
        headers.append(("Authorization", f"Bearer {token}"))
    return SimpleNamespace(path=path, headers=Headers(headers))


def _connection():
    return SimpleNamespace(
        remote_address=("127.0.0.1", 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


@pytest.fixture(autouse=True)
def _clear_process_holder():
    """The sampler holder is process-global, so it must not leak into another test file."""
    yield
    set_active_gauge_sources(None)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LLMUsageStore:
    """A store the route will actually read, in place of the process-wide one."""
    made = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    monkeypatch.setattr("nanoinfra.llm_usage.get_llm_usage_store", lambda: made)
    return made


def _call(**over: Any) -> ToolCallRecord:
    fields: dict[str, Any] = {
        "ts_ms": 1_760_000_000_000,
        "tool": "execute_on_server",
        "source": "user",
        "outcome": "ok",
        "duration_ms": 1_200,
        "session_key": "webui:alberto",
        "turn_id": "turn-1",
        "seq": 0,
        "actor": "alberto",
        "capability_class": "mutate.remote",
    }
    fields.update(over)
    return ToolCallRecord(**fields)


# --- authentication ----------------------------------------------------------------------


@pytest.mark.parametrize("path", [_LIVE, _CALLS])
async def test_neither_route_answers_without_a_token(tmp_path: Path, path: str) -> None:
    """They publish model names, spend and every tool call. This is the WebUI's port."""
    handler = _handler(tmp_path)

    response = await handler.dispatch(_connection(), _request(path))

    assert response.status_code == 401


@pytest.mark.parametrize("path", [_LIVE, _CALLS])
async def test_a_wrong_token_is_refused(tmp_path: Path, path: str) -> None:
    handler = _handler(tmp_path)

    response = await handler.dispatch(_connection(), _request(path, token="not-the-token"))

    assert response.status_code == 401


# --- the live route ----------------------------------------------------------------------


async def test_a_process_that_is_not_a_gateway_says_so(tmp_path: Path) -> None:
    """`nanoinfra webui` against a remote gateway has nothing to sample.

    Seven zeros would read as a healthy, idle deployment. `available: false` reads as what it is.
    """
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(_connection(), _request(_LIVE, token=token))

    assert response.status_code == 200
    payload = _body(response)
    assert payload["available"] is False
    assert payload["gauges"] == []


async def test_the_gateway_s_own_numbers_come_back(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)
    set_active_gauge_sources(
        GaugeSources(pending_approvals=lambda: 3, ws_connections=lambda: 1)
    )

    response = await handler.dispatch(_connection(), _request(_LIVE, token=token))

    payload = _body(response)
    assert payload["available"] is True
    by_name = {gauge["name"]: gauge for gauge in payload["gauges"]}
    assert by_name["nanoinfra_pending_approvals"]["value"] == 3
    assert by_name["nanoinfra_pending_approvals"]["alerting"] is True
    # And the unreadable ones are present with a null rather than dropped or zeroed.
    assert by_name["nanoinfra_context_tokens_used"]["value"] is None


# --- the calls route ---------------------------------------------------------------------


async def test_the_calls_route_reads_the_table_at_last(
    tmp_path: Path, store: LLMUsageStore
) -> None:
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)
    store.record_tool_call(_call())

    response = await handler.dispatch(_connection(), _request(_CALLS, token=token))

    assert response.status_code == 200
    payload = _body(response)
    assert len(payload["calls"]) == 1
    assert payload["calls"][0]["tool"] == "execute_on_server"
    assert payload["retention_days"] > 0


async def test_the_query_string_filters_are_applied(
    tmp_path: Path, store: LLMUsageStore
) -> None:
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)
    store.record_tool_call(_call(tool="execute_on_server", outcome="ok"))
    store.record_tool_call(_call(tool="read_file", outcome="error", error_kind="tool_error"))

    response = await handler.dispatch(
        _connection(), _request(f"{_CALLS}?tool=read_file&outcome=error", token=token)
    )

    payload = _body(response)
    assert [row["tool"] for row in payload["calls"]] == ["read_file"]


async def test_the_limit_and_the_cursor_come_from_the_query_string(
    tmp_path: Path, store: LLMUsageStore
) -> None:
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)
    for index in range(3):
        store.record_tool_call(_call(ts_ms=1_760_000_000_000 + index, tool=f"tool-{index}"))

    first = _body(
        await handler.dispatch(_connection(), _request(f"{_CALLS}?limit=1", token=token))
    )
    assert len(first["calls"]) == 1
    assert first["has_more"] is True

    second = _body(
        await handler.dispatch(
            _connection(),
            _request(f"{_CALLS}?limit=1&before={first['next_before_id']}", token=token),
        )
    )
    assert second["calls"][0]["id"] < first["calls"][0]["id"]


async def test_a_junk_limit_does_not_500_the_route(
    tmp_path: Path, store: LLMUsageStore
) -> None:
    """Every one of these arrives from a query string, so none of them may be trusted."""
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)
    store.record_tool_call(_call())

    for query in ("limit=abc", "limit=-1", "limit=", "before=xyz", "limit=99999999999999999999"):
        response = await handler.dispatch(
            _connection(), _request(f"{_CALLS}?{query}", token=token)
        )
        assert response.status_code == 200, query


async def test_a_hostile_filter_value_is_data_and_not_sql(
    tmp_path: Path, store: LLMUsageStore
) -> None:
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)
    store.record_tool_call(_call())

    response = await handler.dispatch(
        _connection(),
        _request(f"{_CALLS}?tool=%27%20OR%201%3D1%20--", token=token),
    )

    assert response.status_code == 200
    assert _body(response)["calls"] == []
    # The table survived, which a string-formatted where clause would not guarantee.
    assert store.tool_call_count() == 1


async def test_the_page_carries_no_field_an_argument_could_be_written_to(
    tmp_path: Path, store: LLMUsageStore
) -> None:
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)
    store.record_tool_call(_call())

    keys = set(_body(await handler.dispatch(_connection(), _request(_CALLS, token=token)))["calls"][0])

    assert keys & {"arguments", "args", "command", "command_text", "output", "result"} == set()


async def test_a_store_that_cannot_be_read_is_not_an_empty_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 500 is the honest answer. An empty `calls` list would read as an idle deployment."""
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)

    def _explode() -> Any:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr("nanoinfra.llm_usage.get_llm_usage_store", _explode)

    response = await handler.dispatch(_connection(), _request(_CALLS, token=token))

    assert response.status_code == 500
