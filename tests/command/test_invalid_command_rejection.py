"""An invalid slash command is answered, not handed to the model.

Before this, `/nwe` fell through to the LLM, which then had to guess whether it was a typo, a new
feature, or prose. A plain rejection with a suggestion is better than a guess.
Ported from upstream f45436b6 (nanoinfraorg/nanoinfra#145).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanoinfra.bus.events import InboundMessage
from nanoinfra.command.router import CommandContext, CommandRouter


async def _noop(_ctx: CommandContext) -> None:
    return None


@pytest.fixture
def router() -> CommandRouter:
    router = CommandRouter()
    router.exact("/new", _noop)
    router.exact("/status", _noop)
    router.prefix("/pairing ", _noop)
    return router


def _ctx(raw: str) -> CommandContext:
    msg = InboundMessage(channel="websocket", chat_id="c1", sender_id="u1", content=raw)
    msg.metadata = {}
    # loop is required but the rejection path never touches it.
    return CommandContext(
        msg=msg, session=None, key="websocket:c1", raw=raw, loop=MagicMock()
    )


async def _answer(router: CommandRouter, raw: str) -> str:
    result = await router.dispatch(_ctx(raw))
    assert result is not None, f"expected {raw!r} to be answered"
    return str(result.content)


async def test_an_unknown_command_suggests_a_close_match(router: CommandRouter) -> None:
    content = await _answer(router, "/nwe")

    assert "/nwe" in content
    assert "/new" in content


async def test_an_unknown_command_with_no_close_match_points_at_help(
    router: CommandRouter,
) -> None:
    content = await _answer(router, "/zzzzzz")

    assert "/zzzzzz" in content
    assert "/help" in content


async def test_a_known_command_given_arguments_it_does_not_take_says_so(
    router: CommandRouter,
) -> None:
    """`/new tomorrow` is a different mistake from `/nwe`, and gets a different answer."""
    content = await _answer(router, "/new tomorrow")

    assert "does not accept arguments" in content
    assert "/new" in content


async def test_a_prefix_command_with_bad_arguments_points_at_help(
    router: CommandRouter,
) -> None:
    content = await _answer(router, "/pairing")

    assert "/pairing" in content
    assert "/help" in content


async def test_a_valid_command_is_untouched(router: CommandRouter) -> None:
    assert await router.dispatch(_ctx("/new")) is None
    assert await router.dispatch(_ctx("/pairing list")) is None


async def test_plain_prose_is_left_to_the_model(router: CommandRouter) -> None:
    """A rejection here would swallow an ordinary message."""
    assert await router.dispatch(_ctx("what is the status")) is None
    assert await router.dispatch(_ctx("")) is None


async def test_the_answer_renders_as_text(router: CommandRouter) -> None:
    """Command names contain slashes and underscores; markdown would mangle them."""
    result = await router.dispatch(_ctx("/zzzzzz"))

    assert result is not None
    assert result.metadata.get("render_as") == "text"


async def test_the_answer_goes_back_to_the_same_chat(router: CommandRouter) -> None:
    result = await router.dispatch(_ctx("/zzzzzz"))

    assert result is not None
    assert result.channel == "websocket"
    assert result.chat_id == "c1"
