"""The catalog publishes three kinds and the client has to route them (#207).

The skills-server has published `skill`, `agent-plugin` and `connector` since its v0.3.0. This
client read none of that, so every row was a skill: a published connector would have been unpacked
into `<workspace>/skills`, where it is text nothing will ever activate, and the operator would have
seen an install that reported success and did nothing.

Where a package lands is a security decision -- a connector is requests made with a live credential,
a plugin is code the executor runs -- so the kind is read from the catalog and never accepted from
the caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from nanoinfra.webui import skills_marketplace as marketplace
from nanoinfra.webui.skills_marketplace import (
    KIND_AGENT_PLUGIN,
    KIND_CONNECTOR,
    KIND_SKILL,
    SkillsMarketplaceError,
    install_marketplace_skill,
    search_marketplace_skills,
)

BASE_URL = "https://catalog.invalid"

GRANTS = {
    "kind": "connector",
    "operations": [
        {"name": "list_contacts", "class": "read", "method": "GET", "path": "/v1/contacts"},
    ],
    "classes": ["read"],
    "hosts": ["api.acme.example"],
    "scopes": ["crm.read"],
}


def _archive(name: str) -> bytes:
    """A minimal package archive: one directory holding one file."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("connector.json", '{"name": "' + name + '"}')
        archive.writestr("SKILL.md", "# " + name + "\n")
    return buffer.getvalue()


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake catalog whose kind, grants and archive each test decides."""
    state: dict[str, Any] = {"kind": KIND_SKILL, "grants": None, "queries": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/search":
            state["queries"].append(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "query": request.url.params.get("q", ""),
                    "skills": [{
                        "skill_id": "acme-crm",
                        "display_name": "Acme CRM",
                        "current_version": 1,
                        "downloads": 3,
                        "kind": state["kind"],
                    }],
                },
            )
        if path.startswith("/api/v1/skills/") and path.endswith("/download"):
            return httpx.Response(200, content=_archive("acme-crm"))
        if path.startswith("/api/v1/skills/"):
            payload: dict[str, Any] = {"skill_id": "acme-crm", "kind": state["kind"]}
            if state["grants"] is not None:
                payload["grants"] = state["grants"]
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    def client(base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(marketplace, "_nanoinfra_client", client)
    return state


# --- the listing ---------------------------------------------------------------------------


async def test_a_row_says_what_kind_it_is(catalog: dict[str, Any], tmp_path: Path) -> None:
    catalog["kind"] = KIND_CONNECTOR

    payload = await search_marketplace_skills(
        "acme", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert payload["skills"][0]["kind"] == KIND_CONNECTOR


async def test_a_row_with_no_kind_is_a_skill(catalog: dict[str, Any], tmp_path: Path) -> None:
    """Those rows predate the kinds, and that is what they are."""
    catalog["kind"] = None

    payload = await search_marketplace_skills(
        "acme", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert payload["skills"][0]["kind"] == KIND_SKILL


async def test_asking_for_one_kind_asks_the_catalog(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """Narrowed by the catalog, so a page showing connectors does not download the rest of it."""
    await search_marketplace_skills(
        "acme", tmp_path, nanoinfra_base_url=BASE_URL, kind=KIND_CONNECTOR
    )

    assert catalog["queries"][-1].get("kind") == KIND_CONNECTOR


async def test_an_unknown_kind_is_refused_rather_than_forwarded(tmp_path: Path) -> None:
    with pytest.raises(SkillsMarketplaceError, match="unknown package kind"):
        await search_marketplace_skills("acme", tmp_path, kind="malware")


async def test_a_connector_already_on_disk_reads_as_installed(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """It was reported as *not installed* forever, because `installed` asked the skills loader --
    and a connector is not a skill. Every listing then offered to install it again."""
    catalog["kind"] = KIND_CONNECTOR
    (tmp_path / "connector-packages" / "acme-crm").mkdir(parents=True)

    payload = await search_marketplace_skills(
        "acme", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert payload["skills"][0]["installed"] is True


async def test_a_connector_listing_carries_what_each_one_grants(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """The grants are the content of a connector list: "what would this be allowed to do" is the
    question an operator answers before installing, so the row has to carry the answer."""
    catalog["kind"] = KIND_CONNECTOR
    catalog["grants"] = GRANTS

    payload = await search_marketplace_skills(
        "acme", tmp_path, nanoinfra_base_url=BASE_URL, kind=KIND_CONNECTOR
    )

    assert payload["skills"][0]["grants"]["hosts"] == ["api.acme.example"]


async def test_a_row_whose_grants_cannot_be_read_carries_no_grants_key(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """Absent and "grants nothing" are different statements, and the panel renders them
    differently -- one as a warning, the other as an empty table."""
    catalog["kind"] = KIND_CONNECTOR
    catalog["grants"] = None

    payload = await search_marketplace_skills(
        "acme", tmp_path, nanoinfra_base_url=BASE_URL, kind=KIND_CONNECTOR
    )

    assert "grants" not in payload["skills"][0]


async def test_a_skill_listing_does_not_pay_for_grants(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """One detail request per row is worth it for a list whose point is the grants, and waste for
    a list of skills, which grant prompt text."""
    catalog["kind"] = KIND_SKILL
    catalog["grants"] = GRANTS

    payload = await search_marketplace_skills(
        "acme", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert "grants" not in payload["skills"][0]


# --- where it lands ------------------------------------------------------------------------


async def test_a_connector_is_installed_where_the_connector_registry_looks(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    catalog["kind"] = KIND_CONNECTOR

    await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert (tmp_path / "connector-packages" / "acme-crm" / "connector.json").is_file()
    assert not (tmp_path / "skills" / "acme-crm").exists()


async def test_a_plugin_is_installed_where_the_executor_looks(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    catalog["kind"] = KIND_AGENT_PLUGIN

    await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert (tmp_path / "plugins" / "acme-crm").is_dir()


async def test_a_skill_still_lands_in_skills(catalog: dict[str, Any], tmp_path: Path) -> None:
    """The path that already worked has to keep working, unchanged."""
    catalog["kind"] = KIND_SKILL

    await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert (tmp_path / "skills" / "acme-crm" / "SKILL.md").is_file()


async def test_the_kind_is_read_from_the_catalog_not_from_the_row(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """The search row and the detail disagree here on purpose. Where a package lands is a security
    decision, so it is decided by the endpoint that owns the archive."""
    catalog["kind"] = KIND_CONNECTOR

    result = await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert result["kind"] == KIND_CONNECTOR


# --- installed is not working --------------------------------------------------------------


async def test_installing_a_connector_says_what_is_still_missing(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """A connector on disk does nothing: it has no credential and is not in `connectors.active`.
    Reporting plain success would be reporting an install that looked finished and did nothing."""
    catalog["kind"] = KIND_CONNECTOR

    result = await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert "credential" in result["next_step"]
    assert "connectors.active" in result["next_step"]


async def test_installing_a_plugin_names_the_key_that_activates_it(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    catalog["kind"] = KIND_AGENT_PLUGIN

    result = await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert "tools.agentPlugins" in result["next_step"]


async def test_the_grants_come_back_with_the_install(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    """An install that cannot say what it allowed is one nobody can review afterwards."""
    catalog["kind"] = KIND_CONNECTOR
    catalog["grants"] = GRANTS

    result = await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert result["grants"]["hosts"] == ["api.acme.example"]


async def test_a_skill_reports_no_next_step_because_there_is_none(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    catalog["kind"] = KIND_SKILL

    result = await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert "next_step" not in result


async def test_installing_a_connector_twice_is_not_an_error(
    catalog: dict[str, Any], tmp_path: Path
) -> None:
    catalog["kind"] = KIND_CONNECTOR
    (tmp_path / "connector-packages" / "acme-crm").mkdir(parents=True)

    result = await install_marketplace_skill(
        "", "acme-crm", tmp_path, provider="nanoinfra", nanoinfra_base_url=BASE_URL
    )

    assert result["already_installed"] is True
