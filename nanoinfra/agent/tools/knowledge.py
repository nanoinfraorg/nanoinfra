"""Search the operator's own documents, and cite what you used (#239, #242).

This tool is the *only* way knowledge reaches the model. Nothing is injected into a prompt
section, so a knowledge base costs nothing on a turn that does not ask for it -- which is the
lesson of #203, #204 and #210 applied to content that grows with the operator's own writing.

The freshness pass lives here rather than in the automation because of who is waiting: somebody
who just saved a runbook asks about it immediately, and a cron reindex would tell them it does
not exist. The pass costs a directory walk and stats, and opens no index when nothing changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanoinfra.agent.tools.base import Tool, ToolResult, tool_parameters
from nanoinfra.agent.tools.context import ToolContext
from nanoinfra.agent.tools.schema import StringSchema, tool_parameters_schema
from nanoinfra.config.schema import KnowledgeConfig
from nanoinfra.knowledge import (
    HYBRID_INSTALL_HINT,
    ReindexReport,
    SearchHit,
    hybrid_available,
    knowledge_root,
    refresh_workspace,
    search_workspace,
)
from nanoinfra.security.workspace_access import current_tool_workspace

#: How many refusals from the freshness pass are named in one answer. A document the operator is
#: asking about may be the one that was skipped, and that is worth knowing at the point of the
#: question -- but not at the cost of a page of it.
_MAX_REPORTED_SKIPS = 3

_CITATION_CONTRACT = (
    "Cite the path#section of every fragment you use. An answer with no citation is not a claim."
)


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema(
            "Words that would appear in the document. This is lexical search: it matches terms, "
            "not meaning, so 'CrashLoopBackOff' finds a runbook that 'pod will not start' does "
            "not.",
            min_length=1,
            max_length=500,
        ),
        required=["query"],
    )
)
class KnowledgeSearchTool(Tool):
    """Retrieve fragments of the workspace knowledge base, with citations."""

    capability_class = "read"

    def __init__(self, workspace: Path, config: KnowledgeConfig) -> None:
        self._workspace = workspace
        self._config = config

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        knowledge = getattr(ctx.config, "knowledge", None)
        return bool(ctx.workspace) and isinstance(knowledge, KnowledgeConfig) and knowledge.enabled

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        if not ctx.workspace:
            raise RuntimeError("KnowledgeSearchTool requires a workspace")
        return cls(workspace=Path(ctx.workspace), config=ctx.config.knowledge)

    @property
    def name(self) -> str:
        return "knowledge_search"

    @property
    def description(self) -> str:
        return (
            "Search the operator's own documents in the workspace knowledge folder. Use it before "
            "answering anything about this deployment's runbooks, conventions, hosts or "
            "procedures -- those answers are written down here, not in your training data. "
            "Returns fragments, each with a path#section citation, a snippet and a score. "
            f"{_CITATION_CONTRACT} If nothing matches, say so instead of answering from memory."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        query: str,
        **_extra: Any,
    ) -> ToolResult:
        text = query.strip()
        if not text:
            return ToolResult.error("Error: query must not be empty")

        # Hybrid without its dependency is refused rather than quietly answered with BM25F.
        # Half-working is the failure mode the proposal names: an operator who chose hybrid and
        # got lexical results would believe the vectors were consulted.
        if self._config.mode == "hybrid" and not hybrid_available():
            return ToolResult.error(
                "Knowledge is configured for hybrid mode, which needs semlix's semantic extra. "
                f"Install it with `{HYBRID_INSTALL_HINT}` and restart, or set "
                "tools.knowledge.mode to \"lexical\"."
            )

        workspace = self._active_workspace()
        root = knowledge_root(workspace)
        if not root.is_dir():
            return ToolResult(
                f"There is no knowledge base yet. Drop documents in {root} "
                "(folders and subfolders are fine) and they become searchable."
            )

        report = await refresh_workspace(workspace=workspace, config=self._config)
        hits = await search_workspace(workspace=workspace, config=self._config, query=text)
        if not hits:
            return ToolResult(self._render_empty(text, report))
        return ToolResult(self._render_hits(text, hits, report))

    def _active_workspace(self) -> Path:
        """The workspace of this turn, which is not always the agent's default one.

        A WebUI client can be scoped to a project, and each identity has its own workspace. The
        knowledge base follows that scope for the same reason the file tools do: it is the
        operator's own content, and it belongs to the workspace it was dropped in.

        The ``knowledge-index`` automation only walks the agent's own workspace, so a scoped
        workspace is maintained entirely by the freshness pass below. That is enough to keep it
        correct -- the pass collects deletions too -- and it costs the scoped turn the walk.
        """
        scoped = current_tool_workspace(self._workspace).project_path
        return Path(scoped) if scoped is not None else self._workspace

    def _render_hits(self, query: str, hits: list[SearchHit], report: ReindexReport) -> str:
        lines = [
            f"{len(hits)} fragment(s) for {query!r} from the knowledge base.",
            _CITATION_CONTRACT,
            "",
        ]
        for position, hit in enumerate(hits, start=1):
            lines.append(f"{position}. {hit.citation}  (score {hit.score:.2f})")
            lines.append(f"   {hit.snippet}")
        notes = self._render_notes(report)
        if notes:
            lines.extend(("", notes))
        return "\n".join(lines)

    def _render_empty(self, query: str, report: ReindexReport) -> str:
        lines = [
            f"No fragment matches {query!r}. {report.documents} document(s) are indexed.",
            "This is lexical search: try the words the document itself would use. Do not answer "
            "from memory as though the knowledge base had said it.",
        ]
        notes = self._render_notes(report)
        if notes:
            lines.append(notes)
        return "\n".join(lines)

    @staticmethod
    def _render_notes(report: ReindexReport) -> str:
        """Name what the last pass refused, so a missing document has a reason (#244)."""
        problems = [skip.describe() for skip in report.skipped_details] + list(report.errors)
        if not problems:
            return ""
        shown = problems[:_MAX_REPORTED_SKIPS]
        suffix = "" if len(problems) <= _MAX_REPORTED_SKIPS else f" (+{len(problems) - len(shown)} more)"
        return "Not indexed: " + "; ".join(shown) + suffix
