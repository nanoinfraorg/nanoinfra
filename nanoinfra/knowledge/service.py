"""Build, refresh and search the workspace knowledge base (#241, #242).

The work is split the way the proposal splits it, and the split is the whole design:

* the **automation** owns the full pass -- walk the tree, drop entries whose files are gone,
  rebuild what changed, write a run record with counts;
* the **tool** owns freshness -- before searching it compares what is on disk against the
  manifest and indexes what changed, so a runbook saved seconds ago is findable.

Both call the same pass. The only differences are that a search-time pass never writes the
manifest when nothing changed (a disk write per search would be a cost paid by every turn) and
that it does not overwrite the automation's run record with a no-op.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from dataclasses import dataclass, field
from pathlib import Path

from nanoinfra.config.schema import KnowledgeConfig
from nanoinfra.knowledge.chunking import chunk_document
from nanoinfra.knowledge.manifest import (
    FileRecord,
    Manifest,
    RunRecord,
    SkipRecord,
    load_manifest,
    save_manifest,
)
from nanoinfra.knowledge.store import KnowledgeIndexBusyError, LexicalIndex, SearchHit
from nanoinfra.knowledge.walk import (
    INDEX_DIR_NAME,
    REASON_NOT_TEXT,
    REASON_UNREADABLE,
    SNIFF_BYTES,
    Candidate,
    Skip,
    iter_candidates,
    looks_like_text,
)

#: Where the operator drops documents. Folders and subfolders, whatever they like.
KNOWLEDGE_DIR_NAME = "knowledge"

TRIGGER_AUTOMATION = "automation"
TRIGGER_SEARCH = "search"

#: What to install for the hybrid mode. `semantic` is the numpy-only extra; `semantic-full` also
#: pulls sentence-transformers and a model download, which is a decision an operator makes
#: deliberately rather than one a settings toggle makes for them.
HYBRID_INSTALL_HINT = "pip install 'semlix[semantic]'"


def knowledge_root(workspace: Path) -> Path:
    """The knowledge folder of one workspace."""
    return Path(workspace) / KNOWLEDGE_DIR_NAME


def index_dir(root: Path) -> Path:
    """The index, beside the documents so a restored workspace restores its index too."""
    return root / INDEX_DIR_NAME


def hybrid_available() -> bool:
    """Whether the hybrid mode's dependency is installed.

    ``numpy`` is the whole of semlix's ``semantic`` extra, and ``import semlix.semantic`` fails
    with ModuleNotFoundError without it. Probed by spec rather than imported, because this is
    asked while rendering a settings panel and importing numpy to answer a greyed-out toggle
    would be the more expensive half of the check.
    """
    return importlib.util.find_spec("numpy") is not None


@dataclass
class ReindexReport:
    """What one pass did. Also what the settings panel and the cron log report."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list[str])
    #: Every refusal with its reason, so an oversized file is skipped *and reported* (#244).
    skipped_details: list[Skip] = field(default_factory=list[Skip])
    #: True when the whole index was rebuilt, which a mode change forces.
    rebuilt: bool = False
    documents: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed or self.rebuilt)

    def summary(self) -> str:
        head = (
            f"knowledge: {self.added} added, {self.updated} updated, "
            f"{self.removed} removed, {self.skipped} skipped "
            f"({self.documents} documents indexed) in {self.duration_ms}ms"
        )
        lines = [head]
        # The reasons travel with the counts. A run record saying "2 skipped" and nothing else
        # leaves an operator with a knowledge base that is quietly missing its largest runbook.
        lines.extend(f"  skipped {skip.describe()}" for skip in self.skipped_details)
        lines.extend(f"  failed {error}" for error in self.errors)
        return "\n".join(lines)


async def reindex_workspace(*, workspace: Path, config: KnowledgeConfig) -> ReindexReport:
    """The full pass the ``knowledge-index`` system job runs.

    Called by ``gateway_runtime`` with the agent's workspace. Off-thread because it walks a
    directory tree and writes an index, and the gateway's loop is also serving chat.
    """
    return await asyncio.to_thread(
        run_pass, knowledge_root(workspace), config, TRIGGER_AUTOMATION
    )


async def refresh_workspace(*, workspace: Path, config: KnowledgeConfig) -> ReindexReport:
    """The freshness pass the search tool runs before it answers (#242)."""
    return await asyncio.to_thread(run_pass, knowledge_root(workspace), config, TRIGGER_SEARCH)


async def search_workspace(
    *, workspace: Path, config: KnowledgeConfig, query: str, limit: int | None = None
) -> list[SearchHit]:
    """Search the knowledge base. Does not refresh -- the caller owns that decision."""
    root = knowledge_root(workspace)
    return await asyncio.to_thread(_search_sync, root, query, limit or config.max_results)


def run_pass(root: Path, config: KnowledgeConfig, trigger: str) -> ReindexReport:
    """Index what changed under *root*, drop what is gone, and report what was refused."""
    started = time.perf_counter()
    directory = index_dir(root)
    manifest = load_manifest(directory)
    report = ReindexReport()

    # A mode change means the stored index answers a different question than the config asks, so
    # the next pass rebuilds rather than searching for vectors nobody wrote.
    rebuild = manifest.mode != config.mode
    report.rebuilt = rebuild

    if not root.is_dir():
        # No folder is not an error: the operator has not created one yet. Nothing is written
        # either -- the index lives inside the folder, so there is nothing to empty, and creating
        # it here would leave an empty `.index` in every workspace that never wanted one.
        report.duration_ms = _elapsed_ms(started)
        return report

    candidates, skips = iter_candidates(
        root,
        exclude=list(config.exclude),
        max_file_bytes=config.max_file_bytes,
        max_total_bytes=config.max_total_bytes,
    )
    # Two vocabularies, deliberately: *skipped* is policy refusing a file, *failed* is the file
    # refusing to be read. The settings panel reports them apart because they need different
    # actions -- one is a config decision, the other is a permission to fix.
    report.skipped_details = [skip for skip in skips if skip.reason != REASON_UNREADABLE]
    report.skipped = len(report.skipped_details)
    report.errors = [
        skip.describe() for skip in skips if skip.reason == REASON_UNREADABLE
    ]

    known = {} if rebuild else dict(manifest.files)
    current = {candidate.rel: candidate for candidate in candidates}
    # A refusal decided from the file's bytes is remembered, so a stray image in the folder does
    # not make every search open the index to re-read it and refuse it again. A refusal from the
    # filesystem is not remembered: a chmod does not change the mtime, and an operator who fixes
    # a permission must not have to touch the file as well.
    carried = _carried_refusals(manifest.skipped if not rebuild else [], current)
    stale = [
        candidate
        for candidate in candidates
        if candidate.rel not in carried and _needs_index(known.get(candidate.rel), candidate)
    ]
    gone = [rel for rel in known if rel not in current]
    report.skipped_details.extend(carried.values())
    report.skipped += len(carried)

    if not stale and not gone and not rebuild:
        # The common case, and the reason the freshness pass is affordable on every search: no
        # index is opened and no file is written.
        report.documents = len(known)
        report.duration_ms = _elapsed_ms(started)
        if trigger == TRIGGER_AUTOMATION:
            _record_run(directory, config, manifest, report, trigger, report.skipped_details)
        return report

    files = dict(known)
    try:
        index = LexicalIndex.open(directory, recreate=rebuild)
        with index.batch() as batch:
            for rel in gone:
                batch.remove(rel)
                files.pop(rel, None)
                report.removed += 1
            for candidate in stale:
                text, refusal = _read_text(candidate)
                if refusal is not None:
                    # Dropped from the manifest rather than kept: the next pass must retry it,
                    # and a record with no fragments behind it would say it was indexed.
                    if refusal.reason == REASON_UNREADABLE:
                        report.errors.append(refusal.describe())
                    else:
                        report.skipped_details.append(refusal)
                        report.skipped += 1
                    if files.pop(candidate.rel, None) is not None:
                        batch.remove(candidate.rel)
                    continue
                chunks = chunk_document(text or "", suffix=candidate.path.suffix)
                batch.replace(candidate.rel, chunks)
                if candidate.rel in known:
                    report.updated += 1
                else:
                    report.added += 1
                files[candidate.rel] = FileRecord(
                    mtime_ns=candidate.mtime_ns, size=candidate.size, chunks=len(chunks)
                )
    except KnowledgeIndexBusyError as exc:
        # Reported, never raised past here: one contended pass must not take down a chat turn or
        # a cron tick, and the next pass indexes the same work.
        report.errors.append(str(exc))
        report.documents = len(known)
        report.duration_ms = _elapsed_ms(started)
        return report

    manifest.files = files
    report.documents = len(files)
    report.duration_ms = _elapsed_ms(started)
    _record_run(directory, config, manifest, report, trigger, report.skipped_details)
    return report


def status_payload(workspace: Path, config: KnowledgeConfig) -> dict[str, object]:
    """What the settings panel shows: the mode, the caps, and what the last run did (#243)."""
    root = knowledge_root(workspace)
    manifest = load_manifest(index_dir(root))
    hybrid = hybrid_available()
    last = manifest.last_run
    return {
        "enabled": config.enabled,
        "mode": config.mode,
        "indexed_mode": manifest.mode,
        "reindex_interval_s": config.reindex_interval_s,
        "exclude": list(config.exclude),
        "max_file_bytes": config.max_file_bytes,
        "max_total_bytes": config.max_total_bytes,
        "max_results": config.max_results,
        "path": str(root),
        "exists": root.is_dir(),
        "documents": len(manifest.files),
        "fragments": sum(record.chunks for record in manifest.files.values()),
        "indexed_bytes": manifest.indexed_bytes,
        "hybrid_available": hybrid,
        "hybrid_install_hint": None if hybrid else HYBRID_INSTALL_HINT,
        "skipped": [
            {"path": record.rel, "reason": record.reason, "detail": record.detail}
            for record in manifest.skipped
        ],
        "errors": list(manifest.errors),
        "last_run": None
        if last is None
        else {
            "trigger": last.trigger,
            "finished_at_ms": last.finished_at_ms,
            "added": last.added,
            "updated": last.updated,
            "removed": last.removed,
            "skipped": last.skipped,
            "errors": last.errors,
            "duration_ms": last.duration_ms,
        },
    }


def _search_sync(root: Path, query: str, limit: int) -> list[SearchHit]:
    directory = index_dir(root)
    if not directory.is_dir():
        return []
    return LexicalIndex.open(directory).search(query, limit)


def _record_run(
    directory: Path,
    config: KnowledgeConfig,
    manifest: Manifest,
    report: ReindexReport,
    trigger: str,
    skips: list[Skip],
) -> None:
    manifest.mode = config.mode
    manifest.skipped = [
        SkipRecord(
            rel=skip.rel,
            reason=skip.reason,
            detail=skip.detail,
            mtime_ns=skip.mtime_ns,
            size=skip.size,
        )
        for skip in skips
    ]
    manifest.errors = list(report.errors)
    manifest.last_run = RunRecord(
        trigger=trigger,
        finished_at_ms=int(time.time() * 1000),
        added=report.added,
        updated=report.updated,
        removed=report.removed,
        skipped=report.skipped,
        errors=len(report.errors),
        duration_ms=report.duration_ms,
    )
    save_manifest(directory, manifest)


def _carried_refusals(
    recorded: list[SkipRecord], current: dict[str, Candidate]
) -> dict[str, Skip]:
    """Refusals from an earlier pass that still describe the file on disk."""
    carried: dict[str, Skip] = {}
    for record in recorded:
        if record.reason != REASON_NOT_TEXT or not record.mtime_ns:
            continue
        candidate = current.get(record.rel)
        if candidate is None:
            continue
        if candidate.mtime_ns == record.mtime_ns and candidate.size == record.size:
            carried[record.rel] = Skip(
                rel=record.rel,
                reason=record.reason,
                detail=record.detail,
                mtime_ns=record.mtime_ns,
                size=record.size,
            )
    return carried


def _needs_index(known: FileRecord | None, candidate: Candidate) -> bool:
    """Whether a file changed since it was indexed.

    Size as well as mtime: a same-second edit that keeps the length is rare, and an edit that
    changes the length with a preserved mtime is what a restore from backup looks like.
    """
    if known is None:
        return True
    return known.mtime_ns != candidate.mtime_ns or known.size != candidate.size


def _read_text(candidate: Candidate) -> tuple[str | None, Skip | None]:
    """Read one document, or say why it was refused.

    The text check lives here rather than in the walk because this is where the bytes already
    are. Asked of the bytes and never of the extension: a ``.md`` holding a JPEG is a JPEG, and
    its bytes would otherwise become index terms.
    """
    try:
        raw = candidate.path.read_bytes()
    except OSError as exc:
        return None, Skip(candidate.rel, REASON_UNREADABLE, exc.strerror or "")
    if not looks_like_text(raw[:SNIFF_BYTES]):
        return None, Skip(
            candidate.rel, REASON_NOT_TEXT, mtime_ns=candidate.mtime_ns, size=candidate.size
        )
    # errors="replace" rather than a refusal: one bad byte in a 200-line runbook must not cost
    # the whole document, and the replacement character is not a term anybody searches for.
    return raw.decode("utf-8", errors="replace"), None


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
