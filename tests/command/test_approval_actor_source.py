# tests/command/test_approval_actor_source.py
"""An approval actor never comes from a value the client chose -- nanoinfraorg/nanoinfra#81.

`nanoinfra/command/approvals.py` states that the actor comes from the channel: *"`channel` and
`sender_id` carry the identity the channel authenticated."* On the WebSocket channel that sentence
was false. `client_id` is a query parameter the browser picks, and it falls back to `anon-<uuid>`,
so a `/approve` typed in the WebUI chat named a value the client controlled.

It was not exploitable in a shipped configuration, because `websocket` is absent from the default
`gates.approvalPaths`. **The refusal is the property, so the tests below configure the deployment
that would have made it exploitable** rather than relying on that default.

`sender_id` keeps its meaning. It keys a session, it matches `allowFrom` and it reaches the pairing
store, so replacing it would change session identity for every WebUI user of every deployment. The
authenticated identity gets a field of its own instead.
"""

from __future__ import annotations

from nanoinfra.bus.events import InboundMessage
from nanoinfra.command.approvals import approval_actor

_CHOSEN_BY_THE_CLIENT = "alberto@example.com"
_VERIFIED = "webui:alberto@example.com"


def _message(**over: object) -> InboundMessage:
    fields: dict[str, object] = {
        "channel": "websocket",
        "sender_id": "browser-picked-this",
        "chat_id": "chat-1",
        "content": "/approve 0123456789abcdef",
    }
    fields.update(over)
    return InboundMessage(**fields)  # pyright: ignore[reportArgumentType]


# --------------------------------------------------------- the field the channel authenticated


def test_a_message_carries_no_authenticated_sender_by_default() -> None:
    """`None` is the honest default. Most channels authenticate a sender and set nothing here.

    An empty string would read as a name, which is the distinction #67 keeps on the wire for the
    same reason.
    """
    assert _message().authenticated_sender is None


def test_the_verified_identity_is_the_actor_when_a_channel_offers_one() -> None:
    actor = approval_actor(_message(authenticated_sender=_VERIFIED))

    assert actor == _VERIFIED


def test_a_client_chosen_sender_id_never_becomes_the_actor() -> None:
    """The defect itself. The query string held this value.

    A deployment that put `websocket` in `gates.approvalPaths` and named a `client_id` in
    `gates.approvers` would have matched it.
    """
    actor = approval_actor(_message(sender_id=_CHOSEN_BY_THE_CLIENT))

    assert actor != _CHOSEN_BY_THE_CLIENT
    assert actor == ""


def test_an_anonymous_client_id_never_becomes_the_actor() -> None:
    """The fallback shape. `anon-<uuid>` names nobody and must not read as somebody."""
    actor = approval_actor(_message(sender_id="anon-2f1c9b0a4d7e"))

    assert actor == ""


# --------------------------------------------------------- the channels that do authenticate


def test_a_telegram_account_id_is_still_the_actor() -> None:
    """Telegram authenticates its own numeric id, so `sender_id` is honest there."""
    actor = approval_actor(
        _message(channel="telegram", sender_id="43110", authenticated_sender=None)
    )

    assert actor == "43110"


def test_a_telegram_username_is_still_dropped() -> None:
    """The channel appends a username for its own allowlist, and a username changes at will."""
    actor = approval_actor(_message(channel="telegram", sender_id="43110|opsuser"))

    assert actor == "43110"


def test_an_authenticated_sender_wins_over_a_sender_id() -> None:
    """A channel that offers both is telling us which one it authenticated."""
    actor = approval_actor(
        _message(channel="telegram", sender_id="43110", authenticated_sender="43999")
    )

    assert actor == "43999"


# --------------------------------------------------------- what an operator reads


def test_a_channel_that_authenticates_nobody_gets_a_refusal_that_names_the_inbox() -> None:
    """A refusal that said only "denied" would send an operator to file a bug.

    The WebUI has an answer surface that reads the verified identity (#27), so the refusal points
    at it.
    """
    from nanoinfra.command.approvals import unauthenticated_channel_refusal

    text = unauthenticated_channel_refusal("websocket")

    assert "websocket" in text
    assert "approvals" in text.lower()


def test_a_blank_authenticated_sender_is_not_an_identity() -> None:
    """A channel that sets the field to whitespace authenticated nobody, whatever it meant."""
    assert approval_actor(_message(authenticated_sender="   ")) == ""
