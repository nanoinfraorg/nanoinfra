# tests/webui/test_audit_routes.py
"""Item 27 (#29): the audit log becomes readable without a shell.

The read side of the log shipped with #16's reader (`nanoinfra/webui/audit_api.py`). No route
served it, so an operator still needed shell access to answer "what did the gate decide?".

Two fields drive a reviewer's judgement rather than the layout. `same_path` marks a decision whose
request and approval arrived on one channel, and the viewer shows it even when policy allowed the
action. `command_digest` is what the log holds by default, because a resolved command routinely
embeds a secret.

The viewer reads. The route refuses every method that is not a read, so "no delete control" is a
property of the server and not of the layout.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.datastructures import Headers

from nanoinfra.agent.tools.capabilities import MUTATE_INVENTORY, MUTATE_REMOTE
from nanoinfra.channels.websocket.runtime import WebSocketConfig
from nanoinfra.config.gates import AuditConfig
from nanoinfra.gates.audit import AuditStore
from nanoinfra.webui.audit_api import AUDIT_READ_PATH, AuditReadSurface
from nanoinfra.webui.gateway_services import build_gateway_services

_SECRET_COMMAND = "mysql -p s3cr3t-key-material -e 'flush hosts'"


def _store(tmp_path: Path, *, record_command_text: bool = False) -> AuditStore:
    return AuditStore(
        tmp_path / "gates",
        config=AuditConfig.model_validate({"recordCommandText": record_command_text}),
    )


def _denial(store: AuditStore) -> None:
    store.record(
        decision="deny",
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id="s1",
        tool="execute_on_server",
        scope="group",
        hosts=["10.0.2.11", "10.0.2.12"],
        command=_SECRET_COMMAND,
        reason="unattended mutate.remote at group scope is denied",
    )


def _handler(tmp_path: Path) -> Any:
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


def _request(path: str, *, token: str | None = None, method: str = "GET") -> Any:
    headers: list[tuple[str, str]] = []
    if token is not None:
        headers.append(("Authorization", f"Bearer {token}"))
    return SimpleNamespace(path=path, headers=Headers(headers), method=method)


def _connection() -> Any:
    return SimpleNamespace(
        remote_address=("127.0.0.1", 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


async def _get(handler: Any, path: str, *, method: str = "GET") -> Any:
    token = handler.tokens.issue_api_token(300)
    return await handler.dispatch(_connection(), _request(path, token=token, method=method))


# -- the surface ------------------------------------------------------------------------


def test_a_denial_appears_with_its_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _denial(store)

    page = AuditReadSurface(store).page({})

    assert page["total"] == 1
    record = page["records"][0]
    assert record["decision"] == "deny"
    assert record["reason"] == "unattended mutate.remote at group scope is denied"


def test_a_latched_refusal_appears(tmp_path: Path) -> None:
    """#15's refusals are the record that shows a retry loop, so the viewer must list them."""
    store = _store(tmp_path)
    store.record(
        decision="refused",
        capability_class=MUTATE_REMOTE,
        execution_context="interactive",
        session_id="s1",
        tool="execute_on_server",
        reason="a denial is terminal for this session",
    )

    page = AuditReadSurface(store).page({})

    assert page["records"][0]["decision"] == "refused"


def test_a_context_filter_isolates_automation_decisions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _denial(store)
    store.record(
        decision="allow",
        capability_class=MUTATE_INVENTORY,
        execution_context="interactive",
        session_id="s2",
        tool="create_server",
    )

    page = AuditReadSurface(store).page({"executionContext": ["automation"]})

    assert page["total"] == 1
    assert page["records"][0]["executionContext"] == "automation"


def test_no_record_carries_the_command_text_by_default(tmp_path: Path) -> None:
    """The acceptance case: secrets do not appear under default settings."""
    store = _store(tmp_path)
    _denial(store)

    page = AuditReadSurface(store).page({})

    assert "s3cr3t-key-material" not in json.dumps(page)
    assert page["records"][0]["commandDigest"].startswith("sha256:")
    assert page["records"][0]["commandText"] is None
    assert page["records"][0]["holdsCommandText"] is False


def test_a_record_with_full_text_is_marked(tmp_path: Path) -> None:
    """A reader must know that the text may hold a secret."""
    store = _store(tmp_path, record_command_text=True)
    _denial(store)

    record = AuditReadSurface(store).page({})["records"][0]

    assert record["commandText"] == _SECRET_COMMAND
    assert record["holdsCommandText"] is True


def test_a_shared_channel_is_marked_even_when_policy_allowed_the_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(
        decision="allow",
        capability_class=MUTATE_REMOTE,
        execution_context="interactive",
        session_id="s1",
        origin_path="webui",
        approval_path="webui",
    )

    record = AuditReadSurface(store).page({})["records"][0]

    assert record["samePath"] is True
    assert record["decision"] == "allow"


def test_the_resolved_targets_sit_beside_the_grant_id(tmp_path: Path) -> None:
    """#24 records the addresses a grant permitted, so a reviewer sees them and not a label."""
    store = _store(tmp_path)
    store.record(
        decision="grant",
        capability_class=MUTATE_REMOTE,
        execution_context="automation",
        session_id="s1",
        grant_id="reload-web",
        hosts=["10.0.2.11", "10.0.2.12"],
        scope="group",
    )

    record = AuditReadSurface(store).page({})["records"][0]

    assert record["grantId"] == "reload-web"
    assert record["hosts"] == ["10.0.2.11", "10.0.2.12"]
    assert record["hostCount"] == 2


def test_an_unknown_filter_value_matches_nothing(tmp_path: Path) -> None:
    """A filter fails closed. A typo narrows the answer instead of widening it."""
    store = _store(tmp_path)
    _denial(store)

    page = AuditReadSurface(store).page({"decision": ["denied-typo"]})

    assert page["total"] == 0
    assert page["records"] == []


def test_the_surface_bounds_the_page_size(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(5):
        _denial(store)

    page = AuditReadSurface(store).page({"limit": ["9999"]})

    assert page["limit"] <= 200
    assert page["total"] == 5


def test_the_surface_offers_no_write(tmp_path: Path) -> None:
    """The log appends. A reader that could prune it would make that false."""
    names = [name for name in dir(AuditReadSurface) if not name.startswith("_")]

    assert names == ["page"]


def test_the_surface_reports_the_filter_choices(tmp_path: Path) -> None:
    """The viewer builds its selects from the server, so a new decision needs no UI edit."""
    store = _store(tmp_path)

    page = AuditReadSurface(store).page({})

    assert "deny" in page["choices"]["decision"]
    assert MUTATE_REMOTE in page["choices"]["capabilityClass"]
    assert "automation" in page["choices"]["executionContext"]


# -- the route --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_route_refuses_a_request_with_no_token(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    handler.attach_audit_surface(AuditReadSurface(_store(tmp_path)))

    response = await handler.dispatch(_connection(), _request(AUDIT_READ_PATH))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_the_route_answers_503_until_the_gateway_attaches_the_store(tmp_path: Path) -> None:
    """A viewer that cannot reach the log must not render an empty log."""
    handler = _handler(tmp_path)

    response = await _get(handler, AUDIT_READ_PATH)

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_the_route_answers_a_page(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    store = _store(tmp_path)
    _denial(store)
    handler.attach_audit_surface(AuditReadSurface(store))

    response = await _get(handler, AUDIT_READ_PATH)

    assert response.status_code == 200
    assert _body(response)["records"][0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_the_route_passes_every_filter_through(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    store = _store(tmp_path)
    _denial(store)
    handler.attach_audit_surface(AuditReadSurface(store))

    path = (
        f"{AUDIT_READ_PATH}?decision=deny&capabilityClass=mutate.remote"
        "&executionContext=automation&sessionId=s1&limit=10&offset=0"
    )
    response = await _get(handler, path)

    assert _body(response)["total"] == 1


@pytest.mark.asyncio
async def test_the_route_refuses_a_delete(tmp_path: Path) -> None:
    """The acceptance case: the viewer offers no delete control, and neither does the route."""
    handler = _handler(tmp_path)
    handler.attach_audit_surface(AuditReadSurface(_store(tmp_path)))

    response = await _get(handler, AUDIT_READ_PATH, method="DELETE")

    assert response.status_code == 405


def test_the_gateway_hands_the_audit_store_to_the_webui_handler(tmp_path: Path) -> None:
    """The route exists, and only the gateway can fill it (#29).

    A viewer that answered 503 in every deployment would be a route and not a feature. The
    gateway attaches the read surface beside the latch surface, because both are operator
    surfaces on the same handler.
    """
    from nanoinfra.cli.gateway_runtime import _attach_audit_read_surface

    handler = _handler(tmp_path)
    store = _store(tmp_path)
    _denial(store)
    channels = SimpleNamespace(
        channels={"websocket": SimpleNamespace(gateway=SimpleNamespace(http=handler))}
    )

    _attach_audit_read_surface(channels, store)

    assert handler.audit is not None
    assert handler.audit.page({})["total"] == 1
