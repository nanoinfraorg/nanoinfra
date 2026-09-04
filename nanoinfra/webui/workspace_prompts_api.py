"""The workspace's own prompt overrides, readable and writable from the panel (#264).

Two prompts run without anybody watching, and neither is the prompt of the agent you talk to:

- **`dream`** is the memory consolidation engine. It decides which file a learned fact belongs in
  -- conduct in `SOUL.md`, the person in `USER.md`, the project in `memory/MEMORY.md`, a workflow
  in `skills/<name>/SKILL.md` -- and how ruthlessly stale content is pruned.
- **`evaluator`** is the heartbeat's notification gate: whether a result is worth interrupting
  somebody for. It must keep instructing the model to call `evaluate_notification`; a replacement
  that drops that leaves the gate failing closed and silent, which is why that is stated here and
  again in the payload.

The mechanism already existed (`utils/workspace_prompts.py`) and Dream and the evaluator have used
it since before this panel. What it did not have was a way to *see* it: the only path was a slash
command an operator had to know about, and then a text file. Nothing showed the text you were
about to replace, which makes an override a rewrite from memory.

**Nothing is created until an operator changes something.** An existing file wins over the
packaged prompt permanently, so a workspace seeded with copies of today's prompts would silently
stop receiving every later improvement to them. Saving writes the file; restoring deletes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanoinfra.utils.workspace_prompts import (
    WORKSPACE_PROMPT_MAX_CHARS,
    load_workspace_prompt_override,
    workspace_prompt_file,
)

#: name -> (what it controls, what a replacement must keep)
#:
#: The packaged text comes from ``packaged_prompt`` below rather than from a template name here.
#: Rendering the template directly was wrong twice: `dream.md` needs a `skill_creator_path` and
#: silently rendered an empty one, and `evaluator.md` holds **two** prompts in one file selected
#: by `part`, so a bare render returned an empty string -- a panel offering to replace nothing.
WORKSPACE_PROMPTS: dict[str, tuple[str, str]] = {
    "dream": (
        "How memory is organised: which file each learned fact goes to, and how hard stale "
        "content is pruned.",
        "",
    ),
    "evaluator": (
        "Whether a heartbeat result is worth notifying you about.",
        "It must still tell the model to call the `evaluate_notification` tool. Without that the "
        "gate fails closed and stays silent.",
    ),
}


def packaged_prompt(name: str) -> str:
    """The built-in prompt, taken from the helper its own consumer uses.

    One source rather than two: whatever the panel shows as "the default" has to be the text the
    running code would use, and each consumer renders its template with its own variables.
    """
    if name == "dream":
        from nanoinfra.agent.memory import MemoryStore

        return MemoryStore.default_dream_prompt()
    if name == "evaluator":
        from nanoinfra.utils.evaluator import default_evaluator_prompt

        return default_evaluator_prompt()
    raise KeyError(name)


def workspace_prompts_payload(workspace: Path) -> dict[str, Any]:
    """``GET /api/settings/workspace-prompts`` -- both prompts, with the text in force.

    ``source`` is ``"workspace"`` when this workspace has replaced the prompt and ``"platform"``
    when it has not. The packaged text travels either way, because "restore the default" has to
    put back something the panel can show first.
    """
    return {
        "prompts": [
            {
                "name": name,
                "controls": controls,
                "requirement": requirement,
                "text": override if override is not None else packaged,
                "platform_text": packaged,
                "source": "workspace" if override is not None else "platform",
                "path": str(workspace_prompt_file(workspace, name)),
                "max_chars": WORKSPACE_PROMPT_MAX_CHARS,
            }
            for name, (controls, requirement) in WORKSPACE_PROMPTS.items()
            for packaged in [packaged_prompt(name)]
            for override in [
                load_workspace_prompt_override(workspace_prompt_file(workspace, name))[0]
            ]
        ]
    }


def save_workspace_prompt(workspace: Path, name: str, text: str) -> dict[str, Any]:
    """Write one override, or delete it when the text is the packaged one.

    Deleting rather than storing an identical copy is the whole point: a file that happens to
    match today's packaged prompt still *wins* tomorrow, so keeping it would freeze this
    workspace's memory behaviour at this version without saying so.
    """
    if name not in WORKSPACE_PROMPTS:
        raise KeyError(name)
    packaged = packaged_prompt(name)
    path = workspace_prompt_file(workspace, name)
    body = text.strip()
    if not body or body == packaged.strip():
        path.unlink(missing_ok=True)
        return workspace_prompts_payload(workspace)
    if len(body) > WORKSPACE_PROMPT_MAX_CHARS:
        raise ValueError(
            f"a workspace prompt is capped at {WORKSPACE_PROMPT_MAX_CHARS:,} characters"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n", encoding="utf-8")
    return workspace_prompts_payload(workspace)


__all__ = ["WORKSPACE_PROMPTS", "save_workspace_prompt", "workspace_prompts_payload"]
