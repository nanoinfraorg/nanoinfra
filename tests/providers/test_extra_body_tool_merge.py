"""Configuring a provider-side tool must not delete our own tools from the request.

`tools` sent inside `extra_body` is *replaced* by the SDK rather than merged, so configuring an
OpenRouter server tool used to strip every function nanoinfra generated from its tool registry --
the agent silently lost read_file, exec, and the rest for that provider.
Ported from upstream 57d81bc1 (nanoinfraorg/nanoinfra#145).
"""

from __future__ import annotations

from typing import Any

from nanoinfra.providers.openai_compat_provider import _merge_chat_extra_body

_OURS: list[dict[str, Any]] = [{"type": "function", "function": {"name": "read_file"}}]
_THEIRS: list[dict[str, Any]] = [{"type": "web", "id": "openrouter/web"}]


def test_configured_tools_are_appended_not_substituted() -> None:
    merged = _merge_chat_extra_body({"tools": list(_OURS)}, {"tools": list(_THEIRS)})

    assert merged["tools"] == [*_OURS, *_THEIRS]
    # And they stay at the top level, where the API reads them.
    assert "tools" not in merged.get("extra_body", {})


def test_ordinary_extra_body_fields_still_deep_merge() -> None:
    kwargs = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    extra = {"chat_template_kwargs": {"top_k": 5}, "repetition_penalty": 1.1}

    merged = _merge_chat_extra_body(kwargs, extra)

    assert merged["extra_body"]["chat_template_kwargs"] == {
        "enable_thinking": False,
        "top_k": 5,
    }
    assert merged["extra_body"]["repetition_penalty"] == 1.1


def test_tools_are_not_left_inside_extra_body() -> None:
    """The whole bug: a configured tool list arriving in extra_body."""
    merged = _merge_chat_extra_body({"tools": list(_OURS)}, {"tools": list(_THEIRS), "seed": 1})

    assert merged["extra_body"] == {"seed": 1}
    assert len(merged["tools"]) == 2


def test_configured_tools_with_no_local_tools_are_used_as_is() -> None:
    merged = _merge_chat_extra_body({}, {"tools": list(_THEIRS)})

    assert merged["tools"] == _THEIRS


def test_a_non_list_configured_tools_value_replaces_rather_than_concatenates() -> None:
    """Nothing sensible to append to; pass the operator's value through."""
    merged = _merge_chat_extra_body({"tools": list(_OURS)}, {"tools": "auto"})

    assert merged["tools"] == "auto"


def test_no_configured_tools_leaves_ours_untouched() -> None:
    merged = _merge_chat_extra_body({"tools": list(_OURS)}, {"seed": 1})

    assert merged["tools"] == _OURS
    assert merged["extra_body"] == {"seed": 1}


def test_the_input_kwargs_are_not_mutated() -> None:
    kwargs: dict[str, Any] = {"tools": list(_OURS)}

    _merge_chat_extra_body(kwargs, {"tools": list(_THEIRS)})

    assert kwargs["tools"] == _OURS
