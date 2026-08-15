# tests/webui/test_latch_api.py
"""Item 26 (#28): the operator surface that clears a denial latch.

Two properties carry the security value of this item, and neither one is a behaviour test.
The clear path must be unreachable from a tool, and the refusal count must survive a restart
(#32). Both are checkable, so this file checks them instead of trusting the design.

The rest covers the two routes, the idempotent clear, and the audit record that names the
operator who cleared the latch.
"""

from __future__ import annotations

import ast
import collections
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.datastructures import Headers

from nanoinfra.channels.websocket.runtime import WebSocketConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.latch import LatchController
from nanoinfra.gates.runtime import GateRuntime, build_gate_runtime
from nanoinfra.webui.gateway_services import build_gateway_services
from nanoinfra.webui.latch_api import (
    LATCH_CLEAR_PATH,
    LATCH_READ_PATH,
    LATCH_VALUES_HEADER,
    LatchClearError,
    LatchOperatorSurface,
    latch_values_from_request,
    operator_actor,
)
from nanoinfra.webui.ws_http import GatewayHTTPHandler

_SESSION = "websocket:chat-1"
_CLASS = "mutate.remote"

# Every module the agent can load, at any import depth. The clear path must stay out of it.
_TOOLS = Path("nanoinfra/agent/tools")
_FORBIDDEN_FOR_TOOLS = (
    "nanoinfra.webui.latch_api",
    "nanoinfra.webui.ws_http",
)


# -- helpers ----------------------------------------------------------------------------


def _surface(root: Path) -> tuple[LatchOperatorSurface, GateRuntime]:
    """Build one gateway's worth of gate state, and split the halves the way #33 does."""
    runtime, controller = build_gate_runtime(GatesConfig(), root=root)
    return LatchOperatorSurface(controller=controller, audit=runtime.audit), runtime


def _deny(runtime: GateRuntime, *, session_id: str = _SESSION, tool: str = "execute_on_server"):
    return runtime.refuse_action(
        session_id=session_id,
        capability_class=_CLASS,
        tool=tool,
        reason="unattended mutate.remote at group scope is denied",
        execution_context="automation",
    )


def _refuse(runtime: GateRuntime, *, session_id: str = _SESSION, tool: str = "execute_on_server"):
    return runtime.latched_refusal(
        session_id=session_id, capability_class=_CLASS, tool=tool
    )


def _records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for segment in sorted(root.glob("gate-*.jsonl")):
        for line in segment.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _imported_modules(path: Path) -> set[str]:
    """Every module name a file imports, at any depth, including inside a function."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _first_party_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in Path("nanoinfra").rglob("*.py"):
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _tool_import_closure() -> set[str]:
    """Every first-party module the tool package reaches, transitively.

    A one-level check passes while a two-hop path stays open, so this walks the whole graph.
    """
    modules = _first_party_modules()
    graph = {
        name: {edge for edge in _imported_modules(path) if edge in modules}
        for name, path in modules.items()
    }
    seeds = [name for name in modules if name.startswith("nanoinfra.agent.tools")]
    seen = set(seeds)
    queue = collections.deque(seeds)
    while queue:
        for edge in graph.get(queue.popleft(), ()):
            if edge not in seen:
                seen.add(edge)
                queue.append(edge)
    return seen


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


def _request(path: str, *, token: str | None = None, values: dict[str, Any] | None = None):
    headers: list[tuple[str, str]] = []
    if token is not None:
        headers.append(("Authorization", f"Bearer {token}"))
    if values is not None:
        headers.append((LATCH_VALUES_HEADER, json.dumps(values)))
    return SimpleNamespace(path=path, headers=Headers(headers))


def _connection():
    return SimpleNamespace(
        remote_address=("127.0.0.1", 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


# -- the read surface -------------------------------------------------------------------


def test_an_empty_log_reports_no_latched_session(tmp_path: Path) -> None:
    surface, _ = _surface(tmp_path / "gates")

    payload = surface.payload()

    assert payload["latches"] == []
    assert payload["degraded"] is False
    assert "no latched sessions" in payload["summary"]


def test_a_denial_shows_the_session_the_class_the_time_and_the_reason(tmp_path: Path) -> None:
    surface, runtime = _surface(tmp_path / "gates")

    _deny(runtime)

    entry = surface.payload()["latches"][0]
    assert entry["sessionId"] == _SESSION
    assert entry["capabilityClass"] == _CLASS
    assert entry["reason"] == "unattended mutate.remote at group scope is denied"
    assert entry["refusals"] == 0
    # An operator reads the time, so it has to be a real timestamp and not the epoch.
    assert entry["deniedAt"] is not None
    assert entry["deniedAt"].startswith("20")


def test_the_refusal_count_rises_with_each_blocked_attempt(tmp_path: Path) -> None:
    surface, runtime = _surface(tmp_path / "gates")
    _deny(runtime)

    _refuse(runtime)
    _refuse(runtime)

    entry = surface.payload()["latches"][0]
    assert entry["refusals"] == 2
    assert [attempt["tool"] for attempt in entry["attempts"]] == [
        "execute_on_server",
        "execute_on_server",
    ]


def test_the_refusal_count_survives_a_restart(tmp_path: Path) -> None:
    """#32. The count comes from the audit log, so a restart must not reset the banner."""
    root = tmp_path / "gates"
    _, runtime = _surface(root)
    _deny(runtime)
    _refuse(runtime)
    _refuse(runtime)
    _refuse(runtime)

    restarted, _ = _surface(root)  # a fresh process over the same log

    entry = restarted.payload()["latches"][0]
    assert entry["refusals"] == 3
    assert entry["capabilityClass"] == _CLASS


def test_a_second_session_gets_its_own_entry(tmp_path: Path) -> None:
    surface, runtime = _surface(tmp_path / "gates")

    _deny(runtime)
    _deny(runtime, session_id="websocket:chat-2")

    assert [entry["sessionId"] for entry in surface.payload()["latches"]] == [
        "websocket:chat-1",
        "websocket:chat-2",
    ]


def test_an_unreadable_log_reports_degraded_and_never_reads_as_unlatched(tmp_path: Path) -> None:
    """An empty state must not read as "nothing is enforced". #32 keeps every session latched."""
    root = tmp_path / "gates"
    surface, runtime = _surface(root)
    _deny(runtime)
    (root / "gate-2026-01-01.jsonl").mkdir()  # a directory, so the read raises OSError

    payload = surface.payload()

    assert payload["degraded"] is True
    assert "every session stays latched" in payload["summary"]


# -- the clear surface ------------------------------------------------------------------


def test_a_clear_lifts_the_latch_and_names_the_actor_in_the_audit_log(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    surface, runtime = _surface(root)
    _deny(runtime)

    result = surface.clear(
        {"sessionId": _SESSION, "capabilityClass": _CLASS, "reason": "false positive"},
        actor="webui:ops@example.com",
    )

    assert result == {
        "cleared": True,
        "sessionId": _SESSION,
        "capabilityClass": _CLASS,
        "actor": "webui:ops@example.com",
    }
    assert surface.payload()["latches"] == []
    cleared = [record for record in _records(root) if record["decision"] == "cleared"]
    assert [record["actor"] for record in cleared] == ["webui:ops@example.com"]
    assert cleared[0]["session_id"] == _SESSION
    assert cleared[0]["capability_class"] == _CLASS
    assert cleared[0]["reason"] == "false positive"


def test_a_cleared_class_reaches_the_gate_and_stops_refusing(tmp_path: Path) -> None:
    """The clear has to reach the live gate, and not only the payload an operator reads."""
    surface, runtime = _surface(tmp_path / "gates")
    _deny(runtime)
    assert _refuse(runtime) is not None

    surface.clear({"sessionId": _SESSION, "capabilityClass": _CLASS}, actor="webui")

    assert _refuse(runtime) is None


def test_a_second_clear_is_idempotent_and_records_once(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    surface, runtime = _surface(root)
    _deny(runtime)
    values = {"sessionId": _SESSION, "capabilityClass": _CLASS}

    first = surface.clear(values, actor="webui")
    second = surface.clear(values, actor="webui")

    assert first["cleared"] is True
    assert second["cleared"] is False
    assert len([record for record in _records(root) if record["decision"] == "cleared"]) == 1


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"sessionId": _SESSION},
        {"capabilityClass": _CLASS},
        {"sessionId": "", "capabilityClass": _CLASS},
        {"sessionId": _SESSION, "capabilityClass": 7},
    ],
)
def test_a_clear_needs_a_session_and_a_class(tmp_path: Path, values: dict[str, Any]) -> None:
    surface, _ = _surface(tmp_path / "gates")

    with pytest.raises(LatchClearError):
        surface.clear(values, actor="webui")


# -- the property that makes the split real ---------------------------------------------


def test_no_tool_module_reaches_the_clear_path(tmp_path: Path) -> None:
    """The acceptance criterion of #28, as a check rather than a promise.

    A tool that cannot import the accessor cannot hold the controller, whatever it writes.
    """
    closure = _tool_import_closure()

    assert [name for name in _FORBIDDEN_FOR_TOOLS if name in closure] == []


def test_no_tool_module_imports_the_operator_surface_directly() -> None:
    offenders = [
        str(path)
        for path in _TOOLS.rglob("*.py")
        if "nanoinfra.webui.latch_api" in _imported_modules(path)
    ]

    assert offenders == []


def test_the_gate_runtime_still_exposes_no_clear() -> None:
    """The gate half travels toward the tools, so it must carry no way to lift a latch."""
    assert not hasattr(GateRuntime, "clear")
    assert not hasattr(GateRuntime, "clear_session")
    assert [name for name in dir(GateRuntime) if "clear" in name] == []


def test_the_gate_runtime_holds_no_controller(tmp_path: Path) -> None:
    _, runtime = _surface(tmp_path / "gates")

    reachable = [getattr(runtime, name) for name in dir(runtime) if not name.startswith("__")]

    assert [value for value in reachable if isinstance(value, LatchController)] == []


def test_the_surface_refuses_anything_that_is_not_the_operator_half(tmp_path: Path) -> None:
    """A request carries strings. Those must fail at the door, the way #15 makes them fail."""
    _, runtime = _surface(tmp_path / "gates")

    with pytest.raises(TypeError):
        LatchOperatorSurface(
            controller={"session_id": _SESSION},
            audit=runtime.audit,
        )


def test_the_surface_exposes_the_two_operations_and_nothing_else(tmp_path: Path) -> None:
    """A route holds this object. It must not be able to hand the controller on."""
    surface, _ = _surface(tmp_path / "gates")

    assert sorted(name for name in dir(surface) if not name.startswith("_")) == [
        "clear",
        "payload",
    ]


# -- the routes -------------------------------------------------------------------------


async def test_both_routes_refuse_a_request_with_no_token(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    surface, runtime = _surface(tmp_path / "gates")
    _deny(runtime)
    handler.attach_latch_surface(surface)

    for path in (LATCH_READ_PATH, LATCH_CLEAR_PATH):
        response = await handler.dispatch(_connection(), _request(path))
        assert response.status_code == 401, path
    assert surface.payload()["latches"] != []


async def test_the_read_route_returns_the_latched_sessions(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    surface, runtime = _surface(tmp_path / "gates")
    _deny(runtime)
    _refuse(runtime)
    handler.attach_latch_surface(surface)
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(_connection(), _request(LATCH_READ_PATH, token=token))

    assert response.status_code == 200
    entry = _body(response)["latches"][0]
    assert (entry["sessionId"], entry["capabilityClass"], entry["refusals"]) == (
        _SESSION,
        _CLASS,
        1,
    )


async def test_the_clear_route_lifts_the_latch(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    root = tmp_path / "gates"
    surface, runtime = _surface(root)
    _deny(runtime)
    handler.attach_latch_surface(surface)
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(),
        _request(
            LATCH_CLEAR_PATH,
            token=token,
            values={"sessionId": _SESSION, "capabilityClass": _CLASS},
        ),
    )

    assert response.status_code == 200
    assert _body(response)["cleared"] is True
    assert _body(response)["actor"] == "webui"
    assert [record["actor"] for record in _records(root) if record["decision"] == "cleared"] == [
        "webui"
    ]


async def test_the_clear_route_rejects_a_payload_with_no_session(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    surface, runtime = _surface(tmp_path / "gates")
    _deny(runtime)
    handler.attach_latch_surface(surface)
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(), _request(LATCH_CLEAR_PATH, token=token, values={"reason": "please"})
    )

    assert response.status_code == 400
    assert surface.payload()["latches"] != []


async def test_the_routes_answer_503_until_the_gateway_attaches_the_surface(
    tmp_path: Path,
) -> None:
    """A WebUI with no gate runtime must say so, and must not read as "no latches"."""
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)

    for path in (LATCH_READ_PATH, LATCH_CLEAR_PATH):
        response = await handler.dispatch(_connection(), _request(path, token=token))
        assert response.status_code == 503, path


# -- how the controller gets there ------------------------------------------------------


async def test_the_gateway_hands_the_controller_to_the_webui_handler(tmp_path: Path) -> None:
    """The gateway owns the controller, so the gateway is the only caller that hands it on."""
    from nanoinfra.cli.gateway_runtime import _attach_latch_operator_surface

    handler = _handler(tmp_path)
    runtime, controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    _deny(runtime)
    websocket = SimpleNamespace(gateway=SimpleNamespace(http=handler))

    _attach_latch_operator_surface(
        SimpleNamespace(channels={"websocket": websocket}), controller, runtime.audit
    )

    token = handler.tokens.issue_api_token(300)
    response = await handler.dispatch(_connection(), _request(LATCH_READ_PATH, token=token))
    assert response.status_code == 200
    assert _body(response)["latches"][0]["sessionId"] == _SESSION


def test_a_gateway_with_no_webui_channel_still_boots(tmp_path: Path) -> None:
    """No WebUI means no operator control. That is a warning, and never a failed boot."""
    from nanoinfra.cli.gateway_runtime import _attach_latch_operator_surface

    runtime, controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")

    _attach_latch_operator_surface(
        SimpleNamespace(channels={"telegram": SimpleNamespace()}), controller, runtime.audit
    )


def test_the_gateway_no_longer_drops_the_controller_on_the_floor() -> None:
    """#33 left the controller unused. A source check keeps it wired to the operator surface."""
    import inspect

    from nanoinfra.cli import gateway_runtime

    source = inspect.getsource(gateway_runtime)

    assert "_ = latch_controller" not in source
    assert "_attach_latch_operator_surface(channels, latch_controller" in source
    assert "gate=latch_controller" not in source


# -- who the actor is -------------------------------------------------------------------


def test_the_actor_is_the_path_when_no_proxy_asserts_an_identity() -> None:
    request = _request(LATCH_CLEAR_PATH)

    assert operator_actor(request, SimpleNamespace(trusted_proxy_auth=None)) == "webui"


def test_a_trusted_proxy_assertion_names_the_operator() -> None:
    request = SimpleNamespace(
        path=LATCH_CLEAR_PATH,
        headers=Headers([("Cf-Access-Authenticated-User-Email", "ops@example.com")]),
    )
    setattr(request, "_nanoinfra_trusted_proxy_authenticated", True)
    config = SimpleNamespace(
        trusted_proxy_auth=SimpleNamespace(
            assertion_header="Cf-Access-Authenticated-User-Email"
        )
    )

    assert operator_actor(request, config) == "webui:ops@example.com"


def test_an_untrusted_peer_cannot_name_itself() -> None:
    """The flag comes from the peer check. Without it the header is a claim from the client."""
    request = SimpleNamespace(
        path=LATCH_CLEAR_PATH,
        headers=Headers([("Cf-Access-Authenticated-User-Email", "attacker@example.com")]),
    )
    config = SimpleNamespace(
        trusted_proxy_auth=SimpleNamespace(
            assertion_header="Cf-Access-Authenticated-User-Email"
        )
    )

    assert operator_actor(request, config) == "webui"


def test_the_clear_values_come_from_the_header() -> None:
    values = {"sessionId": _SESSION, "capabilityClass": _CLASS}

    assert latch_values_from_request(_request(LATCH_CLEAR_PATH, values=values)) == values


@pytest.mark.parametrize("raw", ["", "not json", '"a string"', "[1, 2]"])
def test_a_malformed_values_header_is_an_invalid_payload(raw: str) -> None:
    request = SimpleNamespace(path=LATCH_CLEAR_PATH, headers=Headers([(LATCH_VALUES_HEADER, raw)]))

    assert latch_values_from_request(request) is None
