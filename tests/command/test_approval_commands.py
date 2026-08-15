# tests/command/test_approval_commands.py
"""Item 41 (#43): the slash commands that answer one suspended approval.

#38 built the wait and the operator socket. #27 built the inbox, and the inbox answers over
HTTP from the WebUI. So ``webui`` was the only path that could answer. A deployment that runs
the WebUI alone has a nominal second path, because the chat arrives on ``websocket`` and the
answer arrives from the same browser with the same token. These commands make a real second
path exist.

Three properties carry the security value of this file, and none of them is a layout test.

The answer path must stay out of every tool import closure. The commands run inside the gateway
process, so the import graph is the protection that keeps a tool away from the client.

The actor must come from the channel. A sender that ``gates.approvers`` does not name answers
nothing, and no argument of the command names an actor.

The refusal must say why, and it must name no approver. An operator who reads "denied" files a
bug. An operator who reads the rule takes an action.
"""

from __future__ import annotations

import ast
import collections
import socket
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanoinfra.bus.events import InboundMessage
from nanoinfra.command.approvals import (
    APPROVE_COMMAND,
    DENY_COMMAND,
    ApprovalAnswerSurface,
    approval_actor,
    register_approval_commands,
)
from nanoinfra.command.router import CommandContext, CommandRouter
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.operator_socket import (
    ApprovalService,
    OperatorClient,
    bind_operator_socket,
    serve_operator_socket,
)
from nanoinfra.gates.pending import ApprovalState, PendingApprovalStore
from nanoinfra.gates.prompt import render_approval_prompt_for_hosts
from nanoinfra.gates.tokens import ApprovalTokenStore

_COMMAND = "systemctl reload nginx"
_HOSTS = ("10.0.2.11", "10.0.2.12")
_SESSION = "websocket:chat-1"

# The Telegram account of the one approver, and the id the channel puts on an inbound message.
# The channel appends the username for its own allowlist match, and the account id is the half
# that Telegram authenticates.
_APPROVER = "770123456"
_APPROVER_SENDER_ID = f"{_APPROVER}|ops_lead"
_STRANGER_SENDER_ID = "999000111|passer_by"

# Every module the agent can load, at any import depth. The answer path must stay out of it.
_TOOLS = Path("nanoinfra/agent/tools")
_FORBIDDEN_FOR_TOOLS = (
    "nanoinfra.command.approvals",
    "nanoinfra.gates.approval_delivery",
    "nanoinfra.gates.executor.operator_socket",
)


def _gates(**over: Any) -> GatesConfig:
    """A policy with one approver on Telegram and two authenticated paths."""
    values: dict[str, Any] = {
        "approvers": [{"channel": "telegram", "sender": _APPROVER}],
        "approvalPaths": ["webui", "telegram"],
    }
    values.update(over)
    return GatesConfig.model_validate(values)


class _CountingClient(OperatorClient):
    """The real client, plus a count of the calls one answer costs."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__(socket_path, timeout_s=5.0)
        self.calls: list[str] = []

    def pending(self) -> Any:
        self.calls.append("pending")
        return super().pending()

    def approve(self, **kwargs: Any) -> Any:
        self.calls.append("approve")
        return super().approve(**kwargs)

    def deny(self, **kwargs: Any) -> Any:
        self.calls.append("deny")
        return super().deny(**kwargs)


class _Executor:
    """One in-process operator socket, and the store behind it."""

    def __init__(self, socket_path: Path, store: PendingApprovalStore) -> None:
        self.socket_path = socket_path
        self.store = store
        self.client = _CountingClient(socket_path)

    def suspend(self, *, origin_path: str = "websocket", timeout_s: float = 30.0) -> Any:
        prompt = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_HOSTS)
        return self.store.create(
            session_id=_SESSION,
            origin_path=origin_path,
            execution_context="interactive",
            capability_class="mutate.remote",
            scope="group",
            hosts=prompt.hosts,
            command=prompt.command,
            payload=prompt.text,
            target_digest=prompt.target_digest,
            timeout_s=timeout_s,
        )

    def router(self) -> CommandRouter:
        router = CommandRouter()
        register_approval_commands(
            router, surface=ApprovalAnswerSurface(client=self.client)
        )
        return router


@pytest.fixture
def executor(tmp_path: Path):
    """A factory for one in-process operator socket. Each call binds its own path."""
    listeners: list[socket.socket] = []

    def build(*, gates: GatesConfig | None = None, name: str = "e") -> _Executor:
        policy = gates if gates is not None else _gates()
        store = PendingApprovalStore()
        service = ApprovalService(
            pending=store, tokens=ApprovalTokenStore(), gates_loader=lambda: policy
        )
        path = tmp_path / "run" / "operator" / f"{name}.op.sock"
        listener = bind_operator_socket(path)
        listeners.append(listener)
        threading.Thread(
            target=serve_operator_socket, args=(listener, service), daemon=True
        ).start()
        return _Executor(path, store)

    try:
        yield build
    finally:
        for listener in listeners:
            listener.close()


async def _answer(
    router: CommandRouter, text: str, *, channel: str = "telegram", sender: str
) -> str:
    """Route one chat message the way the agent loop routes a priority command."""
    assert router.is_priority(text), f"{text!r} must route before the agent turn"
    msg = InboundMessage(
        channel=channel, sender_id=sender, chat_id=sender, content=text
    )
    ctx = CommandContext(
        msg=msg, session=None, key=f"{channel}:{sender}", raw=text, loop=MagicMock()
    )
    reply = await router.dispatch_priority(ctx)
    assert reply is not None, "a priority command must answer"
    assert reply.channel == channel
    assert reply.chat_id == sender
    return reply.content


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


# -- the route ---------------------------------------------------------------------------


def test_an_answer_routes_before_the_agent_turn(executor: Any) -> None:
    """The model never sees the answer, and it cannot issue one.

    The priority tier runs in the loop before every other branch. A command with an argument
    needed a prefix tier there, because the tier matched an exact string only.
    """
    router = executor().router()

    assert router.is_priority(f"{APPROVE_COMMAND} abc123")
    assert router.is_priority(f"{DENY_COMMAND} abc123 the window is closed")
    assert router.is_priority(APPROVE_COMMAND)
    assert router.is_priority(DENY_COMMAND)


def test_the_answer_stays_out_of_the_dispatch_tier(executor: Any) -> None:
    """A mid-turn injection must not carry an answer. The priority tier owns both commands."""
    router = executor().router()

    assert not router.is_dispatchable_command(f"{APPROVE_COMMAND} abc123")
    assert not router.is_dispatchable_command(f"{DENY_COMMAND} abc123")


def test_a_command_with_a_bot_suffix_still_routes(executor: Any) -> None:
    """Telegram delivers ``/approve@bot <id>`` in a group. The router strips the suffix."""
    router = executor().router()

    assert router.is_priority(f"{APPROVE_COMMAND}@nanoinfra_bot abc123")


# -- the actor ---------------------------------------------------------------------------


def test_a_telegram_actor_is_the_account_id_and_not_the_username() -> None:
    """The account id is the half Telegram authenticates. A username is user-controlled.

    An approver set that named a username would move approval authority to a value the user
    changes at will, so the actor drops the suffix the channel adds for its allowlist.
    """
    assert approval_actor("telegram", _APPROVER_SENDER_ID) == _APPROVER
    assert approval_actor("telegram", _APPROVER) == _APPROVER


def test_another_channel_keeps_its_sender_id_unchanged() -> None:
    """No channel but Telegram decorates its sender id, so nothing else is normalized."""
    assert approval_actor("websocket", "browser-1") == "browser-1"
    assert approval_actor("discord", "84|nick") == "84|nick"


# -- the approval ------------------------------------------------------------------------


async def test_an_approver_on_another_path_approves_the_action(executor: Any) -> None:
    """The acceptance case: a WebSocket action, and an answer from Telegram."""
    running = executor()
    approval = running.suspend(origin_path="websocket")

    reply = await _answer(
        running.router(),
        f"{APPROVE_COMMAND} {approval.request_id}",
        sender=_APPROVER_SENDER_ID,
    )

    outcome = running.store.wait(approval.request_id)
    assert outcome.state is ApprovalState.APPROVED
    assert outcome.actor == _APPROVER
    assert outcome.approval_path == "telegram"
    assert approval.request_id in reply


async def test_the_approval_carries_the_digest_the_executor_rendered(
    executor: Any,
) -> None:
    """The operator types a request id, and the surface echoes the executor's own digest.

    The record is immutable and the id is a uuid, so the digest re-read proves the executor
    still holds those exact bytes.
    """
    running = executor()
    approval = running.suspend()

    await _answer(
        running.router(),
        f"{APPROVE_COMMAND} {approval.request_id}",
        sender=_APPROVER_SENDER_ID,
    )

    assert running.store.wait(approval.request_id).state is ApprovalState.APPROVED
    assert running.client.calls == ["pending", "approve"]


async def test_a_denial_carries_the_reason_and_costs_one_call(executor: Any) -> None:
    """A denial costs one call, and never more than an approval (#27 applies the same rule)."""
    running = executor()
    approval = running.suspend()

    reply = await _answer(
        running.router(),
        f"{DENY_COMMAND} {approval.request_id} the change window is closed",
        sender=_APPROVER_SENDER_ID,
    )

    outcome = running.store.wait(approval.request_id)
    assert outcome.state is ApprovalState.DENIED
    assert outcome.reason == "the change window is closed"
    assert outcome.actor == _APPROVER
    assert running.client.calls == ["deny"]
    assert approval.request_id in reply


async def test_a_denial_needs_no_reason(executor: Any) -> None:
    """A reason helps a reviewer. A missing one must not keep an action alive."""
    running = executor()
    approval = running.suspend()

    await _answer(
        running.router(), f"{DENY_COMMAND} {approval.request_id}", sender=_APPROVER_SENDER_ID
    )

    assert running.store.wait(approval.request_id).state is ApprovalState.DENIED


# -- every refusal an operator can meet --------------------------------------------------


async def test_a_sender_the_config_does_not_name_answers_nothing(executor: Any) -> None:
    """Reachability grants nothing. ``gates.approvers`` is the only source of authority.

    The refusal names the rule and the identity that failed it. It names no approver, because
    a refusal that listed the approver set would tell a stranger who to impersonate.
    """
    running = executor()
    approval = running.suspend()

    reply = await _answer(
        running.router(),
        f"{APPROVE_COMMAND} {approval.request_id}",
        sender=_STRANGER_SENDER_ID,
    )

    assert running.store.get(approval.request_id) is not None
    assert running.store.pending()  # the action still waits
    assert "gates.approvers" in reply
    assert _APPROVER not in reply


async def test_a_request_that_arrived_on_telegram_takes_no_answer_there(
    executor: Any,
) -> None:
    """Path independence holds by construction, and the refusal says why (#13)."""
    running = executor()
    approval = running.suspend(origin_path="telegram")

    reply = await _answer(
        running.router(),
        f"{APPROVE_COMMAND} {approval.request_id}",
        sender=_APPROVER_SENDER_ID,
    )

    assert running.store.pending()  # the action still waits
    assert "telegram" in reply
    assert "another authenticated path" in reply


async def test_a_denial_from_the_origin_path_is_refused_too(executor: Any) -> None:
    """A denial is terminal (#15), so it takes the same identity and path checks."""
    running = executor()
    approval = running.suspend(origin_path="telegram")

    reply = await _answer(
        running.router(),
        f"{DENY_COMMAND} {approval.request_id} no",
        sender=_APPROVER_SENDER_ID,
    )

    assert running.store.pending()
    assert "another authenticated path" in reply


async def test_an_unknown_request_id_reads_as_an_action_that_is_gone(
    executor: Any,
) -> None:
    """Three events read the same to an operator, so the sentence names all three."""
    running = executor()

    reply = await _answer(
        running.router(), f"{APPROVE_COMMAND} 0123456789abcdef", sender=_APPROVER_SENDER_ID
    )

    assert "0123456789abcdef" in reply
    assert "expired" in reply


async def test_a_command_with_no_request_id_states_the_two_forms(executor: Any) -> None:
    """A bare command must not reach the executor, and it must not reach the model."""
    running = executor()

    reply = await _answer(running.router(), APPROVE_COMMAND, sender=_APPROVER_SENDER_ID)

    assert APPROVE_COMMAND in reply
    assert DENY_COMMAND in reply
    assert running.client.calls == []


async def test_extra_text_on_an_approval_states_the_two_forms(executor: Any) -> None:
    """A field this module ignored would be a field an operator believes they sent."""
    running = executor()
    approval = running.suspend()

    reply = await _answer(
        running.router(),
        f"{APPROVE_COMMAND} {approval.request_id} yes please",
        sender=_APPROVER_SENDER_ID,
    )

    assert APPROVE_COMMAND in reply
    assert running.client.calls == []
    assert running.store.pending()


async def test_an_absurd_request_id_refuses_rather_than_truncates(executor: Any) -> None:
    """A truncated id would name another request, or no request at all."""
    running = executor()

    reply = await _answer(
        running.router(), f"{APPROVE_COMMAND} {'a' * 400}", sender=_APPROVER_SENDER_ID
    )

    assert APPROVE_COMMAND in reply
    assert running.client.calls == []


async def test_an_unreachable_executor_reports_that_nothing_answered(
    tmp_path: Path,
) -> None:
    """An answer that never arrived must not read as a refusal the executor issued."""
    surface = ApprovalAnswerSurface(client=OperatorClient(tmp_path / "absent.op.sock"))
    router = CommandRouter()
    register_approval_commands(router, surface=surface)

    reply = await _answer(
        router, f"{APPROVE_COMMAND} 0123456789abcdef", sender=_APPROVER_SENDER_ID
    )

    assert "not reachable" in reply


def test_the_surface_refuses_anything_that_is_not_the_operator_client() -> None:
    """A chat message carries strings. Those must fail at the door."""
    with pytest.raises(TypeError):
        ApprovalAnswerSurface(client={"request_id": "abc"})


def test_the_surface_exposes_the_two_answers_and_nothing_else(tmp_path: Path) -> None:
    """A router holds this object. It must not be able to hand the client on."""
    surface = ApprovalAnswerSurface(client=OperatorClient(tmp_path / "absent.op.sock"))

    assert sorted(name for name in dir(surface) if not name.startswith("_")) == [
        "approve",
        "deny",
    ]


# -- the import closure ------------------------------------------------------------------


def test_no_tool_module_reaches_the_answer_path() -> None:
    """The acceptance criterion of #43, as a check rather than a promise.

    The commands run inside the gateway process, and the gateway runs as the agent's account.
    A tool that cannot import the client cannot answer, whatever the model writes.
    """
    closure = _tool_import_closure()

    assert [name for name in _FORBIDDEN_FOR_TOOLS if name in closure] == []


def test_no_tool_module_imports_the_answer_modules_directly() -> None:
    offenders = [
        str(path)
        for path in _TOOLS.rglob("*.py")
        for name in _FORBIDDEN_FOR_TOOLS
        if name in _imported_modules(path)
    ]

    assert offenders == []
