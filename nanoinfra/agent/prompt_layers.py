"""Where a prompt section's text comes from, and in what order (#262).

Three layers, narrowest last:

1. **The packaged template** -- nanoinfra's own text, shipped in `templates/agent/`.
2. **`<workspace>/prompts/<name>.md`** -- the deployment's own, for every agent in that
   workspace. This is not a new mechanism: `utils/workspace_prompts.py` already carries it, and
   Dream and the heartbeat evaluator have used it since before named agents existed. Empty or
   deleted falls back to the layer below, which is the idiom that file's README documents.
3. **`agents.named[x].promptSections[section]`** -- one agent only.

The reason this module exists rather than the lookup living inline: the panel that offers to
replace a section has to show *the text that is actually in force*, and for a workspace with its
own `prompts/tool_contract.md` the packaged template is not that text. A panel that showed the
packaged one would be telling an operator their default is something it is not, and "restore the
default" would then overwrite their own file's text with ours.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanoinfra.utils.prompt_templates import render_template
from nanoinfra.utils.workspace_prompts import (
    load_workspace_prompt_override,
    workspace_prompt_file,
)

#: The prompt sections that are prose rather than assembled data, mapped to the packaged template
#: and to the `prompts/<name>.md` a workspace may override it with.
#:
#: The workspace name has no `agent/` prefix and no `.md`: `prompts/` is flat and already holds
#: `dream.md` and `evaluator.md`, so these join them as siblings rather than in a subdirectory
#: nothing else uses.
PROSE_SECTIONS: dict[str, tuple[str, str]] = {
    "Runtime": ("agent/identity.md", "identity"),
    "Safety notes": ("agent/safety_notes.md", "safety_notes"),
    "Tool usage notes": ("agent/tool_contract.md", "tool_contract"),
}


def workspace_section_override(workspace: Path, section: str) -> str | None:
    """The workspace's own text for *section*, or ``None`` when it does not define one.

    ``None`` for an empty file as well as a missing one: emptying the file is how that mechanism
    spells *go back to the default*, and the README in every workspace says so.
    """
    entry = PROSE_SECTIONS.get(section)
    if entry is None:
        return None
    text, _original = load_workspace_prompt_override(
        workspace_prompt_file(workspace, entry[1])
    )
    return text


def section_default(workspace: Path | None, section: str) -> tuple[str | None, str]:
    """The default for *section* before any per-agent override, and which layer it came from.

    Returns ``(text, source)`` where source is ``"workspace"`` or ``"platform"``, and
    ``(None, "assembled")`` for a section a turn builds rather than a file -- the memory block,
    the history, the advertised lists. ``None`` is deliberate there: an empty string would render
    as a blank editor and invite an operator to think the section was empty.

    The packaged text for a templated section is its **source**, placeholders intact, because that
    is what a replacement has to start from. Rendering it would bake one turn's paths into text
    somebody then edits.
    """
    entry = PROSE_SECTIONS.get(section)
    if entry is None:
        return None, "assembled"
    if workspace is not None:
        override = workspace_section_override(workspace, section)
        if override is not None:
            return override, "workspace"
    template, _name = entry
    return template_source(template), "platform"


def template_source(template: str) -> str:
    """A packaged template as written, placeholders intact."""
    path = Path(__file__).resolve().parents[1] / "templates" / template
    return path.read_text(encoding="utf-8")


def rendered_section(workspace: Path | None, section: str, **variables: Any) -> str:
    """The text a turn should use for *section*: the workspace's if it has one, else ours.

    A workspace override is taken verbatim rather than rendered, matching how `dream.md` and
    `evaluator.md` are treated: the file is the prompt, not a template, and running an operator's
    prose through a template engine would turn a stray brace into an error they never asked for.
    """
    if workspace is not None:
        override = workspace_section_override(workspace, section)
        if override is not None:
            return override
    entry = PROSE_SECTIONS.get(section)
    if entry is None:
        raise KeyError(f"{section!r} is not a prose section")
    return render_template(entry[0], **variables)
