"""What the index believes is on disk, and what the last pass did (#242, #243).

The freshness pass needs an answer to "has this file changed since it was indexed" that costs a
stat rather than a read, so the manifest holds ``(mtime_ns, size)`` per file. It also carries the
last run's counts, because the WebUI panel has to be able to say *documents indexed, skipped,
failed, and when* -- and because a skipped file that nobody records is a silent drop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from nanoinfra.utils.run_records import write_run_record

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest"


@dataclass(frozen=True)
class FileRecord:
    mtime_ns: int
    size: int
    chunks: int


@dataclass(frozen=True)
class SkipRecord:
    """One refusal, kept so the panel can name it and the next pass can honour it."""

    rel: str
    reason: str
    detail: str = ""
    #: Zero unless the refusal came from reading the file. See :class:`nanoinfra.knowledge.walk.Skip`.
    mtime_ns: int = 0
    size: int = 0


@dataclass(frozen=True)
class RunRecord:
    """One indexing pass, as the settings panel reports it."""

    #: ``automation`` for the cron pass, ``search`` for the freshness pass the tool runs.
    trigger: str
    finished_at_ms: int
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_ms: int = 0


@dataclass
class Manifest:
    """The mutable state of one knowledge index."""

    version: int = MANIFEST_VERSION
    #: The mode the index was *built* in. A change means the stored index answers a different
    #: question than the config asks, so the next pass rebuilds instead of searching for vectors
    #: that were never written.
    mode: str = "lexical"
    files: dict[str, FileRecord] = field(default_factory=dict[str, FileRecord])
    skipped: list[SkipRecord] = field(default_factory=list[SkipRecord])
    errors: list[str] = field(default_factory=list[str])
    last_run: RunRecord | None = None

    @property
    def indexed_bytes(self) -> int:
        return sum(record.size for record in self.files.values())

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "files": {
                rel: {"mtime_ns": rec.mtime_ns, "size": rec.size, "chunks": rec.chunks}
                for rel, rec in sorted(self.files.items())
            },
            "skipped": [
                {
                    "path": rec.rel,
                    "reason": rec.reason,
                    "detail": rec.detail,
                    "mtime_ns": rec.mtime_ns,
                    "size": rec.size,
                }
                for rec in self.skipped
            ],
            "errors": list(self.errors),
            "last_run": None
            if self.last_run is None
            else {
                "trigger": self.last_run.trigger,
                "finished_at_ms": self.last_run.finished_at_ms,
                "added": self.last_run.added,
                "updated": self.last_run.updated,
                "removed": self.last_run.removed,
                "skipped": self.last_run.skipped,
                "errors": self.last_run.errors,
                "duration_ms": self.last_run.duration_ms,
            },
        }


def manifest_path(index_dir: Path) -> Path:
    return index_dir / f"{MANIFEST_NAME}.json"


def load_manifest(index_dir: Path) -> Manifest:
    """Read the manifest, or return an empty one.

    A manifest that cannot be parsed is treated as absent rather than as an error: the index is
    rebuildable from the documents, and refusing to search because a JSON file was truncated by a
    crash would turn a recoverable state into an outage.
    """
    try:
        raw = cast(object, json.loads(manifest_path(index_dir).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Manifest()
    if not isinstance(raw, dict):
        return Manifest()
    payload = cast(dict[str, Any], raw)
    if payload.get("version") != MANIFEST_VERSION:
        return Manifest()

    files: dict[str, FileRecord] = {}
    raw_files = payload.get("files")
    if isinstance(raw_files, dict):
        for rel, value in cast(dict[str, Any], raw_files).items():
            if not isinstance(value, dict):
                continue
            entry = cast(dict[str, Any], value)
            files[str(rel)] = FileRecord(
                mtime_ns=_as_int(entry.get("mtime_ns")),
                size=_as_int(entry.get("size")),
                chunks=_as_int(entry.get("chunks")),
            )

    skipped: list[SkipRecord] = []
    raw_skipped = payload.get("skipped")
    if isinstance(raw_skipped, list):
        for value in cast(list[Any], raw_skipped):
            if not isinstance(value, dict):
                continue
            entry = cast(dict[str, Any], value)
            skipped.append(
                SkipRecord(
                    rel=str(entry.get("path") or ""),
                    reason=str(entry.get("reason") or ""),
                    detail=str(entry.get("detail") or ""),
                    mtime_ns=_as_int(entry.get("mtime_ns")),
                    size=_as_int(entry.get("size")),
                )
            )

    errors_raw = payload.get("errors")
    errors = (
        [str(item) for item in cast(list[Any], errors_raw)] if isinstance(errors_raw, list) else []
    )

    return Manifest(
        version=MANIFEST_VERSION,
        mode=str(payload.get("mode") or "lexical"),
        files=files,
        skipped=skipped,
        errors=errors,
        last_run=_load_run(payload.get("last_run")),
    )


def save_manifest(index_dir: Path, manifest: Manifest) -> None:
    """Write the manifest atomically.

    Through ``write_run_record`` rather than a private copy of the same code: it already does the
    temp-file-then-rename with both fsyncs, and this file is exactly what it is for -- a durable
    record of what a run did.
    """
    write_run_record(index_dir, MANIFEST_NAME, manifest.to_payload())


def _load_run(value: object) -> RunRecord | None:
    if not isinstance(value, dict):
        return None
    entry = cast(dict[str, Any], value)
    return RunRecord(
        trigger=str(entry.get("trigger") or ""),
        finished_at_ms=_as_int(entry.get("finished_at_ms")),
        added=_as_int(entry.get("added")),
        updated=_as_int(entry.get("updated")),
        removed=_as_int(entry.get("removed")),
        skipped=_as_int(entry.get("skipped")),
        errors=_as_int(entry.get("errors")),
        duration_ms=_as_int(entry.get("duration_ms")),
    )


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
