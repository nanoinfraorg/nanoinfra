from __future__ import annotations

import json
from pathlib import Path

from nanoinfra.diagrams.catalog import load_catalog


def _write_catalog_file(directory: Path, name: str, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def test_builtin_catalog_loads_and_includes_group_type(tmp_path: Path):
    catalog = load_catalog(tmp_path)
    ids = {t.id for t in catalog}
    assert "dns" in ids
    assert "web_server" in ids

    group = next(t for t in catalog if t.is_group)
    assert group.id == "__group__"


def test_builtin_dns_has_no_powerdns_by_default(tmp_path: Path):
    catalog = load_catalog(tmp_path)
    dns = next(t for t in catalog if t.id == "dns")
    assert "powerdns" not in {p.id for p in dns.providers}


def test_workspace_provider_addition_appends_to_existing_type(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "powerdns.json",
        {
            "componentTypeId": "dns",
            "providers": [
                {
                    "id": "powerdns",
                    "label": "PowerDNS",
                    "kind": "api",
                    "integration": {"type": "api"},
                    "fields": [{"key": "apiKey", "label": "API Key", "kind": "secret"}],
                }
            ],
        },
    )

    catalog = load_catalog(tmp_path)
    dns = next(t for t in catalog if t.id == "dns")
    provider_ids = {p.id for p in dns.providers}
    # The addition is appended, the built-in providers are untouched.
    assert provider_ids == {"cloudflare", "route53", "powerdns"}

    powerdns = next(p for p in dns.providers if p.id == "powerdns")
    assert powerdns.label == "PowerDNS"
    assert powerdns.integration is not None
    assert powerdns.integration.type == "api"


def test_workspace_provider_addition_overrides_existing_provider_by_id(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "override.json",
        {
            "componentTypeId": "cache",
            "providers": [
                {"id": "redis", "label": "Redis (custom build)", "kind": "docker", "fields": []},
            ],
        },
    )

    catalog = load_catalog(tmp_path)
    cache = next(t for t in catalog if t.id == "cache")
    assert len(cache.providers) == 1
    assert cache.providers[0].label == "Redis (custom build)"


def test_workspace_provider_addition_for_unknown_type_is_skipped(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "ghost.json",
        {"componentTypeId": "does-not-exist", "providers": [{"id": "x", "label": "X"}]},
    )

    # Should not raise, and every built-in type stays untouched.
    catalog = load_catalog(tmp_path)
    assert "does-not-exist" not in {t.id for t in catalog}


def test_workspace_full_type_shadows_builtin_by_id(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "dns.json",
        {
            "id": "dns",
            "label": "DNS (custom)",
            "category": "Edge",
            "iconKey": "dns",
            "providers": [{"id": "only-mine", "label": "Only mine", "kind": "api"}],
        },
    )

    catalog = load_catalog(tmp_path)
    dns_types = [t for t in catalog if t.id == "dns"]
    assert len(dns_types) == 1
    dns = dns_types[0]
    assert dns.label == "DNS (custom)"
    assert {p.id for p in dns.providers} == {"only-mine"}


def test_workspace_full_type_adds_new_category(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "monitoring.json",
        {
            "id": "monitoring",
            "label": "Monitoring",
            "category": "Observability",
            "iconKey": "monitoring",
            "providers": [{"id": "prometheus", "label": "Prometheus", "kind": "docker"}],
        },
    )

    catalog = load_catalog(tmp_path)
    monitoring = next(t for t in catalog if t.id == "monitoring")
    assert monitoring.category == "Observability"
    assert {p.id for p in monitoring.providers} == {"prometheus"}


def test_malformed_workspace_file_is_skipped(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "broken.json").write_text("not json{", encoding="utf-8")

    # Should not raise; built-in catalog still loads.
    catalog = load_catalog(tmp_path)
    assert any(t.id == "dns" for t in catalog)


def test_skill_integration_reports_not_installed_when_no_skills_dir(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "with_skill.json",
        {
            "componentTypeId": "dns",
            "providers": [
                {
                    "id": "powerdns",
                    "label": "PowerDNS",
                    "kind": "api",
                    "integration": {"type": "skill", "skillName": "powerdns"},
                }
            ],
        },
    )

    skills_workspace = tmp_path / "skills-workspace"
    skills_workspace.mkdir()
    catalog = load_catalog(tmp_path, skills_workspace_path=skills_workspace, disabled_skills=set())

    dns = next(t for t in catalog if t.id == "dns")
    powerdns = next(p for p in dns.providers if p.id == "powerdns")
    assert powerdns.integration is not None
    assert powerdns.integration.skill_installed is False
    assert powerdns.integration.skill_enabled is False


def test_skill_integration_reports_installed_and_enabled(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "with_skill.json",
        {
            "componentTypeId": "dns",
            "providers": [
                {
                    "id": "powerdns",
                    "label": "PowerDNS",
                    "kind": "api",
                    "integration": {"type": "skill", "skillName": "powerdns"},
                }
            ],
        },
    )

    skills_workspace = tmp_path / "skills-workspace"
    skill_dir = skills_workspace / "skills" / "powerdns"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: powerdns\ndescription: Manage PowerDNS\n---\nBody.",
        encoding="utf-8",
    )

    catalog = load_catalog(tmp_path, skills_workspace_path=skills_workspace, disabled_skills=set())
    dns = next(t for t in catalog if t.id == "dns")
    powerdns = next(p for p in dns.providers if p.id == "powerdns")
    assert powerdns.integration is not None
    assert powerdns.integration.skill_installed is True
    assert powerdns.integration.skill_enabled is True


def test_skill_integration_installed_but_disabled(tmp_path: Path):
    catalog_dir = tmp_path / "diagrams" / "catalog"
    _write_catalog_file(
        catalog_dir,
        "with_skill.json",
        {
            "componentTypeId": "dns",
            "providers": [
                {
                    "id": "powerdns",
                    "label": "PowerDNS",
                    "kind": "api",
                    "integration": {"type": "skill", "skillName": "powerdns"},
                }
            ],
        },
    )

    skills_workspace = tmp_path / "skills-workspace"
    skill_dir = skills_workspace / "skills" / "powerdns"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: powerdns\ndescription: Manage PowerDNS\n---\nBody.",
        encoding="utf-8",
    )

    catalog = load_catalog(
        tmp_path,
        skills_workspace_path=skills_workspace,
        disabled_skills={"powerdns"},
    )
    dns = next(t for t in catalog if t.id == "dns")
    powerdns = next(p for p in dns.providers if p.id == "powerdns")
    assert powerdns.integration is not None
    assert powerdns.integration.skill_installed is True
    assert powerdns.integration.skill_enabled is False


def test_api_and_internal_integration_do_not_trigger_skill_lookup(tmp_path: Path):
    # No skills_workspace_path passed at all -- must not raise or need one.
    catalog = load_catalog(tmp_path)
    dns = next(t for t in catalog if t.id == "dns")
    cloudflare = next(p for p in dns.providers if p.id == "cloudflare")
    assert cloudflare.integration is not None
    assert cloudflare.integration.type == "api"
    assert cloudflare.integration.skill_installed is None
