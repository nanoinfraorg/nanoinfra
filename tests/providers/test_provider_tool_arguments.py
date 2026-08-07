"""Shared tool-argument parsing policy tests."""

import json

from nanoinfra.providers.base import (
    ToolCallRequest,
    parse_tool_arguments,
    tool_arguments_json_for_replay,
    tool_arguments_object_for_replay,
)


def test_parse_tool_arguments_preserves_malformed_executable_arguments() -> None:
    assert parse_tool_arguments('{path:"foo.txt"}') == '{path:"foo.txt"}'


def test_parse_tool_arguments_preserves_non_object_executable_arguments() -> None:
    assert parse_tool_arguments('["foo.txt"]') == ["foo.txt"]
    assert parse_tool_arguments("false") is False
    assert parse_tool_arguments("null") == "null"


def test_tool_arguments_object_for_replay_repairs_object_like_history_arguments() -> None:
    assert tool_arguments_object_for_replay('{path:"foo.txt"}') == {"path": "foo.txt"}


def test_tool_arguments_object_for_replay_keeps_history_object_shaped() -> None:
    for arguments in ['["foo.txt"]', "false", "null", "0", ["foo.txt"], False, None, 0]:
        assert tool_arguments_object_for_replay(arguments) == {}


def test_tool_arguments_json_for_replay_returns_object_string() -> None:
    assert tool_arguments_json_for_replay('{path:"foo.txt"}') == '{"path": "foo.txt"}'


def test_redacted_masks_only_the_named_dict_arguments() -> None:
    tc = ToolCallRequest(id="1", name="create_secret", arguments={"name": "db", "value": "hunter2"})
    redacted = tc.redacted(frozenset({"value"}))
    assert redacted.arguments == {"name": "db", "value": "[REDACTED]"}
    assert tc.arguments == {"name": "db", "value": "hunter2"}, "original call must be untouched"


def test_redacted_masks_json_string_arguments() -> None:
    tc = ToolCallRequest(id="1", name="create_secret", arguments=json.dumps({"name": "db", "value": "hunter2"}))
    redacted = tc.redacted(frozenset({"value"}))
    # Parsed back to a dict in the process -- to_openai_tool_call() re-serializes
    # either shape identically, so this is not observable downstream.
    assert redacted.arguments == {"name": "db", "value": "[REDACTED]"}


def test_redacted_is_a_noop_with_empty_sensitive_params() -> None:
    tc = ToolCallRequest(id="1", name="read_file", arguments={"path": "a.txt"})
    assert tc.redacted(frozenset()) is tc


def test_redacted_leaves_unparseable_string_arguments_untouched() -> None:
    tc = ToolCallRequest(id="1", name="create_secret", arguments="not json")
    assert tc.redacted(frozenset({"value"})) is tc


def test_redacted_leaves_non_dict_arguments_untouched() -> None:
    tc = ToolCallRequest(id="1", name="create_secret", arguments=["value"])
    assert tc.redacted(frozenset({"value"})) is tc


def test_redacted_preserves_other_fields() -> None:
    tc = ToolCallRequest(id="call-1", name="create_secret", arguments={"value": "secret"}, extra_content={"x": 1})
    redacted = tc.redacted(frozenset({"value"}))
    assert redacted.id == "call-1"
    assert redacted.name == "create_secret"
    assert redacted.extra_content == {"x": 1}
