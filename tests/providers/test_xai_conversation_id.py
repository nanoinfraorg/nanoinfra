"""One chat is one conversation to xAI, so its prefix cache can be hit.

xAI caches the longest matching prefix automatically, and its documentation is explicit that
`x-grok-conv-id` is what routes a request to the server holding that prefix. This code generated a
fresh `uuid4()` per request, so every call announced itself as a new conversation, landed on a
different server in their fleet and found a cold cache.

Measured on a live deployment before the fix: `cached_tokens` was **128 on every call** across four
calls sharing an identical 17,300-token prefix -- including two nine seconds apart, and including
the very first call, which had nothing to hit. A constant that appears on a cold call is a floor the
provider always reports, not a measurement of our prefix.
"""

from __future__ import annotations

import uuid

from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.providers.xai_grok_provider import _build_headers, _conversation_id


def _turn(session_key: str) -> object:
    return request_context(
        RequestContext(channel="websocket", chat_id="c", session_key=session_key)
    )


def test_two_turns_of_one_chat_are_one_conversation() -> None:
    """The whole point: the second turn has to reach the server holding the first turn's prefix."""
    with _turn("websocket:abc"):
        first = _conversation_id()
    with _turn("websocket:abc"):
        second = _conversation_id()

    assert first == second


def test_two_chats_are_two_conversations() -> None:
    """Otherwise two chats fight over one server's prefix, and each eviction costs the other."""
    with _turn("websocket:abc"):
        first = _conversation_id()
    with _turn("websocket:def"):
        second = _conversation_id()

    assert first != second


def test_the_id_is_a_uuid_and_not_the_session_key() -> None:
    """A chat id must not travel in a request header."""
    with _turn("websocket:abc"):
        value = _conversation_id()

    assert uuid.UUID(value)
    assert "abc" not in value


def test_an_unbound_call_is_stable_within_the_process() -> None:
    """A background call or a probe still shares a prefix with the next one, so a fresh id per
    request is the bug rather than the safe default."""
    assert _conversation_id() == _conversation_id()


def test_the_header_carries_it_and_the_request_id_stays_unique() -> None:
    """`x-grok-conv-id` is the routing key and `x-grok-req-id` identifies one attempt: making both
    per-request is what defeated the cache."""
    with _turn("websocket:abc"):
        first = _build_headers("token", "grok-4.5")
        second = _build_headers("token", "grok-4.5")

    assert first["x-grok-conv-id"] == second["x-grok-conv-id"]
    assert first["x-grok-session-id"] == second["x-grok-session-id"]
    assert first["x-grok-req-id"] != second["x-grok-req-id"]


def test_the_token_and_model_still_reach_the_headers() -> None:
    with _turn("websocket:abc"):
        headers = _build_headers("secret-token", "grok-4.5")

    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["x-grok-model-override"] == "grok-4.5"
