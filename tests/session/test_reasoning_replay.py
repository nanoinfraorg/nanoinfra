# tests/session/test_reasoning_replay.py
"""nanoinfraorg/nanoinfra#48: a scrubbed thinking block never replays to a provider.

A provider needs a signature that matches the text of a thinking block. The scrub changes the
text, so the persisted block loses its signature and carries a marker (#48). This module holds
the other half of that decision. The replay path reads the marker and sends no such block.

A signed block is untouched, and it replays exactly as it does today.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.agent.redaction import (
    REASONING_SCRUB_MARKER_KEY,
    REASONING_SCRUBBED_MARKER,
    REASONING_WITHHELD_MARKER,
)
from nanoinfra.session.manager import Session

SIGNATURE = "the-signature-the-provider-issued"

_SIGNED_BLOCK: dict[str, Any] = {
    "type": "thinking",
    "thinking": "I restart nginx on web1",
    "signature": SIGNATURE,
}
_SCRUBBED_BLOCK: dict[str, Any] = {
    "type": "thinking",
    "thinking": "I run mysql -p[redacted secret: prod-db-password] on db1",
    REASONING_SCRUB_MARKER_KEY: REASONING_SCRUBBED_MARKER,
}
_WITHHELD_BLOCK: dict[str, Any] = {
    "type": "thinking",
    "thinking": "[nanoinfra withheld this text...]",
    REASONING_SCRUB_MARKER_KEY: REASONING_WITHHELD_MARKER,
}


def _session(*blocks: dict[str, Any]) -> Session:
    return Session(
        key="s1",
        messages=[
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": "done",
                "reasoning_content": "I run mysql -p[redacted secret: prod-db-password] on db1",
                "thinking_blocks": list(blocks),
            },
        ],
    )


def test_a_scrubbed_thinking_block_never_replays() -> None:
    """A signature that no longer matches its text is worse than no block at all."""
    history = _session(_SCRUBBED_BLOCK).get_history()

    assert "thinking_blocks" not in history[-1]


def test_a_withheld_thinking_block_never_replays() -> None:
    """A block nobody scrubbed holds a marker in place of its text, so it replays no better."""
    history = _session(_WITHHELD_BLOCK).get_history()

    assert "thinking_blocks" not in history[-1]


def test_a_signed_thinking_block_still_replays() -> None:
    """A turn that held no secret changes in no way."""
    history = _session(_SIGNED_BLOCK).get_history()

    assert history[-1]["thinking_blocks"] == [_SIGNED_BLOCK]


def test_one_scrubbed_block_never_drops_a_signed_block() -> None:
    """One turn can hold several blocks, and only the changed one loses its replay."""
    history = _session(_SIGNED_BLOCK, _SCRUBBED_BLOCK).get_history()

    assert history[-1]["thinking_blocks"] == [_SIGNED_BLOCK]


def test_a_scrubbed_turn_still_replays_its_text_and_its_reasoning() -> None:
    """The turn stays in the conversation. Only the block goes."""
    history = _session(_SCRUBBED_BLOCK).get_history()

    assert history[-1]["content"] == "done"
    assert "redacted secret" in str(history[-1]["reasoning_content"])
