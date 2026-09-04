"""A knowledge base must cost nothing on a turn that does not ask for it (#237, #203).

This is the test that keeps the design honest. The whole reason knowledge arrives by tool call is
that the alternative -- a section of the stable prompt -- is the *31K for a hola* problem at a
larger scale, and worse than the tool schemas were, because it grows with the operator's own
writing. So: the prompt is measured section by section with no knowledge base, and again with an
indexed one, and nothing may move.
"""

from __future__ import annotations

from pathlib import Path

from nanoinfra.agent.context import ContextBuilder
from nanoinfra.config.schema import KnowledgeConfig
from nanoinfra.knowledge import knowledge_root
from nanoinfra.knowledge.service import run_pass

CANARY = "CANARY-4718-only-in-the-knowledge-base"


def _sections(builder: ContextBuilder) -> dict[str, int]:
    prompt = builder.build_system_prompt()
    manifest = builder.last_manifest
    assert manifest is not None
    sections = {section.name: section.chars for section in manifest.sections}
    sections["__prompt__"] = len(prompt)
    return sections


def test_no_prompt_section_grows_when_a_knowledge_base_exists(tmp_path: Path) -> None:
    builder = ContextBuilder(workspace=tmp_path)
    _sections(builder)
    # Measured twice before the change, so a difference afterwards is the knowledge base and not
    # a first build creating a memory file.
    baseline = _sections(builder)
    assert _sections(builder) == baseline

    root = knowledge_root(tmp_path)
    (root / "runbooks").mkdir(parents=True)
    (root / "runbooks" / "pods.md").write_text(
        f"# Pods\n\n{CANARY}\n\n## Restart\n\nRun kubectl rollout restart.\n", encoding="utf-8"
    )
    report = run_pass(root, KnowledgeConfig(enabled=True), "automation")
    assert report.added == 1

    assert _sections(builder) == baseline
    assert CANARY not in builder.build_system_prompt()


def test_the_skill_is_summarised_not_loaded(tmp_path: Path) -> None:
    """The skill is prompt content, so it is one catalogue line until a turn invokes it."""
    builder = ContextBuilder(workspace=tmp_path)

    prompt = builder.build_system_prompt()

    assert "**knowledge**" in prompt
    # The body of the skill -- the citation contract, the search advice -- stays on disk.
    assert "An answer with no citation is not a claim." not in prompt
    loaded = builder.skills.load_skills_for_context(["knowledge"])
    assert "An answer with no citation is not a claim." in loaded
