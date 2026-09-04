"""A chunk is only useful if its anchor points somewhere a reader can open (#239)."""

from __future__ import annotations

from nanoinfra.knowledge.chunking import MAX_CHUNK_CHARS, chunk_document, slugify


def test_markdown_splits_on_headings_and_anchors_are_github_shaped() -> None:
    chunks = chunk_document(
        "# Pods\n\nOverview.\n\n## Restart the pod\n\nRun kubectl rollout restart.\n",
        suffix=".md",
    )

    assert [chunk.section for chunk in chunks] == ["pods", "restart-the-pod"]
    assert [chunk.heading for chunk in chunks] == ["Pods", "Restart the pod"]
    assert "kubectl rollout restart" in chunks[1].text


def test_a_hash_inside_a_fence_is_not_a_heading() -> None:
    """A shell comment in a code sample used to take the rest of the document with it."""
    chunks = chunk_document(
        "## Setup\n\n```bash\n# install the agent\napt install nanoinfra\n```\n\nDone.\n",
        suffix=".md",
    )

    assert [chunk.section for chunk in chunks] == ["setup"]
    assert "apt install nanoinfra" in chunks[0].text


def test_a_repeated_heading_gets_the_renderer_s_suffix() -> None:
    chunks = chunk_document("## Notes\n\nfirst\n\n## Notes\n\nsecond\n", suffix=".md")

    assert [chunk.section for chunk in chunks] == ["notes", "notes-2"]


def test_text_before_the_first_heading_is_cited_by_line_range() -> None:
    chunks = chunk_document("Standing note.\n\n# Later\n\nbody\n", suffix=".md")

    assert chunks[0].section == "L1-L2"
    assert chunks[1].section == "later"


def test_a_file_with_no_headings_is_cited_by_line_range() -> None:
    chunks = chunk_document("alpha\nbeta\ngamma\n", suffix=".txt")

    assert [chunk.section for chunk in chunks] == ["L1-L3"]


def test_a_long_section_splits_into_parts_that_share_the_anchor() -> None:
    """The citation names a section. A reader does not care which half of it scored."""
    body = "\n".join(f"line {index} about failover" for index in range(400))
    chunks = chunk_document(f"## Failover\n\n{body}\n", suffix=".md")

    assert len(chunks) > 1
    assert {chunk.section for chunk in chunks} == {"failover"}
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_every_chunk_has_a_non_empty_section() -> None:
    """A hit with no source is a bug, so an anchorless chunk must not exist to begin with."""
    for text, suffix in (
        ("# a\n\nb\n", ".md"),
        ("no headings here\n", ".txt"),
        ("#### deep\n\nbody\n", ".markdown"),
        ("## ???\n\nbody\n", ".md"),
    ):
        chunks = chunk_document(text, suffix=suffix)
        assert chunks
        assert all(chunk.section for chunk in chunks)


def test_slugify_drops_punctuation_and_keeps_words() -> None:
    assert slugify("Restart the pod (safely)") == "restart-the-pod-safely"
    assert slugify("  Spaces   collapse  ") == "spaces-collapse"
    assert slugify("###") == ""
