# tests/providers/test_responses_payload_redaction.py
"""nanoinfraorg/nanoinfra#54: the named text carriers of a Responses payload.

#52 left the payload of a provider state byte-exact, and it gave an honest reason: only the
provider that issued a field knows whether that field must stay byte-exact. #54 answers that
reason rather than working around it. This provider names its own text carriers, and everything
it does not name still reaches the file unchanged.

The set is named on purpose, and a deep walk over every string is refused. A named set is
reviewable. A deep walk would rewrite an item id, a server-side call id, or a base64 image, and
a wrong edit there breaks a replay the operator cannot recover.

The carriers, with the builder that proves each one:

- ``function_call.arguments`` -- ``converters.py`` builds it from the tool arguments.
- ``function_call_output.output``, as a string and as a content-part list. ``convert_tool_output``
  returns both shapes.
- ``message.content[].text`` for a text part. ``convert_messages`` builds the assistant item with
  ``output_text``, and ``convert_user_message`` builds a user item with ``input_text``.
- ``reasoning``, in three places. See the reasoning tests below for the evidence.

**A scrubbed item gains no marker key.** The message path marks a changed thinking block (#48),
and that answer is wrong here. These items are the request input of the next turn: they go
straight into ``body["input"]``. A key the API does not define is a rejected request, so the
placeholder inside the text is the only record a reader gets.
"""

from __future__ import annotations

import json
from typing import Any

from nanoinfra.providers.openai_responses.converters import convert_messages
from nanoinfra.providers.openai_responses.redaction import (
    RESPONSES_REASONING_SEAL_KEY,
    scrub_provider_state_payload,
    scrub_responses_payload,
)
from nanoinfra.providers.openai_responses.state import RESPONSES_STATE_KIND

SECRET_VALUE = "hunter2-correct-horse-battery"
PLACEHOLDER = "[redacted secret: prod-db-password]"
RESOLVED_COMMAND = f"mysql --host=db1 --user=app -p{SECRET_VALUE} -e 'select 1'"
SCRUBBED_COMMAND = f"mysql --host=db1 --user=app -p{PLACEHOLDER} -e 'select 1'"


def _scrub(text: str, capability_class: str | None) -> str:
    """A stand-in for the executor. The real sentinels live in the other process (#41)."""
    _ = capability_class
    return text.replace(SECRET_VALUE, PLACEHOLDER)


def _payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(items), "context_tokens": 4096}


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["items"]


# -- function_call.arguments -------------------------------------------------


def test_the_arguments_of_a_function_call_scrub() -> None:
    payload = _payload(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "execute_on_server",
            "arguments": json.dumps({"server_id_or_name": "db1", "command": RESOLVED_COMMAND}),
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert SECRET_VALUE not in scrubbed["arguments"]
    assert PLACEHOLDER in scrubbed["arguments"]


def test_a_function_call_keeps_every_identifier_it_carries() -> None:
    """An id, a call id, and a name are the provider's own handles, so they stay byte-exact."""
    payload = _payload(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "execute_on_server",
            "arguments": json.dumps({"command": RESOLVED_COMMAND}),
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["id"] == "fc_1"
    assert scrubbed["call_id"] == "call_1"
    assert scrubbed["name"] == "execute_on_server"
    assert scrubbed["type"] == "function_call"


# -- function_call_output.output ---------------------------------------------


def test_a_string_tool_output_scrubs() -> None:
    payload = _payload(
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": f"$ {RESOLVED_COMMAND}\n1 row in set",
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["output"] == f"$ {SCRUBBED_COMMAND}\n1 row in set"


def test_a_content_part_list_tool_output_scrubs() -> None:
    """``convert_tool_output`` returns a part list for a multimodal tool result."""
    payload = _payload(
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": [
                {"type": "input_text", "text": f"password={SECRET_VALUE}"},
                {"type": "input_text", "text": "second line"},
            ],
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["output"][0]["text"] == f"password={PLACEHOLDER}"
    assert scrubbed["output"][1]["text"] == "second line"


def test_an_image_part_of_a_tool_output_stays_byte_exact() -> None:
    """A base64 image is not text. A scrub of it would corrupt the image and protect nothing."""
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    payload = _payload(
        {
            "type": "function_call_output",
            "call_id": "call_3",
            "output": [
                {"type": "input_text", "text": f"read with {SECRET_VALUE}"},
                {"type": "input_image", "image_url": image, "detail": "auto"},
                {"type": "input_file", "file_id": "file_abc", "filename": "notes.pdf"},
            ],
        }
    )

    parts = _items(scrub_responses_payload(payload, _scrub))[0]["output"]

    assert parts[0]["text"] == f"read with {PLACEHOLDER}"
    assert parts[1] == {"type": "input_image", "image_url": image, "detail": "auto"}
    assert parts[2] == {"type": "input_file", "file_id": "file_abc", "filename": "notes.pdf"}


# -- message.content[].text --------------------------------------------------


def test_an_output_text_part_of_an_assistant_message_scrubs() -> None:
    payload = _payload(
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "id": "msg_1",
            "content": [{"type": "output_text", "text": f"I will run {RESOLVED_COMMAND}"}],
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["content"][0]["text"] == f"I will run {SCRUBBED_COMMAND}"
    assert scrubbed["id"] == "msg_1"
    assert scrubbed["status"] == "completed"


def test_an_input_text_part_of_a_user_item_scrubs() -> None:
    """``convert_user_message`` builds an item with a role and no ``type`` at all.

    That shape is a message as much as the assistant item is, and a user can paste a credential
    into chat. The acceptance clause names the whole file, so this item scrubs too.
    """
    payload = _payload(
        {"role": "user", "content": [{"type": "input_text", "text": f"use {SECRET_VALUE}"}]}
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["content"][0]["text"] == f"use {PLACEHOLDER}"
    assert scrubbed["role"] == "user"
    assert "type" not in scrubbed


def test_a_refusal_part_of_an_assistant_message_scrubs() -> None:
    """A refusal names its text under a key of its own, and ``parsing.py`` reads it as content."""
    payload = _payload(
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "id": "msg_2",
            "content": [{"type": "refusal", "refusal": f"I will not run {RESOLVED_COMMAND}"}],
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["content"][0]["refusal"] == f"I will not run {SCRUBBED_COMMAND}"


def test_an_input_image_part_of_a_user_item_stays_byte_exact() -> None:
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    payload = _payload(
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"look at this after {SECRET_VALUE}"},
                {"type": "input_image", "image_url": image, "detail": "auto"},
            ],
        }
    )

    parts = _items(scrub_responses_payload(payload, _scrub))[0]["content"]

    assert parts[0]["text"] == f"look at this after {PLACEHOLDER}"
    assert parts[1]["image_url"] == image


# -- the reasoning item ------------------------------------------------------


def test_a_reasoning_item_we_build_holds_readable_text_and_scrubs() -> None:
    """The evidence for item 3 of #54, first shape.

    ``convert_messages`` with ``preserve_reasoning=True`` copies ``reasoning_content`` into a
    reasoning item as an ``output_text`` part. That is readable plaintext, and the DeepSeek spec
    turns that flag on, so the shape is live rather than hypothetical.
    """
    _, built = convert_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": f"the plan is {RESOLVED_COMMAND}",
            }
        ],
        preserve_reasoning=True,
    )
    assert built[0]["type"] == "reasoning"
    assert SECRET_VALUE in built[0]["content"][0]["text"]

    scrubbed = _items(scrub_responses_payload(_payload(*built), _scrub))[0]

    assert scrubbed["content"][0]["text"] == f"the plan is {SCRUBBED_COMMAND}"


def test_a_reasoning_summary_from_the_server_scrubs() -> None:
    """The evidence for item 3 of #54, second shape.

    A server reasoning item carries ``summary[].text``, and ``parsing.py`` reads that field back
    as the ``reasoning_content`` of the turn. So the field is readable text by the provider's own
    definition, and it reaches the payload through the output items of the response.
    """
    payload = _payload(
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "gAAAAABo8Zk3-opaque",
            "summary": [{"type": "summary_text", "text": f"I ran {RESOLVED_COMMAND}"}],
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["summary"][0]["text"] == f"I ran {SCRUBBED_COMMAND}"
    assert scrubbed["id"] == "rs_1"


def test_a_reasoning_item_with_string_content_scrubs() -> None:
    """``_extract_reasoning_summary_from_output`` reads a plain string content, so it exists."""
    payload = _payload({"type": "reasoning", "id": "rs_2", "content": f"plan: {RESOLVED_COMMAND}"})

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert scrubbed["content"] == f"plan: {SCRUBBED_COMMAND}"


def test_a_changed_reasoning_item_drops_its_seal() -> None:
    """The #48 rule, on the field #54 found.

    ``encrypted_content`` is the same reasoning in sealed form. Keeping it beside a scrubbed
    summary would replay to the provider the very text the scrub removed, so the scrub would be
    cosmetic. The seal goes, and the item stays so the function call that follows it keeps its
    required predecessor.
    """
    payload = _payload(
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "gAAAAABo8Zk3-opaque",
            "summary": [{"type": "summary_text", "text": f"I ran {RESOLVED_COMMAND}"}],
        }
    )

    scrubbed = _items(scrub_responses_payload(payload, _scrub))[0]

    assert RESPONSES_REASONING_SEAL_KEY not in scrubbed
    assert scrubbed["type"] == "reasoning"
    assert scrubbed["id"] == "rs_1"


def test_an_unchanged_reasoning_item_keeps_its_seal() -> None:
    """A turn that held no secret replays exactly as it does today."""
    item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "gAAAAABo8Zk3-opaque",
        "summary": [{"type": "summary_text", "text": "I read the row count"}],
    }

    scrubbed = _items(scrub_responses_payload(_payload(item), _scrub))[0]

    assert scrubbed == item


def test_a_scrubbed_item_carries_no_marker_key() -> None:
    """These items are request input, so a key the API does not define is a rejected request."""
    payload = _payload(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "execute_on_server",
            "arguments": json.dumps({"command": RESOLVED_COMMAND}),
        },
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "gAAAAABo8Zk3-opaque",
            "summary": [{"type": "summary_text", "text": f"I ran {RESOLVED_COMMAND}"}],
        },
    )

    scrubbed = scrub_responses_payload(payload, _scrub)

    assert "nanoinfra_scrubbed" not in json.dumps(scrubbed)
    assert set(_items(scrubbed)[0]) == {"type", "id", "call_id", "name", "arguments"}
    assert set(_items(scrubbed)[1]) == {"type", "id", "summary"}


# -- what stays byte-exact ---------------------------------------------------


def test_an_item_type_this_module_does_not_name_stays_byte_exact() -> None:
    """A deep walk is what #52 refused, and that reason still holds for what is not named."""
    item = {
        "type": "web_search_call",
        "id": "ws_1",
        "status": "completed",
        "action": {"type": "search", "query": "how to restart nginx"},
    }

    scrubbed = scrub_responses_payload(_payload(item), _scrub)

    assert _items(scrubbed)[0] == item


def test_a_payload_with_no_carrier_returns_the_same_object() -> None:
    """Byte identity is object identity here, so no rebuild can change one byte."""
    payload = _payload({"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"})

    assert scrub_responses_payload(payload, _scrub) is payload


def test_a_payload_with_no_secret_serialises_identically() -> None:
    """Item 6 of #54, at the bytes."""
    payload = _payload(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "execute_on_server",
            "arguments": json.dumps({"command": "systemctl restart nginx"}),
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": "ok"}],
        },
    )
    before = json.dumps(payload, ensure_ascii=False)

    after = json.dumps(scrub_responses_payload(payload, _scrub), ensure_ascii=False)

    assert after == before


def test_a_payload_with_no_items_key_stays_byte_exact() -> None:
    payload = {"conversation_id": "conv_1", "context_tokens": 10}

    assert scrub_responses_payload(payload, _scrub) is payload


def test_the_input_payload_is_never_mutated() -> None:
    """The live turn holds these same items, so a scrub copies rather than edits."""
    payload = _payload(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "execute_on_server",
            "arguments": json.dumps({"command": RESOLVED_COMMAND}),
        }
    )
    before = json.dumps(payload, ensure_ascii=False)

    scrub_responses_payload(payload, _scrub)

    assert json.dumps(payload, ensure_ascii=False) == before


def test_the_context_tokens_of_a_payload_survive() -> None:
    payload = _payload(
        {"type": "function_call_output", "call_id": "c1", "output": RESOLVED_COMMAND}
    )

    assert scrub_responses_payload(payload, _scrub)["context_tokens"] == 4096


# -- the dispatch on the state kind -----------------------------------------


def test_a_responses_state_takes_the_scrub() -> None:
    payload = _payload(
        {"type": "function_call_output", "call_id": "c1", "output": RESOLVED_COMMAND}
    )

    scrubbed = scrub_provider_state_payload(RESPONSES_STATE_KIND, payload, _scrub)

    assert _items(scrubbed)[0]["output"] == SCRUBBED_COMMAND


def test_a_state_of_another_kind_stays_byte_exact() -> None:
    """Only the provider that issued a payload names its carriers, so another kind stays whole."""
    payload = _payload(
        {"type": "function_call_output", "call_id": "c1", "output": RESOLVED_COMMAND}
    )

    assert scrub_provider_state_payload("anthropic_prompt_cache", payload, _scrub) is payload


def test_the_codex_state_shares_this_call_site() -> None:
    """``openai_codex_provider.py`` builds its state with ``build_responses_state`` as well.

    So its ``kind`` is this kind, and one call site covers both providers.
    """
    from nanoinfra.providers.openai_responses.state import build_responses_state

    state = build_responses_state(
        provider="openai_codex:https://chatgpt.com/backend-api/codex",
        model="gpt-5.6-codex",
        input_items=[
            {"type": "function_call_output", "call_id": "c1", "output": RESOLVED_COMMAND}
        ],
        output_items=[],
    )

    assert state.kind == RESPONSES_STATE_KIND
    scrubbed = scrub_provider_state_payload(state.kind, state.payload, _scrub)
    assert _items(scrubbed)[0]["output"] == SCRUBBED_COMMAND
