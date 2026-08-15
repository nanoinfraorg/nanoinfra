# tests/agent/test_tool_call_signature_scrub.py
"""A changed tool call must carry no stale signature -- nanoinfraorg/nanoinfra#53.

#48 settled the rule for a signed field: a scrub that changes signed text drops the signature,
because a provider needs a signature that matches the text it signed, and a mismatched pair is
worse than no block at all. `_unsigned_thinking_block` does that for a thinking block.

`_redact_tool_calls` did the opposite by construction. It copied every key of the call and then
replaced the arguments, so a sibling key survived a change to the text it describes.

No provider in this tree emits a signed tool call today. Anthropic signs a thinking block and
Bedrock signs a reasoning block, and both take the #48 path. Gemini's function call carries a
`thought_signature`, and an OpenAI-compatible surface passes an unknown key through, so the key
arrives the day somebody adds that provider or a provider adds the field.

So this file pins a rule rather than closing a live leak. The rule matters more here than in #48:
a tool call replays on the very next iteration of the same turn, so an unnecessary drop would
break a live conversation rather than an old transcript.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nanoinfra.agent.redaction import (
    REASONING_SCRUB_MARKER_KEY,
    SecretSentinel,
    redact_message,
    scrub_one_text,
    withheld_message,
)

SECRET_VALUE = "hunter2-correct-horse-battery"
SECRET_NAME = "db1-password"

_SENTINELS = [SecretSentinel(name=SECRET_NAME, value=SECRET_VALUE)]


def _scrub(text: str, capability_class: str | None) -> str:
    return scrub_one_text(text, capability_class, _SENTINELS)


def _call(arguments: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """One tool call, with any sibling key a provider may add beside the function."""
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": "execute_on_server", "arguments": json.dumps(arguments)},
        **extra,
    }


def _redacted_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run the message path, which is the only caller of the tool-call redaction."""
    message = {"role": "assistant", "content": "running it", "tool_calls": calls}
    redacted = redact_message(message, _scrub)
    return list(redacted["tool_calls"])


# ------------------------------------------------------------- a changed call loses the seal


@pytest.mark.parametrize("signature_key", ["thought_signature", "signature"])
def test_a_changed_argument_drops_the_signature(signature_key: str) -> None:
    """The #48 rule, on the field #48 did not cover.

    Two key names carry a signature in practice. `thought_signature` is the Gemini function
    call, and `signature` is the name every other signed field in this repository uses.
    """
    calls = _redacted_calls([
        _call({"command": f"mysql -p{SECRET_VALUE}"}, **{signature_key: "the-provider-bytes"})
    ])

    assert signature_key not in calls[0]
    assert SECRET_VALUE not in json.dumps(calls[0])


def test_a_changed_call_says_why_it_lost_the_signature() -> None:
    """A reader months later must tell a dropped signature from a call that never had one."""
    calls = _redacted_calls([
        _call({"command": f"mysql -p{SECRET_VALUE}"}, thought_signature="the-provider-bytes")
    ])

    marker = calls[0][REASONING_SCRUB_MARKER_KEY]

    assert "signature" in marker
    assert SECRET_VALUE not in marker


def test_the_arguments_stay_parseable_json_after_the_drop() -> None:
    """Session history replays to a provider, so the arguments must still parse."""
    calls = _redacted_calls([
        _call({"command": f"mysql -p{SECRET_VALUE}"}, thought_signature="the-provider-bytes")
    ])

    arguments = json.loads(calls[0]["function"]["arguments"])

    assert SECRET_VALUE not in arguments["command"]
    assert "mysql" in arguments["command"]


def test_every_other_key_of_a_changed_call_survives() -> None:
    """Only the signature goes. An id or a name a provider needs must not go with it."""
    calls = _redacted_calls([
        _call(
            {"command": f"mysql -p{SECRET_VALUE}"},
            thought_signature="the-provider-bytes",
            index=2,
            extra_provider_field="a value the provider needs",
        )
    ])

    assert calls[0]["id"] == "call_1"
    assert calls[0]["type"] == "function"
    assert calls[0]["index"] == 2
    assert calls[0]["extra_provider_field"] == "a value the provider needs"
    assert calls[0]["function"]["name"] == "execute_on_server"


# ------------------------------------------------------------- an untouched call is untouched


def test_a_call_with_no_secret_keeps_its_signature() -> None:
    """This half matters more than in #48.

    A tool call replays on the very next iteration of the same turn, so an unnecessary drop
    breaks a live conversation rather than an old transcript.
    """
    calls = _redacted_calls([
        _call({"command": "uptime"}, thought_signature="the-provider-bytes")
    ])

    assert calls[0]["thought_signature"] == "the-provider-bytes"
    assert REASONING_SCRUB_MARKER_KEY not in calls[0]


def test_a_call_with_no_secret_is_field_for_field_identical() -> None:
    original = _call({"command": "uptime"}, thought_signature="the-provider-bytes", index=0)

    calls = _redacted_calls([dict(original)])

    assert calls[0] == original


def test_one_changed_call_never_unsigns_another() -> None:
    """Two calls in one turn, and only the one the scrub touched loses its seal."""
    calls = _redacted_calls([
        _call({"command": "uptime"}, thought_signature="the-clean-one"),
        _call({"command": f"mysql -p{SECRET_VALUE}"}, thought_signature="the-changed-one"),
    ])

    assert calls[0]["thought_signature"] == "the-clean-one"
    assert "thought_signature" not in calls[1]


def test_a_call_with_no_signature_gains_no_marker() -> None:
    """A marker on a call that never carried a signature would say nothing true."""
    calls = _redacted_calls([_call({"command": f"mysql -p{SECRET_VALUE}"})])

    assert REASONING_SCRUB_MARKER_KEY not in calls[0]
    assert SECRET_VALUE not in json.dumps(calls[0])


def test_a_signature_inside_the_function_also_goes() -> None:
    """A provider may map its own shape with the signature under the function.

    The rule follows the arguments, and the arguments live under the function, so both levels
    are checked rather than only the one this repository happens to build.
    """
    call = _call({"command": f"mysql -p{SECRET_VALUE}"})
    call["function"]["thought_signature"] = "the-provider-bytes"

    calls = _redacted_calls([call])

    assert "thought_signature" not in calls[0]["function"]
    assert REASONING_SCRUB_MARKER_KEY in calls[0]


# ------------------------------------------------------------- the fail-closed path


def test_a_withheld_call_drops_its_signature() -> None:
    """The arguments are replaced whole here, so a signature over them is stale for certain.

    A withheld thinking block follows the same rule under #48, and a withheld call had no
    equivalent.
    """
    message = {
        "role": "assistant",
        "content": "running it",
        "tool_calls": [
            _call({"command": f"mysql -p{SECRET_VALUE}"}, thought_signature="the-provider-bytes")
        ],
    }

    withheld = withheld_message(message, "no executor answered")
    call = withheld["tool_calls"][0]

    assert "thought_signature" not in call
    assert "no longer matches" in call[REASONING_SCRUB_MARKER_KEY]
    assert SECRET_VALUE not in json.dumps(call)


def test_a_withheld_call_keeps_parseable_arguments() -> None:
    """A bare marker would leave a call whose arguments are not JSON."""
    message = {
        "role": "assistant",
        "content": "running it",
        "tool_calls": [_call({"command": f"mysql -p{SECRET_VALUE}"})],
    }

    withheld = withheld_message(message, "no executor answered")

    assert json.loads(withheld["tool_calls"][0]["function"]["arguments"])


def test_a_withheld_call_with_no_arguments_gains_no_marker() -> None:
    """Nothing was replaced, so nothing became stale."""
    message = {
        "role": "assistant",
        "content": "running it",
        "tool_calls": [{"id": "call_1", "type": "function", "thought_signature": "kept"}],
    }

    withheld = withheld_message(message, "no executor answered")
    call = withheld["tool_calls"][0]

    assert call["thought_signature"] == "kept"
    assert REASONING_SCRUB_MARKER_KEY not in call
