"""One chat is one cache bucket, for a provider that routes on a key.

Several providers cache the longest matching prompt prefix automatically and then need to be told
*which* cache a request belongs to. xAI's `x-grok-conv-id` and OpenAI's `prompt_cache_key` are the
same idea, and the xAI path proved what happens without it: 128 cached tokens on every call across
four calls sharing a byte-identical 17,300-token prefix, and 50% once the key was stable.

OpenAI's default routing hashes the **first 256 tokens**, which in this codebase are byte-identical
across every chat — the Runtime and Bootstrap sections come first and never vary. So without a key
every session of every deployment lands in one bucket and they evict each other.
"""

from __future__ import annotations

import uuid
from typing import Any

from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.providers.base import prefix_cache_key
from nanoinfra.providers.registry import find_by_name

NAMESPACE = uuid.UUID("2b7d5c90-8e14-5a6f-b3d2-19c4f7a08e51")
OTHER_NAMESPACE = uuid.UUID("6f9b1f4e-0d3a-5c1b-9f2e-7a4c8d1b6e30")


def _turn(session_key: str) -> Any:
    return request_context(
        RequestContext(channel="websocket", chat_id="c", session_key=session_key)
    )


# --- the key ------------------------------------------------------------------------------


def test_two_turns_of_one_chat_share_a_key() -> None:
    with _turn("websocket:abc"):
        first = prefix_cache_key(NAMESPACE)
    with _turn("websocket:abc"):
        second = prefix_cache_key(NAMESPACE)

    assert first == second


def test_two_chats_do_not() -> None:
    with _turn("websocket:abc"):
        first = prefix_cache_key(NAMESPACE)
    with _turn("websocket:def"):
        second = prefix_cache_key(NAMESPACE)

    assert first != second


def test_the_chat_id_does_not_travel_to_the_provider() -> None:
    with _turn("websocket:abc"):
        value = prefix_cache_key(NAMESPACE)

    assert uuid.UUID(value)
    assert "abc" not in value


def test_the_same_chat_presents_a_different_key_to_each_provider() -> None:
    """One opaque key reused across providers would let two of them correlate a conversation."""
    with _turn("websocket:abc"):
        assert prefix_cache_key(NAMESPACE) != prefix_cache_key(OTHER_NAMESPACE)


def test_an_unbound_call_is_stable_within_the_process() -> None:
    """A probe or a title generation still shares a prefix with the next one."""
    assert prefix_cache_key(NAMESPACE) == prefix_cache_key(NAMESPACE)


# --- who gets it --------------------------------------------------------------------------


def test_openai_asks_for_the_routing_hint() -> None:
    spec: Any = find_by_name("openai")

    assert spec is not None
    assert spec.supports_prompt_cache_key is True


def test_a_provider_that_merely_speaks_the_openai_protocol_does_not() -> None:
    """`prompt_cache_key` is a body field on a path 42 providers share, and one that rejects an
    unknown field answers 400 rather than ignoring it. So it is opt-in, not protocol-wide."""
    for name in ("ollama", "moonshot", "deepseek", "groq", "vllm", "lm_studio"):
        spec: Any = find_by_name(name)
        assert spec is not None, name
        assert spec.supports_prompt_cache_key is False, name


def test_nothing_is_sent_by_default() -> None:
    """A new provider spec must not inherit a field its endpoint may refuse."""
    from nanoinfra.providers.registry import ProviderSpec

    probe = ProviderSpec(name="probe", keywords=("probe",), env_key="PROBE_API_KEY")

    assert probe.supports_prompt_cache_key is False
