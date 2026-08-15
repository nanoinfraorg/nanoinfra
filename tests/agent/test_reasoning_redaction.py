# tests/agent/test_reasoning_redaction.py
"""nanoinfraorg/nanoinfra#48: persisted reasoning holds no stored secret value.

#17 keeps a credential value out of a transcript and out of the reasoning pane. #41 moved the
scrub into the executor. Neither one covered ``reasoning_content`` and ``thinking_blocks``, and
a session file holds both. A model that plans a remote action writes the resolved command in
its reasoning, and a resolved command embeds a credential.

The structure tests call the executor's own scrub function in process, the way
``tests/agent/test_redaction.py`` does. The boundary tests cross a real scrub socket through
the ``scrub_service`` fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.redaction import (
    REASONING_SCRUB_MARKER_KEY,
    ScrubText,
    SecretSentinel,
    TranscriptRedactor,
    redact_messages,
    scrub_one_text,
)
from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS
from nanoinfra.bus.queue import MessageBus
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.session.manager import Session

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"
SIGNATURE = "the-signature-the-provider-issued"

_Scrubber = Callable[[Path], object]


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)


@pytest.fixture
def sentinels() -> list[SecretSentinel]:
    return [SecretSentinel(name=SECRET_NAME, value=SECRET_VALUE)]


def _scrub(sentinels: list[SecretSentinel]) -> ScrubText:
    """The scrubber the executor is, as a local callable."""

    def scrub(text: str, capability_class: str | None) -> str:
        return scrub_one_text(text, capability_class, sentinels)

    return scrub


def _stored_secret(workspace: Path) -> str:
    """Create one local secret, and return its name."""
    secret = SecretStore(workspace).create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )
    return secret.name


def _thinking_message(
    thinking: str,
    *,
    reasoning: str | None = None,
    signature: str = SIGNATURE,
) -> dict[str, Any]:
    """One assistant message with both reasoning fields, the way a provider returns them."""
    return {
        "role": "assistant",
        "content": "done",
        "reasoning_content": thinking if reasoning is None else reasoning,
        "thinking_blocks": [
            {"type": "thinking", "thinking": thinking, "signature": signature}
        ],
    }


def _first_block(message: dict[str, Any]) -> dict[str, Any]:
    blocks: Any = message["thinking_blocks"]
    return blocks[0]


# -- the reasoning text scrubs ----------------------------------------------


def test_reasoning_content_loses_a_stored_secret_value(
    sentinels: list[SecretSentinel],
) -> None:
    """The field a DeepSeek turn or a Bedrock turn persists."""
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = redact_messages([message], _scrub(sentinels))[0]

    assert SECRET_VALUE not in json.dumps(redacted)
    assert SECRET_NAME in str(redacted["reasoning_content"])


def test_a_thinking_block_loses_a_stored_secret_value(
    sentinels: list[SecretSentinel],
) -> None:
    """The field an Anthropic turn persists."""
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = redact_messages([message], _scrub(sentinels))[0]

    assert SECRET_VALUE not in json.dumps(_first_block(redacted))
    assert SECRET_NAME in str(_first_block(redacted)["thinking"])


def test_reasoning_scrubs_value_by_value_and_never_drops_whole(
    sentinels: list[SecretSentinel],
) -> None:
    """Reasoning is not a tool result, so the words around the value survive."""
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = redact_messages([message], _scrub(sentinels))[0]

    assert "I run mysql -p" in str(redacted["reasoning_content"])
    assert "on db1" in str(redacted["reasoning_content"])


def test_a_credential_access_class_never_drops_the_reasoning_whole(
    sentinels: list[SecretSentinel],
) -> None:
    """A class belongs to a tool result. Reasoning carries no class of its own."""
    message = _thinking_message(f"the value is {SECRET_VALUE}")
    message["name"] = "read_secret"

    redacted = redact_messages(
        [message], _scrub(sentinels), capability_of=lambda _name: CREDENTIAL_ACCESS
    )[0]

    assert "the value is" in str(redacted["reasoning_content"])
    assert "credential.access" not in str(redacted["reasoning_content"])


def test_a_scrubbed_thinking_block_loses_its_signature(
    sentinels: list[SecretSentinel],
) -> None:
    """A provider needs a signature that matches the text. This one no longer does."""
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = redact_messages([message], _scrub(sentinels))[0]

    assert "signature" not in _first_block(redacted)
    assert SIGNATURE not in json.dumps(redacted)


def test_a_scrubbed_thinking_block_says_why_it_lost_the_signature(
    sentinels: list[SecretSentinel],
) -> None:
    """A reader months later must tell a scrubbed block from a short one."""
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = redact_messages([message], _scrub(sentinels))[0]

    marker = str(_first_block(redacted)[REASONING_SCRUB_MARKER_KEY])
    assert "signature" in marker
    assert "credential" in marker


def test_a_thinking_block_that_held_no_secret_keeps_its_signature(
    sentinels: list[SecretSentinel],
) -> None:
    """A turn that held no secret changes in no way, and it replays as it does today."""
    message = _thinking_message("I restart nginx on web1")

    redacted = redact_messages([message], _scrub(sentinels))[0]

    assert _first_block(redacted) == {
        "type": "thinking",
        "thinking": "I restart nginx on web1",
        "signature": SIGNATURE,
    }


def test_an_untouched_block_carries_no_marker(sentinels: list[SecretSentinel]) -> None:
    """The marker is the difference between a scrubbed block and a plain one."""
    redacted = redact_messages([_thinking_message("all clear")], _scrub(sentinels))[0]

    assert REASONING_SCRUB_MARKER_KEY not in _first_block(redacted)


def test_a_redacted_thinking_block_survives_the_scrub(
    sentinels: list[SecretSentinel],
) -> None:
    """A ``redacted_thinking`` block holds opaque provider bytes, and no secret matches it."""
    message = {
        "role": "assistant",
        "content": "done",
        "reasoning_content": "",
        "thinking_blocks": [
            {"type": "redacted_thinking", "redactedContentBase64": "b3BhcXVl"}
        ],
    }

    redacted = redact_messages([message], _scrub(sentinels))[0]

    assert _first_block(redacted) == {
        "type": "redacted_thinking",
        "redactedContentBase64": "b3BhcXVl",
    }


def test_the_scrub_never_mutates_the_live_message(
    sentinels: list[SecretSentinel],
) -> None:
    """The turn in flight keeps its real reasoning and its real signature."""
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redact_messages([message], _scrub(sentinels))

    assert SECRET_VALUE in str(message["reasoning_content"])
    assert _first_block(message)["signature"] == SIGNATURE


def test_an_empty_reasoning_field_stays_empty(sentinels: list[SecretSentinel]) -> None:
    """DeepSeek needs the key, so the key stays and the empty value stays empty."""
    message = {"role": "assistant", "content": "done", "reasoning_content": ""}

    redacted = redact_messages([message], _scrub(sentinels))[0]

    assert redacted["reasoning_content"] == ""


# -- a scrub that cannot run ------------------------------------------------


def test_no_scrubber_withholds_the_reasoning_content(tmp_path: Path) -> None:
    """Fail closed, the same as #41 set for the rest of the transcript."""
    _stored_secret(tmp_path)
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = TranscriptRedactor.for_workspace(tmp_path).messages([message])[0]

    assert SECRET_VALUE not in json.dumps(redacted)
    assert "withheld" in str(redacted["reasoning_content"])


def test_no_scrubber_withholds_a_thinking_block(tmp_path: Path) -> None:
    _stored_secret(tmp_path)
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = TranscriptRedactor.for_workspace(tmp_path).messages([message])[0]

    assert SECRET_VALUE not in json.dumps(_first_block(redacted))
    assert "withheld" in str(_first_block(redacted)["thinking"])


def test_a_withheld_thinking_block_loses_its_signature(tmp_path: Path) -> None:
    """The text no longer matches the signature, so the pair must not persist."""
    _stored_secret(tmp_path)
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = TranscriptRedactor.for_workspace(tmp_path).messages([message])[0]

    assert "signature" not in _first_block(redacted)
    assert SIGNATURE not in json.dumps(redacted)


def test_a_withheld_thinking_block_says_why(tmp_path: Path) -> None:
    _stored_secret(tmp_path)
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = TranscriptRedactor.for_workspace(tmp_path).messages([message])[0]

    marker = str(_first_block(redacted)[REASONING_SCRUB_MARKER_KEY])
    assert "signature" in marker


def test_a_withheld_message_keeps_its_reasoning_keys(tmp_path: Path) -> None:
    """The record keeps its shape, so a reader still sees which fields the turn held."""
    _stored_secret(tmp_path)
    message = _thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")

    redacted = TranscriptRedactor.for_workspace(tmp_path).messages([message])[0]

    assert "reasoning_content" in redacted
    assert _first_block(redacted)["type"] == "thinking"


# -- the session boundary ---------------------------------------------------


def _loop(workspace: Path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(), provider=MagicMock(), workspace=workspace, model="test-model"
    )


def test_a_saved_turn_scrubs_the_reasoning_of_its_assistant_message(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """``sessions/*.jsonl`` is the chat transcript, and the save stage writes it."""
    name = _stored_secret(tmp_path)
    scrub_service(tmp_path)
    session = Session(key="s1", messages=[])

    _loop(tmp_path)._save_turn(
        session, [_thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")], 0
    )

    persisted = json.dumps(session.messages)
    assert SECRET_VALUE not in persisted
    assert name in persisted
    assert SIGNATURE not in persisted


def test_a_saved_turn_withholds_reasoning_when_no_executor_answers(
    tmp_path: Path,
) -> None:
    """No scrub service runs here, so the save stage must persist no raw reasoning."""
    _stored_secret(tmp_path)
    session = Session(key="s1", messages=[])

    _loop(tmp_path)._save_turn(
        session, [_thinking_message(f"I run mysql -p{SECRET_VALUE} on db1")], 0
    )

    persisted = json.dumps(session.messages)
    assert SECRET_VALUE not in persisted
    assert "withheld" in persisted


def test_a_saved_turn_keeps_ordinary_reasoning(
    tmp_path: Path, scrub_service: _Scrubber
) -> None:
    """The scrub removes the value and nothing else."""
    _stored_secret(tmp_path)
    scrub_service(tmp_path)
    session = Session(key="s1", messages=[])

    _loop(tmp_path)._save_turn(session, [_thinking_message("I restart nginx")], 0)

    persisted = json.dumps(session.messages)
    assert "I restart nginx" in persisted
    assert SIGNATURE in persisted
