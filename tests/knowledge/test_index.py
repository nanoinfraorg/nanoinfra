"""The knowledge index: what it indexes, what it refuses, and what it reports (#241, #244).

The refusals are the tests worth having. An index that finds a runbook is easy to believe; an
index that quietly indexed ``.env`` or quietly dropped the largest document is the failure an
operator only discovers from the wrong answer.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanoinfra.config.schema import KnowledgeConfig
from nanoinfra.knowledge import (
    index_dir,
    knowledge_root,
    reindex_workspace,
    search_workspace,
    status_payload,
)
from nanoinfra.knowledge.manifest import load_manifest
from nanoinfra.knowledge.service import run_pass
from nanoinfra.knowledge.walk import REASON_NOT_TEXT, REASON_OUTSIDE_WORKSPACE, REASON_TOO_LARGE

RUNBOOK = (
    "# Pods\n\nOverview of the pod runbook.\n\n"
    "## Restart the pod\n\nRun kubectl rollout restart deployment/api and watch the rollout.\n"
)


def _config(**overrides: object) -> KnowledgeConfig:
    return KnowledgeConfig(enabled=True, **overrides)  # pyright: ignore[reportArgumentType]


def _root(workspace: Path) -> Path:
    root = knowledge_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    return root


async def test_a_document_in_a_subfolder_is_searchable_after_the_automation_runs(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    (root / "runbooks" / "k8s").mkdir(parents=True)
    (root / "runbooks" / "k8s" / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    config = _config()

    report = await reindex_workspace(workspace=tmp_path, config=config)
    hits = await search_workspace(workspace=tmp_path, config=config, query="rollout restart")

    assert report.added == 1
    assert [hit.citation for hit in hits] == ["runbooks/k8s/pods.md#restart-the-pod"]


async def test_every_result_carries_a_path_section_and_a_snippet(tmp_path: Path) -> None:
    """A result with no source is a bug, and so is one with no snippet."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    (root / "plain.txt").write_text("kubectl rollout restart is also written here\n", "utf-8")
    config = _config()
    await reindex_workspace(workspace=tmp_path, config=config)

    hits = await search_workspace(workspace=tmp_path, config=config, query="rollout")

    assert len(hits) == 2
    for hit in hits:
        assert hit.path
        assert hit.section
        assert hit.citation == f"{hit.path}#{hit.section}"
        assert hit.snippet.strip()
        assert hit.score > 0


async def test_a_deleted_document_stops_appearing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    doc = root / "pods.md"
    doc.write_text(RUNBOOK, encoding="utf-8")
    config = _config()
    await reindex_workspace(workspace=tmp_path, config=config)
    assert await search_workspace(workspace=tmp_path, config=config, query="rollout")

    doc.unlink()
    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert report.removed == 1
    assert await search_workspace(workspace=tmp_path, config=config, query="rollout") == []
    assert load_manifest(index_dir(root)).files == {}


async def test_secrets_are_not_indexed(tmp_path: Path) -> None:
    """``.env`` and ``*.pem`` by name, and anything under ``secrets/`` by folder."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    (root / ".env").write_text("DATABASE_URL=postgres://user:hunter2@localhost/db\n", "utf-8")
    (root / ".env.production").write_text("TOKEN=hunter2\n", encoding="utf-8")
    (root / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\nhunter2\n", encoding="utf-8")
    (root / "id_rsa").write_text("hunter2\n", encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "notes.md").write_text("# Vault\n\nhunter2 is the password.\n", "utf-8")
    (root / "nested" / "secrets").mkdir(parents=True)
    (root / "nested" / "secrets" / "keys.md").write_text("# Keys\n\nhunter2\n", encoding="utf-8")
    config = _config()

    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert report.added == 1
    assert sorted(load_manifest(index_dir(root)).files) == ["pods.md"]
    # The exclusions are silent by design: they are policy, not a problem to report.
    assert report.skipped == 0
    assert await search_workspace(workspace=tmp_path, config=config, query="hunter2") == []


async def test_a_symlink_out_of_the_workspace_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "leaked.md").write_text("# Leaked\n\ncanary-outside-the-boundary\n", "utf-8")
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    (root / "link.md").symlink_to(outside / "leaked.md")
    (root / "tree").symlink_to(outside, target_is_directory=True)
    config = _config()

    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert sorted(load_manifest(index_dir(root)).files) == ["pods.md"]
    assert [skip.reason for skip in report.skipped_details] == [REASON_OUTSIDE_WORKSPACE]
    hits = await search_workspace(workspace=tmp_path, config=config, query="canary")
    assert hits == []


async def test_a_symlink_inside_the_folder_is_still_indexed(tmp_path: Path) -> None:
    """The boundary is the refusal, not the symlink itself."""
    root = _root(tmp_path)
    (root / "real.md").write_text("# Real\n\ninside-the-boundary marker\n", encoding="utf-8")
    (root / "alias.md").symlink_to(root / "real.md")
    config = _config()

    await reindex_workspace(workspace=tmp_path, config=config)

    citations = {
        hit.citation
        for hit in await search_workspace(workspace=tmp_path, config=config, query="marker")
    }
    assert citations == {"real.md#real", "alias.md#real"}


async def test_an_oversized_file_is_skipped_and_reported(tmp_path: Path) -> None:
    """Skipped *and* reported. A silent drop is indistinguishable from a document that
    was indexed and never matched."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    (root / "huge.md").write_text("# Huge\n\n" + ("filler " * 5000), encoding="utf-8")
    config = _config(max_file_bytes=2048)

    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert report.skipped == 1
    assert report.skipped_details[0].rel == "huge.md"
    assert report.skipped_details[0].reason == REASON_TOO_LARGE
    assert "huge.md: larger than the per-file limit" in report.summary()
    assert await search_workspace(workspace=tmp_path, config=config, query="filler") == []
    # And it survives the run, so the settings panel can say what the last pass refused.
    size = (root / "huge.md").stat().st_size
    recorded = status_payload(tmp_path, config)["skipped"]
    assert recorded == [
        {"path": "huge.md", "reason": REASON_TOO_LARGE, "detail": f"{size} bytes"}
    ]


async def test_the_total_cap_stops_the_index_and_names_what_it_dropped(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for name in ("a.md", "b.md", "c.md"):
        (root / name).write_text(f"# {name}\n\n" + ("payload " * 200), encoding="utf-8")
    config = _config(max_total_bytes=2048)

    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert report.added == 1
    assert report.skipped == 2
    assert {skip.reason for skip in report.skipped_details} == {"total_budget"}


async def test_a_binary_file_is_skipped_rather_than_turned_into_terms(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "diagram.md").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    config = _config()

    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert report.added == 0
    assert [skip.reason for skip in report.skipped_details] == [REASON_NOT_TEXT]


def test_a_refused_binary_is_remembered_rather_than_re_read_every_search(
    tmp_path: Path,
) -> None:
    """Otherwise one stray image makes every search open the index writer."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    (root / "diagram.md").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    config = _config()
    run_pass(root, config, "automation")
    manifest_file = index_dir(root) / "manifest.json"
    before = manifest_file.stat().st_mtime_ns

    second = run_pass(root, config, "search")

    assert second.changed is False
    # Still reported, so the panel does not lose the reason on the next pass.
    assert [skip.reason for skip in second.skipped_details] == [REASON_NOT_TEXT]
    assert manifest_file.stat().st_mtime_ns == before


def test_a_refused_binary_that_becomes_text_is_indexed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    doc = root / "notes.md"
    doc.write_bytes(b"\x00\x00not text yet")
    config = _config()
    assert run_pass(root, config, "automation").added == 0

    doc.write_text("# Notes\n\nnow it is a document about failover\n", encoding="utf-8")
    report = run_pass(root, config, "automation")

    assert report.added == 1
    assert report.skipped == 0


async def test_the_index_does_not_index_itself(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    config = _config()

    await reindex_workspace(workspace=tmp_path, config=config)
    second = await reindex_workspace(workspace=tmp_path, config=config)

    assert index_dir(root).is_dir()
    assert second.added == 0
    assert second.updated == 0
    assert sorted(load_manifest(index_dir(root)).files) == ["pods.md"]


async def test_an_edited_document_is_reindexed_not_duplicated(tmp_path: Path) -> None:
    root = _root(tmp_path)
    doc = root / "pods.md"
    doc.write_text("# Pods\n\nthe first wording\n", encoding="utf-8")
    config = _config()
    await reindex_workspace(workspace=tmp_path, config=config)

    doc.write_text("# Pods\n\nthe second wording\n", encoding="utf-8")
    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert (report.added, report.updated) == (0, 1)
    assert await search_workspace(workspace=tmp_path, config=config, query="first") == []
    assert len(await search_workspace(workspace=tmp_path, config=config, query="second")) == 1


async def test_switching_the_mode_rebuilds_rather_than_searching_absent_vectors(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    lexical = _config()
    await reindex_workspace(workspace=tmp_path, config=lexical)
    assert load_manifest(index_dir(root)).mode == "lexical"

    hybrid = _config(mode="hybrid")
    switched = await reindex_workspace(workspace=tmp_path, config=hybrid)

    assert switched.rebuilt is True
    assert switched.added == 1
    assert load_manifest(index_dir(root)).mode == "hybrid"
    # And the pass after the switch is a no-op again, so a mode change costs one rebuild.
    assert (await reindex_workspace(workspace=tmp_path, config=hybrid)).rebuilt is False


async def test_an_unreadable_document_is_a_failure_not_a_skip_and_is_retried(
    tmp_path: Path,
) -> None:
    """*Skipped* is policy refusing a file; *failed* is the file refusing to be read."""
    root = _root(tmp_path)
    doc = root / "locked.md"
    doc.write_text(RUNBOOK, encoding="utf-8")
    config = _config()
    doc.chmod(0o000)
    try:
        report = await reindex_workspace(workspace=tmp_path, config=config)
    finally:
        doc.chmod(0o644)

    assert report.errors == ["locked.md: could not be read (Permission denied)"]
    assert report.skipped == 0
    # Not recorded as indexed, so the next pass tries again rather than believing it is done.
    assert load_manifest(index_dir(root)).files == {}
    assert (await reindex_workspace(workspace=tmp_path, config=config)).added == 1


async def test_a_workspace_with_no_knowledge_folder_writes_nothing(tmp_path: Path) -> None:
    """The index lives inside the folder, so an absent folder must not create one."""
    config = _config()

    report = await reindex_workspace(workspace=tmp_path, config=config)

    assert (report.added, report.removed, report.documents) == (0, 0, 0)
    assert list(tmp_path.iterdir()) == []


def test_a_pass_with_nothing_to_do_writes_nothing_from_a_search(tmp_path: Path) -> None:
    """The freshness pass runs on every search, so a no-op must not cost a disk write."""
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    config = _config()
    run_pass(root, config, "automation")
    manifest_file = index_dir(root) / "manifest.json"
    before = manifest_file.stat().st_mtime_ns

    report = run_pass(root, config, "search")

    assert report.changed is False
    assert report.documents == 1
    assert manifest_file.stat().st_mtime_ns == before


def test_a_truncated_manifest_is_treated_as_absent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "pods.md").write_text(RUNBOOK, encoding="utf-8")
    config = _config()
    run_pass(root, config, "automation")
    (index_dir(root) / "manifest.json").write_text('{"version": 1, "files":', encoding="utf-8")

    report = run_pass(root, config, "automation")

    assert report.added == 1
    assert json.loads((index_dir(root) / "manifest.json").read_text(encoding="utf-8"))["files"]


async def test_the_report_carries_what_the_cron_job_logs(tmp_path: Path) -> None:
    """The contract with ``gateway_runtime``: it reads these five counts and calls summary().

    The system job lives in a file this package does not own, so the shape it depends on is
    asserted here rather than left to a KeyError at 03:00.
    """
    report = await reindex_workspace(workspace=tmp_path, config=_config())

    for name in ("added", "updated", "removed", "skipped", "duration_ms"):
        assert isinstance(getattr(report, name), int)
    assert isinstance(report.errors, list)
    assert isinstance(report.summary(), str)
