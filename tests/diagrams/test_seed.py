from __future__ import annotations

from pathlib import Path

from nanoinfra.diagrams.seed import EXAMPLE_DIAGRAMS, seed_example_diagram_if_new_workspace
from nanoinfra.diagrams.store import DiagramStore


def test_seeds_every_example_into_brand_new_workspace(tmp_path: Path):
    store = DiagramStore(tmp_path)

    seed_example_diagram_if_new_workspace(store)

    summaries = store.list_diagrams()
    assert len(summaries) == len(EXAMPLE_DIAGRAMS)
    assert {s.name for s in summaries} == {d["name"] for d in EXAMPLE_DIAGRAMS}

    by_name = {d["name"]: d for d in EXAMPLE_DIAGRAMS}
    for summary in summaries:
        saved = store.get(summary.id)
        expected = by_name[summary.name]
        assert {n.id for n in saved.nodes} == {n["id"] for n in expected["nodes"]}
        assert {e.id for e in saved.edges} == {e["id"] for e in expected["edges"]}


def test_does_not_reseed_once_workspace_has_ever_had_diagrams(tmp_path: Path):
    store = DiagramStore(tmp_path)
    seed_example_diagram_if_new_workspace(store)
    for summary in store.list_diagrams():
        store.delete(summary.id)
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


def test_example_diagrams_use_only_catalog_component_pairs(tmp_path: Path):
    """Every componentTypeId/providerId any seed example uses must exist in
    the real catalog -- silently saving an unknown pair would render as a
    fallback/unknown component in the WebUI, not a broken diagram, so
    nothing else catches this by construction."""
    from nanoinfra.diagrams.catalog import load_catalog

    catalog_types = load_catalog(tmp_path, skills_workspace_path=tmp_path)
    valid_pairs = {
        (component_type.id, provider.id)
        for component_type in catalog_types
        for provider in component_type.providers
    }
    for diagram in EXAMPLE_DIAGRAMS:
        for node in diagram["nodes"]:
            pair = (node["data"]["componentTypeId"], node["data"]["providerId"])
            assert pair in valid_pairs, (
                f"{diagram['name']!r} node {node['id']!r} uses unknown pair {pair}"
            )


def test_example_diagram_edges_reference_real_node_ids() -> None:
    """A typo'd source/target id would be silently dropped by
    normalize_diagram (dangling edges are dropped, not rejected) -- catch it
    here instead of the edge just quietly vanishing on first load."""
    for diagram in EXAMPLE_DIAGRAMS:
        node_ids = {n["id"] for n in diagram["nodes"]}
        for edge in diagram["edges"]:
            assert edge["source"] in node_ids, f"{diagram['name']!r} edge {edge['id']!r} has unknown source"
            assert edge["target"] in node_ids, f"{diagram['name']!r} edge {edge['id']!r} has unknown target"


def test_seeds_a_workspace_that_only_has_a_catalog_override(tmp_path: Path):
    """`diagrams/` is also the parent of `diagrams/catalog/` -- #103.

    The product tells the operator to create a workspace catalog override, and creating it used to
    disable the example diagrams with no message at all.
    """
    override = tmp_path / "diagrams" / "catalog" / "my-components.json"
    override.parent.mkdir(parents=True)
    override.write_text('{"componentTypes": []}', encoding="utf-8")
    store = DiagramStore(tmp_path)

    seed_example_diagram_if_new_workspace(store)

    assert store.list_diagrams(), "an operator following the documented path received nothing"
    assert override.exists(), "and their override is untouched"
