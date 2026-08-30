"""The connector kind: declared classes, the gate that answers per operation, and the engine.

The property under test throughout is the one the kind exists for: two operations of one
connector resolve to two different capability classes, so a read runs while a write asks. An
MCP server cannot do that, and every assertion here would pass trivially if the classes came
from the tool name instead of the manifest.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nanoinfra.agent.tools.capabilities import capability_class_of
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.config.gates import GatesConfig, ScopePolicy, StandingGrant
from nanoinfra.connectors.contracts import ConnectorPlugin, operation
from nanoinfra.connectors.credentials import (
    ConnectorCredential,
    CredentialError,
    check_connector_scopes,
    scope_subset,
)
from nanoinfra.connectors.engine import (
    ConnectorCallError,
    call,
    prepare,
    project,
)
from nanoinfra.connectors.registry import (
    capped_operations,
    discover_connectors,
    enabled_operations,
    load_connector_package,
    operation_summary,
)
from nanoinfra.connectors.tools import build_tools
from nanoinfra.gates.executor.client import ExecutorClient, ExecutorUnavailableError
from nanoinfra.gates.executor.connector_credentials import RefreshTokenSource
from nanoinfra.gates.executor.protocol import ExecuteResponse
from nanoinfra.gates.policy import Outcome, evaluate, evaluate_connector

CALENDAR = "google-calendar"


class _FixedTokens:
    """A token source that mints nothing. Records what was asked for."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, bool]] = []

    async def access_token(
        self, connector: str, capability_class: str, *, force_refresh: bool = False
    ) -> str:
        self.asked.append((connector, capability_class, force_refresh))
        return f"token-for-{capability_class}"


class _FakeExecutor(ExecutorClient):
    """Stands in for the executor. Records the frame the tool would have sent.

    A subclass rather than a mock, so a signature change here fails a test rather than
    silently passing a keyword nothing reads.
    """

    def __init__(self, response: Any = None) -> None:
        super().__init__("/nonexistent/executor.sock")
        self.calls: list[dict[str, Any]] = []
        self._response = response

    def connector_call(self, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def calendar() -> ConnectorPlugin:
    plugin = discover_connectors()[CALENDAR]
    return plugin


# --- the manifest contract -------------------------------------------------------------


def test_calendar_declares_two_classes(calendar: ConnectorPlugin) -> None:
    assert calendar.classes == ("read", "mutate.remote")
    assert calendar.operation("list_events") is not None
    assert calendar.operation("create_event") is not None


def test_package_name_and_manifest_name_must_agree() -> None:
    assert load_connector_package("google_calendar") is not None
    assert load_connector_package("no_such_connector") is None


def test_a_read_class_on_a_writing_method_is_refused() -> None:
    """The check that makes a declared class worth trusting."""
    with pytest.raises(ValueError, match="read class on a writing method"):
        operation("send_it", "read", "POST", "/v1/send")


def test_an_unknown_capability_class_is_refused() -> None:
    with pytest.raises(ValueError, match="not one the gate knows"):
        operation("read_it", "google.read", "GET", "/v1/thing")


def test_a_plaintext_base_url_is_refused() -> None:
    with pytest.raises(ValueError, match="must be https"):
        ConnectorPlugin(
            name="insecure",
            display_name="Insecure",
            base_url="http://example.test",
            operations=(operation("read_it", "read", "GET", "/v1/thing"),),
        )


# --- the tools -------------------------------------------------------------------------


def _tools(calendar: ConnectorPlugin, executor: _FakeExecutor) -> dict[str, Any]:
    return {
        tool.name: tool
        for tool in build_tools(calendar, calendar.operations, client=executor)
    }


def test_two_operations_become_two_tools_with_two_classes(calendar: ConnectorPlugin) -> None:
    """The whole point of the kind, asserted on the tools the model would see."""
    tools = _tools(calendar, _FakeExecutor())
    assert capability_class_of(tools["google_calendar_list_events"]) == "read"
    assert capability_class_of(tools["google_calendar_create_event"]) == "mutate.remote"
    assert tools["google_calendar_list_events"].read_only is True
    assert tools["google_calendar_create_event"].read_only is False


def test_a_tool_refuses_an_undeclared_argument(calendar: ConnectorPlugin) -> None:
    """An extra argument would become a query parameter nobody reviewed."""
    tools = _tools(calendar, _FakeExecutor())
    assert tools["google_calendar_list_events"].parameters["additionalProperties"] is False


def test_only_a_write_offers_a_preview(calendar: ConnectorPlugin) -> None:
    """A read is not gated, so previewing one would cost a round trip and teach nothing."""
    tools = _tools(calendar, _FakeExecutor())
    write = tools["google_calendar_create_event"].parameters["properties"]
    read = tools["google_calendar_list_events"].parameters["properties"]
    assert write["dry_run"]["default"] is True
    assert "dry_run" not in read


def test_enabled_operations_narrows_what_the_model_sees(calendar: ConnectorPlugin) -> None:
    chosen = enabled_operations(calendar, ["list_events"])
    assert [op.name for op in chosen] == ["list_events"]
    assert [op.name for op in enabled_operations(calendar, None)] == [
        op.name for op in calendar.operations
    ]


def test_a_read_ceiling_drops_the_writes(calendar: ConnectorPlugin) -> None:
    """`maxClass` is the operator's answer to a package declaring its own classes."""
    capped = capped_operations(calendar.operations, "read")
    assert [op.name for op in capped] == ["list_events", "list_calendars", "get_event"]
    assert len(capped_operations(calendar.operations, None)) == len(calendar.operations)


def test_the_row_shows_a_class_per_operation(calendar: ConnectorPlugin) -> None:
    rows = {row["name"]: row for row in operation_summary(calendar)}
    assert rows["list_events"]["capability_class"] == "read"
    assert rows["list_events"]["method"] == "GET"
    assert rows["create_event"]["capability_class"] == "mutate.remote"
    assert rows["create_event"]["tool"] == "google_calendar_create_event"


# --- the gate --------------------------------------------------------------------------


def _gates(**unattended: Any) -> GatesConfig:
    return GatesConfig(**unattended)


def test_a_read_is_allowed_in_every_context() -> None:
    for context in ("interactive", "cron", "trigger"):
        decision = evaluate_connector(
            _gates(),
            capability_class="read",
            execution_context=context,
            connector=CALENDAR,
            operation="list_events",
        )
        assert decision.outcome is Outcome.ALLOW, context


def test_a_write_asks_when_a_person_is_present() -> None:
    decision = evaluate_connector(
        _gates(),
        capability_class="mutate.remote",
        execution_context="interactive",
        connector=CALENDAR,
        operation="create_event",
    )
    assert decision.outcome is Outcome.APPROVE


def test_a_write_is_denied_unattended_with_no_grant() -> None:
    decision = evaluate_connector(
        _gates(),
        capability_class="mutate.remote",
        execution_context="cron",
        connector=CALENDAR,
        operation="create_event",
    )
    assert decision.outcome is Outcome.DENY


def test_a_grant_naming_the_connector_permits_the_write_unattended() -> None:
    gates = GatesConfig(
        unattended={"mutate.remote": ScopePolicy(host="grant")},
        standingGrants=[
            StandingGrant(
                id="cal-write",
                contexts=["unattended"],
                connectors=[CALENDAR],
                operations=["create_event"],
            )
        ],
    )
    permitted = evaluate_connector(
        gates,
        capability_class="mutate.remote",
        execution_context="cron",
        connector=CALENDAR,
        operation="create_event",
    )
    assert permitted.outcome is Outcome.ALLOW
    assert permitted.grant_id == "cal-write"

    # The same grant does not cover a second operation of the same connector.
    other = evaluate_connector(
        gates,
        capability_class="mutate.remote",
        execution_context="cron",
        connector=CALENDAR,
        operation="delete_event",
    )
    assert other.outcome is Outcome.DENY
    assert "delete_event" in other.reason


def test_a_connector_grant_does_not_cover_a_command() -> None:
    """The two grant kinds must not leak into each other."""
    gates = GatesConfig(
        unattended={"mutate.remote": ScopePolicy(host="grant")},
        standingGrants=[
            StandingGrant(id="cal", connectors=[CALENDAR], operations=["create_event"])
        ],
    )
    decision = evaluate(
        gates,
        capability_class="mutate.remote",
        scope="host",
        execution_context="cron",
        hosts=("db-1",),
        command="create_event",
    )
    assert decision.outcome is Outcome.DENY


def test_a_host_grant_does_not_cover_a_connector_call() -> None:
    gates = GatesConfig(
        unattended={"mutate.remote": ScopePolicy(host="grant")},
        standingGrants=[StandingGrant(id="db", hosts=["db-1"], commands=["create_event"])],
    )
    decision = evaluate_connector(
        gates,
        capability_class="mutate.remote",
        execution_context="cron",
        connector=CALENDAR,
        operation="create_event",
    )
    assert decision.outcome is Outcome.DENY


def test_a_grant_cannot_name_both_kinds() -> None:
    with pytest.raises(ValueError, match="never both"):
        StandingGrant(hosts=["db-1"], commands=["ls"], connectors=[CALENDAR], operations=["x"])


def test_half_a_connector_grant_is_refused() -> None:
    with pytest.raises(ValueError, match="matches nothing"):
        StandingGrant(connectors=[CALENDAR])


def test_a_class_a_connector_cannot_hold_is_denied() -> None:
    decision = evaluate_connector(
        _gates(),
        capability_class="credential.access",
        execution_context="interactive",
        connector=CALENDAR,
        operation="create_event",
    )
    assert decision.outcome is Outcome.DENY


# --- the engine ------------------------------------------------------------------------


def test_a_path_placeholder_is_filled_and_quoted_whole(calendar: ConnectorPlugin) -> None:
    op = calendar.operation("get_event")
    assert op is not None
    prepared = prepare(
        calendar, op, {"calendarId": "team@example.test", "eventId": "abc/../../secret"}
    )
    assert "team%40example.test" in prepared.url
    # A value carrying '/' or '..' stays a value; it must not climb the API's own routes.
    assert prepared.url.endswith("/events/abc%2F..%2F..%2Fsecret")


def test_a_missing_placeholder_names_itself(calendar: ConnectorPlugin) -> None:
    op = calendar.operation("get_event")
    assert op is not None
    with pytest.raises(ConnectorCallError, match="eventId"):
        prepare(calendar, op, {"calendarId": "primary"})


def test_a_get_puts_the_rest_in_the_query_and_a_post_in_the_body(
    calendar: ConnectorPlugin,
) -> None:
    listing = calendar.operation("list_events")
    creating = calendar.operation("create_event")
    assert listing is not None and creating is not None

    read = prepare(calendar, listing, {"calendarId": "primary", "timeMin": "2026-09-01T00:00:00Z"})
    assert read.params == {"timeMin": "2026-09-01T00:00:00Z"}
    assert read.body is None

    write = prepare(calendar, creating, {"calendarId": "primary", "summary": "Standup"})
    assert write.body == {"summary": "Standup"}
    assert write.params == {}


def test_the_projection_keeps_declared_fields_and_the_paging_key(
    calendar: ConnectorPlugin,
) -> None:
    op = calendar.operation("list_events")
    assert op is not None
    payload = {
        "nextPageToken": "p2",
        "kind": "calendar#events",
        "items": [
            {
                "id": "e1",
                "summary": "Standup",
                "start": {"dateTime": "2026-09-01T10:00:00-06:00"},
                "end": {"dateTime": "2026-09-01T10:15:00-06:00"},
                "status": "confirmed",
                "attendees": [{"email": "a@example.test", "responseStatus": "accepted"}],
                "conferenceData": {"lots": "of noise"},
                "reminders": {"useDefault": True},
            }
        ],
    }
    projected = project(payload, op)
    assert projected["nextPageToken"] == "p2"
    event = projected["items"][0]
    assert event["id"] == "e1"
    assert event["start"] == {"dateTime": "2026-09-01T10:00:00-06:00"}
    assert event["attendees"] == [{"email": "a@example.test"}]
    # The fields nobody declared do not reach the model.
    assert "conferenceData" not in event
    assert "reminders" not in event
    assert "responseStatus" not in event["attendees"][0]


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_a_call_sends_the_minted_token_and_returns_the_projection(
    calendar: ConnectorPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = calendar.operation("list_events")
    assert op is not None
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"items": [{"id": "e1", "summary": "Standup"}]})

    monkeypatch.setattr(
        "nanoinfra.connectors.engine._client",
        lambda: httpx.AsyncClient(transport=_transport(handler)),
    )
    tokens = _FixedTokens()
    result = await call(calendar, op, {"calendarId": "primary"}, tokens=tokens)
    assert seen["auth"] == "Bearer token-for-read"
    assert result["items"] == [{"id": "e1", "summary": "Standup"}]
    # The token was minted for the operation's own class, not for the connector as a whole.
    assert tokens.asked == [(CALENDAR, "read", False)]


async def test_a_401_refreshes_once_and_retries_once(
    calendar: ConnectorPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = calendar.operation("list_events")
    assert op is not None
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("authorization", ""))
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(
        "nanoinfra.connectors.engine._client",
        lambda: httpx.AsyncClient(transport=_transport(handler)),
    )
    tokens = _FixedTokens()
    await call(calendar, op, {"calendarId": "primary"}, tokens=tokens)
    assert len(attempts) == 2
    assert tokens.asked[-1] == (CALENDAR, "read", True)


async def test_a_second_401_asks_for_re_authorisation(
    calendar: ConnectorPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = calendar.operation("list_events")
    assert op is not None

    monkeypatch.setattr(
        "nanoinfra.connectors.engine._client",
        lambda: httpx.AsyncClient(
            transport=_transport(lambda _r: httpx.Response(401, json={"error": "revoked"}))
        ),
    )
    with pytest.raises(ConnectorCallError) as raised:
        await call(calendar, op, {"calendarId": "primary"}, tokens=_FixedTokens())
    assert raised.value.reauthorize is True
    assert "re-authorising" in str(raised.value)


async def test_a_429_is_a_failure_of_the_action_and_says_it_can_be_retried(
    calendar: ConnectorPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = calendar.operation("list_events")
    assert op is not None

    monkeypatch.setattr(
        "nanoinfra.connectors.engine._client",
        lambda: httpx.AsyncClient(
            transport=_transport(
                lambda _r: httpx.Response(
                    429, headers={"retry-after": "30"}, json={"error": {"message": "slow down"}}
                )
            )
        ),
    )
    with pytest.raises(ConnectorCallError) as raised:
        await call(calendar, op, {"calendarId": "primary"}, tokens=_FixedTokens())
    assert raised.value.retryable is True
    assert "Retry-After: 30" in str(raised.value)


async def test_a_tool_sends_the_call_to_the_executor_and_never_the_wire(
    calendar: ConnectorPlugin,
) -> None:
    """The agent side opens no transport and holds no token: it submits one frame."""
    executor = _FakeExecutor(
        ExecuteResponse(
            ok=True,
            output='{"items": []}',
            exit_code=0,
            error=None,
            reason="google_calendar_list_events ran as read.",
        )
    )
    tools = _tools(calendar, executor)
    with request_context(
        RequestContext(channel="webui", chat_id="c1", session_key="s1", execution_context="cron")
    ):
        result = await tools["google_calendar_list_events"].execute(timeMin="2026-09-01T00:00:00Z")
    assert getattr(result, "is_error", False) is False
    assert result == '{"items": []}'
    sent = executor.calls[0]
    assert sent["connector"] == CALENDAR
    assert sent["operation"] == "list_events"
    assert json.loads(sent["arguments_json"]) == {"timeMin": "2026-09-01T00:00:00Z"}
    # A read never previews, because a read is not gated.
    assert sent["preview_requested"] is False
    assert sent["execution_context"] == "cron"


async def test_a_write_previews_by_default_and_says_what_the_gate_would_answer(
    calendar: ConnectorPlugin,
) -> None:
    executor = _FakeExecutor(
        ExecuteResponse(
            ok=True,
            output="POST https://www.googleapis.com/calendar/v3/calendars/primary/events",
            exit_code=None,
            error=None,
            reason="preview only.",
            preview_outcome="approve",
            preview_reason="google-calendar.create_event is mutate.remote, which is approve.",
        )
    )
    tools = _tools(calendar, executor)
    with request_context(
        RequestContext(
            channel="webui", chat_id="c1", session_key="s1", execution_context="interactive"
        )
    ):
        result = await tools["google_calendar_create_event"].execute(
            summary="Standup", start={"dateTime": "x"}, end={"dateTime": "y"}
        )
    assert executor.calls[0]["preview_requested"] is True
    assert "Nothing was sent" in str(result)
    assert "'approve'" in str(result)
    # dry_run is the tool's own argument and must not travel to the executor as a call argument.
    assert "dry_run" not in json.loads(executor.calls[0]["arguments_json"])


async def test_a_refusal_from_the_executor_is_reported_as_an_error(
    calendar: ConnectorPlugin,
) -> None:
    executor = _FakeExecutor(
        ExecuteResponse(
            ok=False,
            output="",
            exit_code=None,
            error=None,
            reason=(
                "Refusing google_calendar_create_event. gates.unattended.mutate.remote.host is "
                "'deny'."
            ),
        )
    )
    tools = _tools(calendar, executor)
    with request_context(
        RequestContext(channel="webui", chat_id="c1", session_key="s1", execution_context="cron")
    ):
        result = await tools["google_calendar_create_event"].execute(
            summary="Standup", start={"dateTime": "x"}, end={"dateTime": "y"}, dry_run=False
        )
    assert getattr(result, "is_error", False) is True
    assert "deny" in str(result)


async def test_an_unreachable_executor_reads_as_a_deployment_fault(
    calendar: ConnectorPlugin,
) -> None:
    """Not as a policy decision: an operator must not read a broken deployment as a refusal."""
    executor = _FakeExecutor(ExecutorUnavailableError("no such socket"))
    tools = _tools(calendar, executor)
    with request_context(
        RequestContext(channel="webui", chat_id="c1", session_key="s1", execution_context="cron")
    ):
        result = await tools["google_calendar_list_events"].execute()
    assert getattr(result, "is_error", False) is True
    assert "deployment fault" in str(result)


# --- credentials -----------------------------------------------------------------------


class _Secrets:
    def __init__(self, **values: str) -> None:
        self._values = values

    def resolve_plaintext(self, secret_id: str) -> str | None:
        return self._values.get(secret_id)


def _credential(*scopes: str) -> ConnectorCredential:
    return ConnectorCredential(
        name="google-workspace",
        client_id="client.apps.googleusercontent.test",
        secret_ref="google-refresh",
        client_secret_ref="google-client-secret",
        token_url="https://oauth2.googleapis.com/token",
        scopes=scopes,
    )


def test_a_connector_asking_for_a_scope_the_credential_lacks_is_refused(
    calendar: ConnectorPlugin,
) -> None:
    """Refused at enable time, naming both sets -- not at 03:00 in a run record."""
    read_only = _credential("https://www.googleapis.com/auth/calendar.readonly")
    with pytest.raises(CredentialError) as raised:
        check_connector_scopes(calendar, read_only)
    assert "calendar.events" in str(raised.value)


def test_a_credential_with_every_scope_passes(calendar: ConnectorPlugin) -> None:
    full = _credential(
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    )
    check_connector_scopes(calendar, full)


def test_the_scope_asked_for_is_the_class_and_not_the_credential(
    calendar: ConnectorPlugin,
) -> None:
    """A read receives a token that cannot write. This is the lateral-use stop."""
    full = _credential(
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    )
    for_read = scope_subset(calendar.credential.scopes_for("read"), full)
    assert for_read == ("https://www.googleapis.com/auth/calendar.readonly",)
    for_write = scope_subset(calendar.credential.scopes_for("mutate.remote"), full)
    assert for_write == ("https://www.googleapis.com/auth/calendar.events",)


async def test_the_refresh_exchange_narrows_the_scope_and_caches_per_class(
    calendar: ConnectorPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchanges: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(
            pair.split("=", 1) for pair in request.content.decode().split("&") if "=" in pair
        )
        exchanges.append(body)
        return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})

    source = RefreshTokenSource(
        credential=_credential(
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ),
        secrets=_Secrets(**{"google-refresh": "rt-1", "google-client-secret": "cs-1"}),
        spec=calendar.credential,
    )
    monkeypatch.setattr(
        source, "_client", lambda: httpx.AsyncClient(transport=_transport(handler))
    )

    first = await source.access_token(CALENDAR, "read")
    second = await source.access_token(CALENDAR, "read")
    assert first == second == "at-1"
    # One exchange for two calls of the same class.
    assert len(exchanges) == 1
    assert exchanges[0]["grant_type"] == "refresh_token"
    assert "calendar.readonly" in exchanges[0]["scope"]
    assert "calendar.events" not in exchanges[0]["scope"]

    await source.access_token(CALENDAR, "mutate.remote")
    # The write class is a separate cache entry, so it cannot be served the read's token.
    assert len(exchanges) == 2
    assert "calendar.events" in exchanges[1]["scope"]


async def test_a_revoked_refresh_token_says_re_authorise(
    calendar: ConnectorPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = RefreshTokenSource(
        credential=_credential("https://www.googleapis.com/auth/calendar.readonly"),
        secrets=_Secrets(**{"google-refresh": "rt-1", "google-client-secret": "cs-1"}),
        spec=calendar.credential,
    )
    monkeypatch.setattr(
        source,
        "_client",
        lambda: httpx.AsyncClient(
            transport=_transport(
                lambda _r: httpx.Response(
                    400, text=json.dumps({"error": "invalid_grant"})
                )
            )
        ),
    )
    with pytest.raises(CredentialError, match="re-authorising"):
        await source.access_token(CALENDAR, "read")


def test_a_missing_secret_is_not_reported_as_an_empty_token(calendar: ConnectorPlugin) -> None:
    source = RefreshTokenSource(
        credential=_credential("https://www.googleapis.com/auth/calendar.readonly"),
        secrets=_Secrets(),
        spec=calendar.credential,
    )
    with pytest.raises(CredentialError, match="holds no value"):
        source._secret("google-refresh", "refresh token")
