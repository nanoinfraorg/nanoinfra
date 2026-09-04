"""The two prompts that run unattended, readable and writable (#264).

Their nature, because it is the question that prompted this: neither is the prompt of the agent
you talk to. `dream` is the memory consolidation engine — it decides which file each learned fact
goes to and how hard stale content is pruned. `evaluator` is the heartbeat's notification gate —
whether a result is worth interrupting somebody for.

The mechanism (`utils/workspace_prompts.py`) predates this by months. What it lacked was any way
to see it: a slash command an operator had to know about, then a text file, and nothing showing
the text you were about to replace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.utils.workspace_prompts import workspace_prompt_file
from nanoinfra.webui.workspace_prompts_api import (
    packaged_prompt,
    save_workspace_prompt,
    workspace_prompts_payload,
)


def _rows(workspace: Path) -> dict[str, dict]:
    return {row["name"]: row for row in workspace_prompts_payload(workspace)["prompts"]}


def test_both_prompts_arrive_with_the_text_in_force(tmp_path: Path) -> None:
    rows = _rows(tmp_path)

    assert set(rows) == {"dream", "evaluator"}
    for name, row in rows.items():
        assert row["source"] == "platform", name
        assert row["text"] == row["platform_text"], name
        assert row["text"].strip(), f"{name} has no text, so there is nothing to replace"


def test_the_packaged_text_is_the_one_its_own_consumer_uses(tmp_path: Path) -> None:
    """Rendering the templates directly was wrong twice: `dream.md` needs a `skill_creator_path`
    and silently rendered an empty one, and `evaluator.md` holds **two** prompts in one file
    selected by `part`, so a bare render returned an empty string — a panel offering to replace
    nothing at all."""
    from nanoinfra.agent.memory import MemoryStore
    from nanoinfra.utils.evaluator import default_evaluator_prompt

    assert packaged_prompt("dream") == MemoryStore.default_dream_prompt()
    assert packaged_prompt("evaluator") == default_evaluator_prompt()
    assert len(packaged_prompt("evaluator")) > 100


def test_each_prompt_says_what_it_controls(tmp_path: Path) -> None:
    """The question that started this: what is the nature of these two if I cannot use them."""
    rows = _rows(tmp_path)

    assert "memory" in rows["dream"]["controls"].lower()
    assert "heartbeat" in rows["evaluator"]["controls"].lower()


def test_the_evaluator_states_what_a_replacement_must_keep(tmp_path: Path) -> None:
    """Load-bearing: a replacement that stops telling the model to call the tool leaves the gate
    failing closed and silent, so the operator finds out by never being notified again."""
    assert "evaluate_notification" in _rows(tmp_path)["evaluator"]["requirement"]
    # And the one with no such requirement says nothing rather than inventing a caveat.
    assert _rows(tmp_path)["dream"]["requirement"] == ""


def test_saving_a_change_creates_the_file_and_reports_the_new_source(tmp_path: Path) -> None:
    payload = save_workspace_prompt(tmp_path, "dream", "Keep everything. Prune nothing.")

    row = {r["name"]: r for r in payload["prompts"]}["dream"]
    assert row["source"] == "workspace"
    assert row["text"] == "Keep everything. Prune nothing."
    assert workspace_prompt_file(tmp_path, "dream").exists()


def test_text_equal_to_the_packaged_prompt_removes_the_override(tmp_path: Path) -> None:
    """The rule that keeps this from being a trap. A file that merely *matches* today's packaged
    prompt still wins tomorrow, so storing one would freeze this workspace's memory behaviour at
    this version and nothing would say so."""
    save_workspace_prompt(tmp_path, "dream", "mine")
    assert workspace_prompt_file(tmp_path, "dream").exists()

    save_workspace_prompt(tmp_path, "dream", packaged_prompt("dream"))

    assert not workspace_prompt_file(tmp_path, "dream").exists()
    assert _rows(tmp_path)["dream"]["source"] == "platform"


def test_an_emptied_box_restores_the_platform_prompt(tmp_path: Path) -> None:
    """Which is the idiom the README in every workspace already documents: delete or empty the
    file to go back to the built-in behaviour."""
    save_workspace_prompt(tmp_path, "dream", "mine")

    save_workspace_prompt(tmp_path, "dream", "   ")

    assert not workspace_prompt_file(tmp_path, "dream").exists()


def test_an_oversized_prompt_is_refused_with_its_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="capped at"):
        save_workspace_prompt(tmp_path, "dream", "x" * 40_000)


def test_a_prompt_nobody_defines_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        save_workspace_prompt(tmp_path, "identity", "mine")


def test_the_payload_says_where_the_file_lives(tmp_path: Path) -> None:
    """An operator may well want to edit it in an editor too, and this mechanism is a file. The
    panel does not pretend to be the only way in."""
    assert _rows(tmp_path)["dream"]["path"].endswith("prompts/dream.md")
