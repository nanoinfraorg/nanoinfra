"""`knowledge_search`: freshness, citations, and the refusal to half-work (#239, #242, #244)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nanoinfra.agent.tools.context import ToolContext
from nanoinfra.agent.tools.knowledge import KnowledgeSearchTool
from nanoinfra.agent.tools.loader import ToolLoader
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.config.schema import Config
from nanoinfra.knowledge import knowledge_root, reindex_workspace, search_workspace

RUNBOOK = (
    "# Pods\n\nOverview.\n\n"
    "## Restart the pod\n\nRun kubectl rollout restart deployment/api.\n"
)


def _context(workspace: Path, *, enabled: bool = True, mode: str = "lexical") -> ToolContext:
    config = Config().tools
    config.knowledge.enabled = enabled
    config.knowledge.mode = mode  # pyright: ignore[reportAttributeAccessIssue]
    return ToolContext(config=config, workspace=str(workspace))


def _tool(workspace: Path, **kwargs: object) -> KnowledgeSearchTool:
    ctx = _context(workspace, **kwargs)  # pyright: ignore[reportArgumentType]
    tool = KnowledgeSearchTool.create(ctx)
    assert isinstance(tool, KnowledgeSearchTool)
    return tool


def _root(workspace: Path) -> Path:
    root = knowledge_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_the_tool_is_read_only_and_declares_the_read_class() -> None:
    tool = KnowledgeSearchTool(Path("/nonexistent"), Config().tools.knowledge)

    assert tool.name == "knowledge_search"
    assert tool.capability_class == "read"
    assert tool.read_only is True


def test_the_description_carries_the_citation_contract() -> None:
    """The contract has to reach the model even on a turn that loads no skill."""
    description = KnowledgeSearchTool(Path("/x"), Config().tools.knowledge).description

    assert "path#section" in description
    assert "An answer with no citation is not a claim." in description


def test_the_tool_is_registered_only_when_knowledge_is_enabled(tmp_path: Path) -> None:
    off = ToolRegistry()
    ToolLoader().load(_context(tmp_path, enabled=False), off)
    on = ToolRegistry()
    ToolLoader().load(_context(tmp_path, enabled=True), on)

    assert off.has("knowledge_search") is False
    assert on.has("knowledge_search") is True


async def test_a_document_dropped_a_second_ago_is_searchable_before_the_automation_runs(
    tmp_path: Path,
) -> None:
    """The whole reason the tool owns freshness: the person who saved it asks immediately."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    tool = _tool(tmp_path)
    await reindex_workspace(workspace=tmp_path, config=tool._config)  # pyright: ignore[reportPrivateUsage]

    (root / "dns.md").write_text("# DNS\n\nFlush with resolvectl flush-caches.\n", "utf-8")
    # No automation pass in between. A plain search does not see it yet...
    assert await search_workspace(
        workspace=tmp_path, config=tool._config, query="resolvectl"  # pyright: ignore[reportPrivateUsage]
    ) == []

    result = await tool.execute(query="resolvectl")

    assert "dns.md#dns" in result
    assert "resolvectl flush-caches" in result


async def test_a_deleted_document_stops_appearing_through_the_tool(tmp_path: Path) -> None:
    root = _root(tmp_path)
    doc = root / "pods.md"
    doc.write_text(RUNBOOK, encoding="utf-8")
    tool = _tool(tmp_path)
    assert "pods.md#restart-the-pod" in await tool.execute(query="rollout restart")

    doc.unlink()
    result = await tool.execute(query="rollout restart")

    assert "pods.md" not in result
    assert "No fragment matches" in result


async def test_every_rendered_result_carries_a_citation_and_a_snippet(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    tool = _tool(tmp_path)

    result = await tool.execute(query="kubectl rollout")

    lines = [line for line in result.splitlines() if line.startswith("1. ")]
    # Shape, not the number: the score is BM25's and a fixed value here would be a test of
    # semlix's arithmetic rather than of the contract this answer has to carry.
    assert re.fullmatch(r"1\. pods\.md#restart-the-pod  \(score \d+\.\d\d\)", lines[0])
    snippet = result.splitlines()[result.splitlines().index(lines[0]) + 1]
    assert snippet.startswith("   ")
    assert "kubectl rollout restart deployment/api" in snippet
    assert "An answer with no citation is not a claim." in result


async def test_an_empty_result_refuses_to_invite_a_guess(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    tool = _tool(tmp_path)

    result = await tool.execute(query="quantum tunnelling")

    assert result.is_error is False
    assert "1 document(s) are indexed" in result
    assert "Do not answer from memory" in result


async def test_an_absent_folder_says_where_to_put_documents(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.execute(query="anything")

    assert "There is no knowledge base yet" in result
    assert str(knowledge_root(tmp_path)) in result


async def test_hybrid_without_the_extra_reports_the_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It does not half-work: an operator who chose hybrid must not be served lexical hits."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    monkeypatch.setattr("nanoinfra.agent.tools.knowledge.hybrid_available", lambda: False)
    tool = _tool(tmp_path, mode="hybrid")

    result = await tool.execute(query="rollout restart")

    assert result.is_error is True
    assert "pip install 'semlix[semantic]'" in result
    assert "pods.md" not in result


async def test_hybrid_with_the_extra_present_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    monkeypatch.setattr("nanoinfra.agent.tools.knowledge.hybrid_available", lambda: True)
    tool = _tool(tmp_path, mode="hybrid")

    result = await tool.execute(query="rollout restart")

    assert result.is_error is False
    assert "pods.md#restart-the-pod" in result


async def test_a_skipped_document_is_named_in_the_answer(tmp_path: Path) -> None:
    """The document the operator is asking about may be the one that was refused."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    (root / "huge.md").write_text("# Huge\n\n" + ("filler " * 5000), encoding="utf-8")
    tool = _tool(tmp_path)
    tool._config.max_file_bytes = 2048  # pyright: ignore[reportPrivateUsage]

    result = await tool.execute(query="rollout restart")

    assert "Not indexed: huge.md: larger than the per-file limit" in result


async def test_an_empty_query_is_an_error(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.execute(query="   ")

    assert result.is_error is True
