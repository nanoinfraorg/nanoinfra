# tests/agent/test_redaction.py
"""Item 14 (#17): credential material never reaches a persisted transcript.

The sentinels moved to the executor with #41, so this module drives the structure half: which
fields a transcript scrubs, which result drops whole, and where the bound applies. The scrub of
one text against one sentinel set lives in ``tests/gates/test_scrub_socket.py``.

``_scrub`` below is the same function the executor runs, called in process. The tests that cross
a real persistence boundary use the ``scrub_service`` fixture instead, so they cross the socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from nanoinfra.agent.memory import MemoryStore
from nanoinfra.agent.redaction import (
    CREDENTIAL_ACCESS,
    MIN_REDACTABLE_SECRET_CHARS,
    TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
    ScrubText,
    SecretSentinel,
    redact_messages,
    redact_text,
    scrub_one_text,
)
from nanoinfra.agent.subagent_transcript import SubagentTranscriptStore
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore

SECRET_VALUE = "hunter2-correct-horse-battery"
SECRET_NAME = "prod-db-password"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)


def _scrub(sentinels: list[SecretSentinel]) -> ScrubText:
    """The scrubber the executor is, as a local callable."""

    def scrub(text: str, capability_class: str | None) -> str:
        return scrub_one_text(text, capability_class, sentinels)

    return scrub


@pytest.fixture
def sentinels() -> list[SecretSentinel]:
    return [SecretSentinel(name=SECRET_NAME, value=SECRET_VALUE)]


def _stored_secret(workspace: Path) -> str:
    """Create one local secret and return its id."""
    store = SecretStore(workspace)
    secret = store.create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )
    return secret.id


# -- text scrubbing ---------------------------------------------------------


def test_secret_value_is_replaced_by_a_reference_to_its_name(
    sentinels: list[SecretSentinel],
) -> None:
    """The value goes. The name stays, so an operator knows which secret ran."""
    scrubbed = redact_text(f"DB_PASSWORD={SECRET_VALUE}\n", sentinels)

    assert SECRET_VALUE not in scrubbed
    assert SECRET_NAME in scrubbed
    assert scrubbed.startswith("DB_PASSWORD=[redacted secret")


def test_text_without_a_secret_is_returned_unchanged(
    sentinels: list[SecretSentinel],
) -> None:
    assert redact_text("nothing to see", sentinels) == "nothing to see"


def test_no_sentinels_leaves_the_text_alone() -> None:
    assert redact_text(f"value {SECRET_VALUE}", []) == f"value {SECRET_VALUE}"


def test_short_secret_values_are_never_used_as_sentinels() -> None:
    """A 4-character value matches everywhere. Scrubbing it destroys the text."""
    short = "a" * (MIN_REDACTABLE_SECRET_CHARS - 1)
    text = f"the letter {short} appears often"

    assert redact_text(text, [SecretSentinel(name="tiny", value=short)]) == text


def test_a_secret_stored_with_crlf_is_found_in_lf_text() -> None:
    """An SSH key round-trips through both line endings. Both must match."""
    value = "-----BEGIN KEY-----\r\nabcdefghijklmnop\r\n-----END KEY-----"
    sentinel = SecretSentinel(name="deploy-key", value=value)

    scrubbed = redact_text(value.replace("\r\n", "\n"), [sentinel])

    assert "abcdefghijklmnop" not in scrubbed
    assert "deploy-key" in scrubbed


def test_a_longer_secret_is_scrubbed_before_a_shorter_one_it_contains() -> None:
    """Longest first, or the shorter placeholder hides half of the longer value."""
    outer = SecretSentinel(name="outer", value="abcdefgh-ijklmnop")
    inner = SecretSentinel(name="inner", value="abcdefgh")

    scrubbed = redact_text("token abcdefgh-ijklmnop end", [inner, outer])

    assert "ijklmnop" not in scrubbed
    assert "outer" in scrubbed


def test_a_secret_name_cannot_forge_extra_placeholder_text() -> None:
    """Operator-supplied names are data. They must not shape the placeholder."""
    sentinel = SecretSentinel(name="bad]\n[redacted secret: other", value=SECRET_VALUE)

    scrubbed = redact_text(SECRET_VALUE, [sentinel])

    assert "\n" not in scrubbed
    assert scrubbed.count("[redacted secret:") == 1


# -- message redaction ------------------------------------------------------


def _tool_message(content: str, name: str = "execute_on_server") -> dict[str, object]:
    return {"role": "tool", "tool_call_id": "call_1", "name": name, "content": content}


def test_tool_result_output_is_scrubbed(sentinels: list[SecretSentinel]) -> None:
    redacted = redact_messages([_tool_message(f"env dump: {SECRET_VALUE}")], _scrub(sentinels))

    assert SECRET_VALUE not in str(redacted[0]["content"])
    assert SECRET_NAME in str(redacted[0]["content"])


def test_assistant_tool_call_arguments_are_scrubbed(
    sentinels: list[SecretSentinel],
) -> None:
    """A resolved command carries the credential in its own arguments."""
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "execute_on_server",
                    "arguments": json.dumps({"command": f"mysql -p{SECRET_VALUE}"}),
                },
            }
        ],
    }

    redacted = redact_messages([message], _scrub(sentinels))

    assert SECRET_VALUE not in json.dumps(redacted[0])
    assert SECRET_NAME in json.dumps(redacted[0])


def test_text_blocks_inside_multimodal_content_are_scrubbed(
    sentinels: list[SecretSentinel],
) -> None:
    message = {
        "role": "user",
        "content": [{"type": "text", "text": f"use {SECRET_VALUE}"}],
    }

    redacted = redact_messages([message], _scrub(sentinels))

    assert SECRET_VALUE not in json.dumps(redacted[0])


def test_the_input_messages_are_not_mutated(sentinels: list[SecretSentinel]) -> None:
    """The live turn keeps its real values. Only the persisted copy changes."""
    message = _tool_message(f"env dump: {SECRET_VALUE}")

    redact_messages([message], _scrub(sentinels))

    assert message["content"] == f"env dump: {SECRET_VALUE}"


def test_credential_access_results_are_dropped_whole(
    sentinels: list[SecretSentinel],
) -> None:
    """A credential.access result is a credential. Keep the name, drop the body."""
    message = _tool_message(f"value: {SECRET_VALUE}", name="read_secret")

    redacted = redact_messages(
        [message],
        _scrub(sentinels),
        capability_of=lambda _name: CREDENTIAL_ACCESS,
    )

    content = str(redacted[0]["content"])
    assert SECRET_VALUE not in content
    assert SECRET_NAME in content
    assert "env dump" not in content
    assert "credential.access" in content


def test_credential_access_results_are_dropped_even_when_unrecognized() -> None:
    """No sentinel matched. The body still goes, with an unnamed reference."""
    message = _tool_message("some unknown credential", name="read_secret")

    redacted = redact_messages(
        [message], _scrub([]), capability_of=lambda _name: CREDENTIAL_ACCESS
    )

    assert "some unknown credential" not in str(redacted[0]["content"])


def test_other_capability_classes_keep_their_result(
    sentinels: list[SecretSentinel],
) -> None:
    redacted = redact_messages(
        [_tool_message("exit code 0")], _scrub(sentinels), capability_of=lambda _n: "mutate.remote"
    )

    assert redacted[0]["content"] == "exit code 0"


def test_remote_output_is_truncated_when_a_caller_asks(
    sentinels: list[SecretSentinel],
) -> None:
    """Part 2: the persisted copy is bounded, and the caller names the bound (#56).

    This test read "without the caller asking" while the parameter carried a default. Every
    caller but one passed None, and the one that did not was the subagent transcript store, so
    the default described the behaviour of no path a reader was likely to be looking at.
    """
    long_output = "x" * (TRANSCRIPT_TOOL_RESULT_MAX_CHARS * 3)

    redacted = redact_messages(
        [_tool_message(long_output)],
        _scrub(sentinels),
        max_tool_result_chars=TRANSCRIPT_TOOL_RESULT_MAX_CHARS,
    )

    content = str(redacted[0]["content"])
    assert len(content) < len(long_output)
    assert "chars truncated from output" in content


def test_no_bound_applies_when_a_caller_names_none(
    sentinels: list[SecretSentinel],
) -> None:
    """The default is None now, so a caller that names no bound gets none.

    Four of the five callers want exactly that: the main loop, the session store, the SDK
    snapshot and the checkpoint each hold their own rule about length.
    """
    long_output = "x" * (TRANSCRIPT_TOOL_RESULT_MAX_CHARS * 3)

    redacted = redact_messages([_tool_message(long_output)], _scrub(sentinels))

    assert redacted[0]["content"] == long_output


def test_assistant_content_is_not_truncated(sentinels: list[SecretSentinel]) -> None:
    """Only tool output is bounded. An answer must survive intact."""
    long_answer = "y" * (TRANSCRIPT_TOOL_RESULT_MAX_CHARS * 3)

    redacted = redact_messages(
        [{"role": "assistant", "content": long_answer}], _scrub(sentinels)
    )

    assert redacted[0]["content"] == long_answer


def test_truncation_can_be_disabled_for_a_caller_that_needs_it(
    sentinels: list[SecretSentinel],
) -> None:
    long_output = "x" * (TRANSCRIPT_TOOL_RESULT_MAX_CHARS * 3)

    redacted = redact_messages(
        [_tool_message(long_output)], _scrub(sentinels), max_tool_result_chars=None
    )

    assert redacted[0]["content"] == long_output


# -- persistence boundaries -------------------------------------------------
#
# These tests cross a real socket. The executor performs the scrub after #41, so the
# ``scrub_service`` fixture (tests/agent/conftest.py) runs the executor's answer path for the
# workspace under test.


def test_append_history_redacts_a_resolved_secret(
    tmp_path: Path, scrub_service: Callable[[Path], object]
) -> None:
    """history.jsonl is a persisted transcript. The value must not land there."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    store = MemoryStore(tmp_path)

    store.append_history(f"[RAW] 1 messages\n[..] TOOL: output {SECRET_VALUE}")

    persisted = (tmp_path / "memory" / "history.jsonl").read_text(encoding="utf-8")
    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted


def test_append_history_caps_after_redaction(
    tmp_path: Path, scrub_service: Callable[[Path], object]
) -> None:
    """Scrub first, then cap. A cap through a value would leave half of it."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    store = MemoryStore(tmp_path)
    entry = "a" * 40 + SECRET_VALUE + "b" * 40

    store.append_history(entry, max_chars=50)

    persisted = (tmp_path / "memory" / "history.jsonl").read_text(encoding="utf-8")
    assert SECRET_VALUE not in persisted
    assert SECRET_VALUE[:10] not in persisted


def test_subagent_transcript_redacts_a_resolved_secret(
    tmp_path: Path, scrub_service: Callable[[Path], object]
) -> None:
    """Part 3: the model writes these files and they persist."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    store = SubagentTranscriptStore(tmp_path)

    store.write(
        "abc12345",
        [
            {"role": "user", "content": "check the database"},
            _tool_message(f"connected with {SECRET_VALUE}"),
        ],
    )

    persisted = store.path_for("abc12345").read_text(encoding="utf-8")
    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted
    records = store.read("abc12345")
    assert [r["role"] for r in records] == ["user", "tool"]


def test_subagent_transcript_truncates_remote_output(tmp_path: Path) -> None:
    """No secret in this workspace, so the bound applies with no round trip."""
    store = SubagentTranscriptStore(tmp_path)
    long_output = "x" * (TRANSCRIPT_TOOL_RESULT_MAX_CHARS * 3)

    store.write("abc12345", [_tool_message(long_output)])

    record = store.read("abc12345")[0]
    assert len(str(record["content"])) < len(long_output)
    assert "chars truncated from output" in str(record["content"])


def test_subagent_transcript_redacts_metadata_values(
    tmp_path: Path, scrub_service: Callable[[Path], object]
) -> None:
    """A subagent error string is metadata, and it can quote the credential."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    store = SubagentTranscriptStore(tmp_path)

    store.write(
        "abc12345",
        [{"role": "user", "content": "go"}],
        metadata={"stop_reason": "error", "error": f"auth failed for {SECRET_VALUE}"},
    )

    persisted = store.path_for("abc12345").read_text(encoding="utf-8")
    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted
    meta = store.read("abc12345")[-1]["_transcript_meta"]
    assert meta["stop_reason"] == "error"


def test_subagent_transcript_drops_a_credential_access_result(
    tmp_path: Path, scrub_service: Callable[[Path], object]
) -> None:
    """The store accepts a capability resolver, so #17 part 1 reaches it."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    store = SubagentTranscriptStore(tmp_path)

    store.write(
        "abc12345",
        [_tool_message(f"value: {SECRET_VALUE}", name="read_secret")],
        capability_of=lambda _name: CREDENTIAL_ACCESS,
    )

    content = str(store.read("abc12345")[0]["content"])
    assert content.startswith("[redacted credential.access result")
    assert SECRET_NAME in content


def test_a_broken_secret_store_costs_the_text_and_not_the_transcript(
    tmp_path: Path, scrub_service: Callable[[Path], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#41 inverts the old answer here, and the write still happens.

    The old code persisted the text unscrubbed when the store failed, which is fail open on the
    one path #17 exists to close. The record now keeps its shape and holds a marker instead.
    """
    _stored_secret(tmp_path)
    scrub_service(tmp_path)

    def _explode(self: SecretStore) -> list[object]:
        raise RuntimeError("store is broken")

    monkeypatch.setattr(SecretStore, "list_secrets", _explode)
    store = SubagentTranscriptStore(tmp_path)

    store.write("abc12345", [{"role": "user", "content": "hello"}])

    record = store.read("abc12345")[0]
    assert record["role"] == "user"
    assert "hello" not in str(record["content"])
    assert "withheld" in str(record["content"])
