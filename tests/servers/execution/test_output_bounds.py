# tests/servers/execution/test_output_bounds.py
from __future__ import annotations

from nanoinfra.servers.execution.base import MAX_OUTPUT_CHARS, BoundedOutput, truncate_output


def test_short_output_is_untouched():
    assert truncate_output("up 3 days") == "up 3 days"


def test_output_beyond_the_cap_is_truncated_with_a_note():
    output = truncate_output("x" * (MAX_OUTPUT_CHARS + 1234))
    assert len(output) < MAX_OUTPUT_CHARS + 200
    assert "(1,234 chars truncated from output)" in output


def test_truncation_keeps_head_and_tail():
    body = "HEAD" + ("x" * MAX_OUTPUT_CHARS) + "TAIL"
    output = truncate_output(body)
    assert output.startswith("HEAD")
    assert "TAIL\n(" in output


def test_bounded_output_streams_within_budget():
    buffer = BoundedOutput(max_chars=100)
    for _ in range(50):
        buffer.append("y" * 10)

    text = buffer.text()
    assert buffer.total_chars == 500
    assert len(text.split("\n(")[0]) == 100
    assert "(400 chars truncated from output)" in text


def test_bounded_output_below_budget_has_no_note():
    buffer = BoundedOutput(max_chars=100)
    buffer.append("hello ")
    buffer.append("world")
    assert buffer.text() == "hello world"
    assert buffer.total_chars == 11


def test_bounded_output_keeps_the_newest_tail():
    buffer = BoundedOutput(max_chars=10)
    buffer.append("12345")  # fills the head budget
    for chunk in ("aaa", "bbb", "ccc"):
        buffer.append(chunk)

    retained = buffer.text().split("\n(")[0]
    assert retained.startswith("12345")
    assert retained.endswith("ccc")
