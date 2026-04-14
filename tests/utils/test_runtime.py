from nanobot.utils.runtime import repeated_tool_call_error, tool_call_signature


def test_tool_call_signature_sorts_arguments_stably() -> None:
    first = tool_call_signature("read_file", {"offset": 1, "path": "memory/history.jsonl"})
    second = tool_call_signature("read_file", {"path": "memory/history.jsonl", "offset": 1})

    assert first == second


def test_repeated_tool_call_error_blocks_after_two_attempts() -> None:
    seen: dict[str, int] = {}

    assert repeated_tool_call_error("read_file", {"path": "a.txt"}, seen) is None
    assert repeated_tool_call_error("read_file", {"path": "a.txt"}, seen) is None

    error = repeated_tool_call_error("read_file", {"path": "a.txt"}, seen)

    assert error is not None
    assert "repeated identical call to 'read_file' blocked after 2 attempts" in error
