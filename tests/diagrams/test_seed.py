from __future__ import annotations

from pathlib import Path

from nanoinfra.diagrams.seed import EXAMPLE_DIAGRAM, seed_example_diagram_if_new_workspace
from nanoinfra.diagrams.store import DiagramStore


def test_seeds_example_into_brand_new_workspace(tmp_path: Path):
    store = DiagramStore(tmp_path)

    seed_example_diagram_if_new_workspace(store)

    summaries = store.list_diagrams()
    assert len(summaries) == 1
    assert summaries[0].name == EXAMPLE_DIAGRAM["name"]

    saved = store.get(summaries[0].id)
    assert {n.id for n in saved.nodes} == {n["id"] for n in EXAMPLE_DIAGRAM["nodes"]}
    assert {e.id for e in saved.edges} == {e["id"] for e in EXAMPLE_DIAGRAM["edges"]}


def test_does_not_reseed_once_workspace_has_ever_had_diagrams(tmp_path: Path):
    store = DiagramStore(tmp_path)
    seed_example_diagram_if_new_workspace(store)
    only = store.list_diagrams()[0]
    store.delete(only.id)
    assert store.list_diagrams() == []

    seed_example_diagram_if_new_workspace(store)

    assert store.list_diagrams() == []


def test_does_not_seed_workspace_that_already_has_a_diagram(tmp_path: Path):
    store = DiagramStore(tmp_path)
    store.create({"name": "User's own diagram", "nodes": [], "edges": []})

    seed_example_diagram_if_new_workspace(store)

    summaries = store.list_diagrams()
    assert len(summaries) == 1
    assert summaries[0].name == "User's own diagram"


def test_example_diagram_uses_only_catalog_component_pairs(tmp_path: Path):
    """Every componentTypeId/providerId the seed uses must exist in the real
    catalog -- silently saving an unknown pair would render as a fallback/
    unknown component in the WebUI, not a broken diagram, so nothing else
    catches this by construction."""
    from nanoinfra.diagrams.catalog import load_catalog

    catalog_types = load_catalog(tmp_path, skills_workspace_path=tmp_path)
    valid_pairs = {
        (component_type.id, provider.id)
        for component_type in catalog_types
        for provider in component_type.providers
    }
    for node in EXAMPLE_DIAGRAM["nodes"]:
        pair = (node["data"]["componentTypeId"], node["data"]["providerId"])
        assert pair in valid_pairs, f"seed node {node['id']!r} uses unknown pair {pair}"
