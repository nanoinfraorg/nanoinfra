"""Dynamic component catalog for infra diagrams.

Mirrors ``nanoinfra/agent/skills.py``'s ``SkillsLoader`` discovery pattern:
built-in JSON files shipped with the package, plus workspace JSON files a
user drops in — merged live on every call, no caching, no restart needed.
Nothing about the palette of component types/providers is hardcoded in the
WebUI; this module is the single source of truth the ``/api/webui/diagrams/
catalog`` route serves.

Two workspace file shapes, distinguished by which key is present:

- Full type (has ``id``): a brand-new component type, or overrides a
  built-in type by matching ``id`` (shadow-by-id, like ``SkillsLoader``
  letting workspace entries take priority over built-in ones by name).
- Provider addition (has ``componentTypeId`` instead of ``id``, plus a
  ``providers`` array): appends/overrides providers onto an *existing*
  type without needing to copy that type's other providers. This is how
  "add PowerDNS under DNS" works with zero code changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from loguru import logger

from nanoinfra.agent.skills import SkillsLoader

BUILTIN_CATALOG_DIR = Path(__file__).parent / "catalog"

IntegrationType = Literal["skill", "api", "internal"]
_INTEGRATION_TYPES: tuple[IntegrationType, ...] = ("skill", "api", "internal")


@dataclass
class ProviderIntegration:
    """How nanoinfra's agent operates a provider — orthogonal to ``kind`` (deployment shape)."""

    type: IntegrationType
    skill_name: str | None = None
    # Computed live against the real Skills system — never stored in a catalog file.
    skill_installed: bool | None = None
    skill_enabled: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderIntegration | None":
        raw_type = data.get("type")
        if raw_type not in _INTEGRATION_TYPES:
            return None
        skill_name = data.get("skillName")
        return cls(
            type=raw_type,
            skill_name=str(skill_name) if skill_name else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type}
        if self.skill_name is not None:
            result["skillName"] = self.skill_name
        if self.skill_installed is not None:
            result["skillInstalled"] = self.skill_installed
        if self.skill_enabled is not None:
            result["skillEnabled"] = self.skill_enabled
        return result


@dataclass
class ProviderField:
    key: str
    label: str
    kind: str = "text"
    placeholder: str | None = None
    linked_component_type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderField":
        return cls(
            key=str(data.get("key") or ""),
            label=str(data.get("label") or ""),
            kind=str(data.get("kind") or "text"),
            placeholder=data.get("placeholder"),
            linked_component_type=data.get("linkedComponentType"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"key": self.key, "label": self.label, "kind": self.kind}
        if self.placeholder is not None:
            result["placeholder"] = self.placeholder
        if self.linked_component_type is not None:
            result["linkedComponentType"] = self.linked_component_type
        return result


def _fields_from_list(raw: Any) -> list[ProviderField]:
    if not isinstance(raw, list):
        return []
    items = cast(list[Any], raw)
    return [ProviderField.from_dict(cast(dict[str, Any], item)) for item in items if isinstance(item, dict)]


@dataclass
class ComponentProvider:
    id: str
    label: str
    kind: str = "api"
    fields: list[ProviderField] = field(default_factory=list)
    integration: ProviderIntegration | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentProvider":
        raw_integration = data.get("integration")
        integration = (
            ProviderIntegration.from_dict(cast(dict[str, Any], raw_integration))
            if isinstance(raw_integration, dict)
            else None
        )
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            kind=str(data.get("kind") or "api"),
            fields=_fields_from_list(data.get("fields")),
            integration=integration,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "fields": [f.to_dict() for f in self.fields],
        }
        if self.integration is not None:
            result["integration"] = self.integration.to_dict()
        return result


def _providers_from_list(raw: Any) -> list[ComponentProvider]:
    if not isinstance(raw, list):
        return []
    items = cast(list[Any], raw)
    return [ComponentProvider.from_dict(cast(dict[str, Any], item)) for item in items if isinstance(item, dict)]


@dataclass
class ComponentType:
    id: str
    label: str
    category: str
    icon_key: str
    providers: list[ComponentProvider] = field(default_factory=list)
    is_group: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentType":
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            category=str(data.get("category") or ""),
            icon_key=str(data.get("iconKey") or ""),
            providers=_providers_from_list(data.get("providers")),
            is_group=bool(data.get("isGroup", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "iconKey": self.icon_key,
            "providers": [p.to_dict() for p in self.providers],
        }
        if self.is_group:
            result["isGroup"] = True
        return result


def _read_json_files(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable catalog file {}: {}", path, exc)
            continue
        if not isinstance(raw, dict):
            logger.warning("Skipping malformed catalog file {}", path)
            continue
        entries.append(cast(dict[str, Any], raw))
    return entries


def _add_full_type(
    entry: dict[str, Any],
    types_by_id: dict[str, ComponentType],
    order: list[str],
) -> None:
    component_type = ComponentType.from_dict(entry)
    if not component_type.id:
        logger.warning("Skipping catalog type with no id: {}", entry)
        return
    if component_type.id not in types_by_id:
        order.append(component_type.id)
    types_by_id[component_type.id] = component_type


def _add_provider_addition(entry: dict[str, Any], types_by_id: dict[str, ComponentType]) -> None:
    target_id = str(entry.get("componentTypeId") or "")
    raw_providers = entry.get("providers")
    if not target_id or not isinstance(raw_providers, list):
        logger.warning("Skipping malformed provider-addition catalog file: {}", entry)
        return
    target = types_by_id.get(target_id)
    if target is None:
        logger.warning("Skipping provider addition for unknown component type {!r}", target_id)
        return
    providers = cast(list[Any], raw_providers)
    existing_index_by_id = {p.id: i for i, p in enumerate(target.providers)}
    for raw_provider in providers:
        if not isinstance(raw_provider, dict):
            continue
        provider = ComponentProvider.from_dict(cast(dict[str, Any], raw_provider))
        if not provider.id:
            continue
        if provider.id in existing_index_by_id:
            target.providers[existing_index_by_id[provider.id]] = provider
        else:
            existing_index_by_id[provider.id] = len(target.providers)
            target.providers.append(provider)


def _enrich_skill_status(
    component_types: list[ComponentType],
    skills_workspace_path: Path | None,
    disabled_skills: set[str],
) -> None:
    """Compute skillInstalled/skillEnabled live — never trust a static catalog label."""
    needs_check = any(
        p.integration is not None and p.integration.type == "skill" and p.integration.skill_name
        for t in component_types
        for p in t.providers
    )
    if not needs_check:
        return
    installed: set[str] = set()
    if skills_workspace_path is not None:
        loader = SkillsLoader(skills_workspace_path)
        installed = {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}
    for component_type in component_types:
        for provider in component_type.providers:
            integration = provider.integration
            if integration is None or integration.type != "skill" or not integration.skill_name:
                continue
            integration.skill_installed = integration.skill_name in installed
            integration.skill_enabled = bool(integration.skill_installed) and integration.skill_name not in disabled_skills


def load_catalog(
    workspace_path: Path,
    *,
    skills_workspace_path: Path | None = None,
    disabled_skills: set[str] | None = None,
) -> list[ComponentType]:
    """Merge built-in + workspace catalog files, enriched with live skill status.

    Scans both directories on every call — catalog edits are rare admin
    actions and the data is small, so this mirrors ``SkillsLoader``'s
    no-cache philosophy rather than adding cache-invalidation complexity.
    """
    types_by_id: dict[str, ComponentType] = {}
    order: list[str] = []

    builtin_entries = _read_json_files(BUILTIN_CATALOG_DIR)
    workspace_entries = _read_json_files(Path(workspace_path) / "diagrams" / "catalog")

    # Full types are registered first (workspace after built-in, so a
    # matching id shadows the built-in definition) — provider-addition
    # files are applied afterward so they can target a type defined by
    # either source.
    full_type_entries = [e for e in builtin_entries if "id" in e] + [e for e in workspace_entries if "id" in e]
    addition_entries = [e for e in builtin_entries if "id" not in e] + [e for e in workspace_entries if "id" not in e]

    for entry in full_type_entries:
        _add_full_type(entry, types_by_id, order)
    for entry in addition_entries:
        _add_provider_addition(entry, types_by_id)

    component_types = [types_by_id[type_id] for type_id in order]
    _enrich_skill_status(component_types, skills_workspace_path, disabled_skills or set())
    return component_types
