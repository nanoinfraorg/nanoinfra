"""What the prompt is made of, recorded while it is assembled (#203).

The question these exist for was asked about a real turn: *31K input tokens for a "hola"*. Answering
it took an SSH session and a hand-written SQLite query, and the answer -- 7.4K of system prompt and
~23K of tool schemas -- was nowhere in the product. So the manifest's job is not to be clever; it is
to be present, to add up, and to carry no content.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nanoinfra.agent.context import ContextBuilder
from nanoinfra.agent.prompt_manifest import PromptManifest
from nanoinfra.utils.helpers import count_text_tokens


@pytest.fixture
def workspace() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "AGENTS.md").write_text(
        "# Project\n\n" + ("Project instructions. " * 300), encoding="utf-8"
    )
    (root / "memory").mkdir()
    (root / "memory" / "MEMORY.md").write_text(
        "# Memory\n\n" + ("One remembered fact. " * 500), encoding="utf-8"
    )
    return root


# --- it adds up ---------------------------------------------------------------------------


def test_the_sections_account_for_the_whole_prompt(workspace: Path) -> None:
    """A breakdown that does not sum to the thing it breaks down is a breakdown nobody can use."""
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(channel="websocket")

    whole = count_text_tokens(prompt)
    total = builder.last_manifest.total_tokens()
    # The gap is the `\n\n---\n\n` between sections, which belongs to no section.
    assert 0 <= whole - total <= len(builder.last_manifest.sections) * 3


def test_every_section_of_a_real_prompt_is_named(workspace: Path) -> None:
    builder = ContextBuilder(workspace)

    builder.build_system_prompt(channel="websocket", session_summary="An archived summary.")

    names = [section.name for section in builder.last_manifest.sections]
    assert "Runtime" in names
    assert "Bootstrap files" in names
    assert "Memory" in names
    assert "Session summary" in names
    # Order is preserved, because prefix caching reuses a *prefix*: what comes before the volatile
    # part is the question a reader of this is most likely to be asking.
    assert names.index("Runtime") == 0
    assert names.index("Session summary") == len(names) - 1


def test_the_largest_section_is_findable(workspace: Path) -> None:
    """The whole point: one look says which section is paying for the turn."""
    builder = ContextBuilder(workspace)

    builder.build_system_prompt(channel="websocket")

    largest = max(builder.last_manifest.sections, key=lambda section: section.tokens)
    assert largest.name in {"Memory", "Bootstrap files", "Tool usage notes"}


def test_a_prompt_with_no_optional_sections_still_produces_a_manifest(tmp_path: Path) -> None:
    builder = ContextBuilder(tmp_path)

    builder.build_system_prompt(channel="websocket")

    assert builder.last_manifest.sections
    assert builder.last_manifest.total_tokens() > 0


def test_building_twice_replaces_rather_than_accumulates(workspace: Path) -> None:
    builder = ContextBuilder(workspace)

    builder.build_system_prompt(channel="websocket")
    first = len(builder.last_manifest.sections)
    builder.build_system_prompt(channel="websocket")

    assert len(builder.last_manifest.sections) == first


# --- it carries no content ----------------------------------------------------------------


def test_the_manifest_holds_no_prompt_text(workspace: Path) -> None:
    """A manifest is displayed in a browser and persisted with the turn. A prompt holds MEMORY.md,
    AGENTS.md and the conversation, so one that carried its text would be a second copy of the
    conversation living somewhere nobody expects one."""
    builder = ContextBuilder(workspace)

    builder.build_system_prompt(channel="websocket")

    serialised = repr(builder.last_manifest.as_dict())
    assert "One remembered fact" not in serialised
    assert "Project instructions" not in serialised
    for section in builder.last_manifest.sections:
        assert section.chars > 0
        assert section.tokens > 0


def test_the_payload_shape_is_names_numbers_and_groups(workspace: Path) -> None:
    builder = ContextBuilder(workspace)
    builder.build_system_prompt(channel="websocket")

    payload = builder.last_manifest.as_dict()

    assert set(payload) == {"sections", "groups", "total_tokens", "measured"}
    assert payload["measured"] is False, "the provider does not itemise; these are our estimates"
    assert set(payload["sections"][0]) <= {"name", "chars", "tokens", "group", "detail", "items"}


# --- the manifest itself ------------------------------------------------------------------


def test_groups_total_separately() -> None:
    manifest = PromptManifest()
    manifest.add("AGENTS.md", "x" * 400)
    manifest.add_counted("github-nanoinfraorg", chars=32_000, tokens=8_200, group="tools", items=15)
    manifest.add_counted("Messages", chars=900, tokens=220, group="messages", items=4)

    groups = manifest.group_totals()

    assert groups["tools"] == 8_200
    assert groups["messages"] == 220
    assert manifest.total_tokens() == sum(groups.values())


def test_an_empty_section_is_not_listed() -> None:
    manifest = PromptManifest()

    manifest.add("Session summary", "")

    assert manifest.sections == []


def test_the_summary_line_names_the_groups() -> None:
    manifest = PromptManifest()
    manifest.add_counted("tools", chars=1, tokens=23_000, group="tools")
    manifest.add_counted("system", chars=1, tokens=7_400, group="system")

    line = manifest.summary_line()

    assert "30,400" in line
    assert "tools 23,000" in line


# --- the tool schemas, which are the larger half ------------------------------------------


def test_the_schema_breakdown_attributes_by_source_not_by_name() -> None:
    """`mcp_<server>_<tool>` is sanitised and both halves may hold underscores, so the name cannot
    be split reliably. Every tool says where it came from instead."""
    from nanoinfra.agent.tools.loader import ToolLoader
    from nanoinfra.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for cls in ToolLoader().discover():
        try:
            registry.register(cls())
        except Exception:
            continue

    rows = registry.schema_breakdown()

    assert rows, "a registry with tools has a breakdown"
    assert all(row["source"] == "builtin" for row in rows)
    assert sum(row["items"] for row in rows) == len(registry.get_definitions())
    # Largest first, because the row worth reading is the one paying for the turn.
    assert rows == sorted(rows, key=lambda row: -row["tokens"])
