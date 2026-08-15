"""The text carriers of a Responses payload -- nanoinfraorg/nanoinfra#54.

#52 scrubbed the half of a provider state this repository builds, and it left the payload
byte-exact with an honest reason: only the provider that issued a field knows whether that field
must stay byte-exact. It then claimed that the Responses payload "is a list of server-side
identifiers rather than text". That claim is false. ``converters.py`` builds the items straight
out of message text, and ``pending_messages`` is empty on that path, so #52 covered none of it.

This module answers #52's reason rather than working around it. The provider names its own text
carriers here, beside the converters that build them, and everything it does not name still
reaches the file byte-exact.

**The set is named, and a deep walk is refused.** A named set is reviewable: a reader can check
each entry against the builder that produces it. A deep walk over every string would rewrite an
item id, a server-side call id, or a base64 image, and a wrong edit there breaks a replay the
operator cannot recover. That is the reason #52 gave for touching nothing, and it still holds for
every field this module does not name.

The carriers, with the evidence for each:

- ``function_call.arguments``. ``convert_messages`` writes the tool arguments into it.
- ``function_call_output.output``, as a string and as a content-part list. ``convert_tool_output``
  returns a string for an ordinary tool result and a part list for a multimodal one.
- ``message.content[].text`` for a text part. ``convert_messages`` builds the assistant item with
  an ``output_text`` part. ``convert_user_message`` builds the user item with an ``input_text``
  part, and that item carries a ``role`` and no ``type`` at all, so the shape check reads both.
- ``message.content[].refusal`` for a refusal part. ``parse_response_output`` reads that key as
  content of the turn, and the output items of a response reach the payload, so a refusal that
  quoted the command it refused would persist it.
- the reasoning item, in three places. See ``_scrub_reasoning``.

**A reasoning item can hold readable text on this path, and here is the evidence.**
``convert_messages`` with ``preserve_reasoning`` copies ``reasoning_content`` into a reasoning
item as an ``output_text`` part, and the DeepSeek spec turns that flag on. A server reasoning
item carries ``summary[].text``, and ``parsing.py`` reads that field back as the
``reasoning_content`` of the turn, so the provider itself treats it as readable text. Both shapes
reach the payload: the first through the request input, the second through the response output.

**A changed reasoning item drops ``encrypted_content``.** That field is the same reasoning in
sealed form. Keeping it beside a scrubbed summary would replay to the provider the very text the
scrub removed, so the scrub would be cosmetic, and #48 already settled the shape of this rule for
a signature. The item itself stays, because the API requires a reasoning item to precede the
function call it belongs to, and removing the item would break the request rather than the seal.

**A scrubbed item gains no marker key, and that differs from #48 on purpose.** The message path
marks a changed thinking block so a reader months later knows why it is short. These items are
the request input of the next turn: ``prepare_responses_input`` copies them straight into
``body["input"]``. A key the API does not define is a rejected request, so the marker cannot ride
here. The placeholder inside the text is the record a reader gets instead, and it names the
secret that went.

**Every text scrubs value by value, and none drops whole.** #17 drops a ``credential.access``
result whole, and that needs the capability class of the tool that produced the result. A payload
item names a ``call_id`` and a tool name, and the class comes from the tool registry, which the
session store does not hold. So this module passes no class. The credential value still goes,
because a sentinel matches it either way, and the whole-drop rule still applies on the message
line of the same file, where the caller does know the tool.

**Nothing here holds a sentinel.** The caller passes a scrub function, and the executor owns the
values it removes (#41). This module imports no credential store and no socket client.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from nanoinfra.providers.openai_responses.state import RESPONSES_STATE_KIND

#: What one text costs to scrub: the text, plus the capability class of the tool that produced
#: it, or None when the caller knows none. The shape matches ``ScrubText`` in
#: ``nanoinfra/agent/redaction.py``, and it is spelled again here so this package imports no
#: agent module. #52 states the direction: ``providers`` is imported by every provider, so a
#: dependency on the agent redaction path would run the wrong way.
ScrubOneText = Callable[[str, str | None], str]

#: The key of a Responses payload that holds the replayable items. ``state.py`` writes it.
_ITEMS_KEY = "items"

#: The item types this module recognises. Anything else stays byte-exact.
_FUNCTION_CALL = "function_call"
_FUNCTION_CALL_OUTPUT = "function_call_output"
_MESSAGE = "message"
_REASONING = "reasoning"

#: Each content-part type that carries readable text, and the key it carries it under.
#:
#: ``input_text`` comes from ``convert_user_message`` and from ``convert_tool_output``,
#: ``output_text`` comes from the assistant item and from the server, and ``text`` is the
#: Chat-style spelling a caller may pass through. ``refusal`` names its text under a key of its
#: own, and ``parsing.py`` reads it as content of the turn, so it is model text like the rest.
#:
#: An ``input_image`` part carries a data URL and an ``input_file`` part carries file bytes, so
#: neither one is text and a scrub of either would corrupt it. Both stay out of this map, which
#: is what keeps them byte-exact.
RESPONSES_TEXT_PART_KEYS = {
    "input_text": "text",
    "output_text": "text",
    "text": "text",
    "refusal": "refusal",
}

#: The part type of a reasoning summary, and its key. ``parsing.py`` reads exactly this type.
_SUMMARY_PART_KEYS = {"summary_text": "text"}

#: The sealed copy of a reasoning item. A scrub that changed the readable text drops this.
RESPONSES_REASONING_SEAL_KEY = "encrypted_content"


def scrub_provider_state_payload(
    kind: str, payload: dict[str, Any], scrub: ScrubOneText
) -> dict[str, Any]:
    """Scrub the payload of a state this module recognises, or return it unchanged.

    The caller holds no provider, so the dispatch on *kind* lives here. A payload of another kind
    reaches the file byte-exact, because only the provider that issued it knows which of its own
    fields carry text.

    ``openai_codex_provider.py`` builds its state with ``build_responses_state`` as well, so its
    kind is this kind and this one call site covers both providers.
    """
    if kind != RESPONSES_STATE_KIND:
        return payload
    return scrub_responses_payload(payload, scrub)


def scrub_responses_payload(payload: dict[str, Any], scrub: ScrubOneText) -> dict[str, Any]:
    """Return a persist-safe copy of one Responses payload.

    The input is never mutated. The live turn holds these same items, because
    ``build_responses_state`` stores the request input of the turn that just ran.

    A payload with nothing to scrub comes back as the very same object. Byte identity is then
    object identity, so no rebuild can change one byte of a session that held no secret.
    """
    raw_items = payload.get(_ITEMS_KEY)
    if not isinstance(raw_items, list):
        return payload
    items = cast("list[Any]", raw_items)
    scrubbed = [_scrub_item(item, scrub) for item in items]
    if all(new is old for new, old in zip(scrubbed, items)):
        return payload
    out = dict(payload)
    out[_ITEMS_KEY] = scrubbed
    return out


def _scrub_item(item: Any, scrub: ScrubOneText) -> Any:
    """Scrub one item, or return it unchanged.

    The return is the same object when nothing changed, which is what keeps a clean payload
    byte-identical to the one this file held before #54.
    """
    if not isinstance(item, dict):
        return item
    fields = cast("dict[str, Any]", item)
    item_type = cast(object, fields.get("type"))
    if item_type == _FUNCTION_CALL:
        return _replaced(fields, "arguments", _scrub_string(fields.get("arguments"), scrub))
    if item_type == _FUNCTION_CALL_OUTPUT:
        return _replaced(fields, "output", _scrub_text_field(fields.get("output"), scrub))
    if item_type == _REASONING:
        return _scrub_reasoning(fields, scrub)
    if _is_message(fields, item_type):
        return _replaced(fields, "content", _scrub_text_field(fields.get("content"), scrub))
    return fields


def _is_message(fields: dict[str, Any], item_type: object) -> bool:
    """Report whether this item is a message, in either of the two shapes that exist.

    ``convert_messages`` builds the assistant item with ``type`` set to ``message``.
    ``convert_user_message`` builds the user item with a ``role`` and a content list and no
    ``type`` at all, and a user can paste a credential into chat, so both shapes scrub.
    """
    if item_type == _MESSAGE:
        return True
    return (
        item_type is None
        and isinstance(fields.get("role"), str)
        and isinstance(fields.get("content"), list)
    )


def _scrub_reasoning(fields: dict[str, Any], scrub: ScrubOneText) -> Any:
    """Scrub the readable text of a reasoning item, and drop the seal when it changed.

    Three fields can hold readable text, and ``parsing.py`` reads all three back as the reasoning
    of the turn: ``content`` as a plain string, ``content`` as a part list, and ``summary`` as a
    list of ``summary_text`` parts.

    ``encrypted_content`` goes when any of those changed. It is the same reasoning in sealed form,
    so keeping it would send the provider the text the scrub removed. The item itself stays: the
    API needs a reasoning item to precede the function call it belongs to, so dropping the item
    would break the next request instead of only the seal.
    """
    content = _scrub_text_field(fields.get("content"), scrub)
    summary = _scrub_summary(fields.get("summary"), scrub)
    if content is fields.get("content") and summary is fields.get("summary"):
        return fields
    out: dict[str, Any] = dict(fields)
    if "content" in out:
        out["content"] = content
    if "summary" in out:
        out["summary"] = summary
    out.pop(RESPONSES_REASONING_SEAL_KEY, None)
    return out


def _scrub_summary(summary: Any, scrub: ScrubOneText) -> Any:
    if not isinstance(summary, list):
        return summary
    parts = cast("list[Any]", summary)
    scrubbed: list[Any] = [_scrub_part(part, scrub, keys=_SUMMARY_PART_KEYS) for part in parts]
    if all(new is old for new, old in zip(scrubbed, parts)):
        return parts
    return scrubbed


def _scrub_text_field(value: Any, scrub: ScrubOneText) -> Any:
    """Scrub a field that holds either one text or a list of content parts.

    ``convert_tool_output`` returns both shapes for the same field, so both are named here.
    """
    if isinstance(value, str):
        return _scrub_string(value, scrub)
    if not isinstance(value, list):
        return value
    parts = cast("list[Any]", value)
    scrubbed: list[Any] = [
        _scrub_part(part, scrub, keys=RESPONSES_TEXT_PART_KEYS) for part in parts
    ]
    if all(new is old for new, old in zip(scrubbed, parts)):
        return parts
    return scrubbed


def _scrub_part(part: Any, scrub: ScrubOneText, *, keys: dict[str, str]) -> Any:
    """Scrub one content part, under the key its own type names.

    A part type that *keys* does not name comes back untouched. That is how an image part and a
    file part stay byte-exact: they are absent from the map rather than special-cased.
    """
    if not isinstance(part, dict):
        return part
    fields = cast("dict[str, Any]", part)
    key = keys.get(str(cast(object, fields.get("type"))))
    if key is None:
        return fields
    return _replaced(fields, key, _scrub_string(fields.get(key), scrub))


def _scrub_string(value: Any, scrub: ScrubOneText) -> Any:
    """Scrub one string value, and pass anything else through.

    The class is None. This module states in its own docstring why no payload text drops whole.

    **An answer equal to the question comes back as the original object.** That is what makes
    object identity a usable signal for "nothing changed" in every caller above. The scrub
    crosses a socket, so it returns a fresh string even when it removed nothing, and an identity
    test on that fresh string would report a change on every text. A reasoning item would then
    lose its seal for a turn that held no secret.
    """
    if not isinstance(value, str) or not value:
        return value
    scrubbed = scrub(value, None)
    return value if scrubbed == value else scrubbed


def _replaced(fields: dict[str, Any], key: str, value: Any) -> Any:
    """Return *fields* with *key* set, or the same object when the value did not change."""
    if key not in fields or value is cast(object, fields[key]):
        return fields
    out: dict[str, Any] = dict(fields)
    out[key] = value
    return out


__all__ = [
    "RESPONSES_REASONING_SEAL_KEY",
    "RESPONSES_TEXT_PART_KEYS",
    "ScrubOneText",
    "scrub_provider_state_payload",
    "scrub_responses_payload",
]
