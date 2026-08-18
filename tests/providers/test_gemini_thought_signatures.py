"""Switching models mid-conversation must not make Gemini reject the history.

Gemini requires the first tool call of each function-call step to carry a thought signature. Tool
calls produced by another provider carry none, so a mid-conversation switch to Gemini produced a
request Gemini refused. Dropping the history would cost the model its tool context, so Google's
documented migration value is used instead.
Ported from upstream 0c684c5a (nanoinfraorg/nanoinfra#145).
"""

from __future__ import annotations

from typing import Any

from nanoinfra.providers.openai_compat_provider import (
    _GEMINI_SKIP_THOUGHT_SIGNATURE,
    OpenAICompatProvider,
)
from nanoinfra.providers.registry import find_by_name


def _provider() -> OpenAICompatProvider:
    spec = find_by_name("gemini")
    assert spec is not None
    return OpenAICompatProvider(api_key="k", default_model="gemini-3-pro", spec=spec)


def _call(name: str, signature: str | None = None) -> dict[str, Any]:
    call: dict[str, Any] = {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }
    if signature is not None:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def _signature_of(call: dict[str, Any]) -> str | None:
    return call.get("extra_content", {}).get("google", {}).get("thought_signature")


def test_an_unsigned_imported_step_gets_the_migration_value() -> None:
    provider = _provider()
    messages = [{"role": "assistant", "tool_calls": [_call("read_file")]}]

    result = provider._ensure_gemini_thought_signatures(messages)

    assert _signature_of(result[0]["tool_calls"][0]) == _GEMINI_SKIP_THOUGHT_SIGNATURE


def test_an_existing_signature_is_left_alone() -> None:
    provider = _provider()
    messages = [{"role": "assistant", "tool_calls": [_call("read_file", "real-signature")]}]

    result = provider._ensure_gemini_thought_signatures(messages)

    assert _signature_of(result[0]["tool_calls"][0]) == "real-signature"


def test_native_parallel_calls_keep_their_order_and_stay_unsigned() -> None:
    """Gemini signs only the first call of a step; later ones must not be touched."""
    provider = _provider()
    messages = [
        {
            "role": "assistant",
            "tool_calls": [_call("first", "real-signature"), _call("second"), _call("third")],
        }
    ]

    result = provider._ensure_gemini_thought_signatures(messages)
    calls = result[0]["tool_calls"]

    assert [c["function"]["name"] for c in calls] == ["first", "second", "third"]
    assert _signature_of(calls[0]) == "real-signature"
    assert _signature_of(calls[1]) is None
    assert _signature_of(calls[2]) is None


def test_only_the_first_call_of_an_unsigned_step_is_given_the_value() -> None:
    provider = _provider()
    messages = [{"role": "assistant", "tool_calls": [_call("a"), _call("b")]}]

    calls = provider._ensure_gemini_thought_signatures(messages)[0]["tool_calls"]

    assert _signature_of(calls[0]) == _GEMINI_SKIP_THOUGHT_SIGNATURE
    assert _signature_of(calls[1]) is None


def test_other_roles_and_plain_messages_pass_through() -> None:
    provider = _provider()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "tool_call_id": "call_x", "content": "result"},
    ]

    assert provider._ensure_gemini_thought_signatures(messages) == messages


def test_a_step_whose_calls_are_all_malformed_keeps_only_its_prose() -> None:
    provider = _provider()
    messages = [
        {"role": "assistant", "content": "I will look", "tool_calls": ["not-a-dict"]},
        {"role": "assistant", "tool_calls": ["not-a-dict"]},
    ]

    result = provider._ensure_gemini_thought_signatures(messages)

    assert len(result) == 1
    assert result[0]["content"] == "I will look"
    assert "tool_calls" not in result[0]


def test_an_unrelated_extra_content_key_survives() -> None:
    """A sibling field under extra_content must not be dropped by the rewrite."""
    provider = _provider()
    call = _call("read_file")
    call["extra_content"] = {"google": {"other": 1}, "vendor": {"keep": True}}
    messages = [{"role": "assistant", "tool_calls": [call]}]

    rewritten = provider._ensure_gemini_thought_signatures(messages)[0]["tool_calls"][0]

    assert rewritten["extra_content"]["vendor"] == {"keep": True}
    assert rewritten["extra_content"]["google"]["other"] == 1
    assert (
        rewritten["extra_content"]["google"]["thought_signature"]
        == _GEMINI_SKIP_THOUGHT_SIGNATURE
    )


def _step_with_result() -> list[dict[str, Any]]:
    """An assistant tool step plus its result, so sanitize keeps it as a complete pair."""
    call = _call("read_file")
    return [
        {"role": "assistant", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": "ok"},
    ]


def test_sanitize_applies_the_rewrite_for_gemini() -> None:
    """Through the real seam, not just the helper."""
    sanitized = _provider()._sanitize_messages(_step_with_result())

    assert _signature_of(sanitized[0]["tool_calls"][0]) == _GEMINI_SKIP_THOUGHT_SIGNATURE


def test_sanitize_leaves_another_provider_alone() -> None:
    """The guard is on the spec: an OpenAI conversation must not grow a Google field."""
    spec = find_by_name("openai")
    assert spec is not None
    provider = OpenAICompatProvider(api_key="k", default_model="gpt-4o", spec=spec)

    sanitized = provider._sanitize_messages(_step_with_result())

    assert _signature_of(sanitized[0]["tool_calls"][0]) is None
