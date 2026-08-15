# tests/channels/test_telegram_approval_commands.py
"""Item 41 (#43): Telegram carries the answer to one suspended approval.

The channel forwards a fixed set of slash commands to the bus, and the catch-all message handler
excludes every command. A command outside that set therefore reaches nothing at all, so
``/approve`` and ``/deny`` needed a place in the set before the router could ever see them.

The content must arrive verbatim. The request id and the reason are the two values the executor
and the audit log read, so the channel must not reshape either one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

# Check optional Telegram dependencies before running tests
try:
    import telegram  # noqa: F401
except ImportError:  # pragma: no cover -- the channel suite skips the same way
    pytest.skip(
        "Telegram dependencies not installed (python-telegram-bot)", allow_module_level=True
    )

from nanoinfra.bus.events import InboundMessage
from nanoinfra.channels.telegram.runtime import TelegramChannel, TelegramConfig
from nanoinfra.command.approvals import APPROVE_COMMAND, DENY_COMMAND, approval_actor

_REQUEST_ID = "0f8c1d2b3a4e5f60718293a4b5c6d7e8"
_APPROVER_ACCOUNT = "770123456"
_APPROVER_USERNAME = "ops_lead"


class _Bus:
    """A bus that keeps what the channel published."""

    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []

    async def publish_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)


def _channel(bus: _Bus) -> TelegramChannel:
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "test-token", "allowFrom": [_APPROVER_ACCOUNT]}
    )
    return TelegramChannel(config, bus)  # type: ignore[arg-type]


def _update(text: str) -> Any:
    user = SimpleNamespace(
        id=int(_APPROVER_ACCOUNT), username=_APPROVER_USERNAME, first_name="Ops"
    )
    message = SimpleNamespace(
        message_id=41,
        chat_id=int(_APPROVER_ACCOUNT),
        chat=SimpleNamespace(type="private", is_forum=False),
        text=text,
        message_thread_id=None,
        reply_to_message=None,
    )
    return SimpleNamespace(message=message, effective_user=user)


# -- the forwarded set -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"{APPROVE_COMMAND} {_REQUEST_ID}",
        f"{DENY_COMMAND} {_REQUEST_ID}",
        f"{DENY_COMMAND} {_REQUEST_ID} the change window is closed",
        f"{APPROVE_COMMAND}@nanoinfra_bot {_REQUEST_ID}",
        APPROVE_COMMAND,
        DENY_COMMAND,
    ],
)
def test_an_answer_reaches_the_forwarded_command_set(text: str) -> None:
    """The catch-all handler excludes commands, so an absent command reaches nothing."""
    assert TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE.match(text)


def test_the_command_menu_names_both_answers() -> None:
    """An operator discovers the answer in the bot menu, and not in the source."""
    commands = {command.command for command in TelegramChannel.BOT_COMMANDS}

    assert "approve" in commands
    assert "deny" in commands


def test_no_other_command_gained_a_route() -> None:
    """A word that looks like an answer must not become one."""
    assert not TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE.match("/approved")
    assert not TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE.match("/denylist")


# -- what reaches the bus ----------------------------------------------------------------


async def test_the_request_id_and_the_reason_reach_the_bus_unchanged() -> None:
    """The executor reads the id, and the audit log reads the reason."""
    bus = _Bus()
    channel = _channel(bus)

    await channel._process_forward_command(  # noqa: SLF001 -- the ingress path under test
        _update(f"{DENY_COMMAND} {_REQUEST_ID} the change window is closed"), None
    )

    assert len(bus.inbound) == 1
    assert bus.inbound[0].content == f"{DENY_COMMAND} {_REQUEST_ID} the change window is closed"


async def test_the_bot_suffix_never_reaches_the_router() -> None:
    """Telegram appends ``@bot`` in a group. The channel strips it before the bus."""
    bus = _Bus()
    channel = _channel(bus)

    await channel._process_forward_command(  # noqa: SLF001
        _update(f"{APPROVE_COMMAND}@nanoinfra_bot {_REQUEST_ID}"), None
    )

    assert bus.inbound[0].content == f"{APPROVE_COMMAND} {_REQUEST_ID}"


async def test_the_sender_id_carries_the_account_the_actor_rule_reads() -> None:
    """The channel decorates its sender id, and #43 reads the authenticated half back out."""
    bus = _Bus()
    channel = _channel(bus)

    await channel._process_forward_command(  # noqa: SLF001
        _update(f"{APPROVE_COMMAND} {_REQUEST_ID}"), None
    )

    msg = bus.inbound[0]
    assert msg.sender_id == f"{_APPROVER_ACCOUNT}|{_APPROVER_USERNAME}"
    assert approval_actor(msg) == _APPROVER_ACCOUNT


async def test_a_sender_outside_allow_from_never_reaches_the_bus() -> None:
    """Reachability is a separate list from authority, and it stops the message first.

    An approver must sit on both lists, for two different reasons. ``allowFrom`` admits the
    message, and ``gates.approvers`` decides whether the answer counts.
    """
    bus = _Bus()
    channel = _channel(bus)
    update = _update(f"{APPROVE_COMMAND} {_REQUEST_ID}")
    update.effective_user = SimpleNamespace(id=999000111, username="passer_by", first_name="P")
    channel.send = _refuse_send  # type: ignore[method-assign]

    await channel._process_forward_command(update, None)  # noqa: SLF001

    assert bus.inbound == []


async def _refuse_send(msg: Any) -> None:
    """The pairing reply has no bot here, and this test asks about the bus."""
    return None
