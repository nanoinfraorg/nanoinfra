"""Install a connector package from the catalog (#195, part 2).

The transport and the archive safety are the skills pipeline's, unchanged: it already refuses
zip-slip, absolute paths, traversal, symlinks, duplicate paths and archives past its file-count and
size caps, over a DNS-pinned client that follows no redirects. That keeps a package from landing
where it should not, and says nothing at all about the code inside it -- which is why the package
format runs none, and why `connector.json` is validated before a single file is written to the
workspace.

Installing writes a package. **Enabling is still an operator action in config**: `tools.connectors`
plus `connectors.<name>`, the same rule `tools.agentPlugins` states on the Apps page. A browser
action that also granted a credential would be an authorisation nobody performed.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from nanoinfra.connectors.package import (
    PACKAGE_FILE,
    ConnectorPackageError,
    load_connector_package,
    parse_connector_package,
)
from nanoinfra.connectors.registry import workspace_connector_root
from nanoinfra.webui.skills_marketplace import (
    SkillsMarketplaceError,
    _extract_nanoinfra_archive,  # pyright: ignore[reportPrivateUsage]
    _nanoinfra_client,  # pyright: ignore[reportPrivateUsage]
    _valid_skill_id,  # pyright: ignore[reportPrivateUsage]
    _validate_nanoinfra_archive,  # pyright: ignore[reportPrivateUsage]
)

_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024


class ConnectorMarketplaceError(SkillsMarketplaceError):
    """A refused install, with the status a route should answer."""


async def _download_connector_archive(
    client: httpx.AsyncClient,
    connector_id: str,
    destination: Path,
) -> None:
    """Stream one connector package from the catalog's own download endpoint."""
    received = 0
    async with client.stream(
        "GET", f"/api/v1/connectors/{quote(connector_id, safe='')}/download"
    ) as response:
        if response.status_code == 404:
            raise ConnectorMarketplaceError(
                "connector not found on the nanoinfra catalog", status=404
            )
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_DOWNLOAD_BYTES:
            raise ConnectorMarketplaceError("connector package is too large", status=413)
        with destination.open("wb") as output:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > _MAX_DOWNLOAD_BYTES:
                    raise ConnectorMarketplaceError("connector package is too large", status=413)
                output.write(chunk)


def _validate_before_install(staged: Path, connector_id: str) -> dict[str, Any]:
    """Read the contract out of the staged directory and refuse anything it disallows.

    The order is the point: validated in a temporary directory, and only then moved into the
    workspace. A package that fails here never existed as far as the deployment is concerned.
    """
    try:
        plugin = load_connector_package(staged, expected_name=connector_id)
    except ConnectorPackageError as exc:
        raise ConnectorMarketplaceError(str(exc), status=400) from exc
    return {
        "name": plugin.name,
        "display_name": plugin.display_name,
        "base_url": plugin.base_url,
        "allowed_hosts": list(plugin.credential.allowed_hosts),
        "operations": [
            {"name": op.name, "class": op.capability_class, "method": op.method, "path": op.path}
            for op in plugin.operations
        ],
        "classes": sorted(plugin.classes),
        "scopes": list(plugin.credential.declared_scopes()),
        "setup_fields": sorted((plugin.setup.fields if plugin.setup else {})),
    }


async def install_marketplace_connector(
    connector_id: str,
    workspace_path: Path,
    *,
    base_url: str,
) -> dict[str, Any]:
    """Download, validate and write one connector package. Never enables it."""
    if not _valid_skill_id(connector_id):
        raise ConnectorMarketplaceError("invalid connector name", status=400)

    root = workspace_connector_root(workspace_path)
    target = root / connector_id
    if target.exists():
        return {
            "installed": True,
            "already_installed": True,
            "name": connector_id,
            "enabled": False,
        }

    with tempfile.TemporaryDirectory(prefix="nanoinfra-connector-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "package.zip"
        async with _nanoinfra_client(base_url) as client:
            await _download_connector_archive(client, connector_id, archive)
        _validate_nanoinfra_archive(archive)
        staged = tmp_path / "staged"
        staged.mkdir()
        _extract_nanoinfra_archive(archive, staged)
        # A catalog archive may hold the package inside a single top-level directory, the way a
        # skill archive does. One level, and only when there is nothing else beside it.
        entries = [entry for entry in staged.iterdir() if entry.name != "__MACOSX"]
        if len(entries) == 1 and entries[0].is_dir() and not (staged / PACKAGE_FILE).exists():
            staged = entries[0]
        summary = _validate_before_install(staged, connector_id)

        root.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(staged), str(target))
        except OSError as exc:
            raise ConnectorMarketplaceError(
                f"could not write the connector package: {exc}", status=500
            ) from exc

    logger.info(
        "connector '{}' installed with {} operation(s); enabling is a config action",
        connector_id,
        len(summary["operations"]),
    )
    return {
        "installed": True,
        "already_installed": False,
        "enabled": False,
        # Said plainly, because the row that shows this is the last place somebody reads before
        # editing config, and "installed" has meant "working" in every other Apps surface.
        "message": (
            f"{summary['display_name']} is installed. It is not active: add it to "
            f"connectors.active and bind a credential whose allowedHosts include "
            f"{', '.join(summary['allowed_hosts']) or 'its base URL host'}."
        ),
        **summary,
    }


def installed_marketplace_connectors(workspace_path: Path) -> list[dict[str, Any]]:
    """Every package written into the workspace, whether or not it activated."""
    root = workspace_connector_root(workspace_path)
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            plugin = load_connector_package(entry)
        except ConnectorPackageError as exc:
            found.append({"name": entry.name, "valid": False, "problem": str(exc)})
            continue
        found.append({
            "name": plugin.name,
            "valid": True,
            "display_name": plugin.display_name,
            "operations": [op.name for op in plugin.operations],
            "classes": sorted(plugin.classes),
        })
    return found


def remove_marketplace_connector(connector_id: str, workspace_path: Path) -> bool:
    """Delete one installed package. Config is not touched: it is the authority, not this."""
    if not _valid_skill_id(connector_id):
        raise ConnectorMarketplaceError("invalid connector name", status=400)
    target = workspace_connector_root(workspace_path) / connector_id
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


__all__ = [
    "ConnectorMarketplaceError",
    "install_marketplace_connector",
    "installed_marketplace_connectors",
    "parse_connector_package",
    "remove_marketplace_connector",
]
