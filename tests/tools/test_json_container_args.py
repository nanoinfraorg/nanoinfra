"""A structured argument sent as a JSON string must be decoded, not rejected.

Some providers string-encode an object or array argument. Our own tools hit this — apply_patch
takes an array of objects, diagrams and servers take nested objects — so the call was rejected
locally. For an MCP tool with a `oneOf` schema it was worse: the string passed our validation
untouched and the server refused it instead, which is the shape of upstream HKUDS/nanobot#5311.
See nanoinfraorg/nanoinfra#146.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.agent.tools.base import Tool


class _Probe(Tool):
    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "probe"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    async def execute(self, **kwargs: Any) -> str:
        return ""


def _probe(prop: dict[str, Any]) -> _Probe:
    return _Probe({"type": "object", "properties": {"value": prop}})


def _round_trip(prop: dict[str, Any], raw: Any) -> tuple[Any, list[str]]:
    tool = _probe(prop)
    casted = tool.cast_params({"value": raw})
    return casted["value"], tool.validate_params(casted)


def test_an_object_sent_as_a_string_is_decoded() -> None:
    value, errors = _round_trip({"type": "object"}, '{"a": 1}')

    assert value == {"a": 1}
    assert errors == []


def test_an_array_sent_as_a_string_is_decoded() -> None:
    value, errors = _round_trip({"type": "array"}, '["x", "y"]')

    assert value == ["x", "y"]
    assert errors == []


def test_a_nested_object_in_an_unambiguous_union_is_decoded() -> None:
    """The reported shape: oneOf of object-or-null, given a JSON string."""
    prop = {"oneOf": [{"type": "object"}, {"type": "null"}]}

    value, errors = _round_trip(prop, '{"type": "path"}')

    assert value == {"type": "path"}
    assert errors == []


def test_a_union_that_also_admits_a_string_is_left_alone() -> None:
    """Guessing here would silently change the caller's argument.

    For `oneOf: [object, string]` a JSON-looking string may genuinely be the string branch, so it
    is passed through. Upstream's version coerces it to a dict.
    """
    prop = {"oneOf": [{"type": "object"}, {"type": "string"}]}

    value, _errors = _round_trip(prop, '{"a": 1}')

    assert value == '{"a": 1}'


def test_json_looking_text_for_a_string_parameter_is_untouched() -> None:
    value, errors = _round_trip({"type": "string"}, '{"a": 1}')

    assert value == '{"a": 1}'
    assert errors == []


def test_malformed_json_stays_a_string_so_the_error_is_the_real_one() -> None:
    """Decoding must not swallow the failure; validation should still name the type problem."""
    value, errors = _round_trip({"type": "object"}, '{"a":')

    assert value == '{"a":'
    assert errors == ["value should be object"]


def test_a_plain_string_for_an_object_parameter_is_not_mangled() -> None:
    value, errors = _round_trip({"type": "object"}, "just text")

    assert value == "just text"
    assert errors == ["value should be object"]


def test_an_already_decoded_object_is_unchanged() -> None:
    value, errors = _round_trip({"type": "object"}, {"a": 1})

    assert value == {"a": 1}
    assert errors == []


def test_a_json_array_of_objects_decodes_and_validates() -> None:
    """apply_patch's shape: array of objects, string-encoded by the provider."""
    prop = {"type": "array", "items": {"type": "object"}}

    value, errors = _round_trip(prop, '[{"path": "a.py"}, {"path": "b.py"}]')

    assert value == [{"path": "a.py"}, {"path": "b.py"}]
    assert errors == []


def test_whitespace_only_and_empty_strings_are_untouched() -> None:
    for raw in ("", "   "):
        value, _errors = _round_trip({"type": "object"}, raw)
        assert value == raw


def test_a_json_scalar_does_not_satisfy_a_container() -> None:
    """`"5"` parses as JSON but is not an object; it must not be accepted as one."""
    value, errors = _round_trip({"type": "object"}, "5")

    assert value == "5"
    assert errors == ["value should be object"]
